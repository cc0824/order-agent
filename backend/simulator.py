"""全部外部事实均为模拟。独立 JSON 用来重现已受理但响应丢失。"""
import copy
import json
from pathlib import Path

from .config import CATALOG_FILE


class Simulator:
    def __init__(self, directory, now, catalog=None, mock_file=None):
        self.path = Path(mock_file) if mock_file is not None else Path(directory) / "simulated_external.json"
        self.timeout_after_create = False
        self.fail_search = False
        self.fail_query = False
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            source = Path(catalog) if catalog else CATALOG_FILE
            offers = json.loads(source.read_text(encoding="utf-8"))
            self.data = {"offers": offers, "orders": {}, "create_calls": 0}
            self.save()
        # 可共享的商品用相对配送时长，进程内固定成一个报价时间。
        # 已有订单中的绝对时间与购买快照不参与换算。
        started_at = now()
        for offer in self.data["offers"]:
            if "delivery_seconds" in offer:
                offer["expected_delivery_at"] = started_at + offer["delivery_seconds"]

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        persisted = copy.deepcopy(self.data)
        for offer in persisted["offers"]:
            if "delivery_seconds" in offer:
                offer.pop("expected_delivery_at", None)
        temp.write_text(json.dumps(persisted, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    @property
    def order_count(self):
        return len(self.data["orders"])

    @property
    def create_calls(self):
        return self.data["create_calls"]

    def offer(self, offer_id):
        for offer in self.data["offers"]:
            if offer["offer_id"] == offer_id:
                return copy.deepcopy(offer)
        raise ValueError("OFFER_NOT_FOUND")

    def search(self):
        if self.fail_search:
            raise ConnectionError("模拟商品源不可用")
        return copy.deepcopy(self.data["offers"])

    @staticmethod
    def priced(offer, requirements):
        result = copy.deepcopy(offer)
        price = offer["price_cents"]
        if requirements.get("promotion_eligible") is True and offer.get("conditional_cents") is not None:
            price = offer["conditional_cents"]
        result["total_cents"] = price + offer["shipping_cents"] + offer["fee_cents"]
        return result

    def change_offer(self, payload):
        allowed = {"price_cents", "shipping_cents", "fee_cents", "stock", "expected_delivery_at"}
        changes = {k: v for k, v in payload.items() if k != "offer_id"}
        if not changes or set(changes) - allowed:
            raise ValueError("INVALID_OFFER_CHANGE")
        for k, v in changes.items():
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                raise ValueError("INVALID_OFFER_CHANGE")
            if k != "expected_delivery_at" and not isinstance(v, int):
                raise ValueError("INVALID_OFFER_CHANGE")
        for offer in self.data["offers"]:
            if offer["offer_id"] == payload["offer_id"]:
                if "expected_delivery_at" in changes:
                    offer.pop("delivery_seconds", None)
                offer.update(changes)
                offer["quote_version"] += 1
                self.save()
                return copy.deepcopy(offer)
        raise ValueError("OFFER_NOT_FOUND")

    def create(self, operation):
        key = operation["idempotency_key"]
        if key in self.data["orders"]:
            return copy.deepcopy(self.data["orders"][key])
        self.data["create_calls"] += 1
        snapshot = operation["snapshot"]
        current = self.offer(snapshot["offer_id"])
        if current["quote_version"] != snapshot["quote_version"] or current["stock"] <= 0:
            self.save()
            raise ValueError("OFFER_CHANGED")
        order = {"order_id": "SIM-" + key[:12], "snapshot": copy.deepcopy(snapshot),
                 "payment": "UNPAID", "delivery": "NOT_SHIPPED", "refund": "NONE",
                 "expected_delivery_at": snapshot["expected_delivery_at"], "version": 1}
        self.data["orders"][key] = order
        for offer in self.data["offers"]:
            if offer["offer_id"] == snapshot["offer_id"]:
                offer["stock"] -= 1
        self.save()
        if self.timeout_after_create:
            self.timeout_after_create = False
            raise TimeoutError("模拟订单已创建，但回执丢失")
        return copy.deepcopy(order)

    def query(self, key):
        if self.fail_query:
            raise ConnectionError("模拟订单查询失败")
        return copy.deepcopy(self.data["orders"].get(key))

    def user_event(self, key, kind, payload):
        order = self.data["orders"].get(key)
        if not order:
            raise ValueError("ORDER_NOT_FOUND")
        if ((kind == "paid" and order["payment"] == "CANCELLED")
                or (kind in {"paid", "refund_requested", "refund_processing"} and order["refund"] == "REFUNDED")
                or (kind in {"shipping", "delivered"} and order["delivery"] == "RETURNED")
                or (kind == "shipping" and order["delivery"] == "DELIVERED")):
            raise ValueError("FACT_CONFLICT")
        updates = {"paid": {"payment": "PAID"}, "cancelled": {"payment": "CANCELLED"},
                   "delivered": {"delivery": "DELIVERED"}, "returned": {"delivery": "RETURNED"},
                   "refund_requested": {"refund": "REQUEST_ACCEPTED"},
                   "refund_processing": {"refund": "PROCESSING"}, "refunded": {"refund": "REFUNDED"}}
        if kind == "shipping":
            order["delivery"] = "SHIPPING"
            order["expected_delivery_at"] = payload["expected_delivery_at"]
        elif kind in updates:
            order.update(updates[kind])
        else:
            raise ValueError("UNSUPPORTED_EVENT")
        order["version"] += 1
        self.save()
        return copy.deepcopy(order)
