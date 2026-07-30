"""Protected management API for workspace-private memory."""

from __future__ import annotations

import secrets
from dataclasses import asdict

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from agent.memory_runtime.errors import MemoryAccessDenied, MemoryVersionConflict


class VersionHashPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    content_hash: str = Field(min_length=64, max_length=64)


class RejectPayload(VersionHashPayload):
    reason: str = Field(default="operator_rejected", min_length=1, max_length=200)


class CorrectPayload(VersionHashPayload):
    content: str = Field(min_length=1, max_length=8000)
    summary: str = Field(min_length=1, max_length=1000)
    source_refs: tuple[str, ...] = Field(min_length=1, max_length=20)


def create_workspace_memory_router(
    service, actor, scope, *, review_service=None, token: str = "",
    allowed_origins: tuple[str, ...] = (),
) -> APIRouter:
    router = APIRouter(prefix="/api/workspace/memories", tags=["workspace-memory"])
    allowed_origins = tuple(value.rstrip("/") for value in allowed_origins if value)

    def require_admin(request: Request) -> None:
        host = (request.client.host if request.client else "").split("%", 1)[0]
        is_loopback = host in {"127.0.0.1", "::1"}
        if not is_loopback:
            scheme, separator, supplied = request.headers.get("authorization", "").partition(" ")
            if not token:
                raise HTTPException(503, detail="workspace_memory_admin_not_configured")
            if (not separator or scheme.casefold() != "bearer" or not supplied
                    or not secrets.compare_digest(supplied, token)):
                raise HTTPException(
                    401, detail="workspace_memory_admin_unauthorized",
                    headers={"WWW-Authenticate": "Bearer"},
                )
        origin = request.headers.get("origin")
        if not is_loopback and not origin:
            raise HTTPException(403, detail="workspace_memory_origin_required")
        if origin:
            same = f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
            if origin.rstrip("/") not in {same.rstrip("/"), *allowed_origins}:
                raise HTTPException(403, detail="workspace_memory_origin_forbidden")

    def call(fn):
        try:
            return fn()
        except MemoryAccessDenied:
            raise HTTPException(404, detail="workspace_memory_not_found") from None
        except MemoryVersionConflict:
            raise HTTPException(409, detail="workspace_memory_version_or_hash_conflict") from None
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from None

    @router.get("")
    async def list_memories(request: Request, status: str | None = None,
                            cursor: str = "", limit: int = Query(50, ge=1, le=100)):
        require_admin(request)
        items = call(lambda: service.store.list_scope(
            actor, scope, status=status, limit=limit, after=cursor,
        ))
        return {
            "items": [asdict(item) for item in items],
            "next_cursor": items[-1].memory_id if len(items) == limit else None,
        }

    @router.get("/export")
    async def export_memories(request: Request):
        require_admin(request)
        return asdict(call(lambda: service.store.export_scope(actor, scope)))

    @router.get("/{memory_id}")
    async def get_memory(memory_id: str, request: Request):
        require_admin(request)
        return asdict(call(lambda: service.get(actor, memory_id, scope=scope)))

    @router.post("/{memory_id}/confirm")
    async def confirm(memory_id: str, payload: VersionHashPayload, request: Request):
        require_admin(request)
        return asdict(call(lambda: service.confirm(
            actor, memory_id, version=payload.version, expected_hash=payload.content_hash,
            scope=scope,
        )))

    @router.post("/{memory_id}/reject")
    async def reject(memory_id: str, payload: RejectPayload, request: Request):
        require_admin(request)
        call(lambda: service.reject(
            actor, memory_id, version=payload.version,
            expected_hash=payload.content_hash, reason=payload.reason, scope=scope,
        ))
        return {"status": "rejected", "memory_id": memory_id}

    @router.patch("/{memory_id}", status_code=201)
    async def correct(memory_id: str, payload: CorrectPayload, request: Request):
        require_admin(request)
        return asdict(call(lambda: service.correct(
            actor, memory_id, version=payload.version, expected_hash=payload.content_hash,
            content=payload.content, summary=payload.summary,
            source_refs=payload.source_refs, scope=scope,
        )))

    @router.delete("/{memory_id}")
    async def delete(memory_id: str, request: Request, version: int = Query(ge=1),
                     content_hash: str = Query(min_length=64, max_length=64),
                     idempotency_key: str = Header(..., alias="Idempotency-Key")):
        del idempotency_key
        require_admin(request)
        current = call(lambda: service.get(actor, memory_id, scope=scope))
        if current.status == "deleted":
            return {"status": "deleted", "memory_id": memory_id}
        if current.version != version or current.content_hash != content_hash:
            raise HTTPException(409, detail="workspace_memory_version_or_hash_conflict")
        call(lambda: service.store.delete_owned(actor, memory_id, version))
        return {"status": "deleted", "memory_id": memory_id}

    @router.post("/review/run")
    async def review(request: Request):
        require_admin(request)
        if review_service is None:
            raise HTTPException(404, detail="workspace_memory_review_disabled")
        return {"status": "completed", "suggestions": await review_service.run_once(actor, scope)}

    return router
