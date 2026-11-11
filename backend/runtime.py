"""页面使用的进程内串行执行器。页面只读快照、投递操作，不执行模型请求。"""
import copy
import math
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

from openai import APIConnectionError, APITimeoutError

from .agent import AgentSession
from .domain import Demo, DomainError


def error_message(error, background=False):
    if isinstance(error, APIConnectionError):
        reason = "请求超时" if isinstance(error, APITimeoutError) else "连接失败"
        if background:
            return f"后台检查时模型服务{reason}，将在下次计划检查时重试。请检查网络或模型服务地址。"
        return f"模型服务{reason}，请检查网络或模型服务地址后重试。"
    return str(error)


class Clock:
    def __init__(self, start=None):
        self.fixed = start
        self.offset = 0

    def __call__(self):
        return (time.time() if self.fixed is None else self.fixed) + self.offset

    def advance(self, seconds):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            raise DomainError("INVALID_TIME_ADVANCE")
        self.offset += seconds


@dataclass
class PageAction:
    kind: str
    payload: dict
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    done: threading.Event = field(default_factory=threading.Event)
    error: str = ""


class PageRuntime:
    URGENT = {"pause", "resume", "end", "requirements", "event", "advance"}

    def __init__(self, directory, user_id="demo", task_id="t1", session_factory=AgentSession, clock=None,
                 mock_file=None):
        self.directory, self.user_id, self.task_id = directory, user_id, task_id
        self.mock_file = mock_file
        self.clock = clock or Clock()
        self.session_factory = session_factory
        self.queue = queue.Queue()
        self.deferred = deque()
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.stopping = threading.Event()
        self.draining = False
        self._snapshot = {}
        self.messages = []
        self.last_error = ""
        self.last_error_source = ""
        self.model_error = ""
        self.busy = False
        self.session = None
        self.thread = threading.Thread(target=self._run, daemon=True, name="demo-page-runtime")
        self.thread.start()
        if not self.ready.wait(15):
            raise DomainError("PAGE_INITIALIZATION_TIMEOUT")
        if self._snapshot.get("startup_error"):
            raise DomainError(self._snapshot["startup_error"])

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self._snapshot)

    def submit(self, kind, **payload):
        if self.stopping.is_set():
            raise DomainError("RUNTIME_STOPPED")
        action = PageAction(kind, copy.deepcopy(payload))
        self.queue.put(action)
        return action

    def _publish(self, pending=None):
        evidence = self.demo.evidence()
        operation = self.demo.current_operation()
        if pending is None:
            pending = self._snapshot.get("pending", [])
        if (not operation or not operation.get("valid") or operation["status"] != "PREPARED"
                or evidence["task"]["lifecycle"] != "ACTIVE"):
            pending = []
        state = {**evidence, "operation": operation, "pending": pending,
                 "notifications": self.demo.notifications(), "messages": self.messages,
                 "catalog": self.demo.sim.data["offers"], "now": self.clock(),
                 "busy": self.busy, "last_error": self.last_error, "last_error_source": self.last_error_source,
                 "model_error": self.model_error,
                 "agent_available": self.session is not None}
        with self.lock:
            self._snapshot = copy.deepcopy(state)

    def _message(self, role, content, action_id=None):
        if content:
            message = {"role": role, "content": content}
            if action_id:
                message["action_id"] = action_id
            self.messages.append(message)
            self.demo.record("page_message", message)
            # 模型调用可能耗时，消息入库后立即对页面可见。
            self._publish()

    def _agent_result(self, result):
        if not result or self.demo.task["lifecycle"] != "ACTIVE":
            return
        if result.get("pending_approval"):
            self._message("assistant", "购买确认已就绪。请核对对话下方的购买快照，点击“确认此模拟订单”或“拒绝购买”。目前尚未生成订单，无需重复输入确认口令。")
            return
        from langchain_core.messages import AIMessage
        for message in reversed(result.get("turn_messages", [])):
            if isinstance(message, AIMessage) and message.content and not message.tool_calls:
                self._message("assistant", message.content)
                break

    def _require_agent(self):
        if self.session is None:
            raise DomainError(self.model_error or "MODEL_CONFIG_REQUIRED")

    def _handle(self, action):
        kind, data = action.kind, action.payload
        if kind in {"pause", "resume", "end"}:
            return self.demo.control(kind)
        if kind == "requirements":
            self.demo.record("user_message", {"page_requirements": data["patch"]})
            return self.demo.update_requirements(data["patch"], expected_version=data.get("expected_version"))
        if kind == "advance":
            self.clock.advance(data["seconds"])
            # 由下一次循环统一检查，禁止从工具回调递归进入 Agent。
            return
        if kind == "event":
            return self.demo.event(data["event_type"], data.get("payload", {}),
                                   data.get("event_id") or uuid.uuid4().hex)
        if kind == "search":
            return self.demo.search()
        if kind == "query":
            return self.demo.get_order_status(data["operation_id"])
        if kind == "chat":
            self._require_agent()
            self._message("user", data["text"], action_id=action.id)
            return self.session.chat(data["text"])
        if kind == "choose":
            self._require_agent()
            self.session.clear_invalid_pending()
            if self.session.pending():
                raise DomainError("请先批准或拒绝当前购买确认")
            op = self.demo.prepare(data["offer_id"], data["quote_version"], data["constraints_version"])
            self._message("user", "选择商品：" + op["snapshot"]["model"] + " / " + str(op["snapshot"]["storage_gb"]) + "GB", action_id=action.id)
            return self.session.chat(f"我已选择此具体商品。请为操作 {op['id']} 调用 place_simulated_order 发起人工确认，等待我批准。")
        if kind in {"approve", "reject"}:
            self._require_agent()
            return self.session.decide(data["operation_id"], kind == "approve")
        raise DomainError("UNKNOWN_PAGE_ACTION")

    def _process(self, action, nested=False):
        try:
            result = self._handle(action)
            if not nested:
                self._agent_result(result)
            self.demo.record("page_action", {"id": action.id, "type": action.kind})
        except Exception as error:
            action.error = error_message(error)
            self.last_error = action.error
            self.last_error_source = action.kind
            self.demo.record("runtime_error", {"action": action.kind, "type": type(error).__name__, "error": str(error)})
        finally:
            self._publish()
            if nested:
                action.done.set()

    def _drain_controls(self):
        if self.draining:
            return
        self.draining = True
        try:
            while True:
                try:
                    action = self.queue.get_nowait()
                except queue.Empty:
                    break
                if action.kind in self.URGENT:
                    self._process(action, nested=True)
                else:
                    self.deferred.append(action)
            if any(a.kind == "chat" for a in self.deferred):
                raise DomainError("NEW_USER_MESSAGE_PENDING: 先处理用户的新消息")
        finally:
            self.draining = False

    def _tick(self):
        if self.session:
            self.session.clear_invalid_pending()

        def dispatch():
            self.busy = True
            self._publish()
            return self.session.system_tick()

        return self.demo.tick(dispatch=dispatch if self.session else None)

    def _run(self):
        self.demo = None
        try:
            self.demo = Demo(self.directory, self.user_id, self.task_id, now=self.clock, mock_file=self.mock_file)
            try:
                self.session = self.session_factory(self.demo)
            except (DomainError, ValueError) as error:
                self.model_error = str(error)
            self.messages = [e["payload"] for e in self.demo.store.events(self.demo.owner, "page_message")]
            self.demo.before_action = self._drain_controls
            self._publish(pending=self.session.pending() if self.session else [])
            self.ready.set()
            while not self.stopping.is_set():
                action = None
                try:
                    action = self.deferred.popleft() if self.deferred else self.queue.get(timeout=0.2)
                except queue.Empty:
                    pass
                self.busy = action is not None
                self._publish()
                if action:
                    self.last_error = ""
                    self.last_error_source = ""
                    # 当前操作已经出队，不要让后续普通输入干扰控制命令自身。
                    old = self.draining
                    if action.kind in self.URGENT:
                        self.draining = True
                    self._process(action)
                    self.draining = old
                else:
                    try:
                        if self._tick() and self.last_error_source == "timer":
                            self.last_error = ""
                            self.last_error_source = ""
                    except Exception as error:
                        self.last_error = error_message(error, background=True)
                        self.last_error_source = "timer"
                        self.demo.record("runtime_error", {"action": "timer", "type": type(error).__name__,
                                                           "error": str(error)})
                        task = self.demo.task
                        task["next_check_at"] = self.clock() + task["requirements"].get("check_interval_seconds", 10)
                        self.demo._save(task)
                try:
                    if self.session:
                        self.session.clear_invalid_pending()
                    pending = self.session.pending() if self.session else []
                except Exception as error:
                    self.last_error = error_message(error)
                    self.last_error_source = "confirmation"
                    self.demo.record("runtime_error", {"action": "confirmation", "type": type(error).__name__,
                                                       "error": str(error)})
                    pending = []
                self.busy = False
                self._publish(pending=pending)
                if action:
                    action.done.set()
        except Exception as error:
            with self.lock:
                self._snapshot = {"startup_error": str(error)}
            self.ready.set()
        finally:
            if self.session:
                self.session.close()
            if self.demo:
                self.demo.close()

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=35)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
