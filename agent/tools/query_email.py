"""Read-only access to locally stored inbound email metadata and RFQ extraction."""

from __future__ import annotations

import json
from typing import Any

from agent.business.email_repository import EmailRepository
from agent.business.email_review_service import EmailReviewService, EmailReviewServiceError
from agent.tools.base import Tool


def _mask_sender(value: str) -> str:
    local, separator, domain = value.partition("@")
    return f"{local[:1]}***@{domain}" if separator and domain else "***"


class QueryInboundEmailTool(Tool):
    """Expose mailbox records without passing raw message bodies to the Agent model."""

    def __init__(self, repository: EmailRepository | None = None) -> None:
        self.service = EmailReviewService(repository or EmailRepository())

    @property
    def name(self) -> str:
        return "query_inbound_email"

    @property
    def description(self) -> str:
        return (
            "查询本机工作区的收件记录和本地已完成的 RFQ 结构化解析。"
            "可列出全部来信或按内部 email_id 读取解析结果；不会返回原始正文，也不能发送邮件。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string", "enum": ["list", "get"], "default": "list",
                    "description": "list 列出邮件；get 读取指定邮件的本地解析结果",
                },
                "email_id": {
                    "type": "integer", "minimum": 1,
                    "description": "action=get 时必填的内部邮件 ID",
                },
                "status": {
                    "type": "string",
                    "enum": ["all", "received", "ignored_non_trade", "needs_review", "confirmed", "failed"],
                    "default": "all",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        action = str(kwargs.get("action", "list"))
        try:
            if action == "list":
                status = str(kwargs.get("status", "all"))
                limit = max(1, min(int(kwargs.get("limit", 20)), 50))
                return json.dumps(
                    self.service.list_reviews(status=status, limit=limit), ensure_ascii=False
                )
            if action != "get":
                return json.dumps({"error_code": "email_query_invalid_action"}, ensure_ascii=False)
            email_id = int(kwargs.get("email_id", 0))
            if email_id <= 0:
                return json.dumps({"error_code": "email_query_email_id_required"}, ensure_ascii=False)
            detail, _ = self.service.detail(email_id)
            result = {
                "email_id": detail["email_id"],
                "provider": detail["provider"],
                "status": detail["status"],
                "subject": detail["subject"],
                "sender_masked": _mask_sender(detail["from_address"]),
                "received_at": detail["created_at"],
                "classification_code": detail.get("classification_code"),
                "extraction_mode": detail.get("extraction_mode"),
                "extractor_version": detail.get("extractor_version"),
                "extraction": detail.get("extraction") or {},
                "body_in_agent_response": False,
                "can_send_email": False,
            }
            # Preserve privacy while still letting NanoClaw explain local RFQ parsing results.
            customer = result["extraction"].get("customer")
            if isinstance(customer, dict) and isinstance(customer.get("email"), dict):
                customer["email"] = {**customer["email"], "value": "[masked]"}
            return json.dumps(result, ensure_ascii=False)
        except (EmailReviewServiceError, TypeError, ValueError) as exc:
            return json.dumps(
                {"error_code": getattr(exc, "code", "email_query_invalid_request")},
                ensure_ascii=False,
            )
