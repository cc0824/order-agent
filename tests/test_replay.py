import json
from pathlib import Path

import pytest

from backend.replay import replay


@pytest.mark.parametrize("name", ["normal", "adjustment", "control"])
def test_replay_three_timelines_from_public_input(name, tmp_path):
    result = replay(Path(__file__).resolve().parents[1] / "config/scenarios" / f"{name}.json", tmp_path / name)
    assert result["assertions_passed"] >= 3
    evidence = json.loads((tmp_path / name / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["mode"] == "OFFLINE_RULE_REPLAY"
    assert evidence["events"]
    assert evidence["external_order_count"] == 1
