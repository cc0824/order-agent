"""默认购买条件不能替代预算决定、跟进授权或订单确认。"""
import json

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from backend.agent import AgentSession
from backend.domain import Demo, DomainError
from test_agent import ToolCallingModel, call


@pytest.fixture
def demo(tmp_path):
    instance = Demo(tmp_path, now=lambda: 1800000000)
    yield instance
    instance.close()


def test_only_budget_is_required_and_defaults_never_authorize_followup(demo):
    req = demo.task["requirements"]
    assert demo.missing_requirements() == ["budget_cents"]
    assert req["region"] == "CN" and req["condition"] == "new"
    assert req["storage_gb"] == 128 and not req.get("model")
    assert req["desired_delivery_at"] == 1800604800
    assert req["followup_until"] == 1800604800
    assert req["check_interval_seconds"] == 600 and req["notification_channel"] == "page"
    assert req["followup_authorized"] is False
    assert "budget_cents" not in req
    assert demo.search()["status"] == "NEEDS_INPUT"
    with pytest.raises(DomainError):
        demo.prepare("iphone-base-128", 1, 0)
    assert demo.sim.order_count == 0


def test_budget_alone_allows_search_without_authorizing_background_or_order(demo):
    demo.update_requirements({"budget_cents": 1000000})
    result = demo.search()
    assert len(result["offers"]) == 3
    assert demo.missing_requirements() == []
    assert demo.tick() is False
    with AgentSession(demo, model=ToolCallingModel()) as session:
        assert session.system_tick() is None
        assert session.model.position == 0
    offer = result["offers"][0]
    op = demo.prepare(offer["offer_id"], offer["quote_version"], result["constraints_version"])
    with pytest.raises(DomainError, match="CONFIRMATION_REQUIRED"):
        demo.place(op["id"])
    assert demo.sim.order_count == 0


def test_followup_can_be_enabled_with_one_explicit_decision_and_disabled_again(demo):
    demo.update_requirements({"budget_cents": 1000000, "followup_authorized": True})
    assert demo.tick() is True
    demo.update_requirements({"followup_authorized": False})
    assert demo.tick() is False
    assert demo.search()["status"] == "OK"


def test_user_preferences_override_defaults_and_empty_optional_context_is_allowed(demo):
    demo.update_requirements({"budget_cents": 600000, "storage_gb": 512,
                              "condition": "全新", "region": "不限",
                              "use_case": "", "current_problem": ""})
    result = demo.search()
    assert result["status"] == "NO_MATCH"
    plus = next(r for r in result["rejected"] if r["offer_id"] == "iphone-plus-512")
    assert plus["reasons"] == ["budget_cents"]


def test_restart_preserves_saved_choices_and_fixed_dates(tmp_path):
    demo = Demo(tmp_path, now=lambda: 1800000000)
    demo.update_requirements({"budget_cents": 900000, "region": "HK", "storage_gb": 512})
    before = demo.task
    demo.close()
    reopened = Demo(tmp_path, now=lambda: 1800001000)
    try:
        assert reopened.task == before
    finally:
        reopened.close()


def test_legacy_empty_task_receives_missing_defaults_without_changing_version(tmp_path):
    demo = Demo(tmp_path, now=lambda: 1800000000)
    task = demo.task
    task["requirements"] = {"region": "HK", "budget_cents": 900000}
    task["constraints_version"] = 3
    demo._save(task)
    demo.close()
    reopened = Demo(tmp_path, now=lambda: 1800001000)
    try:
        req = reopened.task["requirements"]
        assert req["region"] == "HK" and req["budget_cents"] == 900000
        assert req["condition"] == "new" and req["followup_authorized"] is False
        assert reopened.task["constraints_version"] == 3
        assert reopened.search()["status"] == "NO_MATCH"
    finally:
        reopened.close()


def test_shipped_defaults_are_configurable_for_new_tasks(tmp_path, monkeypatch):
    from backend import config
    path = tmp_path / "defaults.json"
    path.write_text(json.dumps({"region": "CN", "condition": "new", "storage_gb": 256,
        "delivery_days": 3, "followup_days": 5, "check_interval_seconds": 900,
        "notification_channel": "page"}), encoding="utf-8")
    monkeypatch.setattr(config, "DEFAULTS_FILE", path)
    demo = Demo(tmp_path / "database", now=lambda: 1800000000)
    try:
        req = demo.task["requirements"]
        assert req["storage_gb"] == 256 and req["check_interval_seconds"] == 900
        assert req["desired_delivery_at"] == 1800259200
        assert req["followup_until"] == 1800432000
        assert req["followup_authorized"] is False
    finally:
        demo.close()


def test_agent_can_capture_budget_then_search_using_authoritative_defaults(demo):
    class BudgetModel(ToolCallingModel):
        def _generate(self, messages, **kwargs):
            snapshot = json.loads(messages[0].content.split("当前权威快照：\n")[1])
            if self.position == 0:
                response = call("update_requirements", {"message_id": snapshot["current_user_message_id"],
                    "patch": {"budget_cents": 1000000}, "expected_version": 0})
            elif self.position == 1:
                response = call("search_iphones", {"constraints_version": 1}, 2)
            else:
                response = AIMessage(content="已按默认条件查询，请选择具体商品。")
            self.position += 1
            return ChatResult(generations=[ChatGeneration(message=response)])

    with AgentSession(demo, model=BudgetModel()) as session:
        session.chat("预算一万元以内，其他用默认条件")
    assert len(demo.task["recommendation"]["offers"]) == 3
    assert demo.task["requirements"]["followup_authorized"] is False
    assert demo.sim.order_count == 0
