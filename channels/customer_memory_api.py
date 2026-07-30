"""Authenticated customer self-service memory API."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from agent.customer_identity.service import CustomerIdentityError, CustomerIdentityService
from agent.customer_identity.session_cookie import AUTH_COOKIE
from agent.memory_runtime.errors import MemoryAccessDenied, MemoryVersionConflict
from agent.memory_runtime.models import MemoryScope
from agent.memory_runtime.services.customer_memory import CustomerMemoryService


class ConsentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: str = Field(min_length=1, max_length=80)
    categories: tuple[str, ...] = Field(min_length=1, max_length=3)
    expires_at: str | None = None


class CandidatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=8000)
    summary: str = Field(min_length=1, max_length=1000)
    memory_type: str
    purpose: str = Field(min_length=1, max_length=80)
    source_refs: tuple[str, ...] = Field(min_length=1, max_length=20)
    conversation_id: str | None = None
    confidence: float = Field(ge=0, le=1)
    importance: float = Field(ge=0, le=1)
    expires_at: str | None = None


class ActivatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consent_record_id: str
    version: int = Field(ge=1)


class CorrectPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=8000)
    summary: str = Field(min_length=1, max_length=1000)
    source_refs: tuple[str, ...] = Field(min_length=1, max_length=20)
    version: int = Field(ge=1)


def create_customer_memory_router(
    identity: CustomerIdentityService, service: CustomerMemoryService,
) -> APIRouter:
    router = APIRouter(prefix="/api/customer/memories", tags=["customer-memory"])

    def actor(request: Request, csrf: str | None = None):
        try:
            customer = identity.require(request.cookies.get(AUTH_COOKIE))
            if csrf is not None:
                identity.require_csrf(customer, csrf)
            return service.actor(customer.tenant_id, customer.account_id)
        except CustomerIdentityError as exc:
            raise HTTPException(exc.status_code, detail=exc.code) from None

    def call(fn):
        try:
            return fn()
        except MemoryAccessDenied:
            raise HTTPException(404, detail="customer_resource_not_found") from None
        except MemoryVersionConflict:
            raise HTTPException(409, detail="customer_version_conflict") from None
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from None

    @router.get("")
    async def list_memories(request: Request, purpose: str = "customer_support"):
        current = actor(request)
        export = call(lambda: service.store.export_scope(
            current, MemoryScope("customer_private", current.tenant_id,
                                 account_id=current.actor_id, purpose=purpose)
        ))
        return {"items": [asdict(item) for item in export.items]}

    @router.get("/export")
    async def export_memories(request: Request, purpose: str = "customer_support"):
        current = actor(request)
        exported = call(lambda: service.store.export_scope(
            current, MemoryScope("customer_private", current.tenant_id,
                                 account_id=current.actor_id, purpose=purpose)
        ))
        return asdict(exported)

    @router.post("/consents", status_code=201)
    async def grant_consent(payload: ConsentPayload, request: Request,
                            x_csrf_token: str | None = Header(None)):
        result = call(lambda: service.grant_consent(
            actor(request, x_csrf_token), purpose=payload.purpose,
            categories=payload.categories, expires_at=payload.expires_at,
        ))
        return asdict(result)

    @router.delete("/consents/{consent_id}")
    async def withdraw_consent(consent_id: str, request: Request,
                               x_csrf_token: str | None = Header(None)):
        count = call(lambda: service.withdraw_consent(actor(request, x_csrf_token), consent_id))
        return {"invalidated_memory_count": count}

    @router.post("/candidates", status_code=201)
    async def create_candidate(payload: CandidatePayload, request: Request,
                               x_csrf_token: str | None = Header(None)):
        result = call(lambda: service.create_candidate(
            actor(request, x_csrf_token), **payload.model_dump()
        ))
        return asdict(result)

    @router.post("/{memory_id}/activate")
    async def activate(memory_id: str, payload: ActivatePayload, request: Request,
                       x_csrf_token: str | None = Header(None)):
        result = call(lambda: service.activate_candidate(
            actor(request, x_csrf_token), memory_id,
            payload.consent_record_id, payload.version,
        ))
        return asdict(result)

    @router.patch("/{memory_id}")
    async def correct(memory_id: str, payload: CorrectPayload, request: Request,
                      x_csrf_token: str | None = Header(None)):
        result = call(lambda: service.correct(
            actor(request, x_csrf_token), memory_id,
            content=payload.content, summary=payload.summary,
            source_refs=payload.source_refs, expected_version=payload.version,
        ))
        return asdict(result)

    @router.delete("/{memory_id}")
    async def invalidate(memory_id: str, request: Request, version: int = Query(ge=1),
                         x_csrf_token: str | None = Header(None)):
        call(lambda: service.store.delete_owned(
            actor(request, x_csrf_token), memory_id, version
        ))
        return {"status": "deleted"}

    @router.delete("")
    async def delete_all(request: Request, idempotency_key: str = Header(..., alias="Idempotency-Key"),
                         x_csrf_token: str | None = Header(None)):
        current = actor(request, x_csrf_token)
        job = call(lambda: service.store.delete_scope(
            current, MemoryScope("customer_private", current.tenant_id,
                                 account_id=current.actor_id, purpose="customer_support"),
            idempotency_key,
        ))
        return asdict(job)

    return router
