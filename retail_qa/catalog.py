"""Approved metric definitions and template capabilities."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


METRICS = {
    "net_sales": {"label": "净销售额", "field": "net_sales_usd", "unit": "USD", "version": "v1", "definition": "已付款金额减已完成退款，按业务日期汇总"},
    "paid_orders": {"label": "已付款订单数", "field": "paid_orders", "unit": "单", "version": "v1", "definition": "业务日期内已付款订单数量"},
    "visits": {"label": "访问次数", "field": "visits", "unit": "次", "version": "v1", "definition": "业务日期内站点访问次数"},
}

CAPABILITIES = {
    "net_sales_period_compare": "net_sales",
    "paid_orders_period_compare": "paid_orders",
    "visits_period_compare": "visits",
}

SITES = {"US": "美国站", "JP": "日本站"}


def period_bounds(period: str, timezone: str, *, now: datetime | None = None) -> dict[str, str]:
    today = (now or datetime.now(ZoneInfo(timezone))).date()
    if period == "yesterday":
        current_start = current_end = today - timedelta(days=1)
        previous_start = previous_end = today - timedelta(days=2)
    elif period == "last_week":
        current_start = today - timedelta(days=today.weekday() + 7)
        current_end = current_start + timedelta(days=6)
        previous_start = current_start - timedelta(days=7)
        previous_end = current_start - timedelta(days=1)
    else:
        raise ValueError("目前模板只支持 yesterday 或 last_week")
    return {"current_start": current_start.isoformat(), "current_end": current_end.isoformat(),
            "previous_start": previous_start.isoformat(), "previous_end": previous_end.isoformat()}


def template_sql(capability: str) -> str:
    if capability not in CAPABILITIES:
        raise ValueError("未知查询能力")
    field = METRICS[CAPABILITIES[capability]]["field"]
    return (
        f"SELECT SUM(CASE WHEN business_date BETWEEN :current_start AND :current_end THEN {field} ELSE 0 END) AS current_value, "
        f"SUM(CASE WHEN business_date BETWEEN :previous_start AND :previous_end THEN {field} ELSE 0 END) AS previous_value "
        "FROM sales_daily WHERE site_id = :site_id AND business_date BETWEEN :previous_start AND :current_end"
    )
