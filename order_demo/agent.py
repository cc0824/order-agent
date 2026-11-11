"""真实 LangChain Agent。测试替身只存在于 tests，生产不静默降级。"""
import json
import os
import sqlite3
import threading
from dataclasses import dataclass

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware, dynamic_prompt
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .domain import DomainError


@dataclass
class RunContext:
    user_id: str
    task_id: str
    source: str
    message_id: str = ""
    cleanup: bool = False
    approved_operation: str = ""


SYSTEM_PROMPT = """你是模拟 iPhone 购买跟进助手，所有外部事实与操作都是模拟。
使用中文。先澄清用途、当前手机问题、地区、总预算（整数分）、型号/容量/成色、
最晚收货时间；分别记录跟进截止时间（Unix秒）、检查间隔、terminal通知和持续跟进授权。
缺少用户决定就等待，沉默不是同意。不得自行增加预算、放宽硬条件或修改授权。
每轮可调用工具获取事实并比较，不能只输出预写结论。附条件优惠资格未知按普通价。
用户明确选择后才 prepare_purchase，再调用 place_simulated_order 触发具体订单人工确认。
下单工具必须使用已准备的 operation_id；人工确认由应用处理，绝不代替用户批准。
仅下单是外部写操作，不得代支付、取消、退货、退款或反馈收货。
下单未知时只查原操作，不得重新下单。工具错误按事实解释，不循环重试下单。
支付成功或物流送达不等于完成；只有应用核对订单和正确收货反馈才能宣布完成。
后台事件不是用户消息，不得据此修改需求或新增购买确认。后台检查用工具查询即可。
任务快照是当前权威状态，旧聊天和旧确认可能失效。已结束/到期不恢复任务。
工具结果包含金额、版本和证据，以结果为准。没有匹配商品时解释具体冲突，等待取舍。
清理中断时立即停止，不发起新的工具调用。
"""


def configured_model():
    load_dotenv()
    if not os.getenv("OPENAI_API_KEY") or not os.getenv("MODEL_NAME"):
        raise DomainError("MODEL_CONFIG_REQUIRED: 请在 .env 配置 OPENAI_API_KEY 和 MODEL_NAME；离线规则重放请使用 --replay")
    return ChatOpenAI(model=os.environ["MODEL_NAME"], api_key=os.environ["OPENAI_API_KEY"],
                      base_url=os.getenv("OPENAI_BASE_URL") or None, temperature=0,
                      timeout=float(os.getenv("MODEL_TIMEOUT_SECONDS", "30")), max_retries=0)


class AgentSession:
    def __init__(self, demo, model=None):
        self.demo = demo
        self.model = configured_model() if model is None else model
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(demo.directory / "checkpoints.sqlite", check_same_thread=False)
        self.checkpointer = SqliteSaver(self.conn)
        self.config = {"configurable": {"thread_id": demo.owner},
                       "recursion_limit": int(os.getenv("AGENT_RECURSION_LIMIT", "16"))}

        def execute(name, runtime, args, handler):
            # ToolNode 自身可并行分发；本 Demo 以一个进程内锁保证工具业务串行。
            with self.lock:
                ctx = runtime.context
                try:
                    if ctx.user_id != demo.task["user_id"] or ctx.task_id != demo.task["task_id"]:
                        raise DomainError("NOT_FOUND")
                    if ctx.cleanup:
                        raise DomainError("CLEANUP_ONLY")
                    if ctx.source != "user" and name in {"update_requirements", "prepare_purchase"}:
                        raise DomainError("USER_DECISION_REQUIRED")
                    if name == "place_simulated_order" and (ctx.source != "approval" or
                            args["operation_id"] != ctx.approved_operation):
                        raise DomainError("CONFIRMATION_REQUIRED")
                    result = handler(ctx)
                except (DomainError, ValueError, KeyError) as error:
                    result = {"error": str(error)}
                demo.record("tool", {"name": name, "args": args, "result": result,
                                     "source": ctx.source, "tool_call_id": runtime.tool_call_id})
                return result

        @tool
        def update_requirements(message_id: str, patch: dict, expected_version: int,
                                runtime: ToolRuntime[RunContext]) -> dict:
            """记录当前用户消息明确给出的需求/授权。金额为整数分，日期为 Unix 秒。"""
            def apply(ctx):
                if ctx.source != "user" or not message_id or message_id != ctx.message_id:
                    raise DomainError("USER_DECISION_REQUIRED")
                return demo.update_requirements(patch, expected_version=expected_version)
            return execute("update_requirements", runtime, dict(message_id=message_id, patch=patch,
                           expected_version=expected_version), apply)

        @tool
        def search_iphones(constraints_version: int, runtime: ToolRuntime[RunContext]) -> dict:
            """按当前硬条件查询模拟商品，返回完整价格、库存、配送、版本及不匹配原因。"""
            return execute("search_iphones", runtime, {"constraints_version": constraints_version},
                           lambda ctx: demo.search(constraints_version))

        @tool
        def prepare_purchase(offer_id: str, quote_version: int, constraints_version: int,
                             runtime: ToolRuntime[RunContext]) -> dict:
            """用户已选具体商品后准备完整购买快照，返回 operation_id；不创建订单。"""
            return execute("prepare_purchase", runtime, dict(offer_id=offer_id, quote_version=quote_version,
                           constraints_version=constraints_version),
                           lambda ctx: demo.prepare(offer_id, quote_version, constraints_version))

        @tool
        def place_simulated_order(operation_id: str, runtime: ToolRuntime[RunContext]) -> dict:
            """提交已准备的模拟订单。先由应用获取人工确认，再复核快照，唯一外部写工具。"""
            return execute("place_simulated_order", runtime, {"operation_id": operation_id},
                           lambda ctx: demo.place(operation_id))

        @tool
        def get_order_status(operation_id: str, runtime: ToolRuntime[RunContext]) -> dict:
            """只查询已提交的同一笔订单。未知结果不能重新下单，支付和售后均为只读。"""
            return execute("get_order_status", runtime, {"operation_id": operation_id},
                           lambda ctx: demo.get_order_status(operation_id))

        @dynamic_prompt
        def current_prompt(request):
            ctx = request.runtime.context
            snapshot = {"task": demo.task, "operation": demo.current_operation(), "now": demo.now(),
                        "missing": demo.missing_requirements(), "trigger": ctx.source,
                        "current_user_message_id": ctx.message_id, "cleanup_only": ctx.cleanup}
            return SYSTEM_PROMPT + "\n当前权威快照：\n" + json.dumps(snapshot, ensure_ascii=False)

        self.graph = create_agent(
            model=self.model,
            tools=[update_requirements, search_iphones, prepare_purchase, place_simulated_order, get_order_status],
            context_schema=RunContext, checkpointer=self.checkpointer,
            middleware=[current_prompt, HumanInTheLoopMiddleware(
                interrupt_on={"place_simulated_order": {"allowed_decisions": ["approve", "reject"]}},
                interrupt_mode="batched")],
        )

    def _context(self, source, message_id="", cleanup=False, approved_operation=""):
        return RunContext(self.demo.task["user_id"], self.demo.task["task_id"], source, message_id,
                          cleanup, approved_operation)

    def _invoke(self, data, context):
        result = self.graph.invoke(data, config=self.config, context=context)
        for message in result.get("messages", []):
            if isinstance(message, AIMessage):
                self.demo.record("agent_message", {"id": message.id, "content": message.content,
                                                  "tool_calls": message.tool_calls},
                                 "agent:" + str(message.id) if message.id else None)
        for action in self.pending():
            self.demo.record("approval_interrupt", action,
                             "interrupt:" + json.dumps(action, sort_keys=True))
        return result

    def pending(self):
        state = self.graph.get_state(self.config)
        return [action for task in state.tasks for interrupt in task.interrupts
                for action in interrupt.value.get("action_requests", [])]

    def clear_invalid_pending(self):
        actions = self.pending()
        if not actions:
            return
        invalid = False
        for action in actions:
            try:
                self.demo._check_operation(self.demo.operation(action["args"]["operation_id"]))
            except (DomainError, KeyError):
                invalid = True
        if invalid:
            for action in actions:
                try:
                    op = self.demo.operation(action["args"]["operation_id"])
                    if op["status"] == "PREPARED":
                        self.demo.reject(op["id"])
                except (DomainError, KeyError):
                    pass
            self._invoke(Command(resume={"decisions": [{"type": "reject", "message": "旧确认已失效，停止。"}
                                                        for _ in actions]}),
                         self._context("system", cleanup=True))

    def chat(self, text):
        self.demo._active()
        message_id = self.demo.record("user_message", {"text": text})
        # 收到新用户指令先清除旧中断；避免将改预算等新消息直接用于恢复旧购买。
        actions = self.pending()
        if actions:
            for action in actions:
                op_id = action["args"]["operation_id"]
                self.demo.reject(op_id)
            self._invoke(Command(resume={"decisions": [{"type": "reject"} for _ in actions]}),
                         self._context("system", cleanup=True))
        return self._invoke({"messages": [{"role": "user", "content": text}]},
                            self._context("user", message_id))

    def system_tick(self):
        self.clear_invalid_pending()
        if self.pending():
            return None
        return self._invoke({"messages": [{"role": "user", "content":
            "[应用定时事件，非用户指令] 按当前授权检查事实，不新增购买，不修改需求。"}]},
                            self._context("timer"))

    def decide(self, operation_id, approved):
        actions = self.pending()
        if len(actions) != 1 or actions[0].get("args", {}).get("operation_id") != operation_id:
            raise DomainError("NO_MATCHING_PENDING_APPROVAL")
        if approved:
            self.demo.approve(operation_id)
        else:
            self.demo.reject(operation_id)
        return self._invoke(Command(resume={"decisions": [{"type": "approve" if approved else "reject"}]}),
                            self._context("approval", approved_operation=operation_id if approved else ""))

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
