"""Unified task orchestration for the RFQ -> quote -> follow-up vertical slice."""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
import re
from typing import Any, Awaitable, Callable, Mapping

from agent.business.task_runtime_repository import (
    ClaimedStep,
    TaskEvent,
    TaskOwner,
    TaskRuntimeError,
    TaskRuntimeRepository,
)
from agent.tools.calculate_quote import calculate_quote_impl
from agent.tools.check_inventory import check_inventory_impl
from agent.tools.search_product import search_product_catalog_impl


RFQ_QUOTE_FOLLOWUP_PLAN: list[dict[str, Any]] = [
    {"step_key": "inquiry_structuring", "label_key": "task.step.inquiry_structuring",
     "executor": "inquiry_structuring", "risk_level": "safe", "audience": "customer", "dependencies": []},
    {"step_key": "missing_information", "label_key": "task.step.missing_information",
     "executor": "missing_information", "risk_level": "safe", "audience": "customer",
     "dependencies": ["inquiry_structuring"]},
    {"step_key": "product_matching", "label_key": "task.step.product_matching",
     "executor": "product_matching", "risk_level": "safe", "audience": "workspace",
     "dependencies": ["missing_information"]},
    {"step_key": "inventory_check", "label_key": "task.step.inventory_check",
     "executor": "inventory_check", "risk_level": "safe", "audience": "workspace",
     "dependencies": ["product_matching"]},
    {"step_key": "commercial_terms", "label_key": "task.step.commercial_terms",
     "executor": "commercial_terms", "risk_level": "safe", "audience": "customer",
     "dependencies": ["inventory_check"]},
    {"step_key": "quote_calculating", "label_key": "task.step.quote_calculating",
     "executor": "quote_calculating", "risk_level": "safe", "audience": "workspace",
     "dependencies": ["commercial_terms"]},
    {"step_key": "quote_drafting", "label_key": "task.step.quote_drafting",
     "executor": "quote_drafting", "risk_level": "safe", "audience": "workspace",
     "dependencies": ["quote_calculating"]},
    {"step_key": "internal_review", "label_key": "task.step.internal_review",
     "executor": "internal_review", "risk_level": "human_gate", "audience": "workspace",
     "dependencies": ["quote_drafting"]},
    {"step_key": "customer_confirmation", "label_key": "task.step.customer_confirmation",
     "executor": "customer_confirmation", "risk_level": "human_gate", "audience": "customer",
     "dependencies": ["internal_review"]},
    {"step_key": "follow_up", "label_key": "task.step.follow_up",
     "executor": "follow_up", "risk_level": "safe", "audience": "workspace",
     "dependencies": ["customer_confirmation"]},
]

STEP_INDEX = {item["step_key"]: index for index, item in enumerate(RFQ_QUOTE_FOLLOWUP_PLAN)}
FIELD_IMPACT_START = {
    "product_description": "product_matching",
    "sku": "product_matching",
    "quantity": "inventory_check",
    "destination": "commercial_terms",
    "incoterm": "commercial_terms",
    "payment_terms": "commercial_terms",
    "currency": "quote_calculating",
    "freight_cost_usd": "quote_calculating",
    "buyer": "quote_drafting",
    "customer_company": "quote_drafting",
    "contact": "follow_up",
}

TRADE_INTENT = re.compile(
    r"(询盘|询价|报价|外贸|quote|quotation|rfq|angebot|anfrage|价格|price|库存|inventory)", re.I)
SKU_RE = re.compile(r"\b[A-Z]{2,}[A-Z0-9-]*-\d+[A-Z0-9-]*\b", re.I)
QUANTITY_RE = re.compile(
    r"(?:数量|qty|quantity|menge)\s*(?:改为|为|is|to|[:=])?\s*([0-9][0-9,]*)", re.I)
LOOSE_QUANTITY_RE = re.compile(
    r"\b([0-9][0-9,]*)\s*(?:pcs|pieces|units|sets|个|件|套|条|台|箱)\b", re.I)
DESTINATION_RE = re.compile(
    r"(?:目的地|交货地|目的港|destination(?:\s+port)?|deliver(?:y)?\s+to|"
    r"ship(?:ping)?\s+to|zielort)\s*(?:改为|为|is|to|[:=])?\s*([^\r\n,;，；]{2,80})",
    re.I,
)
PRODUCT_RE = re.compile(
    r"(?:产品(?:或\s*SKU)?|product(?:_or_sku|\s*(?:or|/)\s*sku)?|artikel)"
    r"\s*(?:改为|为|is|to|[:=])?\s*([^\r\n,;，；]{2,120})", re.I)
PURCHASE_PRODUCT_RE = re.compile(
    r"(?:计划\s*)?(?:采购|求购|需要|purchase|buy|need)\s*"
    r"(?:[0-9][0-9,]*\s*(?:pcs|pieces|units|sets|个|件|套|条|台|箱)\s*)?"
    r"([^\r\n,;，；。]{2,120})", re.I)
INCOTERM_RE = re.compile(r"\b(EXW|FOB|CFR|CIF|DAP|DPU|DDP|FCA|CPT|CIP)\b", re.I)
INCOTERM_DESTINATION_RE = re.compile(
    r"\b(?:CFR|CIF|DAP|DPU|DDP|CPT|CIP)\s+([A-Za-z][A-Za-z .'-]{1,60})"
    r"(?=[\r\n,;，；。]|$)", re.I)
CURRENCY_RE = re.compile(r"\b(USD|EUR|CNY|GBP|JPY)\b", re.I)
ALLOWED_CHANGES = frozenset({
    "product_description", "sku", "quantity", "destination", "incoterm",
    "payment_terms", "currency", "freight_cost_usd", "buyer",
    "customer_company", "contact",
})


Listener = Callable[[TaskEvent], Awaitable[None] | None]


def extract_instruction_changes(content: str) -> dict[str, Any]:
    """Extract deterministic hints only; unknown business facts stay absent."""
    text = str(content).strip()
    changes: dict[str, Any] = {"latest_instruction": text}
    if match := SKU_RE.search(text):
        changes["sku"] = match.group(0).upper()
    if match := QUANTITY_RE.search(text) or LOOSE_QUANTITY_RE.search(text):
        changes["quantity"] = int(match.group(1).replace(",", ""))
    if match := DESTINATION_RE.search(text) or INCOTERM_DESTINATION_RE.search(text):
        changes["destination"] = match.group(1).strip(" .")
    if match := PRODUCT_RE.search(text) or PURCHASE_PRODUCT_RE.search(text):
        changes["product_description"] = match.group(1).strip(" .")
    if match := INCOTERM_RE.search(text):
        changes["incoterm"] = match.group(1).upper()
    if match := CURRENCY_RE.search(text):
        changes["currency"] = match.group(1).upper()
    payment = re.search(
        r"(?:付款(?:条款)?|payment(?:[\s_-]+terms)?|zahlung)\s*"
        r"(?:改为|为|is|to|[:=])\s*([^\r\n]{2,160})",
        text, re.I)
    if payment:
        changes["payment_terms"] = payment.group(1).strip()
    buyer = re.search(
        r"(?:买方|客户|buyer|customer|kunde|company)\s*(?:公司|company)?\s*[:=]\s*"
        r"([^\r\n,;，；]{2,120})", text, re.I)
    if buyer:
        changes["buyer"] = buyer.group(1).strip()
    contact = re.search(
        r"(?:联系人|contact|email|e-mail)\s*[:=]\s*([^\r\n,;，；]{2,160})", text, re.I)
    if contact:
        changes["contact"] = contact.group(1).strip()
    return changes


def normalize_changes(changes: Mapping[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in dict(changes or {}).items():
        if key not in ALLOWED_CHANGES:
            raise TaskRuntimeError("task_change_field_invalid", 422)
        if key == "quantity":
            try:
                normalized = int(value)
            except (TypeError, ValueError):
                raise TaskRuntimeError("task_change_value_invalid", 422) from None
            if normalized <= 0:
                raise TaskRuntimeError("task_change_value_invalid", 422)
            result[key] = normalized
        elif key == "freight_cost_usd":
            try:
                normalized = float(value)
            except (TypeError, ValueError):
                raise TaskRuntimeError("task_change_value_invalid", 422) from None
            if normalized < 0:
                raise TaskRuntimeError("task_change_value_invalid", 422)
            result[key] = normalized
        else:
            normalized = " ".join(str(value).split())
            if not normalized or len(normalized) > 500:
                raise TaskRuntimeError("task_change_value_invalid", 422)
            result[key] = normalized.upper() if key in {"sku", "incoterm", "currency"} else normalized
    return result


def impacted_steps(changes: Mapping[str, Any], current_step: str | None = None) -> list[str]:
    starts = [STEP_INDEX[FIELD_IMPACT_START[key]] for key in changes if key in FIELD_IMPACT_START]
    if not starts:
        starts = [STEP_INDEX.get(current_step or "inquiry_structuring", 0)]
    start = min(starts)
    return [item["step_key"] for item in RFQ_QUOTE_FOLLOWUP_PLAN[start:]]


class TaskRuntimeService:
    def __init__(self, repository: TaskRuntimeRepository, *,
                 artifact_root: str | Path = "outputs/task-runtime",
                 worker_id: str = "task-runtime-local-worker"):
        self.repository = repository
        self.artifact_root = Path(artifact_root).resolve()
        self.worker_id = worker_id
        self._listeners: list[Listener] = []
        self._run_lock = asyncio.Lock()

    def subscribe(self, listener: Listener) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    async def _publish_since(self, task_id: str, sequence: int) -> None:
        for event in self.repository.events(task_id, sequence):
            for listener in list(self._listeners):
                try:
                    value = listener(event)
                    if inspect.isawaitable(value):
                        await value
                except Exception:
                    continue

    @staticmethod
    def _scope(owner: TaskOwner, operation: str, task_id: str | None = None) -> str:
        suffix = f":{task_id}" if task_id else ""
        return f"task-runtime:{owner.tenant_id}:{owner.actor_type}:{owner.actor_id}:{operation}{suffix}"

    async def _mutate(self, task_id: str | None, action: Callable[[], dict[str, Any]], *,
                      owner: TaskOwner, operation: str,
                      idempotency_key: str | None = None,
                      idempotency_payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        before = self.repository.get_task(task_id)["last_sequence"] if task_id else 0
        if idempotency_key:
            result = self.repository.idempotent(
                self._scope(owner, operation, task_id), idempotency_key,
                dict(idempotency_payload or {}), action)
        else:
            result = action()
        resolved_id = task_id or result["task_id"]
        await self._publish_since(resolved_id, before)
        return result

    async def create_task(self, owner: TaskOwner, conversation_id: str, content: str,
                          *, title: str | None = None, changes: Mapping[str, Any] | None = None,
                          run: bool = True, idempotency_key: str | None = None) -> dict[str, Any]:
        explicit = normalize_changes(changes)
        context = extract_instruction_changes(content)
        context.update(explicit)
        context["source_instruction"] = content
        payload = {"conversation_id": conversation_id, "content": content,
                   "title": title, "changes": explicit}
        result = await self._mutate(
            None,
            lambda: self.repository.create_task(
                owner, conversation_id, title or self._title(content), context,
                RFQ_QUOTE_FOLLOWUP_PLAN),
            owner=owner, operation="create", idempotency_key=idempotency_key,
            idempotency_payload=payload,
        )
        if run:
            await self.run_ready(task_id=result["task_id"])
            return self.repository.get_task(result["task_id"], owner)
        return result

    @staticmethod
    def _title(content: str) -> str:
        text = " ".join(str(content).split())
        return (text[:72] + "…") if len(text) > 72 else (text or "新询盘任务")

    async def bind_conversation_message(self, owner: TaskOwner, conversation_id: str,
                                        content: str, task_id: str | None = None,
                                        idempotency_key: str | None = None) -> dict[str, Any] | None:
        if task_id:
            task = self.repository.get_task(task_id, owner)
            if task["conversation_id"] != conversation_id:
                raise TaskRuntimeError("task_not_found", 404)
            return {"binding": "instruction", "task": await self.add_instruction(
                task_id, owner, content, run=True, idempotency_key=idempotency_key)}
        active = [item for item in self.repository.list_tasks(owner, conversation_id)
                  if item["status"] not in {"completed", "cancelled"}]
        if len(active) > 1:
            return {"binding": "selection_required", "task_ids": [item["task_id"] for item in active]}
        if len(active) == 1:
            return {"binding": "instruction", "task": await self.add_instruction(
                active[0]["task_id"], owner, content, run=True,
                idempotency_key=idempotency_key)}
        if TRADE_INTENT.search(content):
            return {"binding": "created", "task": await self.create_task(
                owner, conversation_id, content, run=True,
                idempotency_key=idempotency_key)}
        return None

    async def add_instruction(self, task_id: str, owner: TaskOwner, content: str,
                              *, changes: Mapping[str, Any] | None = None,
                              run: bool = True, idempotency_key: str | None = None) -> dict[str, Any]:
        task = self.repository.get_task(task_id, owner)
        parsed = extract_instruction_changes(content)
        explicit = normalize_changes(changes)
        parsed.update(explicit)
        impacts = impacted_steps(parsed, task.get("current_step_key"))
        if task["status"] == "waiting_input" and task.get("current_step_key") in STEP_INDEX:
            # New input must re-run the blocked validation step. Reusing a
            # waiting_input step would preserve the old human action forever
            # even though the required fields are now present in context.
            blocked_index = STEP_INDEX[task["current_step_key"]]
            impacts = [item["step_key"] for item in RFQ_QUOTE_FOLLOWUP_PLAN[blocked_index:]]
        payload = {"content": content, "changes": explicit}
        result = await self._mutate(
            task_id,
            lambda: self.repository.apply_instruction(
                task_id, owner, content, parsed, impacts, RFQ_QUOTE_FOLLOWUP_PLAN),
            owner=owner, operation="instruction", idempotency_key=idempotency_key,
            idempotency_payload=payload,
        )
        if run and result["status"] not in {"waiting_input", "waiting_review"}:
            await self.run_ready(task_id=task_id)
            return self.repository.get_task(task_id, owner)
        return result

    async def command(self, task_id: str, command: str, owner: TaskOwner,
                      etag: str | None, *, idempotency_key: str | None = None) -> dict[str, Any]:
        result = await self._mutate(
            task_id,
            lambda: self.repository.command(task_id, command, owner, etag, owner.actor_id),
            owner=owner, operation=f"command:{command}", idempotency_key=idempotency_key,
            idempotency_payload={"command": command, "etag": etag},
        )
        if command in {"resume", "retry"}:
            await self.run_ready(task_id=task_id)
            return self.repository.get_task(task_id, owner)
        return result

    async def resolve_action(self, task_id: str, action_id: str, owner: TaskOwner,
                             decision: str, comment: str = "", *,
                             idempotency_key: str | None = None) -> dict[str, Any]:
        result = await self._mutate(
            task_id,
            lambda: self.repository.resolve_action(
                task_id, action_id, owner, decision, comment),
            owner=owner, operation=f"decision:{action_id}", idempotency_key=idempotency_key,
            idempotency_payload={"decision": decision, "comment": comment},
        )
        if decision != "reject":
            await self.run_ready(task_id=task_id)
            return self.repository.get_task(task_id, owner)
        return result

    async def run_ready(self, *, task_id: str | None = None,
                        max_steps: int = 50) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []
        async with self._run_lock:
            for _ in range(max(1, min(max_steps, 100))):
                claim = self.repository.claim_step(self.worker_id, task_id=task_id)
                if claim is None:
                    break
                before = self.repository.get_task(claim.task_id)["last_sequence"]
                try:
                    controlled = self.repository.defer_claim_for_control(claim, self.worker_id)
                    if controlled is not None:
                        outcomes.append({"task_id": claim.task_id, "step_key": claim.step_key,
                                         "status": controlled["status"]})
                    else:
                        outcome = await self._execute_step(claim)
                        outcomes.append({"task_id": claim.task_id, "step_key": claim.step_key,
                                         "status": outcome["status"]})
                except TaskRuntimeError as exc:
                    if exc.code == "task_step_lease_lost":
                        outcomes.append({"task_id": claim.task_id, "step_key": claim.step_key,
                                         "status": "superseded"})
                    else:
                        try:
                            outcome = self.repository.fail_step(
                                claim, self.worker_id, exc.code)
                            outcomes.append({"task_id": claim.task_id, "step_key": claim.step_key,
                                             "status": outcome["status"], "error_code": exc.code})
                        except TaskRuntimeError as fail_exc:
                            if fail_exc.code != "task_step_lease_lost":
                                raise
                            outcomes.append({"task_id": claim.task_id, "step_key": claim.step_key,
                                             "status": "superseded"})
                except Exception as exc:
                    code = type(exc).__name__
                    try:
                        outcome = self.repository.fail_step(claim, self.worker_id, code)
                        outcomes.append({"task_id": claim.task_id, "step_key": claim.step_key,
                                         "status": outcome["status"], "error_code": code})
                    except TaskRuntimeError as fail_exc:
                        if fail_exc.code != "task_step_lease_lost":
                            raise
                        outcomes.append({"task_id": claim.task_id, "step_key": claim.step_key,
                                         "status": "superseded"})
                await self._publish_since(claim.task_id, before)
                self.repository.acknowledge_resume_outbox(claim.task_id)
        return outcomes

    async def _execute_step(self, claim: ClaimedStep) -> dict[str, Any]:
        context = dict(claim.context)
        previous = self._step_outputs(claim.task_id, claim.plan_version)

        if claim.executor == "inquiry_structuring":
            missing = []
            if not (context.get("sku") or context.get("product_description")):
                missing.append("product_or_sku")
            if not context.get("quantity"):
                missing.append("quantity")
            if not context.get("destination"):
                missing.append("destination")
            output = {"structured_fields": {key: context.get(key) for key in (
                "product_description", "sku", "quantity", "destination", "incoterm", "currency")
                if context.get(key)}, "missing_fields": missing}
            if missing:
                return self.repository.wait_step(
                    claim, self.worker_id, status="waiting_input",
                    prompt_key="task.prompt.inquiry_missing", missing=missing, output=output)
            return self.repository.complete_step(claim, self.worker_id, output)

        if claim.executor == "missing_information":
            return self.repository.complete_step(claim, self.worker_id, {
                "status": "ready",
                "confirmed_fields": sorted(key for key in context if not key.startswith("latest")),
            })

        if claim.executor == "product_matching":
            raw = await asyncio.to_thread(
                search_product_catalog_impl,
                keyword=str(context.get("product_description") or ""),
                sku=str(context.get("sku") or ""),
                limit=3,
            )
            payload = json.loads(raw)
            results = payload.get("results") or []
            if not results:
                return self.repository.wait_step(
                    claim, self.worker_id, status="waiting_input",
                    prompt_key="task.prompt.product_not_found", missing=["confirmed_sku"],
                    output={"candidates": []})
            requested_sku = str(context.get("sku") or "")
            if requested_sku and results[0].get("match_type") != "exact_sku":
                return self.repository.wait_step(
                    claim, self.worker_id, status="waiting_input",
                    prompt_key="task.prompt.confirm_alternative_sku", missing=["confirmed_sku"],
                    output={"requested_sku": requested_sku,
                            "candidates": [{"sku": item["sku"], "name": item["name_en"]}
                                           for item in results[:3]]})
            if not requested_sku and len(results) != 1:
                return self.repository.wait_step(
                    claim, self.worker_id, status="waiting_input",
                    prompt_key="task.prompt.confirm_sku", missing=["confirmed_sku"],
                    output={"candidates": [{"sku": item["sku"], "name": item["name_en"]}
                                           for item in results[:3]]})
            selected = results[0]
            return self.repository.complete_step(claim, self.worker_id, {
                "selected_sku": selected["sku"],
                "selected_product": selected["name_en"],
                "match_type": selected.get("match_type"),
                "candidate_count": len(results),
                "source_status": "demo_only_pending_authoritative_product_source",
            })

        sku = str(context.get("sku") or previous.get("product_matching", {}).get("selected_sku") or "")
        if claim.executor == "inventory_check":
            raw = await asyncio.to_thread(
                check_inventory_impl, sku, int(context.get("quantity") or 0))
            output = json.loads(raw)
            if not output.get("available"):
                return self.repository.wait_step(
                    claim, self.worker_id, status="waiting_input",
                    prompt_key="task.prompt.inventory_unavailable",
                    missing=["quantity_or_alternative_sku"], output=output)
            return self.repository.complete_step(claim, self.worker_id, output)

        if claim.executor == "commercial_terms":
            missing = [field for field in ("incoterm", "payment_terms") if not context.get(field)]
            if missing:
                return self.repository.wait_step(
                    claim, self.worker_id, status="waiting_input",
                    prompt_key="task.prompt.commercial_terms", missing=missing,
                    output={"destination": context.get("destination")})
            return self.repository.complete_step(claim, self.worker_id, {
                "destination": context["destination"],
                "incoterm": context["incoterm"],
                "payment_terms": context["payment_terms"],
                "currency": context.get("currency", "USD"),
            })

        if claim.executor == "quote_calculating":
            raw = await asyncio.to_thread(
                calculate_quote_impl,
                sku=sku,
                quantity=int(context["quantity"]),
                delivery_term=str(context["incoterm"]),
                destination_country=str(context["destination"]),
                freight_cost_usd=float(context.get("freight_cost_usd") or 0),
                target_currency=str(context.get("currency") or "USD"),
            )
            output = json.loads(raw)
            if output.get("error"):
                raise TaskRuntimeError("task_quote_calculation_failed")
            output["commercial_source_status"] = "demo_only_pending_authoritative_pricing"
            output["publication_allowed"] = False
            return self.repository.complete_step(claim, self.worker_id, output)

        if claim.executor == "quote_drafting":
            calculation = previous.get("quote_calculating") or {}
            quote = {
                "schema_version": "task-quote-draft.v1",
                "task_id": claim.task_id,
                "plan_version": claim.plan_version,
                "buyer": context.get("buyer", "待补充"),
                "destination": context.get("destination"),
                "sku": sku,
                "quantity": context.get("quantity"),
                "currency": calculation.get("total_target_currency", "USD"),
                "total": calculation.get("total_target_amount"),
                "incoterm": context.get("incoterm"),
                "payment_terms": context.get("payment_terms"),
                "status": "pending_internal_review",
                "warning": "演示产品和价格不可作为正式报价；未审核前不得向客户发布。",
            }
            output_dir = self.artifact_root / claim.task_id / f"plan-{claim.plan_version}"
            output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / "quotation-draft.json"
            path.write_text(json.dumps(quote, ensure_ascii=False, indent=2), encoding="utf-8")
            artifact = self.repository.save_artifact(
                claim.task_id, claim.step_key, claim.plan_version, "internal", "json", path)
            return self.repository.complete_step(claim, self.worker_id, {
                "artifact_id": artifact["artifact_id"],
                "status": "pending_internal_review",
                "content_hash": artifact["sha256"],
            }, [artifact])

        if claim.executor == "internal_review":
            return self.repository.wait_step(
                claim, self.worker_id, status="waiting_review",
                prompt_key="task.prompt.internal_quote_review",
                output={"publication_allowed": False})

        if claim.executor == "customer_confirmation":
            return self.repository.wait_step(
                claim, self.worker_id, status="waiting_review",
                prompt_key="task.prompt.customer_quote_confirmation",
                output={"customer_visible": True})

        if claim.executor == "follow_up":
            return self.repository.complete_step(claim, self.worker_id, {
                "status": "planned",
                "automatic_send": False,
                "tasks": [{"offset_days": 3, "type": "quote_follow_up",
                           "human_review_required": True}],
            })
        raise TaskRuntimeError("task_executor_not_found")

    def _step_outputs(self, task_id: str, plan_version: int) -> dict[str, dict[str, Any]]:
        task = self.repository.get_task(task_id)
        return {item["step_key"]: item.get("output") or {} for item in task["steps"]
                if int(item["plan_version"]) == int(plan_version) and item["status"] == "completed"}

    def workspace_projection(self, task: dict[str, Any]) -> dict[str, Any]:
        return task

    def customer_projection(self, task: dict[str, Any]) -> dict[str, Any]:
        public_steps = [{key: step.get(key) for key in (
            "step_key", "ordinal", "label_key", "status", "completed_at")}
            for step in task["steps"]]
        artifacts = [{key: artifact.get(key) for key in (
            "artifact_id", "kind", "file_name", "byte_size", "sha256", "created_at")}
            for artifact in task.get("artifacts", [])
            if artifact.get("visibility") == "customer" and artifact.get("approved")]
        actions = [{key: action.get(key) for key in (
            "action_id", "step_key", "status", "prompt_key", "payload", "created_at")}
            for action in task.get("human_actions", []) if action.get("audience") == "customer"]
        context = task.get("context") or {}
        return {
            "task_id": task["task_id"],
            "conversation_id": task["conversation_id"],
            "title": task["title"],
            "template_id": task["template_id"],
            "status": task["status"],
            "active_plan_version": task["active_plan_version"],
            "current_step_key": task["current_step_key"],
            "last_sequence": task["last_sequence"],
            "version": task["version"],
            "created_at": task["created_at"],
            "updated_at": task["updated_at"],
            "summary": {key: context.get(key) for key in (
                "product_description", "sku", "quantity", "destination", "incoterm", "currency")
                if context.get(key)},
            "steps": public_steps,
            "human_actions": actions,
            "artifacts": artifacts,
            "etag": task["etag"],
        }

    def snapshot(self, owner: TaskOwner, conversation_id: str) -> dict[str, Any]:
        tasks = self.repository.list_tasks(owner, conversation_id)
        projector = self.customer_projection if owner.is_customer else self.workspace_projection
        return {"type": "task.snapshot", "protocol_version": 2,
                "conversation_id": conversation_id,
                "tasks": [projector(item) for item in tasks]}

    @staticmethod
    def acknowledgement(binding: str, language: str) -> str:
        messages = {
            "zh": {
                "created": "已创建外贸任务，并在外贸工作台中开始执行。你可以随时暂停或补充要求。",
                "instruction": "补充要求已绑定到当前任务。系统已保留有效检查点，并按新计划继续执行。",
                "selection_required": "当前对话有多个进行中的任务，请先在外贸工作台中选择要继续的任务。",
            },
            "en": {
                "created": "The trade task has been created and started in the workbench. You can pause it or add requirements at any time.",
                "instruction": "Your instruction was bound to the selected task. Valid checkpoints were preserved and execution continues from the revised plan.",
                "selection_required": "This conversation has multiple active tasks. Select the task you want to continue in the trade workbench.",
            },
            "de": {
                "created": "Die Außenhandelsaufgabe wurde erstellt und im Arbeitsbereich gestartet. Sie können sie jederzeit pausieren oder ergänzen.",
                "instruction": "Ihre Ergänzung wurde der ausgewählten Aufgabe zugeordnet. Gültige Prüfpunkte bleiben erhalten und der überarbeitete Plan wird fortgesetzt.",
                "selection_required": "In diesem Dialog gibt es mehrere aktive Aufgaben. Wählen Sie zuerst die fortzusetzende Aufgabe aus.",
            },
        }
        selected = language if language in messages else "en"
        return messages[selected].get(binding, messages[selected]["instruction"])


class TaskRuntimeWorker:
    """Small restart-safe worker loop. It never publishes quotes or sends email."""

    def __init__(self, service: TaskRuntimeService, *, poll_seconds: float = 0.5):
        self.service = service
        self.poll_seconds = max(0.1, float(poll_seconds))

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            outcomes = await self.service.run_ready(max_steps=20)
            if outcomes:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                continue
