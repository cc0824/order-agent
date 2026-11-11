import threading
import time

import pytest
import httpx
from langchain_core.messages import AIMessage
from openai import APIConnectionError

from backend.agent import AgentSession
from backend.domain import Demo
from backend.runtime import Clock, PageRuntime
from test_acceptance import clarify, prepared
from test_agent import ToolCallingModel, call


def perform(runtime, kind, **payload):
    action = runtime.submit(kind, **payload)
    assert action.done.wait(5), f"action timed out: {kind}"
    if action.error:
        raise AssertionError(action.error)
    return runtime.snapshot()


def test_page_controls_and_background_checks_without_new_chat(tmp_path):
    with PageRuntime(tmp_path, session_factory=lambda demo: None) as runtime:
        start = runtime.snapshot()["now"]
        state = perform(runtime, "requirements", patch=dict(
            use_case="日常", current_problem="电池老化", region="CN", budget_cents=600000,
            storage_gb=256, condition="new", desired_delivery_at=start + 3600,
            followup_until=start + 7200, check_interval_seconds=1,
            notification_channel="page", followup_authorized=True))
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not runtime.snapshot()["notifications"]:
            threading.Event().wait(0.05)
        assert runtime.snapshot()["notifications"]
        state = perform(runtime, "pause")
        count = len(state["notifications"])
        perform(runtime, "event", event_type="offer", payload={"offer_id": "iphone-base-256", "price_cents": 569900})
        state = perform(runtime, "advance", seconds=20)
        assert state["task"]["lifecycle"] == "PAUSED"
        assert len(state["notifications"]) == count
        perform(runtime, "resume")
        state = perform(runtime, "advance", seconds=20)
        assert len(state["notifications"]) == count
        state = perform(runtime, "end")
        assert state["task"]["lifecycle"] == "ENDED"


class PurchaseModel(ToolCallingModel):
    def _generate(self, messages, **kwargs):
        import re
        from langchain_core.outputs import ChatGeneration, ChatResult
        if messages[-1].type == "human" and "请为操作 " in messages[-1].content:
            op_id = re.search(r"请为操作 ([a-f0-9]+)", messages[-1].content).group(1)
            return ChatResult(generations=[ChatGeneration(message=call("place_simulated_order", {"operation_id": op_id}))])
        return super()._generate(messages, **kwargs)


def test_page_native_approval_and_receipt_complete_real_business(tmp_path):
    def factory(demo):
        clarify(demo, notification_channel="page")
        return AgentSession(demo, model=PurchaseModel())

    with PageRuntime(tmp_path, session_factory=factory) as runtime:
        perform(runtime, "search")
        state = perform(runtime, "choose", offer_id="iphone-base-256", quote_version=1, constraints_version=1)
        assert state["pending"]
        assert state["external_order_count"] == 0
        op_id = state["operation"]["id"]
        state = perform(runtime, "approve", operation_id=op_id)
        assert state["task"]["phase"] == "WAITING_PAYMENT"
        assert state["external_order_count"] == 1
        perform(runtime, "event", event_type="paid", payload={"operation_id": op_id})
        perform(runtime, "event", event_type="delivered", payload={"operation_id": op_id})
        state = perform(runtime, "event", event_type="receipt", payload={"operation_id": op_id, "storage_gb": 256, "correct": True})
        assert state["task"]["lifecycle"] == "COMPLETED"
        assert state["external_order_count"] == 1


def test_native_interrupt_shows_confirmation_instead_of_repeating_old_reply(tmp_path):
    def factory(demo):
        clarify(demo, notification_channel="page")
        op = prepared(demo)
        return AgentSession(demo, model=ToolCallingModel(responses=[
            AIMessage(content="旧回答：还差你一句确认提交"),
            call("place_simulated_order", {"operation_id": op["id"]})]))

    with PageRuntime(tmp_path, session_factory=factory) as runtime:
        perform(runtime, "chat", text="要第1个")
        state = perform(runtime, "chat", text="确认提交")
        assert state["pending"]
        replies = [m["content"] for m in state["messages"] if m["role"] == "assistant"]
        assert replies.count("旧回答：还差你一句确认提交") == 1
        assert "确认此模拟订单" in replies[-1]
        assert state["external_order_count"] == 0
        op_id = state["operation"]["id"]
        state = perform(runtime, "chat", text="确认提交")
        assert state["pending"][0]["args"]["operation_id"] == op_id
        assert state["operation"]["valid"]
        assert state["external_order_count"] == 0
        state = perform(runtime, "approve", operation_id=op_id)
        assert state["external_order_count"] == 1


def test_idle_poll_does_not_publish_busy_and_disable_confirmation_buttons(tmp_path):
    checked = threading.Event()
    busy_states = []

    class ObservedRuntime(PageRuntime):
        def _tick(self):
            busy_states.append(self.snapshot()["busy"])
            checked.set()
            return super()._tick()

    with ObservedRuntime(tmp_path, session_factory=lambda demo: None) as runtime:
        assert checked.wait(5)
        assert not any(busy_states)


class FailFirstConnectionModel(ToolCallingModel):
    def _generate(self, messages, **kwargs):
        if self.position == 0:
            self.position += 1
            raise APIConnectionError(request=httpx.Request("POST", "https://model.invalid/chat/completions"))
        return super()._generate(messages, **kwargs)


def wait_for_runtime(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.02)
    raise AssertionError("后台状态未按预期更新")


def test_successful_background_retry_clears_previous_timer_error(tmp_path):
    clock = Clock(1800000000)

    def factory(demo):
        clarify(demo, notification_channel="page")
        return AgentSession(demo, model=FailFirstConnectionModel())

    with PageRuntime(tmp_path, session_factory=factory, clock=clock) as runtime:
        wait_for_runtime(lambda: bool(runtime.snapshot()["last_error"]))
        clock.advance(11)
        wait_for_runtime(lambda: any(e["kind"] == "tick" for e in runtime.snapshot()["events"]))
        assert runtime.snapshot()["last_error"] == ""


def test_background_connection_failure_explains_source_and_preserves_raw_evidence(tmp_path):
    def factory(demo):
        clarify(demo, notification_channel="page")
        return AgentSession(demo, model=FailFirstConnectionModel())

    with PageRuntime(tmp_path, session_factory=factory, clock=Clock(1800000000)) as runtime:
        wait_for_runtime(lambda: bool(runtime.snapshot()["last_error"]))
        state = runtime.snapshot()
        assert state["last_error_source"] == "timer"
        assert "模型服务" in state["last_error"] and "后台" in state["last_error"]
        event = next(e for e in state["events"] if e["kind"] == "runtime_error")
        assert event["payload"]["type"] == "APIConnectionError"
        assert event["payload"]["error"] == "Connection error."


def test_successful_background_check_does_not_clear_user_action_error(tmp_path):
    clock = Clock(1800000000)

    def factory(demo):
        clarify(demo, notification_channel="page")
        task = demo.task
        task["next_check_at"] = clock() + 10
        demo._save(task)
        return AgentSession(demo, model=FailFirstConnectionModel())

    with PageRuntime(tmp_path, session_factory=factory, clock=clock) as runtime:
        action = runtime.submit("chat", text="查询当前商品")
        assert action.done.wait(5) and action.error
        clock.advance(11)
        wait_for_runtime(lambda: any(e["kind"] == "tick" for e in runtime.snapshot()["events"]))
        assert runtime.snapshot()["last_error"] == action.error


def test_page_missing_model_shows_error_without_silent_purchase(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    with PageRuntime(tmp_path) as runtime:
        assert "MODEL_CONFIG_REQUIRED" in runtime.snapshot()["model_error"]
        action = runtime.submit("chat", text="直接买一台")
        assert action.done.wait(5)
        assert "MODEL_CONFIG_REQUIRED" in action.error
        assert runtime.snapshot()["external_order_count"] == 0


def test_page_pause_received_during_model_call_blocks_prepare(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class SlowModel(ToolCallingModel):
        def _generate(self, messages, **kwargs):
            if not self.position:
                entered.set()
                assert release.wait(5)
            return super()._generate(messages, **kwargs)

    def factory(demo):
        clarify(demo, notification_channel="page")
        return AgentSession(demo, model=SlowModel(responses=[call("prepare_purchase", {
            "offer_id": "iphone-base-256", "quote_version": 1, "constraints_version": 1})]))

    with PageRuntime(tmp_path, session_factory=factory) as runtime:
        chat = runtime.submit("chat", text="选择256GB商品")
        assert entered.wait(5)
        pause = runtime.submit("pause")
        assert not pause.done.is_set()
        release.set()
        assert pause.done.wait(5)
        assert chat.done.wait(5)
        state = runtime.snapshot()
        assert state["task"]["lifecycle"] == "PAUSED"
        assert state["operation"] is None
        assert state["external_order_count"] == 0


def test_invalid_time_advance_rejected():
    clock = Clock(1800000000)
    for value in [-1, float("nan"), float("inf")]:
        with pytest.raises(ValueError):
            clock.advance(value)


def test_stale_page_form_cannot_overwrite_new_requirements(tmp_path):
    with PageRuntime(tmp_path, session_factory=lambda demo: None) as runtime:
        perform(runtime, "requirements", patch={"budget_cents": 600000}, expected_version=0)
        action = runtime.submit("requirements", patch={"budget_cents": 800000}, expected_version=0)
        assert action.done.wait(5)
        assert "STALE_VERSION" in action.error
        assert runtime.snapshot()["task"]["requirements"]["budget_cents"] == 600000


@pytest.mark.parametrize("fail", [False, True])
def test_user_message_is_published_before_model_returns_even_on_error(tmp_path, fail):
    entered, release = threading.Event(), threading.Event()

    class SlowReply(ToolCallingModel):
        def _generate(self, messages, **kwargs):
            entered.set()
            assert release.wait(10)
            if fail:
                raise RuntimeError("模拟模型连接失败")
            return super()._generate(messages, **kwargs)

    def factory(demo):
        return AgentSession(demo, model=SlowReply(responses=[AIMessage(content="已收到。")]))

    with PageRuntime(tmp_path, session_factory=factory) as runtime:
        action = runtime.submit("chat", text="消息应该立即出现")
        try:
            assert entered.wait(5)
            state = runtime.snapshot()
            assert not action.done.is_set()
            assert state["messages"] == [{"role": "user", "content": "消息应该立即出现", "action_id": action.id}]
            assert state["busy"]
        finally:
            release.set()
        assert action.done.wait(5)
        state = runtime.snapshot()
        assert len([m for m in state["messages"] if m["role"] == "user"]) == 1
        assert bool(action.error) == fail


@pytest.mark.parametrize("stage", ["approve", "place"])
def test_pause_then_resume_during_approval_cannot_revive_old_confirmation(tmp_path, stage):
    entered, release = threading.Event(), threading.Event()

    def factory(demo):
        clarify(demo, notification_channel="page")
        session = AgentSession(demo, model=PurchaseModel())
        original = getattr(demo, stage)

        def blocked(op_id):
            # 暂停在操作快照已读取、即将检查控制队列的位置。
            before = demo.before_action

            def barrier():
                demo.before_action = before
                entered.set()
                assert release.wait(5)
                before()

            demo.before_action = barrier
            return original(op_id)

        setattr(demo, stage, blocked)
        return session

    with PageRuntime(tmp_path, session_factory=factory) as runtime:
        perform(runtime, "search")
        state = perform(runtime, "choose", offer_id="iphone-base-256", quote_version=1, constraints_version=1)
        approval = runtime.submit("approve", operation_id=state["operation"]["id"])
        assert entered.wait(5)
        pause = runtime.submit("pause")
        resume = runtime.submit("resume")
        release.set()
        assert approval.done.wait(5)
        assert pause.done.wait(5) and not pause.error
        assert resume.done.wait(5) and not resume.error
        state = runtime.snapshot()
        assert state["task"]["lifecycle"] == "ACTIVE"
        assert state["external_order_count"] == 0
        assert not state["operation"]["valid"]
        assert not state["pending"]
