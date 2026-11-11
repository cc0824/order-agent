"""只使用 Streamlit 标准组件的功能页面。"""
import atexit
import json
import os
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import streamlit as st

from backend.config import load_environment, project_path
from backend.conditions import normalize_condition
from backend.runtime import PageRuntime

TZ = ZoneInfo("Asia/Hong_Kong")
LIFECYCLE = {"ACTIVE": "进行中", "PAUSED": "已暂停", "ENDED": "已结束", "EXPIRED": "已到期", "COMPLETED": "已完成"}
PHASE = {"CLARIFYING": "待补充需求", "SEARCHING": "比较商品", "WAITING_CONFIRMATION": "等待购买确认",
         "ORDER_UNKNOWN": "下单结果待核验", "WAITING_PAYMENT": "等待用户支付", "TRACKING": "跟进配送",
         "VERIFYING_RECEIPT": "等待收货核对", "NEEDS_DECISION": "需要用户决定"}


@st.cache_resource
def get_runtime(directory, user_id, task_id, mock_file):
    runtime = PageRuntime(directory, user_id, task_id, mock_file=mock_file)
    atexit.register(runtime.close)
    return runtime


def timestamp_label(value):
    return datetime.fromtimestamp(value, TZ).strftime("%Y-%m-%d %H:%M") if value else "未填写"


def offer_row(offer):
    return {"商品": offer["model"], "容量": f'{offer["storage_gb"]} GB', "成色": offer["condition"],
            "卖家": offer["seller"], "总费用（元）": f'{offer["total_cents"] / 100:.2f}',
            "预计送达": timestamp_label(offer["expected_delivery_at"])}


def submit(runtime, kind, **data):
    action = runtime.submit(kind, **data)
    st.session_state["last_page_action"] = action
    return action


@st.fragment
def render_requirements(runtime):
    state = runtime.snapshot()
    req = state["task"]["requirements"]
    now = datetime.fromtimestamp(state["now"], TZ)
    defaults = {"use_case": req.get("use_case", ""), "current_problem": req.get("current_problem", ""),
                "region": req.get("region", "CN"),
                "budget_yuan": req["budget_cents"] / 100 if "budget_cents" in req else None,
                "model": req.get("model") or "",
                "storage_gb": req.get("storage_gb", 128), "condition": normalize_condition(req.get("condition", "new")),
                "followup_authorized": req.get("followup_authorized", False),
                "interval": req.get("check_interval_seconds", 600)}
    desired = datetime.fromtimestamp(req["desired_delivery_at"], TZ) if req.get("desired_delivery_at") else now + timedelta(days=7)
    until = datetime.fromtimestamp(req["followup_until"], TZ) if req.get("followup_until") else now + timedelta(days=7)
    defaults.update(desired_date=desired.date(), desired_time=desired.time().replace(tzinfo=None),
                    until_date=until.date(), until_time=until.time().replace(tzinfo=None))
    version = state["task"]["constraints_version"]
    saved = st.session_state.get("requirements_save_action")
    if (saved and saved.done.is_set() and not saved.error
            and all(req.get(k) == v for k, v in saved.payload["patch"].items())):
        st.session_state["requirements_form_version"] = version
        del st.session_state["requirements_save_action"]
    reload_form = st.button("载入当前已生效需求（替换草稿）", key="load_requirements")
    if "requirements_form_version" not in st.session_state or reload_form:
        for key, value in defaults.items():
            st.session_state[key] = value
        st.session_state["requirements_form_version"] = version
    form_version = st.session_state["requirements_form_version"]
    if form_version != version:
        st.warning("生效需求已改变，当前草稿仍保留。保存旧版本会被拒绝；可先记录草稿，再点击上方按钮载入最新需求。")
    with st.form("requirements"):
        st.caption("只需填写预算；其他条件已预填，可展开修改。用途可帮助推荐，但不是必填项。")
        budget = st.number_input("总预算（元，必填）", min_value=1.0, step=100.0, value=None, key="budget_yuan")
        use_case = st.text_input("主要用途（可选）", key="use_case")
        with st.expander("更多选项（已预填，可按需修改）"):
            left, right = st.columns(2)
            with left:
                model = st.text_input("指定型号（可选，留空不限）", key="model")
                storage = st.selectbox("最低容量（GB）", [128, 256, 512, 1024], key="storage_gb")
                problem = st.text_input("当前手机问题（可选）", key="current_problem")
                desired_date = st.date_input("期望收货日期", key="desired_date")
                desired_time = st.time_input("期望收货时间", key="desired_time")
            with right:
                region = st.text_input("购买地区", key="region", help="中国、中国大陆、CN 均可；明确不限制商品地区/版本时填写“不限”。")
                condition = st.selectbox("成色", ["new", "used"], format_func=lambda s: "全新" if s == "new" else "二手", key="condition")
                until_date = st.date_input("自动跟进截止日期", key="until_date")
                until_time = st.time_input("自动跟进截止时间", key="until_time")
                interval = st.number_input("检查间隔（秒）", min_value=1, step=1, key="interval")
            st.caption("日期使用香港时间。收货期限与跟进截止日期分别记录，通知显示在本页面。")
        authorized = st.checkbox("开启持续跟进（明确授权）", key="followup_authorized")
        until = datetime.combine(until_date, until_time, TZ).timestamp()
        st.caption(f"开启后每 {int(interval)} 秒检查一次，跟进至 {timestamp_label(until)}，关键变化通过页面通知；不勾选也可查询和购买。")
        save = st.form_submit_button("保存需求", disabled=state["task"]["lifecycle"] != "ACTIVE")
        if save and budget is None:
            st.error("请先填写总预算，预算不会由系统自动决定。")
        elif save:
            patch = dict(use_case=use_case, current_problem=problem, region=region,
                         budget_cents=int(Decimal(str(budget)) * 100), model=model.strip() or None,
                         storage_gb=storage, condition=condition,
                         desired_delivery_at=datetime.combine(desired_date, desired_time, TZ).timestamp(),
                         followup_until=datetime.combine(until_date, until_time, TZ).timestamp(),
                         check_interval_seconds=int(interval), notification_channel="page", followup_authorized=authorized)
            st.session_state["requirements_save_action"] = submit(
                runtime, "requirements", patch=patch, expected_version=form_version)
            st.success("需求已提交，处理结果会自动刷新。")
    with st.expander("当前已生效的需求"):
        st.json(req)


@st.fragment(run_every=1)
def render_purchase(runtime):
    state = runtime.snapshot()
    active = state["task"]["lifecycle"] == "ACTIVE"
    if st.button("查询符合条件的商品", key="search", disabled=not active or state["busy"]):
        submit(runtime, "search")
    recommendation = state["task"].get("recommendation") or {}
    offers = recommendation.get("offers", [])
    if offers:
        st.dataframe([offer_row(offer) for offer in offers], hide_index=True, width="stretch")
        for offer in offers:
            if offer.get("conditional_cents") is not None:
                st.caption(f'{offer["model"]} 附条件价格 ¥{offer["conditional_cents"]/100:.2f}：{offer.get("discount_condition", "需核实资格")}。比较与确认以表中的实际总费用为准。')
            label = f'选择 {offer["model"]} / {offer["storage_gb"]}GB / {offer["seller"]} / ¥{offer["total_cents"]/100:.2f} 并发起购买确认'
            if st.button(label, key="choose_" + offer["offer_id"], disabled=not active or state["busy"] or not state["agent_available"] or bool(state["pending"])):
                submit(runtime, "choose", offer_id=offer["offer_id"], quote_version=offer["quote_version"],
                       constraints_version=recommendation["constraints_version"])
    elif recommendation.get("status") == "NO_MATCH":
        st.warning("当前没有满足全部硬条件的商品，请调整需求或等待新报价。")
        st.json(recommendation.get("rejected", []))
    op = state["operation"]
    if not op:
        return
    st.subheader("当前购买操作")
    st.dataframe([offer_row(op["snapshot"])], hide_index=True, width="stretch")
    with st.expander("完整购买快照（含地区、费用和报价版本）"):
        st.json(op["snapshot"])
    st.write("操作状态：", op["status"])
    if op.get("order"):
        order = op["order"]
        st.write("订单编号：", order["order_id"])
        st.write("支付 / 配送 / 退款：", order["payment"], "/", order["delivery"], "/", order["refund"])
    if op["status"] in {"ACCEPTED", "UNKNOWN", "SUBMITTING"}:
        if st.button("查询当前订单状态", key="query", disabled=state["busy"]):
            submit(runtime, "query", operation_id=op["id"])
    render_confirmation(runtime, state, show_snapshot=False)


def render_confirmation(runtime, state, prefix="", show_snapshot=True):
    op = state["operation"]
    if (not op or not op.get("valid") or op["status"] != "PREPARED"
            or state["task"]["lifecycle"] != "ACTIVE" or state["now"] >= op["valid_until"]
            or not any(a.get("args", {}).get("operation_id") == op["id"] for a in state["pending"])):
        return
    if show_snapshot:
        st.subheader("待确认的购买")
        st.dataframe([offer_row(op["snapshot"])], hide_index=True, width="stretch")
        with st.expander("完整购买快照（含地区、费用和报价版本）"):
            st.json(op["snapshot"])
    st.caption("确认有效期至 " + timestamp_label(op["valid_until"]))
    st.info("请核对商品、卖家、最终费用及配送条件。确认只创建模拟订单，不会自动付款。")
    yes, no = st.columns(2)
    if yes.button("确认此模拟订单", key=prefix + "approve", disabled=state["busy"]):
        submit(runtime, "approve", operation_id=op["id"])
    if no.button("拒绝购买", key=prefix + "reject", disabled=state["busy"]):
        submit(runtime, "reject", operation_id=op["id"])


@st.fragment
def render_simulation(runtime):
    st.button("载入当前订单的模拟反馈表单（重新读取状态）", key="load_simulation")
    state = runtime.snapshot()
    st.caption("以下操作仅注入模拟事实。支付和售后由测试用户操作，助手只读取结果。")
    seconds = st.number_input("推进演示时间（秒）", min_value=1, value=10, key="advance_seconds")
    if st.button("推进时间", key="advance"):
        submit(runtime, "advance", seconds=int(seconds))
    op = state["operation"]
    if not op or op["status"] not in {"ACCEPTED", "UNKNOWN", "SUBMITTING"}:
        st.caption("模拟订单创建后，点击上方按钮载入支付、物流和收货反馈表单。其他输入不会自动重绘。")
    options = {"用户支付": "paid", "物流送达": "delivered", "用户取消": "cancelled", "用户退回": "returned",
               "退款申请受理": "refund_requested", "退款处理中": "refund_processing", "实际退款": "refunded"}
    if op and op["status"] in {"ACCEPTED", "UNKNOWN", "SUBMITTING"}:
        with st.form("order_event"):
            label = st.selectbox("模拟订单事件", list(options), key="order_event_type")
            if st.form_submit_button("提交订单事件"):
                submit(runtime, "event", event_type=options[label], payload={"operation_id": op["id"]})
        with st.form("receipt"):
            capacity = st.number_input("实际收到容量（GB）", min_value=1, value=op["snapshot"]["storage_gb"], key="received_storage")
            correct = st.checkbox("确认收到的型号、容量、成色与订单一致", key="receipt_correct")
            if st.form_submit_button("提交收货反馈"):
                submit(runtime, "event", event_type="receipt", payload={"operation_id": op["id"], "storage_gb": int(capacity), "correct": correct})
        with st.form("shipping"):
            delay = st.number_input("预计从现在起多少秒送达", min_value=1, value=3600, key="shipping_seconds")
            if st.form_submit_button("提交配送信息"):
                submit(runtime, "event", event_type="shipping", payload={"operation_id": op["id"], "expected_delivery_at": state["now"] + delay})
    with st.form("offer_event"):
        offer = st.selectbox("调整报价的商品", state["catalog"], format_func=lambda o: f'{o["model"]} / {o["storage_gb"]}GB', key="change_offer")
        price = st.number_input("调整后普通价格（元）", min_value=0.01, value=5799.0, key="offer_price")
        stock = st.number_input("调整后库存", min_value=0, value=5, key="offer_stock")
        if st.form_submit_button("更新模拟报价"):
            submit(runtime, "event", event_type="offer", payload={"offer_id": offer["offer_id"],
                   "price_cents": int(Decimal(str(price)) * 100), "stock": int(stock)})
    if st.button("模拟下一笔下单回执超时", key="timeout"):
        submit(runtime, "event", event_type="timeout_next_order", payload={})
    with st.form("source_failure"):
        catalog_failed = st.checkbox("商品查询失败", key="catalog_failed")
        order_failed = st.checkbox("订单查询失败", key="order_failed")
        if st.form_submit_button("更新模拟来源状态"):
            submit(runtime, "event", event_type="source_failure", payload={"catalog": catalog_failed, "order": order_failed})


@st.fragment(run_every=1)
def render_status(runtime):
    state = runtime.snapshot()
    if state.get("startup_error"):
        st.error(state["startup_error"])
        return
    task = state["task"]
    life = task["lifecycle"]
    st.write(f'任务：{task["task_id"]}　状态：{LIFECYCLE.get(life, life)}　阶段：{PHASE.get(task["phase"], task["phase"])}')
    if state["busy"]:
        st.caption("正在处理请求，暂停和结束操作仍可提交；生效前会再次检查当前授权。")
    if state["model_error"]:
        st.warning("模型尚未就绪，请在 config/.env 配置密钥和模型并重启服务。可先填写需求和测试模拟事件，聊天与购买确认需要模型。")
        with st.expander("连接错误"):
            st.code(state["model_error"])
    action = st.session_state.get("last_page_action")
    if action and action.done.is_set():
        if action.error:
            st.error("操作失败：" + action.error)
        else:
            st.success("操作已处理。")
    elif action:
        st.caption("操作已提交，等待处理。")
    if state["last_error"] and (not action or state["last_error"] != action.error):
        if state.get("last_error_source") == "timer":
            st.warning(state["last_error"])
            st.caption("下次后台检查：" + timestamp_label(task["next_check_at"]))
        else:
            st.error(state["last_error"])
    pause, resume, end = st.columns(3)
    if pause.button("暂停", key="pause", disabled=life != "ACTIVE"):
        submit(runtime, "pause")
    if resume.button("恢复", key="resume", disabled=life != "PAUSED"):
        submit(runtime, "resume")
    if end.button("结束任务", key="end", disabled=life not in {"ACTIVE", "PAUSED"}):
        submit(runtime, "end")


@st.fragment(run_every=1)
def render_messages(runtime):
    state = runtime.snapshot()
    published = {m.get("action_id") for m in state["messages"]}
    submissions = st.session_state.get("chat_submissions", [])
    for message in state["messages"]:
        with st.chat_message(message["role"]):
            st.write(message["content"])
    for action in submissions:
        if action.id not in published:
            with st.chat_message("user"):
                st.write(action.payload["text"])
                if action.done.is_set() and action.error:
                    st.error("消息未处理成功：" + action.error)
    if any(not action.done.is_set() for action in submissions):
        st.caption("正在等待 AI 回复…")
    # 后端接收后使用持久消息；完成的本地回显不再保留，避免重复。
    st.session_state["chat_submissions"] = [
        action for action in submissions if not action.done.is_set() or action.id not in published]
    render_confirmation(runtime, state, prefix="chat_")


@st.fragment
def render_chat(runtime):
    history = st.container()
    state = runtime.snapshot()
    if not state["task"]["requirements"].get("budget_cents"):
        st.caption("告诉我预算即可开始，用途和偏好可选；其他默认条件可在“需求设置”修改。")
    # 输入区不随定时检查重绘，也不因 busy 瞬时变化而重建、清空草稿。
    text = st.chat_input("例如：预算6000元，主要打游戏，其他按默认条件", key="chat",
                         disabled=not state["agent_available"])
    if text:
        action = submit(runtime, "chat", text=text)
        st.session_state.setdefault("chat_submissions", []).append(action)
    # 同一次提交即渲染用户消息；消息区仍单独刷新，不重绘聊天输入框。
    with history:
        render_messages(runtime)


@st.fragment(run_every=1)
def render_evidence(runtime):
    state = runtime.snapshot()
    for event in reversed(state["notifications"]):
        st.write(timestamp_label(event["at"]), event["payload"]["code"])
        st.json(event["payload"], expanded=False)
    with st.expander("查看事实与工具执行记录"):
        st.json(state["events"], expanded=False)
    st.download_button("下载任务证据", json.dumps({k: state[k] for k in ["task", "operations", "events", "external_order_count"]}, ensure_ascii=False, indent=2),
                       file_name="evidence.json", mime="application/json")


def render_task(runtime):
    if runtime.snapshot().get("startup_error"):
        st.error(runtime.snapshot()["startup_error"])
        return
    render_status(runtime)
    chat, requirements, purchase, simulation, evidence = st.tabs(["对话", "需求设置", "商品与订单", "模拟测试", "通知与记录"])
    with chat:
        render_chat(runtime)
    with requirements:
        render_requirements(runtime)
    with purchase:
        render_purchase(runtime)
    with simulation:
        render_simulation(runtime)
    with evidence:
        render_evidence(runtime)


def main():
    load_environment()
    st.set_page_config(page_title="iPhone 购买跟进", layout="wide")
    st.title("iPhone 购买跟进")
    st.caption("全部商品、订单、支付和物流均为模拟。消息、状态和通知自动更新；输入区保留草稿。服务运行期间按授权持续检查。")
    if "page_runtime" not in st.session_state:
        try:
            st.session_state["page_runtime"] = get_runtime(str(project_path(os.getenv("DEMO_DATA_DIR", "data/web"))),
                os.getenv("DEMO_USER_ID", "demo"), os.getenv("DEMO_TASK_ID", "t1"),
                str(project_path(os.getenv("DEMO_MOCK_FILE", "config/simulated_external.json"))))
        except Exception as error:
            st.error("任务初始化失败：" + str(error))
            return
    render_task(st.session_state["page_runtime"])
