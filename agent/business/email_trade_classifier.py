"""Conservative local gate for foreign-trade RFQ/inquiry email.

The classifier is intentionally deterministic and precision-oriented. A mail
must contain explicit quotation/inquiry intent plus a commercial detail. It is
not a general-purpose business-email classifier and never calls an LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from channels.email.contracts import EmailEnvelope


@dataclass(frozen=True)
class TradeEmailClassification:
    accepted: bool
    code: str


_INTENT = re.compile(
    r"(?i)(?:\bRFQ\b|request\s+for\s+quot(?:e|ation)|quotation\s+request|"
    r"please\s+(?:send\s+us\s+|provide\s+|kindly\s+)?(?:a\s+)?quot(?:e|ation)|"
    r"(?:product|purchase|business|price)\s+(?:inquiry|enquiry)|"
    r"\b(?:inquiry|enquiry)\s+(?:for|about|regarding)\b|"
    r"询价|询盘|请.{0,6}报价|采购询价|"
    r"Angebotsanfrage|Bitte\s+um\s+(?:ein\s+)?Angebot)"
)
_COMMERCIAL_DETAIL = re.compile(
    r"(?i)(?:\b(?:\d{1,3}(?:[ ,]\d{3})+|\d+(?:\.\d+)?)\s*(?:pcs|pieces|sets|units|cartons|pallets|"
    r"kg|tons?|tonnes)\b|\b(?:EXW|FCA|CPT|CIP|DAP|DPU|DDP|FAS|FOB|CFR|CIF)\b|"
    r"\b(?:unit\s+price|target\s+price|lead\s+time|delivery\s+(?:date|time|to)|"
    r"specification|datasheet|catalog(?:ue)?)\b|数量|交期|规格|单价|目的港)"
)
_OBVIOUS_NON_RFQ = re.compile(
    r"(?i)(?:verification\s+code|security\s+(?:alert|notice)|password\s+reset|"
    r"login\s+alert|newsletter|unsubscribe|验证码|安全通知|密码重置|登录提醒|退订)"
)


def classify_trade_rfq_email(envelope: EmailEnvelope, *, mailbox_address: str = "",
                             allowed_senders: tuple[str, ...] = ()) -> TradeEmailClassification:
    sender = envelope.from_address.strip().lower()
    allowed = {value.strip().lower() for value in allowed_senders if value.strip()}
    if mailbox_address and sender == mailbox_address.strip().lower():
        return TradeEmailClassification(False, "email_trade_self_sent")
    if allowed and sender not in allowed:
        return TradeEmailClassification(False, "email_trade_sender_not_allowed")
    source = f"{envelope.subject}\n{envelope.text_body[:20000]}"
    if _OBVIOUS_NON_RFQ.search(source):
        return TradeEmailClassification(False, "email_trade_obvious_non_rfq")
    if not _INTENT.search(source):
        return TradeEmailClassification(False, "email_trade_intent_missing")
    if not _COMMERCIAL_DETAIL.search(source):
        return TradeEmailClassification(False, "email_trade_detail_missing")
    return TradeEmailClassification(True, "email_trade_rfq_accepted")
