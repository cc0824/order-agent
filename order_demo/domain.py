"""确定性业务边界。模型不能覆盖预算、确认凭证或完成标准。"""
import hashlib
import json
import math
import time
import uuid
from pathlib import Path

from .simulator import Simulator
from .storage import Store, encode


class DomainError(ValueError):
    pass


REQUIRED = ("use_case", "current_problem", "region", "budget_cents", "storage_gb", "condition",
            "desired_delivery_at", "followup_until", "check_interval_seconds", "notification_channel",
            "followup_authorized")
ALLOWED = set(REQUIRED) | {"model", "promotion_eligible", "preferences"}
TERMINAL = {"ENDED", "EXPIRED", "COMPLETED"}


class Demo:
    def __init__(self, directory, user_id="demo", task_id="t1", now=None):
        self.directory = Path(directory)
        self.now = now or time.time
        self.owner = encode([user_id, task_id])
        self.store = Store(directory)
        self.sim = Simulator(directory, self.now)
        self.before_action = lambda: None
        self.silent_notifications = False
        if not self.store.task(self.owner):
            self._save(dict(user_id=user_id, task_id=task_id, lifecycle="ACTIVE", phase="CLARIFYING",
                            requirements={}, constraints_version=0, next_check_at=0,
                            recommendation=None, outcome_reason=None))

    @property
    def task(self):
        return self.store.task(self.owner)

    def _save(self, task):
        self.store.save_task(self.owner, task)

    def record(self, kind, payload, dedupe=None):
        return self.store.event(self.owner, kind, payload, self.now(), dedupe)

    def _phase(self, phase):
        task = self.task
        if task["lifecycle"] == "ACTIVE":
            task["phase"] = phase
            self._save(task)

    def expire(self):
        task = self.task
        until = task["requirements"].get("followup_until")
        if until is not None and self.now() >= until and task["lifecycle"] in {"ACTIVE", "PAUSED"}:
            task.update(lifecycle="EXPIRED", outcome_reason="DEADLINE_REACHED")
            self._save(task)
            self.invalidate("EXPIRED")
            self.record("control", {"action": "expire"})

    def _active(self):
        self.before_action()
        self.expire()
        if self.task["lifecycle"] != "ACTIVE":
            raise DomainError("TASK_NOT_ACTIVE")

    def _notify(self, code, payload, allow_completed=False):
        self.before_action()
        self.expire()
        state = self.task["lifecycle"]
        if state != "ACTIVE" and not (allow_completed and state == "COMPLETED"):
            return
        fingerprint = hashlib.sha256(encode([code, payload]).encode()).hexdigest()
        self.record("observed_baseline" if self.silent_notifications else "notification",
                    {"code": code, **payload}, "notice:" + fingerprint)

    def notifications(self):
        return self.store.events(self.owner, "notification")

    def missing_requirements(self):
        req = self.task["requirements"]
        return [k for k in REQUIRED if not req.get(k)]

    def invalidate(self, reason):
        for op in self.store.operations(self.owner):
            if op["status"] == "PREPARED":
                op.update(valid=False, approved=False, invalid_reason=reason)
                self.store.save_operation(self.owner, op)

    def update_requirements(self, patch, source="user", expected_version=None):
        self._active()
        if source != "user":
            raise DomainError("USER_DECISION_REQUIRED")
        task = self.task
        if expected_version is not None and expected_version != task["constraints_version"]:
            raise DomainError("STALE_VERSION")
        if not patch or set(patch) - ALLOWED:
            raise DomainError("INVALID_REQUIREMENTS")
        req = {**task["requirements"], **patch}
        for key in ("budget_cents", "storage_gb", "check_interval_seconds"):
            if key in req and (type(req[key]) is not int or req[key] <= 0):
                raise DomainError("INVALID_" + key.upper())
        for key in ("desired_delivery_at", "followup_until"):
            if key in req and (type(req[key]) not in (int, float) or not math.isfinite(req[key])):
                raise DomainError("INVALID_DATE")
        for key in ("followup_authorized", "promotion_eligible"):
            if key in req and req[key] is not None and type(req[key]) is not bool:
                raise DomainError("INVALID_AUTHORIZATION")
        for key in ("use_case", "current_problem", "region", "condition"):
            if key in req and (not isinstance(req[key], str) or not req[key].strip()):
                raise DomainError("INVALID_REQUIREMENTS")
        if "notification_channel" in req and req["notification_channel"] != "terminal":
            raise DomainError("ONLY_TERMINAL_SUPPORTED")
        if req == task["requirements"]:
            return task
        task.update(requirements=req, constraints_version=task["constraints_version"] + 1,
                    recommendation=None, next_check_at=self.now())
        self._save(task)
        self.invalidate("CONSTRAINTS_CHANGED")
        self._phase("CLARIFYING" if self.missing_requirements() else "SEARCHING")
        self.record("requirements", {"patch": patch, "version": task["constraints_version"]})
        self.expire()
        return self.task

    def accept_search(self, result, silent=False):
        self._active()
        if result["constraints_version"] != self.task["constraints_version"]:
            self.record("stale_result", result)
            raise DomainError("STALE_VERSION")
        task = self.task
        task["recommendation"] = result
        self._save(task)
        self._phase("SEARCHING" if result["offers"] else "NEEDS_DECISION")
        if not silent:
            summary = [{k: o[k] for k in ("offer_id", "total_cents", "quote_version", "expected_delivery_at")}
                       for o in result["offers"]]
            self._notify("OFFERS" if summary else "NO_MATCH", {"offers": summary})
        return result

    def search(self, version=None, silent=False):
        self._active()
        task = self.task
        version = task["constraints_version"] if version is None else version
        if version != task["constraints_version"]:
            raise DomainError("STALE_VERSION")
        if self.missing_requirements():
            return {"status": "NEEDS_INPUT", "missing": self.missing_requirements(), "offers": [],
                    "constraints_version": version}
        try:
            offers = self.sim.search()
        except ConnectionError as error:
            self.record("source_failure", {"source": "catalog", "error": str(error)})
            return {"status": "UNVERIFIED", "offers": [], "constraints_version": version}
        req = task["requirements"]
        accepted, rejected = [], []
        for raw in offers:
            offer = self.sim.priced(raw, req)
            reasons = []
            for key in ("region", "condition", "model"):
                if req.get(key) and offer[key] != req[key]:
                    reasons.append(key)
            if offer["storage_gb"] < req["storage_gb"]:
                reasons.append("storage_gb")
            if offer["total_cents"] > req["budget_cents"]:
                reasons.append("budget_cents")
            if offer["stock"] <= 0:
                reasons.append("stock")
            if offer["expected_delivery_at"] > req["desired_delivery_at"]:
                reasons.append("desired_delivery_at")
            (rejected if reasons else accepted).append(
                {"offer_id": offer["offer_id"], "reasons": reasons} if reasons else offer)
        accepted.sort(key=lambda o: o["total_cents"])
        result = {"status": "OK" if accepted else "NO_MATCH", "offers": accepted,
                  "rejected": rejected, "constraints_version": version}
        self.record("search", result)
        return self.accept_search(result, silent=silent)

    def operation(self, op_id):
        for op in self.store.operations(self.owner):
            if op["id"] == op_id:
                return op
        raise DomainError("NOT_FOUND")

    def current_operation(self):
        ops = self.store.operations(self.owner)
        return ops[-1] if ops else None

    def prepare(self, offer_id, quote_version, constraints_version):
        self._active()
        if self.task.get("verification_required"):
            raise DomainError("FACTS_UNVERIFIED")
        if constraints_version != self.task["constraints_version"]:
            raise DomainError("STALE_VERSION")
        for existing in self.store.operations(self.owner):
            if existing["status"] in {"SUBMITTING", "UNKNOWN", "ACCEPTED"}:
                raise DomainError("EXISTING_ORDER_REQUIRES_USER_RESOLUTION")
            if existing["status"] == "PREPARED" and existing["valid"]:
                if self.now() >= existing["valid_until"]:
                    self.reject(existing["id"])
                    continue
                if existing["snapshot"]["offer_id"] == offer_id and existing["snapshot"]["quote_version"] == quote_version:
                    return existing
                raise DomainError("REJECT_PENDING_OPERATION_FIRST")
        result = self.search(silent=True)
        offer = next((o for o in result["offers"] if o["offer_id"] == offer_id), None)
        if not offer or offer["quote_version"] != quote_version:
            raise DomainError("OFFER_CHANGED_OR_NOT_SUITABLE")
        op = dict(id=uuid.uuid4().hex, idempotency_key=uuid.uuid4().hex, snapshot=offer,
                  constraints_version=constraints_version, status="PREPARED", approved=False, valid=True,
                  valid_until=self.now() + 300, verify_attempts=0, verify_until=None, order=None)
        self.store.save_operation(self.owner, op)
        self._phase("WAITING_CONFIRMATION")
        self.record("prepared", op)
        return op

    def _check_operation(self, op):
        self._active()
        if self.task.get("verification_required"):
            raise DomainError("FACTS_UNVERIFIED")
        if not op["valid"] or op["constraints_version"] != self.task["constraints_version"]:
            raise DomainError("STALE_VERSION")
        if self.now() >= op["valid_until"]:
            raise DomainError("CONFIRMATION_EXPIRED")
        current = self.sim.priced(self.sim.offer(op["snapshot"]["offer_id"]), self.task["requirements"])
        snapshot = op["snapshot"]
        if current["stock"] <= 0 or any(current[k] != snapshot[k] for k in
                ("quote_version", "total_cents", "seller", "storage_gb", "condition", "region", "expected_delivery_at")):
            raise DomainError("OFFER_CHANGED")
        if current["total_cents"] > self.task["requirements"]["budget_cents"]:
            raise DomainError("OVER_BUDGET")

    def approve(self, op_id):
        op = self.operation(op_id)
        if op["status"] != "PREPARED":
            raise DomainError("NOT_PENDING_APPROVAL")
        self._check_operation(op)
        op.update(approved=True, approved_at=self.now())
        self.store.save_operation(self.owner, op)
        self.record("user_approval", {"operation_id": op_id})
        return op

    def reject(self, op_id):
        op = self.operation(op_id)
        if op["status"] != "PREPARED":
            raise DomainError("ALREADY_SUBMITTED")
        op.update(status="REJECTED", valid=False, approved=False)
        self.store.save_operation(self.owner, op)
        self._phase("NEEDS_DECISION")
        self.record("user_rejection", {"operation_id": op_id})

    def place(self, op_id):
        op = self.operation(op_id)
        if op["status"] in {"ACCEPTED", "UNKNOWN", "SUBMITTING"}:
            return op  # 重放不再次执行外部写操作
        self._check_operation(op)
        if not op["approved"]:
            raise DomainError("CONFIRMATION_REQUIRED")
        op.update(status="SUBMITTING", submitted_at=self.now(), verify_until=self.now() + 1800)
        self.store.save_operation(self.owner, op)
        self.record("order_submit", {"operation_id": op_id, "idempotency_key": op["idempotency_key"]})
        try:
            order = self.sim.create(op)
        except TimeoutError:
            op["status"] = "UNKNOWN"
            self.store.save_operation(self.owner, op)
            self._phase("ORDER_UNKNOWN")
            self._notify("ORDER_UNKNOWN", {"operation_id": op_id})
            return op
        except ValueError as error:
            op.update(status="REJECTED", valid=False, approved=False)
            self.store.save_operation(self.owner, op)
            self._phase("NEEDS_DECISION")
            raise DomainError(str(error)) from error
        op.update(status="ACCEPTED", order=order)
        self.store.save_operation(self.owner, op)
        self._apply_order(op, order)
        return op

    def get_order_status(self, op_id):
        self.before_action()
        self.expire()
        op = self.operation(op_id)
        if op["status"] not in {"UNKNOWN", "SUBMITTING", "ACCEPTED"}:
            raise DomainError("ORDER_NOT_SUBMITTED")
        unknown = op["status"] in {"UNKNOWN", "SUBMITTING"}
        if not unknown and self.task["lifecycle"] != "ACTIVE":
            return op["order"]
        if unknown:
            if op["verify_attempts"] >= 3 or self.now() >= op["verify_until"]:
                return {"status": "UNKNOWN", "verification": "EXHAUSTED"}
            op["verify_attempts"] += 1
            self.store.save_operation(self.owner, op)
        self.record("order_query", {"operation_id": op_id})
        try:
            order = self.sim.query(op["idempotency_key"])
        except ConnectionError as error:
            self.record("source_failure", {"source": "order", "error": str(error)})
            return {"status": "UNVERIFIED"}
        if order is None:
            return {"status": "UNKNOWN"}
        if op.get("order") and order["version"] < op["order"]["version"]:
            self.record("stale_order_fact", order)
            return op["order"]
        op.update(status="ACCEPTED", order=order)
        self.store.save_operation(self.owner, op)
        self._apply_order(op, order)
        return order

    def _apply_order(self, op, order):
        self.record("order_fact", {"operation_id": op["id"], "order": order},
                    f'order:{op["id"]}:{order["version"]}')
        if self.task["lifecycle"] != "ACTIVE":
            return
        if self.task.get("verification_required"):
            self._phase("NEEDS_DECISION")
            return
        if self._complete_if_verified(op, order):
            return
        if order["refund"] == "REFUNDED":
            self._notify("REFUNDED", {"operation_id": op["id"]})
            task = self.task
            task.update(lifecycle="ENDED", outcome_reason="REFUND_EXIT")
            self._save(task)
            return
        if order["refund"] != "NONE" or order["delivery"] == "RETURNED" or order["payment"] == "CANCELLED":
            self._phase("NEEDS_DECISION")
        elif order["delivery"] == "DELIVERED":
            self._phase("VERIFYING_RECEIPT")
        elif order["payment"] == "UNPAID":
            self._phase("WAITING_PAYMENT")
        elif order["expected_delivery_at"] > self.task["requirements"]["desired_delivery_at"]:
            self._phase("NEEDS_DECISION")
            self._notify("DELIVERY_DELAY", {"operation_id": op["id"],
                                          "expected_delivery_at": order["expected_delivery_at"],
                                          "options": ["继续等待", "用户自行取消；新购需新确认"]})
        else:
            self._phase("TRACKING")
        self._notify("ORDER_STATUS", {"operation_id": op["id"], "payment": order["payment"],
                                      "delivery": order["delivery"], "refund": order["refund"]})

    def _complete_if_verified(self, op, order):
        task = self.task
        receipt = task.get("receipt_candidate")
        if (not receipt or receipt["operation_id"] != op["id"] or task["lifecycle"] != "ACTIVE"
                or task.get("verification_required")):
            return False
        correct = (receipt.get("correct") is True and type(receipt.get("storage_gb")) is int
                   and receipt["storage_gb"] == op["snapshot"]["storage_gb"])
        if (correct and order.get("snapshot") == op["snapshot"] and order.get("delivery") == "DELIVERED"
                and order.get("payment") == "PAID" and order.get("refund") == "NONE"):
            task.update(lifecycle="COMPLETED", receipt=receipt, outcome_reason="CORRECT_ITEM_RECEIVED")
            self._save(task)
            self._notify("COMPLETED", {"operation_id": op["id"], "event_id": receipt["event_id"]}, True)
            return True
        return False

    def event(self, kind, payload, event_id):
        self.before_action()
        self.expire()
        arrived_after_terminal = self.task["lifecycle"] in TERMINAL
        dedupe = "external:" + event_id
        if self.store.has_event(self.owner, dedupe):
            existing = next((e["payload"] for e in self.store.events(self.owner, "external_fact")
                             if e["payload"]["source_event_id"] == event_id), None)
            if existing is None or existing.get("type") != kind or existing.get("input") != payload:
                return self._mark_conflict(event_id, kind, payload)
            return {"status": "DUPLICATE"}
        if kind == "offer":
            result = self.sim.change_offer(payload)
            task = self.task
            task["recommendation"] = None
            self._save(task)
            for op in self.store.operations(self.owner):
                if op["status"] == "PREPARED" and op["snapshot"]["offer_id"] == payload["offer_id"]:
                    op.update(valid=False, approved=False, invalid_reason="OFFER_CHANGED")
                    self.store.save_operation(self.owner, op)
        elif kind == "receipt":
            op = self.operation(payload["operation_id"])
            if op["status"] not in {"ACCEPTED", "UNKNOWN", "SUBMITTING"}:
                raise DomainError("ORDER_NOT_SUBMITTED")
            task = self.task
            if task["lifecycle"] in {"ACTIVE", "PAUSED"}:
                task["receipt_candidate"] = {**payload, "event_id": event_id}
                self._save(task)
            if task["lifecycle"] == "ACTIVE":
                order = self.get_order_status(op["id"])
                if self.task["lifecycle"] != "COMPLETED" and not self._complete_if_verified(op, order):
                    self._phase("NEEDS_DECISION")
                    self._notify("RECEIPT_REQUIRES_VERIFICATION", {"operation_id": op["id"], "feedback": payload})
            result = payload
        elif kind == "source_failure":
            self.sim.fail_search = bool(payload.get("catalog", False))
            self.sim.fail_query = bool(payload.get("order", False))
            result = payload
        elif kind == "timeout_next_order":
            self.sim.timeout_after_create = True
            result = payload
        else:
            op = self.operation(payload["operation_id"])
            try:
                result = self.sim.user_event(op["idempotency_key"], kind, payload)
            except ValueError as error:
                if str(error) != "FACT_CONFLICT":
                    raise
                result = self._mark_conflict(event_id, kind, payload)
        self.record("external_fact", {"source_event_id": event_id, "type": kind,
                                     "input": payload, "data": result,
                                     "late": arrived_after_terminal}, dedupe)
        return result

    def _mark_conflict(self, event_id, kind, payload):
        task = self.task
        task["verification_required"] = True
        self._save(task)
        self._phase("NEEDS_DECISION")
        self.record("fact_conflict", {"event_id": event_id, "type": kind, "data": payload})
        self._notify("FACT_CONFLICT", {"event_id": event_id, "type": kind})
        return {"status": "UNVERIFIED", "error": "FACT_CONFLICT"}

    def control(self, action):
        self.before_action()
        self.expire()
        task = self.task
        state = task["lifecycle"]
        if action == "pause" and state == "ACTIVE":
            task["lifecycle"] = "PAUSED"
        elif action == "resume" and state == "PAUSED":
            task["lifecycle"] = "ACTIVE"
        elif action == "end" and state in {"ACTIVE", "PAUSED"}:
            task.update(lifecycle="ENDED", outcome_reason="USER_ABANDONED")
        else:
            raise DomainError("INVALID_CONTROL_TRANSITION")
        self._save(task)
        self.invalidate(action.upper())
        self.record("control", {"action": action})
        if action == "resume":
            # 先静默读取最新基线，不补发暂停期间通知。
            self.silent_notifications = True
            try:
                op = self.current_operation()
                if op and op["status"] in {"UNKNOWN", "SUBMITTING", "ACCEPTED"}:
                    self.get_order_status(op["id"])
                else:
                    self.search()
            finally:
                self.silent_notifications = False
        task = self.task
        task["next_check_at"] = self.now() + task["requirements"].get("check_interval_seconds", 10)
        self._save(task)
        return task

    def tick(self, dispatch=None):
        self.before_action()
        self.expire()
        task = self.task
        if self.now() < task["next_check_at"]:
            return False
        op = self.current_operation()
        if task["lifecycle"] != "ACTIVE":
            if op and op["status"] in {"UNKNOWN", "SUBMITTING"}:
                self.get_order_status(op["id"])
            else:
                return False
        elif self.missing_requirements():
            return False
        elif dispatch:
            dispatch()
        elif op and op["status"] in {"UNKNOWN", "SUBMITTING", "ACCEPTED"}:
            self.get_order_status(op["id"])
        elif not op or not op.get("valid") or op["status"] == "REJECTED":
            self.search()
        task = self.task
        task["next_check_at"] = self.now() + task["requirements"].get("check_interval_seconds", 10)
        self._save(task)
        self.record("tick", {"lifecycle": task["lifecycle"], "dispatch": "agent" if dispatch else "rules"})
        return True

    def evidence(self):
        return {"mode": "SIMULATION", "task": self.task, "operations": self.store.operations(self.owner),
                "events": self.store.events(self.owner), "external_order_count": self.sim.order_count}

    def close(self):
        self.store.close()
