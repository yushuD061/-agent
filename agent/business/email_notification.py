"""Privacy-minimized RFQ summaries and durable QQ outbox delivery."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from bus import OutboundMessage

from .email_repository import EmailRepository


def build_qq_rfq_summary(email_id: int, result: dict) -> str:
    customer = result.get("customer", {})
    company = customer.get("company", {}).get("value") or "待确认"
    country = result.get("country", {}).get("value") or "待确认"
    item_lines = []
    for index, item in enumerate(result.get("items", []), 1):
        product = item.get("product", {}).get("value") or "产品待确认"
        specification = item.get("specification", {}).get("value") or "规格待确认"
        quantity = item.get("quantity", {})
        amount = quantity.get("value")
        unit = quantity.get("unit") or "单位待确认"
        amount_text = str(amount) if amount is not None else "数量待确认"
        item_lines.append(f"{index}. {product}｜{specification}｜{amount_text} {unit}")
    deadline = result.get("delivery_deadline", {}).get("raw") or "待确认"
    trade = result.get("trade_term", {})
    trade_text = " ".join(filter(None, [trade.get("incoterm"), trade.get("named_place"), trade.get("version")])) or "待确认"
    missing = result.get("missing_fields", [])
    missing_text = "、".join(str(item) for item in missing[:10]) if missing else "无"
    items_text = "\n".join(item_lines) or "1. 产品信息待确认"
    return (f"【新邮件询盘待审核】\n内部邮件ID：{email_id}\n客户公司：{company}\n国家/地区：{country}\n"
            f"产品：\n{items_text}\n交期：{deadline}\n贸易条款：{trade_text}\n待确认字段：{missing_text}")


def build_qq_extraction_failure_notice(email_id: int, error_code: str) -> str:
    return (f"【新邮件询盘解析失败】\n内部邮件ID：{email_id}\n错误码：{error_code}\n"
            "邮件已安全入库，请人工检查或稍后重试。通知不包含邮件正文或联系人信息。")


class QQNotificationDispatcher:
    def __init__(self, repository: EmailRepository, sender: Callable[[OutboundMessage], Awaitable[None]]):
        self.repository = repository
        self.sender = sender

    async def dispatch_pending(self, limit: int = 20) -> dict:
        sent = failed = 0
        for row in self.repository.pending_notifications(limit):
            try:
                await self.sender(OutboundMessage(channel="qq", chat_id=row["target_id"],
                                                  target_type=row["target_type"], content=row["content"]))
                self.repository.mark_notification_sent(row["notification_id"])
                sent += 1
            except Exception as exc:
                self.repository.mark_notification_failed(row["notification_id"], type(exc).__name__)
                failed += 1
        return {"sent": sent, "failed": failed}
