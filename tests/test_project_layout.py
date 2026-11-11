"""目录迁移后，配置、商品和隔离运行状态的集成验证。"""
import json
import shutil
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_required_demo_data_is_shipped_with_config():
    catalog = json.loads((ROOT / "config/catalog.json").read_text(encoding="utf-8"))
    state = json.loads((ROOT / "config/simulated_external.json").read_text(encoding="utf-8"))
    assert catalog and state["offers"]
    assert isinstance(state["orders"], dict)
    assert (ROOT / "config/.env.example").is_file()
    for name in ["normal", "adjustment", "control"]:
        scenario = json.loads((ROOT / f"config/scenarios/{name}.json").read_text(encoding="utf-8"))
        assert scenario["steps"]


def test_catalog_and_relative_settings_do_not_depend_on_working_directory(tmp_path, monkeypatch):
    from backend.config import PROJECT_ROOT, project_path
    from backend.domain import Demo

    monkeypatch.chdir(tmp_path)
    assert project_path("config/catalog.json") == PROJECT_ROOT / "config/catalog.json"
    assert project_path(tmp_path / "external.json") == tmp_path / "external.json"
    demo = Demo(tmp_path / "isolated")
    try:
        assert demo.sim.offer("iphone-base-256")["storage_gb"] == 256
        assert demo.sim.path == tmp_path / "isolated/simulated_external.json"
    finally:
        demo.close()


def test_environment_loads_from_config_and_preserves_process_values(tmp_path, monkeypatch):
    from backend import config

    env = tmp_path / "config/.env"
    env.parent.mkdir()
    env.write_text("ORDER_DEMO_CONFIG_PROBE=from_config\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.delenv("ORDER_DEMO_CONFIG_PROBE", raising=False)
    monkeypatch.chdir(tmp_path)
    config.load_environment()
    import os
    assert os.environ["ORDER_DEMO_CONFIG_PROBE"] == "from_config"
    monkeypatch.setenv("ORDER_DEMO_CONFIG_PROBE", "from_process")
    config.load_environment()
    assert os.environ["ORDER_DEMO_CONFIG_PROBE"] == "from_process"


def test_mock_state_can_be_separate_from_sqlite_without_touching_shared_data(tmp_path):
    from backend.domain import Demo

    shared = ROOT / "config/simulated_external.json"
    before = shared.read_bytes()
    state_file = tmp_path / "config/simulated_external.json"
    demo = Demo(tmp_path / "database", mock_file=state_file)
    try:
        demo.event("offer", {"offer_id": "iphone-base-256", "price_cents": 580000}, "new-price")
        assert demo.sim.path == state_file
        assert (tmp_path / "database/business.sqlite").is_file()
        assert not (tmp_path / "database/simulated_external.json").exists()
        data = json.loads(state_file.read_text(encoding="utf-8"))
        assert next(o for o in data["offers"] if o["offer_id"] == "iphone-base-256")["price_cents"] == 580000
    finally:
        demo.close()
    reopened = Demo(tmp_path / "database", mock_file=state_file)
    try:
        assert reopened.sim.offer("iphone-base-256")["price_cents"] == 580000
    finally:
        reopened.close()
    assert shared.read_bytes() == before


def test_shared_mock_keeps_delivery_times_current_without_rewriting_order_evidence(tmp_path):
    from backend.domain import Demo
    from test_acceptance import clarify

    state_file = tmp_path / "config/simulated_external.json"
    state_file.parent.mkdir()
    shutil.copyfile(ROOT / "config/simulated_external.json", state_file)
    clock = [time.time() + 86400 * 365]
    demo = Demo(tmp_path / "database", now=lambda: clock[0], mock_file=state_file)
    try:
        assert all(o["expected_delivery_at"] > clock[0] for o in demo.sim.search())
        clarify(demo, desired_delivery_at=clock[0] + 10)
        assert demo.search()["offers"] == []
        clarify(demo)
        result = demo.search()
        offer = result["offers"][0]
        op = demo.prepare(offer["offer_id"], offer["quote_version"], result["constraints_version"])
        demo.approve(op["id"])
        demo.place(op["id"])
        fixed_snapshot = demo.current_operation()["order"]["snapshot"]
    finally:
        demo.close()
    # 下单保存后重新启动仍有当前配送估计，订单中的原始确认事实不能平移。
    clock[0] += 86400
    reopened = Demo(tmp_path / "database", now=lambda: clock[0], mock_file=state_file)
    try:
        assert all(o["expected_delivery_at"] > clock[0] for o in reopened.sim.search())
        assert reopened.sim.query(op["idempotency_key"])["snapshot"] == fixed_snapshot
    finally:
        reopened.close()
