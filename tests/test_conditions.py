"""自然语言成色与商品标签匹配的回归。"""
import pytest

from backend.agent import AgentSession
from backend.domain import Demo, DomainError
from test_acceptance import clarify
from test_agent import ToolCallingModel, call


@pytest.fixture
def demo(tmp_path):
    instance = Demo(tmp_path, now=lambda: 1800000000)
    yield instance
    instance.close()


@pytest.mark.parametrize("condition", ["全新", "全新机", "new", " NEW "])
def test_new_phone_requirement_matches_all_three_new_offers(demo, condition):
    clarify(demo, condition=condition, budget_cents=1000000, storage_gb=128,
            desired_delivery_at=demo.now() + 7 * 86400)
    assert len(demo.search()["offers"]) == 3
    assert demo.task["requirements"]["condition"] == "new"


@pytest.mark.parametrize("condition", ["二手", " USED "])
def test_used_requirement_matches_only_used_offer(demo, condition):
    demo.sim.data["offers"][0]["condition"] = "used"
    clarify(demo, condition=condition)
    result = demo.search()
    assert [o["offer_id"] for o in result["offers"]] == ["iphone-base-256"]
    assert demo.task["requirements"]["condition"] == "used"


def test_saved_chinese_condition_works_without_rewriting_task_or_version(demo):
    clarify(demo, storage_gb=128)
    task = demo.task
    task["requirements"]["condition"] = "全新"
    demo._save(task)
    with AgentSession(demo, model=ToolCallingModel(responses=[
            call("search_iphones", {"constraints_version": 1})])) as session:
        session.chat("重新查询全新手机")
    result = demo.store.events(demo.owner, "tool")[-1]["payload"]["result"]
    assert len(result["offers"]) == 3
    assert demo.task["constraints_version"] == 1
    assert demo.task["requirements"]["condition"] == "全新"
    assert demo.sim.order_count == 0


def test_new_requirement_still_rejects_used_with_explicit_condition_evidence(demo):
    demo.sim.data["offers"][0]["condition"] = "used"
    clarify(demo, condition="全新")
    result = demo.search()
    assert [o["offer_id"] for o in result["offers"]] == ["iphone-plus-512"]
    rejection = next(r for r in result["rejected"] if r["offer_id"] == "iphone-base-256")
    assert rejection["reasons"] == ["condition"]
    assert rejection["reason_details"]["condition"] == {
        "code": "CONDITION_TAG_MISMATCH", "required_condition": "new", "offer_condition": "used"}


def test_chinese_offer_condition_matches_canonical_requirement(demo):
    demo.sim.data["offers"][0]["condition"] = "全新"
    clarify(demo)
    assert demo.search()["offers"][0]["offer_id"] == "iphone-base-256"


def test_unsupported_condition_is_not_silently_treated_as_new_or_unlimited(demo):
    clarify(demo)
    before = demo.task
    with pytest.raises(DomainError, match="INVALID_CONDITION"):
        demo.update_requirements({"condition": "翻新"})
    assert demo.task == before


def test_new_alias_does_not_relax_budget_or_capacity(demo):
    clarify(demo, condition="全新", storage_gb=512, budget_cents=600000)
    result = demo.search()
    assert not result["offers"]
    plus = next(r for r in result["rejected"] if r["offer_id"] == "iphone-plus-512")
    assert plus["reasons"] == ["budget_cents"]
