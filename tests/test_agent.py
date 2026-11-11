"""真实 LangChain 运行时，模型使用离线测试替身；不是在线模型验收。"""
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from order_demo.agent import AgentSession
from order_demo.domain import Demo, DomainError
from test_acceptance import clarify, prepared


class ToolCallingModel(BaseChatModel):
    responses: list[AIMessage] = Field(default_factory=list)
    position: int = 0

    @property
    def _llm_type(self):
        return "offline-test-tool-calling-model"

    def bind_tools(self, tools: Any, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        message = self.responses[self.position] if self.position < len(self.responses) else AIMessage(content="等待用户决定。")
        self.position += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


def call(name, args, number=1):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{number}", "type": "tool_call"}])


def test_agent_queries_real_catalog_through_registered_tool(tmp_path):
    demo = Demo(tmp_path)
    clarify(demo)
    model = ToolCallingModel(responses=[call("search_iphones", {"constraints_version": 1}), AIMessage(content="请比较并选择。")])
    with AgentSession(demo, model=model) as session:
        session.chat("比较一下符合条件的手机")
        assert demo.task["recommendation"]["offers"][0]["total_cents"] == 579900
        calls = demo.store.events(demo.owner, "tool")
        assert calls[0]["payload"]["name"] == "search_iphones"
        assert demo.sim.order_count == 0
    demo.close()


def test_native_hitl_waits_then_one_user_approval_submits_once(tmp_path):
    demo = Demo(tmp_path)
    clarify(demo)
    op = prepared(demo)
    model = ToolCallingModel(responses=[call("place_simulated_order", {"operation_id": op["id"]})])
    with AgentSession(demo, model=model) as session:
        session.chat("请发起这笔购买的确认")
        assert session.pending()[0]["args"]["operation_id"] == op["id"]
        assert demo.sim.order_count == 0
        session.decide(op["id"], True)
        assert demo.sim.order_count == 1
        assert demo.task["phase"] == "WAITING_PAYMENT"
        assert not session.pending()
        with pytest.raises(DomainError):
            session.decide(op["id"], True)
        assert demo.sim.create_calls == 1
    demo.close()


def test_stale_approval_is_blocked_and_pending_is_rejected(tmp_path):
    demo = Demo(tmp_path)
    clarify(demo)
    op = prepared(demo)
    model = ToolCallingModel(responses=[call("place_simulated_order", {"operation_id": op["id"]})])
    with AgentSession(demo, model=model) as session:
        session.chat("发起确认")
        demo.update_requirements({"budget_cents": 600000})
        with pytest.raises(DomainError):
            session.decide(op["id"], True)
        session.clear_invalid_pending()
        assert not session.pending()
        assert demo.sim.order_count == 0
    demo.close()


def test_background_agent_cannot_modify_user_requirements(tmp_path):
    demo = Demo(tmp_path)
    clarify(demo)
    model = ToolCallingModel(responses=[call("update_requirements", {
        "message_id": "forged", "patch": {"budget_cents": 999999}, "expected_version": 1})])
    with AgentSession(demo, model=model) as session:
        session.system_tick()
        assert demo.task["requirements"]["budget_cents"] == 800000
        assert demo.store.events(demo.owner, "tool")[-1]["payload"]["result"]["error"] == "USER_DECISION_REQUIRED"
    demo.close()


def test_pending_native_interrupt_survives_reopen(tmp_path):
    demo = Demo(tmp_path)
    clarify(demo)
    op = prepared(demo)
    with AgentSession(demo, model=ToolCallingModel(responses=[call("place_simulated_order", {"operation_id": op["id"]})])) as session:
        session.chat("发起确认")
    with AgentSession(demo, model=ToolCallingModel()) as session:
        assert session.pending()
        session.decide(op["id"], True)
        assert demo.sim.order_count == 1
    demo.close()


def test_expired_native_confirmation_can_prepare_fresh_purchase(tmp_path):
    clock = [1800000000]
    demo = Demo(tmp_path, now=lambda: clock[0])
    clarify(demo)
    op = prepared(demo)
    with AgentSession(demo, model=ToolCallingModel(responses=[call("place_simulated_order", {"operation_id": op["id"]})])) as session:
        session.chat("发起确认")
        clock[0] += 301
        session.clear_invalid_pending()
        assert not session.pending()
        fresh = prepared(demo)
        assert fresh["id"] != op["id"]
        demo.approve(fresh["id"])
        assert demo.sim.order_count == 0
    demo.close()


def test_order_approval_does_not_authorize_budget_update_without_user_message(tmp_path):
    demo = Demo(tmp_path)
    clarify(demo)
    op = prepared(demo)
    model = ToolCallingModel(responses=[
        call("place_simulated_order", {"operation_id": op["id"]}),
        call("update_requirements", {"message_id": "", "patch": {"budget_cents": 999999},
                                     "expected_version": 1}, 2),
    ])
    with AgentSession(demo, model=model) as session:
        session.chat("发起确认")
        session.decide(op["id"], True)
        assert demo.task["requirements"]["budget_cents"] == 800000
        assert demo.sim.order_count == 1
        assert demo.store.events(demo.owner, "tool")[-1]["payload"]["result"]["error"] == "USER_DECISION_REQUIRED"
    demo.close()
