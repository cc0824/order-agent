"""非交互式离线验收重放；页面是唯一用户交互入口。"""
import argparse
import json
import sys
import uuid
from pathlib import Path

from .domain import Demo, DomainError
from .runtime import Clock


class RuleReplay:
    def __init__(self, demo, clock):
        self.demo, self.clock = demo, clock

    def _replace(self, value):
        if isinstance(value, dict):
            return {k: self._replace(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._replace(v) for v in value]
        if value == "$operation":
            op = self.demo.current_operation()
            if not op:
                raise DomainError("NO_OPERATION")
            return op["id"]
        if value == "$now":
            return self.demo.now()
        return value

    def handle(self, text):
        command, _, argument = text.strip().partition(" ")
        if command in {"/pause", "/resume", "/end"}:
            return self.demo.control(command[1:])
        if command == "/status":
            return {"task": self.demo.task, "operation": self.demo.current_operation()}
        if command == "/evidence":
            return self.demo.evidence()
        if command == "/advance":
            if not self.clock:
                raise DomainError("CLOCK_REQUIRED")
            self.clock.advance(float(argument))
            self.demo.tick()
            return {"now": self.demo.now()}
        if command == "/event":
            kind, _, data = argument.partition(" ")
            payload = self._replace(json.loads(data or "{}"))
            event_id = payload.pop("event_id", uuid.uuid4().hex)
            return self.demo.event(kind, payload, event_id)
        if command == "/requirements":
            # 明确的用户命令也支持精确输入，方便验证预算、期限和版本。
            self.demo.record("user_message", {"command": text})
            return self.demo.update_requirements(json.loads(argument))
        if command in {"/approve", "/reject"}:
            op_id = self._replace(argument)
            return self.demo.approve(op_id) if command == "/approve" else self.demo.reject(op_id)
        if command == "/search":
            return self.demo.search()
        if command == "/prepare":
            result = self.demo.search()
            offer = next(o for o in result["offers"] if o["offer_id"] == argument)
            return self.demo.prepare(argument, offer["quote_version"], result["constraints_version"])
        if command == "/place":
            return self.demo.place(self._replace(argument))
        if command == "/query":
            return self.demo.get_order_status(self._replace(argument))
        raise DomainError("OFFLINE_REPLAY_REQUIRES_EXPLICIT_COMMAND")


def replay(path, data_dir):
    path, data_dir = Path(path), Path(data_dir)
    if (data_dir / "business.sqlite").exists() or (data_dir / "simulated_external.json").exists():
        raise DomainError("REPLAY_REQUIRES_EMPTY_DATA_DIR: 请使用新的 --data-dir")
    scenario = json.loads(path.read_text(encoding="utf-8"))
    clock = Clock(scenario.get("start", 1_800_000_000))
    demo = Demo(data_dir, now=clock)
    app = RuleReplay(demo, clock)
    passed, steps = 0, []
    try:
        for step in scenario["steps"]:
            demo.record("replay_input", step)
            if "command" in step:
                try:
                    result = app.handle(step["command"])
                except DomainError as error:
                    if not step.get("expect_error") or step["expect_error"] not in str(error):
                        raise
                    result = {"expected_error": str(error)}
                else:
                    if step.get("expect_error"):
                        raise AssertionError("预期拒绝但执行成功: " + step["command"])
            elif "assert" in step:
                state = {"lifecycle": demo.task["lifecycle"], "phase": demo.task["phase"],
                         "order_count": demo.sim.order_count, "create_calls": demo.sim.create_calls,
                         "notification_count": len(demo.notifications())}
                for key, expected in step["assert"].items():
                    if state[key] != expected:
                        raise AssertionError(f"{key}: expected {expected!r}, actual {state[key]!r}")
                    passed += 1
                result = {"passed": step["assert"]}
            else:
                raise DomainError("UNSUPPORTED_REPLAY_STEP")
            steps.append({"input": step, "result": result})
        evidence = {**demo.evidence(), "mode": "OFFLINE_RULE_REPLAY", "scenario": scenario["name"],
                    "assertions_passed": passed, "steps": steps}
        (data_dir / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"scenario": scenario["name"], "mode": evidence["mode"], "assertions_passed": passed,
                "evidence": str((data_dir / "evidence.json").resolve())}
    finally:
        demo.close()


def main():
    parser = argparse.ArgumentParser(description="离线业务验收重放（非控制台聊天）")
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(replay(args.scenario, args.data_dir), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, AssertionError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
