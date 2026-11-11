import json
import subprocess
import sys
from pathlib import Path

import pytest

from order_demo.cli import Application, replay
from order_demo.domain import Demo
from test_acceptance import clarify, prepared


def test_cli_control_is_processed_before_pending_purchase_write(tmp_path):
    demo = Demo(tmp_path)
    clarify(demo)
    op = prepared(demo)
    demo.approve(op["id"])
    app = Application(demo, offline=True, output=lambda value: None)
    app.commands.put("/pause")
    with pytest.raises(ValueError):
        demo.place(op["id"])
    assert demo.sim.order_count == 0
    assert demo.task["lifecycle"] == "PAUSED"
    demo.close()


@pytest.mark.parametrize("name", ["normal", "adjustment", "control"])
def test_replay_three_timelines_from_public_input(name, tmp_path):
    result = replay(Path("fixtures/scenarios") / f"{name}.json", tmp_path / name)
    assert result["assertions_passed"] >= 3
    evidence = json.loads((tmp_path / name / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["mode"] == "OFFLINE_RULE_REPLAY"
    assert evidence["events"]
    assert evidence["external_order_count"] == 1


def test_cli_missing_model_config_fails_explicitly(tmp_path):
    import os
    env = {k: v for k, v in os.environ.items() if k not in {"OPENAI_API_KEY", "MODEL_NAME"}}
    env["PYTHON_DOTENV_DISABLED"] = "1"
    result = subprocess.run([sys.executable, "-X", "utf8", "-m", "order_demo.cli", "--data-dir", str(tmp_path)],
                            capture_output=True, text=True, encoding="utf-8", env=env)
    assert result.returncode == 2
    assert "MODEL_CONFIG_REQUIRED" in result.stderr


def test_queued_time_advance_does_not_reenter_agent_from_tool(tmp_path):
    from order_demo.cli import Clock
    demo = Demo(tmp_path)
    clock = Clock(1800000000)
    demo.now = clock
    clarify(demo)

    class Session:
        def clear_invalid_pending(self):
            raise AssertionError("工具执行期间不能再次进入 Agent")

    app = Application(demo, session=Session(), clock=clock, output=lambda value: None)
    app.commands.put("/advance 10")
    demo.search()
    assert clock() == 1800000010
    demo.close()
