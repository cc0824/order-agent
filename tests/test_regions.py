"""自然语言地区与模拟商品标签的匹配回归。"""
import pytest

from backend.agent import AgentSession
from backend.domain import Demo
from test_acceptance import clarify
from test_agent import ToolCallingModel, call


@pytest.fixture
def demo(tmp_path):
    instance = Demo(tmp_path, now=lambda: 1800000000)
    yield instance
    instance.close()


@pytest.mark.parametrize("region", ["中国", "中国大陆", "China", " cn "])
def test_china_aliases_match_cn_catalog(demo, region):
    clarify(demo, region=region, storage_gb=512, budget_cents=2000000)
    result = demo.search()
    assert [o["offer_id"] for o in result["offers"]] == ["iphone-plus-512"]
    assert demo.task["requirements"]["region"] == "CN"


@pytest.mark.parametrize("region", ["不限", "ANY", "*"])
def test_explicit_unlimited_region_accepts_other_region_tags(demo, region):
    demo.sim.data["offers"][1]["region"] = "HK"
    clarify(demo, region=region, storage_gb=512)
    result = demo.search()
    assert [o["offer_id"] for o in result["offers"]] == ["iphone-plus-512"]
    assert demo.task["requirements"]["region"] == "ANY"


@pytest.mark.parametrize("legacy", ["中国", "不限"])
def test_existing_saved_regions_work_without_rewriting_user_constraints(demo, legacy):
    clarify(demo, storage_gb=512)
    task = demo.task
    task["requirements"]["region"] = legacy
    demo._save(task)
    version = task["constraints_version"]
    assert demo.search()["offers"][0]["offer_id"] == "iphone-plus-512"
    assert demo.task["constraints_version"] == version
    assert demo.task["requirements"]["region"] == legacy


def test_unlimited_region_does_not_relax_capacity_or_budget(demo):
    clarify(demo, region="中国", storage_gb=1024, budget_cents=2000000)
    first = demo.search()
    assert not first["offers"]
    assert all("region" not in r["reasons"] for r in first["rejected"])
    demo.update_requirements({"region": "不限", "storage_gb": 512})
    assert demo.search()["offers"][0]["offer_id"] == "iphone-plus-512"
    demo.update_requirements({"budget_cents": 600000})
    result = demo.search()
    assert not result["offers"]
    plus = next(r for r in result["rejected"] if r["offer_id"] == "iphone-plus-512")
    assert plus["reasons"] == ["budget_cents"]


def test_region_rejection_explains_tag_mismatch_without_claiming_delivery_coverage(demo):
    clarify(demo, region="HK")
    result = demo.search()
    assert not result["offers"]
    details = result["rejected"][0]["reason_details"]["region"]
    assert details["required_region"] == "HK"
    assert details["offer_region"] == "CN"
    assert details["code"] == "REGION_TAG_MISMATCH"
    assert details["delivery_coverage"] == "NOT_VERIFIED"


def test_agent_tool_receives_matches_for_legacy_unlimited_region(demo):
    clarify(demo, storage_gb=512)
    task = demo.task
    task["requirements"]["region"] = "不限"
    demo._save(task)
    model = ToolCallingModel(responses=[call("search_iphones", {"constraints_version": 1})])
    with AgentSession(demo, model=model) as session:
        session.chat("重新查询符合条件的手机")
    result = demo.store.events(demo.owner, "tool")[-1]["payload"]["result"]
    assert [o["offer_id"] for o in result["offers"]] == ["iphone-plus-512"]
    assert demo.sim.order_count == 0
