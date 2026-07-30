"""Audited AI adapter for trade stages; local and compatible APIs share it."""
from __future__ import annotations
import json, time
from typing import Any
from agent.business.trade_workbench_repository import TradeWorkbenchError, content_hash


class AuditedTradeAI:
    def __init__(self, provider, repository, *, provider_type: str,
                 model: str, actor_id: str = "local_operator"):
        self.provider, self.repository = provider, repository
        self.provider_type, self.model, self.actor_id = provider_type, model, actor_id

    async def generate_json(self, campaign_id: str, stage: str, *,
                            prompt_version: str, system_prompt: str,
                            input_payload: dict[str, Any]) -> dict[str, Any]:
        digest, started = content_hash(input_payload), time.monotonic()
        output_digest = None; status = "failed"
        try:
            response = await self.provider.chat([
                {"role": "system", "content": system_prompt + "\nReturn one JSON object only. Do not include hidden reasoning."},
                {"role": "user", "content": json.dumps(input_payload, ensure_ascii=False, sort_keys=True)},
            ], model=self.model)
            if response.finish_reason == "error" or not response.content:
                raise TradeWorkbenchError("trade_ai_provider_failed", 502)
            text = response.content.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            result = json.loads(text)
            if not isinstance(result, dict):
                raise ValueError("not an object")
            output_digest, status = content_hash(result), "completed"
            return result
        except (json.JSONDecodeError, ValueError) as exc:
            raise TradeWorkbenchError("trade_ai_invalid_json", 502) from exc
        finally:
            self.repository.audit_ai(campaign_id, stage, provider_type=self.provider_type,
                model=self.model, prompt_version=prompt_version, input_digest=digest,
                output_digest=output_digest, duration_ms=int((time.monotonic()-started)*1000),
                status=status, actor_id=self.actor_id)
