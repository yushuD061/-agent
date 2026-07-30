"""Allowlisted, read-only knowledge and product access for the public customer agent."""

from __future__ import annotations

import json
from typing import Any

from agent.customer_security import CustomerDataGuard
from agent.tools.base import Tool
from trade_rag.contracts import Actor, QueryRequest
from trade_rag.knowledge_repository import KnowledgeRepository
from trade_rag.pipeline import RagPipeline


class CustomerPublicKnowledgeTool(Tool):
    def __init__(self, repository: KnowledgeRepository | None = None,
                 public_memory_store=None) -> None:
        self.repository = repository or KnowledgeRepository()
        self.public_memory_store = public_memory_store

    @property
    def name(self) -> str:
        return "search_public_knowledge"

    @property
    def description(self) -> str:
        return "Search only knowledge documents explicitly classified as public. Use only to answer the customer's current question."

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
            return json.dumps({"status": "NO_EVIDENCE", "answer": ""})
        try:
            pipeline = RagPipeline()
            for document in self.repository.load_published():
                if document.classification == "public":
                    pipeline.index(document)
            list_pdf_candidates = getattr(self.repository, "list_pdf_index_candidates", None)
            if callable(list_pdf_candidates):
                for document_id in list_pdf_candidates():
                    detail = self.repository.get_document(document_id)
                    if detail.get("classification") != "public":
                        continue
                    document, parents, children = self.repository.load_pdf_chunks(document_id)
                    pipeline.index_prepared(document, parents, children)
            result = pipeline.query(QueryRequest(
                query=query,
                actor=Actor(actor_id="customer-agent", roles=frozenset({"customer"})),
                top_k=3,
            ))
        except Exception:
            return json.dumps({"status": "TEMPORARILY_UNAVAILABLE", "answer": "", "citations": []})
        answer = str(result.get("answer", ""))
        compatible_memory = (
            self.public_memory_store.search(query, top_k=3)
            if self.public_memory_store is not None else []
        )
        if compatible_memory:
            if result.get("status") == "ANSWERED":
                answer = "\n".join([answer, *compatible_memory]).strip()
            else:
                answer = "\n".join(compatible_memory)
        if CustomerDataGuard.inspect_request(answer) or CustomerDataGuard.sanitize_response(answer) != answer:
            return json.dumps({"status": "NO_EVIDENCE", "answer": ""})
        citations = [
            {"document_id": item.get("document_id"), "version": item.get("version")}
            for item in result.get("citations", []) if isinstance(item, dict)
        ]
        return json.dumps({
            "status": "ANSWERED" if compatible_memory else result.get("status", "NO_EVIDENCE"),
            "answer": answer,
            "citations": citations,
            "public_memory_ids": [],
        }, ensure_ascii=False)


class CustomerPublicCatalogTool(Tool):
    @property
    def name(self) -> str:
        return "search_public_product_catalog"

    @property
    def description(self) -> str:
        return "Read allowlisted public product fields. Never returns raw SQL, exact stock, internal prices, costs, customers, quotes, or operational records."

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
        from agent.business.database import check_inventory, get_product_by_sku, list_products

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
                row: dict[str, Any] = {
                    "sku": product.sku,
                    "name": product.name_en or product.name_cn,
                    "category": product.category,
                    "specification": product.specification,
                    "unit": product.unit,
                    "moq": product.moq,
                    "lead_time_days_estimate": product.lead_time_days,
                    "price_status": "sales_confirmation_required",
                }
                if quantity is not None:
                    availability = check_inventory(product.sku, int(quantity))
                    row["requested_quantity_available"] = bool(availability.get("available"))
                rows.append(row)
            payload = json.dumps({"results": rows, "total": len(rows)}, ensure_ascii=False)
            if (CustomerDataGuard.inspect_request(payload) or
                    CustomerDataGuard.sanitize_response(payload) != payload):
                return json.dumps({"results": [], "total": 0, "status": "temporarily_unavailable"})
            return payload
        except Exception:
            return json.dumps({"results": [], "total": 0, "status": "temporarily_unavailable"})
