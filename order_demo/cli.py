"""单进程 CLI。--replay 是明确标记的离线业务重放，不冒充在线 Agent。"""
import argparse
import json
import queue
import sys
import threading
import time
import uuid
from pathlib import Path

from .domain import Demo, DomainError


def display(value):
    print(json.dumps(value, ensure_ascii=False, indent=2) if not isinstance(value, str) else value, flush=True)


class Clock:
    def __init__(self, start=None):
        self.fixed = start
        self.offset = 0

    def __call__(self):
        return (time.time() if self.fixed is None else self.fixed) + self.offset

    def advance(self, seconds):
        if not isinstance(seconds, (int, float)) or seconds < 0:
            raise DomainError("INVALID_TIME_ADVANCE")
        self.offset += seconds


class Application:
    def __init__(self, demo, session=None, offline=False, output=display, clock=None):
        self.demo, self.session, self.offline = demo, session, offline
        self.output, self.clock = output, clock
        self.commands = queue.Queue()
        self.deferred = []
        self.draining = False
        self.seen_notifications = {n["id"] for n in demo.notifications()}
        demo.before_action = self.drain_controls

    def drain_controls(self):
        if self.draining:
            return
        self.draining = True
        try:
            while True:
                try:
                    line = self.commands.get_nowait()
                except queue.Empty:
                    break
                if line.split(" ", 1)[0] in {"/pause", "/resume", "/end", "/requirements", "/event", "/advance"}:
                    try:
                        self.handle(line)
                    except (ValueError, KeyError, TypeError) as error:
                        self.output({"error": str(error)})
                else:
                    self.deferred.append(line)
            if any(not text.startswith("/") for text in self.deferred):
                raise DomainError("NEW_USER_MESSAGE_PENDING: 先处理用户的新消息，旧回合停止")
        finally:
            self.draining = False

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
            if not self.draining:
                self.tick()
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
            if self.offline:
                return self.demo.approve(op_id) if command == "/approve" else self.demo.reject(op_id)
            return self.session.decide(op_id, command == "/approve")
        if self.offline:
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
        if command.startswith("/"):
            raise DomainError("UNKNOWN_COMMAND")
        return self.session.chat(text)

    def tick(self):
        if self.session:
            self.session.clear_invalid_pending()
        return self.demo.tick(dispatch=self.session.system_tick if self.session else None)

    def flush_notifications(self):
        for event in self.demo.notifications():
            if event["id"] not in self.seen_notifications:
                self.output({"通知": event["payload"], "evidence_id": event["id"]})
                self.seen_notifications.add(event["id"])

    def show_result(self, result):
        if not isinstance(result, dict) or "messages" not in result:
            self.output(result)
            return
        if self.demo.task["lifecycle"] == "ACTIVE":
            from langchain_core.messages import AIMessage
            messages = [m for m in result["messages"] if isinstance(m, AIMessage) and m.content]
            if messages:
                self.output(messages[-1].content)
        if self.session.pending():
            for pending in self.session.pending():
                op = self.demo.operation(pending["args"]["operation_id"])
                self.output({"待确认的模拟订单": op["snapshot"], "operation_id": op["id"],
                             "操作": "/approve <operation_id> 或 /reject <operation_id>"})
        self.output({"当前权威状态": self.demo.task})

    def run(self):
        self.output("模拟购买 Demo。普通文本交给 LangChain Agent；/status 查看状态，Ctrl+C 退出。")

        def read_input():
            for line in sys.stdin:
                self.commands.put(line.strip())
            self.commands.put("/quit")

        threading.Thread(target=read_input, daemon=True).start()
        while True:
            try:
                if self.deferred:
                    line = self.deferred.pop(0)
                else:
                    try:
                        line = self.commands.get(timeout=0.25)
                    except queue.Empty:
                        self.tick()
                        self.flush_notifications()
                        continue
                if line == "/quit":
                    break
                result = self.handle(line)
                self.session.clear_invalid_pending()
                self.show_result(result)
                self.flush_notifications()
            except Exception as error:
                # 模型、源服务或坏输入失败保留任务，错误可见且不重复下单。
                self.demo.record("runtime_error", {"type": type(error).__name__, "error": str(error)})
                self.output({"error": str(error)})


def replay(path, data_dir):
    path, data_dir = Path(path), Path(data_dir)
    if (data_dir / "business.sqlite").exists() or (data_dir / "simulated_external.json").exists():
        raise DomainError("REPLAY_REQUIRES_EMPTY_DATA_DIR: 请使用新的 --data-dir")
    scenario = json.loads(path.read_text(encoding="utf-8"))
    clock = Clock(scenario.get("start", 1_800_000_000))
    demo = Demo(data_dir, now=clock)
    app = Application(demo, offline=True, output=lambda value: None, clock=clock)
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
    parser = argparse.ArgumentParser(description="模拟 iPhone 购买跟进，真实 LangChain Agent + 离线验收重放")
    parser.add_argument("--data-dir", default="data/demo")
    parser.add_argument("--user-id", default="demo")
    parser.add_argument("--task-id", default="t1")
    parser.add_argument("--replay", type=Path, help="明确标记的离线业务重放，不调用模型")
    args = parser.parse_args()
    if args.replay:
        try:
            display(replay(args.replay, args.data_dir))
            return 0
        except (ValueError, AssertionError, KeyError) as error:
            print(str(error), file=sys.stderr)
            return 2
    from .agent import AgentSession
    clock = Clock()
    demo = Demo(args.data_dir, user_id=args.user_id, task_id=args.task_id, now=clock)
    try:
        with AgentSession(demo) as session:
            Application(demo, session=session, clock=clock).run()
    except KeyboardInterrupt:
        return 0
    except (DomainError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    finally:
        demo.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
