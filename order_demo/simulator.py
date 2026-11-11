"""全部外部事实均为模拟。独立 JSON 用来重现已受理但响应丢失。"""
import copy
import json
from pathlib import Path


class Simulator:
    def __init__(self, directory, now, catalog=None):
        self.path = Path(directory) / "simulated_external.json"
        self.timeout_after_create = False
        self.fail_search = False
        self.fail_query = False
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            source = Path(catalog) if catalog else Path(__file__).resolve().parents[1] / "fixtures/catalog.json"
            offers = json.loads(source.read_text(encoding="utf-8"))
            for offer in offers:
                offer["expected_delivery_at"] = now() + offer.pop("delivery_seconds")
            self.data = {"offers": offers, "orders": {}, "create_calls": 0}
            self.save()

    def save(self):
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
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
