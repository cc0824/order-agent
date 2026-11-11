"""原题 1–12 的可执行验收。断言业务事实和副作用，不匹配模型措辞。"""
import pytest

from order_demo.domain import Demo, DomainError


@pytest.fixture
def demo(tmp_path):
    clock = [1_800_000_000.0]
    app = Demo(tmp_path, now=lambda: clock[0])
    app.clock = clock
    yield app
    app.close()


def clarify(app, **patch):
    values = dict(use_case="拍照和日常", current_problem="电池老化", region="CN",
                  budget_cents=800000, storage_gb=256, condition="new",
                  desired_delivery_at=app.now() + 3600, followup_until=app.now() + 7200,
                  check_interval_seconds=10, notification_channel="terminal",
                  followup_authorized=True)
    values.update(patch)
    app.update_requirements(values)


def prepared(app):
    result = app.search()
    offer = result["offers"][0]
    return app.prepare(offer["offer_id"], offer["quote_version"], result["constraints_version"])


def ordered(app, timeout=False):
    clarify(app)
    op = prepared(app)
    app.approve(op["id"])
    app.sim.timeout_after_create = timeout
    app.place(op["id"])
    return op["id"]


def test_case_01_vague_needs_clarification_not_order(demo):
    assert {"use_case", "budget_cents"} <= set(demo.missing_requirements())
    assert demo.search()["status"] == "NEEDS_INPUT"
    assert demo.sim.order_count == 0
    assert demo.task["phase"] == "CLARIFYING"


def test_case_02_conditional_discount_requires_verified_eligibility(demo):
    clarify(demo, budget_cents=550000)
    assert demo.search()["offers"] == []  # 4999 资格未知，不能作为可得价
    demo.update_requirements({"budget_cents": 600000})
    offer = demo.search()["offers"][0]
    assert offer["total_cents"] == 579900
    assert offer["conditional_cents"] == 499900


def test_case_03_budget_change_invalidates_late_result_and_confirmation(demo):
    clarify(demo)
    old_result = demo.search()
    op = prepared(demo)
    demo.approve(op["id"])
    demo.update_requirements({"budget_cents": 600000})
    with pytest.raises(DomainError, match="STALE_VERSION"):
        demo.accept_search(old_result)
    with pytest.raises(DomainError):
        demo.place(op["id"])
    assert demo.sim.order_count == 0


def test_case_04_no_suitable_capacity_waits_without_relaxing_budget(demo):
    clarify(demo, storage_gb=1024, budget_cents=600000)
    result = demo.search()
    assert result["status"] == "NO_MATCH"
    assert result["rejected"]
    assert demo.task["phase"] == "NEEDS_DECISION"
    assert demo.task["requirements"]["storage_gb"] == 1024
    assert demo.task["requirements"]["budget_cents"] == 600000


@pytest.mark.parametrize("change", [{"price_cents": 589900}, {"stock": 0}])
def test_case_05_price_or_stock_change_blocks_old_confirmation(demo, change):
    clarify(demo)
    op = prepared(demo)
    demo.approve(op["id"])
    demo.event("offer", {"offer_id": "iphone-base-256", **change}, "price-change")
    with pytest.raises(DomainError):
        demo.place(op["id"])
    assert demo.sim.order_count == 0


def test_case_06_lost_receipt_queries_original_order_never_resubmits(demo):
    op_id = ordered(demo, timeout=True)
    assert demo.operation(op_id)["status"] == "UNKNOWN"
    assert demo.sim.order_count == 1
    demo.place(op_id)  # 同一操作重放也只返回已有状态
    assert demo.sim.create_calls == 1
    demo.get_order_status(op_id)
    assert demo.operation(op_id)["status"] == "ACCEPTED"
    assert demo.task["phase"] == "WAITING_PAYMENT"
    assert demo.sim.order_count == 1


def test_case_07_waiting_payment_is_not_purchase_completion(demo):
    ordered(demo)
    assert demo.task["phase"] == "WAITING_PAYMENT"
    assert demo.task["lifecycle"] == "ACTIVE"
    assert not demo.task.get("receipt")


def test_case_08_delivery_delay_notifies_without_cancel_or_new_order(demo):
    op_id = ordered(demo)
    demo.event("paid", {"operation_id": op_id}, "paid")
    demo.event("shipping", {"operation_id": op_id, "expected_delivery_at": demo.now() + 5000}, "delay")
    demo.get_order_status(op_id)
    assert demo.task["phase"] == "NEEDS_DECISION"
    assert demo.task["lifecycle"] == "ACTIVE"
    assert any(n["payload"]["code"] == "DELIVERY_DELAY" for n in demo.notifications())
    assert demo.sim.order_count == 1
    assert demo.sim.query(demo.operation(op_id)["idempotency_key"])["payment"] == "PAID"


def test_case_09_wrong_received_capacity_never_completes(demo):
    op_id = ordered(demo)
    demo.event("delivered", {"operation_id": op_id}, "delivered")
    demo.get_order_status(op_id)
    demo.event("receipt", {"operation_id": op_id, "storage_gb": 128, "correct": False}, "user-wrong")
    assert demo.task["lifecycle"] != "COMPLETED"
    assert demo.task["phase"] == "NEEDS_DECISION"


def test_case_10_refund_accepted_is_not_actual_refund(demo):
    op_id = ordered(demo)
    demo.event("returned", {"operation_id": op_id}, "return")
    demo.event("refund_requested", {"operation_id": op_id}, "request")
    status = demo.get_order_status(op_id)
    assert status["refund"] == "REQUEST_ACCEPTED"
    assert demo.task["lifecycle"] != "COMPLETED"
    demo.event("refunded", {"operation_id": op_id}, "refunded")
    assert demo.get_order_status(op_id)["refund"] == "REFUNDED"
    assert demo.task["lifecycle"] == "ENDED"
    assert demo.task["outcome_reason"] == "REFUND_EXIT"


def test_case_11_end_then_late_order_fact_does_not_restart(demo):
    op_id = ordered(demo, timeout=True)
    demo.control("end")
    before = len(demo.notifications())
    demo.get_order_status(op_id)
    assert demo.operation(op_id)["status"] == "ACCEPTED"
    assert demo.task["lifecycle"] == "ENDED"
    assert len(demo.notifications()) == before
    with pytest.raises(DomainError):
        demo.prepare("iphone-base-256", 1, demo.task["constraints_version"])
    assert demo.sim.order_count == 1


def test_case_12_completion_requires_order_and_correct_user_receipt(demo):
    op_id = ordered(demo)
    demo.event("paid", {"operation_id": op_id}, "paid")
    demo.event("delivered", {"operation_id": op_id}, "delivered")
    demo.get_order_status(op_id)
    assert demo.task["lifecycle"] == "ACTIVE"
    demo.event("receipt", {"operation_id": op_id, "storage_gb": 256, "correct": True}, "receipt")
    assert demo.task["lifecycle"] == "COMPLETED"
    assert demo.task["receipt"]["event_id"] == "receipt"
    assert demo.operation(op_id)["order"]["delivery"] == "DELIVERED"
    receipt_event = next(e for e in demo.store.events(demo.owner, "external_fact")
                         if e["payload"]["source_event_id"] == "receipt")
    assert receipt_event["payload"]["late"] is False


def test_pause_resume_no_backlog_and_timer_without_chat(demo):
    clarify(demo)
    demo.tick()
    assert demo.notifications()
    demo.control("pause")
    count = len(demo.notifications())
    demo.event("offer", {"offer_id": "iphone-base-256", "price_cents": 569900}, "paused-offer")
    demo.clock[0] += 20
    demo.tick()
    assert len(demo.notifications()) == count
    demo.control("resume")
    demo.clock[0] += 20
    demo.tick()
    assert len(demo.notifications()) == count
    demo.event("offer", {"offer_id": "iphone-base-256", "price_cents": 559900}, "new-offer")
    demo.clock[0] += 20
    demo.tick()
    assert len(demo.notifications()) == count + 1
    demo.clock[0] += 20
    demo.tick()
    assert len(demo.notifications()) == count + 1


def test_expiry_and_bounded_unknown_verification(demo):
    op_id = ordered(demo, timeout=True)
    demo.sim.fail_query = True
    demo.control("end")
    for _ in range(10):
        demo.clock[0] += 15
        demo.tick()
    assert demo.operation(op_id)["verify_attempts"] == 3
    assert demo.sim.create_calls == 1


def test_expiry_is_separate_from_desired_delivery(demo):
    clarify(demo)
    demo.clock[0] += 3601
    demo.tick()
    assert demo.task["lifecycle"] == "ACTIVE"
    demo.clock[0] += 3600
    demo.tick()
    assert demo.task["lifecycle"] == "EXPIRED"


def test_missing_confirmation_and_duplicate_facts(demo):
    clarify(demo)
    op = prepared(demo)
    with pytest.raises(DomainError, match="CONFIRMATION_REQUIRED"):
        demo.place(op["id"])
    demo.event("offer", {"offer_id": "iphone-base-256", "price_cents": 559900}, "same")
    version = demo.sim.offer("iphone-base-256")["quote_version"]
    demo.event("offer", {"offer_id": "iphone-base-256", "price_cents": 559900}, "same")
    assert demo.sim.offer("iphone-base-256")["quote_version"] == version


def test_source_failure_is_unknown_not_no_match(demo):
    clarify(demo)
    demo.sim.fail_search = True
    result = demo.search()
    assert result["status"] == "UNVERIFIED"
    assert demo.task["lifecycle"] != "COMPLETED"


def test_cross_user_cannot_access_operation(demo, tmp_path):
    clarify(demo)
    op = prepared(demo)
    other = Demo(tmp_path, user_id="other", task_id="t1", now=demo.now)
    try:
        with pytest.raises(DomainError, match="NOT_FOUND"):
            other.operation(op["id"])
    finally:
        other.close()


def test_resume_does_not_later_notify_payment_observed_while_paused(demo):
    op_id = ordered(demo)
    demo.control("pause")
    count = len(demo.notifications())
    demo.event("paid", {"operation_id": op_id}, "paused-payment")
    demo.control("resume")
    demo.clock[0] += 20
    demo.tick()
    assert demo.task["phase"] == "TRACKING"
    assert len(demo.notifications()) == count


def test_correct_receipt_is_retained_until_delivery_evidence_arrives(demo):
    op_id = ordered(demo)
    demo.event("paid", {"operation_id": op_id}, "paid")
    demo.event("receipt", {"operation_id": op_id, "correct": True, "storage_gb": 256}, "early-receipt")
    assert demo.task["lifecycle"] == "ACTIVE"
    demo.event("delivered", {"operation_id": op_id}, "late-delivery-evidence")
    demo.get_order_status(op_id)
    assert demo.task["lifecycle"] == "COMPLETED"
    assert demo.task["receipt"]["event_id"] == "early-receipt"


@pytest.mark.parametrize("same_event_id", [False, True])
def test_conflicting_payment_facts_are_unverified_and_never_complete(demo, same_event_id):
    op_id = ordered(demo)
    if same_event_id:
        demo.event("paid", {"operation_id": op_id}, "payment-source-1")
        result = demo.event("cancelled", {"operation_id": op_id}, "payment-source-1")
    else:
        demo.event("cancelled", {"operation_id": op_id}, "cancelled-source-1")
        demo.get_order_status(op_id)
        result = demo.event("paid", {"operation_id": op_id}, "late-paid-source-2")
    assert result["status"] == "UNVERIFIED"
    demo.event("delivered", {"operation_id": op_id}, "delivered")
    demo.event("receipt", {"operation_id": op_id, "correct": True, "storage_gb": 256}, "receipt")
    assert demo.task["lifecycle"] != "COMPLETED"
    assert demo.task["verification_required"] is True


def test_conflicting_offer_source_id_blocks_new_purchase(demo):
    clarify(demo)
    demo.event("offer", {"offer_id": "iphone-base-256", "price_cents": 569900}, "offer-source")
    demo.event("offer", {"offer_id": "iphone-base-256", "price_cents": 559900}, "offer-source")
    with pytest.raises(DomainError, match="FACTS_UNVERIFIED"):
        prepared(demo)
    assert demo.sim.order_count == 0


def test_offer_change_clears_old_recommendation_and_purchase_intent(demo):
    clarify(demo)
    op = prepared(demo)
    demo.event("offer", {"offer_id": "iphone-base-256", "price_cents": 589900}, "new-price")
    assert demo.task["recommendation"] is None
    assert demo.operation(op["id"])["valid"] is False


def test_receipt_while_paused_is_kept_but_only_completes_on_resume(demo):
    op_id = ordered(demo)
    demo.event("paid", {"operation_id": op_id}, "paid")
    demo.event("delivered", {"operation_id": op_id}, "delivered")
    demo.control("pause")
    before = len(demo.notifications())
    demo.event("receipt", {"operation_id": op_id, "correct": True, "storage_gb": 256}, "paused-receipt")
    assert demo.task["lifecycle"] == "PAUSED"
    assert len(demo.notifications()) == before
    demo.control("resume")
    assert demo.task["lifecycle"] == "COMPLETED"
    assert demo.task["receipt"]["event_id"] == "paused-receipt"
    assert len(demo.notifications()) == before
