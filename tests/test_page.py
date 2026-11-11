import threading
import time
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from backend.runtime import PageRuntime
from backend.agent import AgentSession
from langchain_core.messages import AIMessage
from test_acceptance import clarify
from test_agent import ToolCallingModel
from test_runtime import PurchaseModel, perform


def wait_for(predicate):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        if predicate():
            return
        threading.Event().wait(0.05)
    raise AssertionError("页面操作没有完成")


def test_page_renders_and_controls_task_with_buttons(tmp_path):
    with PageRuntime(tmp_path, session_factory=lambda demo: None) as runtime:
        app = AppTest.from_file(Path("app.py").resolve())
        app.session_state["page_runtime"] = runtime
        app.run(timeout=10)
        assert not app.exception
        assert app.title[0].value == "iPhone 购买跟进"
        assert app.chat_input[0].disabled
        app.button(key="pause").click().run()
        wait_for(lambda: runtime.snapshot()["task"]["lifecycle"] == "PAUSED")
        app.run()
        assert app.button(key="pause").disabled
        assert not app.button(key="resume").disabled
        app.button(key="resume").click().run()
        wait_for(lambda: runtime.snapshot()["task"]["lifecycle"] == "ACTIVE")
        app.button(key="end").click().run()
        wait_for(lambda: runtime.snapshot()["task"]["lifecycle"] == "ENDED")
        app.run()
        assert not app.exception
        assert app.button(key="resume").disabled


def test_page_requirements_form_saves_budget_and_page_authorization(tmp_path):
    with PageRuntime(tmp_path, session_factory=lambda demo: None) as runtime:
        app = AppTest.from_file(Path("app.py").resolve())
        app.session_state["page_runtime"] = runtime
        app.run(timeout=10)
        app.text_input(key="use_case").set_value("拍照和日常")
        app.text_input(key="current_problem").set_value("电池老化")
        app.number_input(key="budget_yuan").set_value(6000.0)
        app.checkbox(key="followup_authorized").check()
        app.button(key="FormSubmitter:requirements-保存需求").click().run()
        wait_for(lambda: runtime.snapshot()["task"]["constraints_version"] == 1)
        req = runtime.snapshot()["task"]["requirements"]
        assert req["budget_cents"] == 600000
        assert req["notification_channel"] == "page"
        assert req["followup_authorized"] is True
        app.run()
        assert not app.exception
        app.number_input(key="budget_yuan").set_value(6100.0)
        app.button(key="FormSubmitter:requirements-保存需求").click().run()
        wait_for(lambda: runtime.snapshot()["task"]["requirements"]["budget_cents"] == 610000)


def test_page_loads_legacy_chinese_condition_as_new_selection(tmp_path):
    def factory(demo):
        clarify(demo)
        task = demo.task
        task["requirements"]["condition"] = "全新"
        demo._save(task)
        return None

    with PageRuntime(tmp_path, session_factory=factory) as runtime:
        app = AppTest.from_file(Path("app.py").resolve())
        app.session_state["page_runtime"] = runtime
        app.run(timeout=10)
        assert not app.exception
        assert app.selectbox(key="condition").value == "new"
        app.button(key="load_requirements").click().run()
        assert not app.exception
        assert app.selectbox(key="condition").value == "new"


def test_page_keeps_requirement_draft_until_explicitly_loading_latest(tmp_path):
    with PageRuntime(tmp_path, session_factory=lambda demo: None) as runtime:
        app = AppTest.from_file(Path("app.py").resolve())
        app.session_state["page_runtime"] = runtime
        app.run()
        app.text_input(key="use_case").set_value("尚未提交的拍照需求")
        # 预算必须由用户填写，保存失败后的可选用途草稿仍须保留。
        app.number_input(key="budget_yuan").set_value(None)
        app.button(key="FormSubmitter:requirements-保存需求").click().run()
        assert app.error
        perform(runtime, "requirements", patch={"budget_cents": 750000, "storage_gb": 512})
        app.run()
        assert not app.exception
        assert app.text_input(key="use_case").value == "尚未提交的拍照需求"
        assert app.number_input(key="budget_yuan").value is None
        assert app.selectbox(key="storage_gb").value == 128
        # 旧草稿不能覆盖聊天中生效的新需求；只有明确点击才载入新值。
        assert app.session_state["requirements_form_version"] == 0
        app.button(key="load_requirements").click().run()
        assert app.number_input(key="budget_yuan").value == 7500.0
        assert app.selectbox(key="storage_gb").value == 512


def test_page_only_needs_budget_to_save_and_query_with_followup_disabled(tmp_path):
    with PageRuntime(tmp_path, session_factory=lambda demo: None) as runtime:
        app = AppTest.from_file(Path("app.py").resolve())
        app.session_state["page_runtime"] = runtime
        app.run()
        assert app.number_input(key="budget_yuan").value is None
        assert not app.checkbox(key="followup_authorized").value
        app.number_input(key="budget_yuan").set_value(10000.0)
        app.button(key="FormSubmitter:requirements-保存需求").click().run()
        wait_for(lambda: app.session_state["last_page_action"].done.is_set())
        assert not app.session_state["last_page_action"].error
        perform(runtime, "search")
        state = runtime.snapshot()
        assert len(state["task"]["recommendation"]["offers"]) == 3
        assert state["task"]["requirements"]["followup_authorized"] is False
        assert not any(e["kind"] == "tick" for e in state["events"])


def test_page_buttons_purchase_and_correct_receipt(tmp_path):
    def factory(demo):
        clarify(demo, notification_channel="page")
        return AgentSession(demo, model=PurchaseModel())

    with PageRuntime(tmp_path, session_factory=factory) as runtime:
        app = AppTest.from_file(Path("app.py").resolve())
        app.session_state["page_runtime"] = runtime
        perform(runtime, "search")
        app.run()
        app.button(key="choose_iphone-base-256").click().run()
        wait_for(lambda: bool(runtime.snapshot()["pending"]))
        app.run()
        assert runtime.snapshot()["external_order_count"] == 0
        app.button(key="approve").click().run()
        wait_for(lambda: runtime.snapshot()["task"]["phase"] == "WAITING_PAYMENT")
        app.run()
        app.button(key="FormSubmitter:order_event-提交订单事件").click().run()
        wait_for(lambda: app.session_state["last_page_action"].done.is_set())
        app.run()
        app.button(key="query").click().run()
        wait_for(lambda: runtime.snapshot()["operation"]["order"]["payment"] == "PAID")
        app.run()
        app.selectbox(key="order_event_type").select("物流送达")
        app.button(key="FormSubmitter:order_event-提交订单事件").click().run()
        wait_for(lambda: app.session_state["last_page_action"].done.is_set())
        app.run()
        app.button(key="query").click().run()
        wait_for(lambda: runtime.snapshot()["operation"]["order"]["delivery"] == "DELIVERED")
        app.run()
        assert runtime.snapshot()["task"]["lifecycle"] == "ACTIVE"
        app.checkbox(key="receipt_correct").check()
        app.button(key="FormSubmitter:receipt-提交收货反馈").click().run()
        wait_for(lambda: runtime.snapshot()["task"]["lifecycle"] == "COMPLETED")
        app.run()
        assert not app.exception
        assert runtime.snapshot()["external_order_count"] == 1


def test_page_echoes_queued_chat_before_worker_can_process_it(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class SlowFirstReply(ToolCallingModel):
        def _generate(self, messages, **kwargs):
            if not self.position:
                entered.set()
                assert release.wait(15)
            return super()._generate(messages, **kwargs)

    def factory(demo):
        return AgentSession(demo, model=SlowFirstReply(responses=[
            AIMessage(content="第一条回复"), AIMessage(content="第二条回复")]))

    with PageRuntime(tmp_path, session_factory=factory) as runtime:
        first = runtime.submit("chat", text="第一条消息")
        try:
            assert entered.wait(5)
            app = AppTest.from_file(Path("app.py").resolve())
            app.session_state["page_runtime"] = runtime
            app.run()
            app.chat_input(key="chat").set_value("第二条消息").run()
            assert not app.exception
            second = app.session_state["last_page_action"]
            assert not first.done.is_set() and not second.done.is_set()
            texts = [message.markdown[0].value for message in app.chat_message]
            assert texts == ["第一条消息", "第二条消息"]
            assert any("等待 AI 回复" in caption.value for caption in app.caption)
        finally:
            release.set()
        assert first.done.wait(5) and second.done.wait(5)
        app.run()
        texts = [message.markdown[0].value for message in app.chat_message]
        assert texts.count("第一条消息") == 1
        assert texts.count("第二条消息") == 1
        assert "第一条回复" in texts and "第二条回复" in texts


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_chat_displays_purchase_snapshot_and_handles_native_decision(tmp_path, decision):
    def factory(demo):
        clarify(demo, notification_channel="page")
        return AgentSession(demo, model=PurchaseModel())

    with PageRuntime(tmp_path, session_factory=factory) as runtime:
        perform(runtime, "choose", offer_id="iphone-base-256", quote_version=1, constraints_version=1)
        app = AppTest.from_file(Path("app.py").resolve())
        app.session_state["page_runtime"] = runtime
        app.run(timeout=10)
        assert not app.exception
        chat = app.tabs[0]
        assert chat.dataframe[0].value.iloc[0]["总费用（元）"] == "5799.00"
        assert any("完整购买快照" in expander.label for expander in chat.expander)
        assert runtime.snapshot()["external_order_count"] == 0
        app.button(key="chat_" + decision).click().run()
        wait_for(lambda: app.session_state["last_page_action"].done.is_set())
        assert not app.session_state["last_page_action"].error
        app.run()
        assert not app.exception
        state = runtime.snapshot()
        assert not state["pending"]
        assert state["external_order_count"] == (1 if decision == "approve" else 0)
