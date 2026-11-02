"""订单查询工具：M1a 为 Mock，字段名固定，后续替换真实接口不动上层代码。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class OrderInfo(BaseModel):
    order_id: str
    ship_status: Literal["未发货", "已发货", "已签收"]
    logistics_status: Literal["等待揽件", "运输中", "派送中", "已签收", "停滞"]
    pay_status: Literal["未支付", "已支付", "已退款"]
    shipped_at: datetime | None = None
    carrier: str | None = None


# 本地测试用：可通过 CLI 参数覆盖
_MOCK = {
    "default": OrderInfo(
        order_id="MOCK-001",
        ship_status="未发货",
        logistics_status="等待揽件",
        pay_status="已支付",
    )
}


def get_order(order_id: str = "MOCK-001", **overrides) -> OrderInfo | None:
    base = _MOCK["default"].model_copy(update=overrides)
    return base.model_copy(update={"order_id": order_id})
