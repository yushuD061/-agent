"""Read-only internal analysis tools for the workspace peer agent."""

from __future__ import annotations

import json
from typing import Any

from agent.tools.base import Tool
from trade_rag.contracts import Actor, QueryRequest
from trade_rag.knowledge_repository import KnowledgeRepository
from trade_rag.pipeline import RagPipeline


class WorkspaceKnowledgeAnalysisTool(Tool):
    def __init__(self, repository: KnowledgeRepository | None = None) -> None:
        self.repository = repository or KnowledgeRepository()

    @property
    def name(self) -> str:
        return "analyze_workspace_knowledge"

    @property
    def description(self) -> str:
        return "Read approved workspace knowledge for internal analysis. Internal content must never be copied into public_answer."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 500}},
            "required": ["query"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        query = str(kwargs.get("query", "")).strip()[:500]
        if not query:
            return json.dumps({"status": "NO_EVIDENCE", "items": []})
        try:
            pipeline = RagPipeline()
            for document in self.repository.load_published():
                pipeline.index(document)
            result = pipeline.query(QueryRequest(
                query=query,
                actor=Actor(
                    actor_id="workspace-peer",
                    roles=frozenset({"workspace_agent", "sales"}),
                ),
                top_k=5,
            ))
        except Exception:
            return json.dumps({"status": "TEMPORARILY_UNAVAILABLE", "analysis_material": "", "citations": []})
        source_classes: dict[str, str] = {}
        for document in self.repository.load_published():
            source_classes[document.document_id] = document.classification
        citations = [
            {
                "document_id": item.get("document_id"),
                "version": item.get("version"),
                "classification": source_classes.get(str(item.get("document_id")), "internal"),
            }
            for item in result.get("citations", []) if isinstance(item, dict)
        ]
        return json.dumps({
            "status": result.get("status", "NO_EVIDENCE"),
            "analysis_material": result.get("answer", ""),
            "citations": citations,
        }, ensure_ascii=False)


class WorkspaceProductAnalysisTool(Tool):
    @property
    def name(self) -> str:
        return "analyze_workspace_product"

    @property
    def description(self) -> str:
        return "Read product, exact inventory and internal base price for internal analysis only. Public output may expose only public catalog fields and quantity-specific availability."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "maxLength": 120},
                "sku": {"type": "string", "maxLength": 60},
                "category": {"type": "string", "maxLength": 60},
                "quantity": {"type": "integer", "minimum": 1, "maximum": 100000000},
            },
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        from agent.business.database import get_product_by_sku, list_products

        keyword = str(kwargs.get("keyword", "")).strip()[:120]
        sku = str(kwargs.get("sku", "")).strip()[:60]
        category = str(kwargs.get("category", "")).strip()[:60]
        quantity = kwargs.get("quantity")
        try:
            products = ([get_product_by_sku(sku)] if sku else
                        list_products(category=category, keyword=keyword, limit=5))
            rows = []
            for product in products:
                if product is None or not product.active:
                    continue
                row = {
                    "sku": product.sku,
                    "name_en": product.name_en,
                    "name_cn": product.name_cn,
                    "category": product.category,
                    "specification": product.specification,
                    "unit": product.unit,
                    "moq": product.moq,
                    "lead_time_days": product.lead_time_days,
                    "internal_unit_price_usd": product.price_usd,
                    "internal_exact_inventory": product.inventory,
                }
                if quantity is not None:
                    row["requested_quantity"] = int(quantity)
                    row["requested_quantity_available"] = (
                        int(quantity) >= int(product.moq) and int(quantity) <= int(product.inventory)
                    )
                rows.append(row)
            return json.dumps({"results": rows, "total": len(rows)}, ensure_ascii=False)
        except Exception:
            return json.dumps({"results": [], "total": 0, "status": "temporarily_unavailable"})
