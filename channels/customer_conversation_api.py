"""Authenticated, account-owned customer conversation HTTP router."""

from __future__ import annotations

from dataclasses import asdict
from typing import Awaitable, Callable

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from agent.customer_identity.service import CustomerIdentityError, CustomerIdentityService
from agent.customer_identity.session_cookie import AUTH_COOKIE
from session.customer_conversation import (
    CustomerConversationError,
    CustomerConversationRepository,
    CustomerOwner,
)


class CreateConversationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="New inquiry", min_length=1, max_length=120)


class UpdateConversationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=120)
    status: str | None = None
    version: int = Field(ge=1)


def create_customer_conversation_router(
    identity: CustomerIdentityService,
    repository: CustomerConversationRepository,
    on_deleted: Callable[[CustomerOwner, str], Awaitable[None]] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/customer/conversations", tags=["customer-conversations"])

    def owner(request: Request, *, csrf: str | None = None) -> CustomerOwner:
        try:
            customer = identity.require(request.cookies.get(AUTH_COOKIE))
            if csrf is not None:
                identity.require_csrf(customer, csrf)
            return CustomerOwner(customer.tenant_id, customer.account_id)
        except CustomerIdentityError as exc:
            raise HTTPException(exc.status_code, detail=exc.code) from None

    def call(function):
        try:
            return function()
        except CustomerConversationError as exc:
            raise HTTPException(exc.status_code, detail=exc.code) from None

    @router.get("")
    async def list_conversations(
        request: Request, cursor: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ):
        page = call(lambda: repository.list_owned(owner(request), cursor, limit))
        return {"items": [asdict(item) for item in page.items], "next_cursor": page.next_cursor}

    @router.post("", status_code=201)
    async def create_conversation(
        payload: CreateConversationPayload, request: Request,
        x_csrf_token: str | None = Header(None),
    ):
        item = call(lambda: repository.create(owner(request, csrf=x_csrf_token), payload.title))
        return asdict(item)

    @router.get("/{conversation_id}/messages")
    async def messages(
        conversation_id: str, request: Request, cursor: str | None = None,
        limit: int = Query(100, ge=1, le=200),
    ):
        page = call(lambda: repository.list_messages(
            owner(request), conversation_id, cursor, limit
        ))
        return {"items": [asdict(item) for item in page.items], "next_cursor": page.next_cursor}

    @router.patch("/{conversation_id}")
    async def update_conversation(
        conversation_id: str, payload: UpdateConversationPayload, request: Request,
        x_csrf_token: str | None = Header(None),
    ):
        item = call(lambda: repository.update(
            owner(request, csrf=x_csrf_token), conversation_id,
            title=payload.title, status=payload.status, expected_version=payload.version,
        ))
        return asdict(item)

    @router.delete("/{conversation_id}", status_code=202)
    async def delete_conversation(
        conversation_id: str, request: Request, version: int = Query(ge=1),
        x_csrf_token: str | None = Header(None),
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        job = call(lambda: repository.soft_delete(
            owner(request, csrf=x_csrf_token), conversation_id, version, idempotency_key
        ))
        if on_deleted is not None:
            await on_deleted(owner(request), conversation_id)
        return asdict(job)

    return router
