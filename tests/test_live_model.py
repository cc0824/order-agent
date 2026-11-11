"""可选在线验收；默认跳过，不能把离线结果当成真实模型通过。"""
import os

import pytest
from dotenv import load_dotenv

from order_demo.agent import AgentSession
from order_demo.domain import Demo
from test_acceptance import clarify

load_dotenv()


@pytest.mark.skipif(os.getenv("RUN_LIVE_MODEL") != "1", reason="未显式启用在线模型测试 RUN_LIVE_MODEL=1")
def test_live_model_normal_purchase_with_native_approval(tmp_path):
    assert os.getenv("OPENAI_API_KEY") and os.getenv("MODEL_NAME"), "需要实际模型配置"
    demo = Demo(tmp_path)
    clarify(demo)
    try:
        with AgentSession(demo) as session:
            session.chat("请调用 search_iphones 比较当前条件内的模拟商品。")
            assert demo.task["recommendation"]["offers"]
            session.chat("我选择 iphone-base-256。请准备这个具体商品并发起模拟下单确认，等待我批准。")
            pending = session.pending()
            assert len(pending) == 1
            assert demo.sim.order_count == 0
            op_id = pending[0]["args"]["operation_id"]
            session.decide(op_id, True)
            assert demo.sim.order_count == 1
            assert demo.task["phase"] == "WAITING_PAYMENT"
            demo.event("paid", {"operation_id": op_id}, "live-paid")
            demo.event("delivered", {"operation_id": op_id}, "live-delivered")
            session.system_tick()
            assert demo.task["lifecycle"] == "ACTIVE"
            demo.event("receipt", {"operation_id": op_id, "storage_gb": 256, "correct": True}, "live-receipt")
            assert demo.task["lifecycle"] == "COMPLETED"
    finally:
        demo.close()
