"""Workspace and customer HTTP surfaces for the unified task runtime."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import ipaddress
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from agent.business.task_runtime_repository import TaskOwner, TaskRuntimeError
from agent.business.task_runtime_service import TaskRuntimeService
from agent.customer_identity.service import CustomerIdentityError, CustomerIdentityService
from agent.customer_identity.session_cookie import AUTH_COOKIE


class CreateTaskPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str
    content: str = Field(min_length=1, max_length=10000)
    title: str | None = Field(default=None, max_length=255)
    changes: dict[str, Any] = Field(default_factory=dict)


class InstructionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=10000)
    changes: dict[str, Any] = Field(default_factory=dict)


class DecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str
    comment: str = Field(default="", max_length=1000)


def _loopback(value: str | None) -> bool:
    if value in {"testclient", "testserver", "localhost"}:
        return True
    try:
        return ipaddress.ip_address(value or "").is_loopback
    except ValueError:
        return False


def _fail(exc: Exception):
    if isinstance(exc, TaskRuntimeError):
        raise HTTPException(exc.status_code, exc.code) from None
    if hasattr(exc, "status_code") and hasattr(exc, "code"):
        raise HTTPException(int(getattr(exc, "status_code")), str(getattr(exc, "code"))) from None
    raise HTTPException(500, "task_runtime_internal_error") from exc


def _workspace_guard(request: Request) -> None:
    host, client = (request.url.hostname or "").lower(), request.client.host if request.client else ""
    if not (_loopback(host) and _loopback(client)):
        raise HTTPException(403, "task_workspace_loopback_required")
    origin = request.headers.get("origin")
    if origin:
        parsed = urlparse(origin)
        request_port = request.url.port or (443 if request.url.scheme == "https" else 80)
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if parsed.scheme != request.url.scheme or parsed.hostname != host or origin_port != request_port:
            raise HTTPException(403, "task_origin_forbidden")


def _artifact_response(service: TaskRuntimeService, task: dict[str, Any], artifact_id: str,
                       *, customer: bool) -> FileResponse:
    artifact = next((item for item in task.get("artifacts", [])
                     if item["artifact_id"] == artifact_id), None)
    if artifact is None or (customer and not (
            artifact.get("visibility") == "customer" and artifact.get("approved"))):
        raise HTTPException(404, "task_artifact_not_found")
    path = Path(artifact["storage_path"]).resolve()
    root = service.artifact_root.resolve()
    if not path.is_file() or root not in path.parents:
        raise HTTPException(404, "task_artifact_not_found")
    if hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
        raise HTTPException(409, "task_artifact_hash_mismatch")
    media = "application/json" if artifact["kind"] == "json" else "application/octet-stream"
    return FileResponse(path, media_type=media, filename=artifact["file_name"])


def create_workspace_task_router(service: TaskRuntimeService, owner: TaskOwner,
                                 conversation_guard: Callable[[str], None] | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/tasks", tags=["task-runtime"])

    @router.get("")
    async def list_tasks(request: Request, conversation_id: str | None = None,
                         limit: int = Query(100, ge=1, le=100)):
        _workspace_guard(request)
        return {"items": [service.workspace_projection(item) for item in
                           service.repository.list_tasks(owner, conversation_id, limit)]}

    @router.post("", status_code=201)
    async def create_task(payload: CreateTaskPayload, request: Request,
                          idempotency_key: str | None = Header(None)):
        _workspace_guard(request)
        if not idempotency_key:
            raise HTTPException(400, "task_idempotency_key_required")
        try:
            if conversation_guard is not None:
                conversation_guard(payload.conversation_id)
            return await service.create_task(owner, payload.conversation_id, payload.content,
                title=payload.title, changes=payload.changes, run=True,
                idempotency_key=idempotency_key)
        except Exception as exc:
            _fail(exc)

    @router.get("/{task_id}")
    async def get_task(task_id: str, request: Request, response: Response):
        _workspace_guard(request)
        try:
            result = service.workspace_projection(service.repository.get_task(task_id, owner))
            response.headers["ETag"] = result["etag"]
            return result
        except Exception as exc:
            _fail(exc)

    @router.get("/{task_id}/events")
    async def events(task_id: str, request: Request, after_sequence: int = Query(0, ge=0)):
        _workspace_guard(request)
        try:
            return {"items": [asdict(event) for event in service.repository.events(
                task_id, after_sequence, owner)]}
        except Exception as exc:
            _fail(exc)

    @router.post("/{task_id}/commands/{command}")
    async def command(task_id: str, command: str, request: Request,
                      if_match: str | None = Header(None),
                      idempotency_key: str | None = Header(None)):
        _workspace_guard(request)
        if not idempotency_key:
            raise HTTPException(400, "task_idempotency_key_required")
        try:
            return await service.command(task_id, command, owner, if_match,
                                         idempotency_key=idempotency_key)
        except Exception as exc:
            _fail(exc)

    @router.post("/{task_id}/instructions")
    async def instruct(task_id: str, payload: InstructionPayload, request: Request,
                       idempotency_key: str | None = Header(None)):
        _workspace_guard(request)
        if not idempotency_key:
            raise HTTPException(400, "task_idempotency_key_required")
        try:
            return await service.add_instruction(task_id, owner, payload.content,
                                                 changes=payload.changes, run=True,
                                                 idempotency_key=idempotency_key)
        except Exception as exc:
            _fail(exc)

    @router.post("/{task_id}/actions/{action_id}/decision")
    async def decide(task_id: str, action_id: str, payload: DecisionPayload, request: Request,
                     idempotency_key: str | None = Header(None)):
        _workspace_guard(request)
        if not idempotency_key:
            raise HTTPException(400, "task_idempotency_key_required")
        try:
            return await service.resolve_action(task_id, action_id, owner,
                                                payload.decision, payload.comment,
                                                idempotency_key=idempotency_key)
        except Exception as exc:
            _fail(exc)

    @router.get("/{task_id}/artifacts/{artifact_id}")
    async def artifact(task_id: str, artifact_id: str, request: Request):
        _workspace_guard(request)
        try:
            return _artifact_response(service, service.repository.get_task(task_id, owner),
                                      artifact_id, customer=False)
        except HTTPException:
            raise
        except Exception as exc:
            _fail(exc)

    return router


def create_customer_task_router(service: TaskRuntimeService,
                                identity: CustomerIdentityService,
                                conversation_guard: Callable[[TaskOwner, str], None] | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/customer/tasks", tags=["customer-task-runtime"])

    def owner(request: Request, csrf: str | None = None) -> TaskOwner:
        try:
            customer = identity.require(request.cookies.get(AUTH_COOKIE))
            if csrf is not None:
                identity.require_csrf(customer, csrf)
            return TaskOwner(customer.tenant_id, "customer", customer.account_id,
                             customer.account_id)
        except CustomerIdentityError as exc:
            raise HTTPException(exc.status_code, exc.code) from None

    @router.get("")
    async def list_tasks(request: Request, conversation_id: str | None = None,
                         limit: int = Query(100, ge=1, le=100)):
        actor = owner(request)
        return {"items": [service.customer_projection(item) for item in
                           service.repository.list_tasks(actor, conversation_id, limit)]}

    @router.post("", status_code=201)
    async def create_task(payload: CreateTaskPayload, request: Request,
                          x_csrf_token: str | None = Header(None),
                          idempotency_key: str | None = Header(None)):
        if not idempotency_key:
            raise HTTPException(400, "task_idempotency_key_required")
        actor = owner(request, x_csrf_token)
        try:
            if conversation_guard is not None:
                conversation_guard(actor, payload.conversation_id)
            task = await service.create_task(actor, payload.conversation_id, payload.content,
                title=payload.title, changes=payload.changes, run=True,
                idempotency_key=idempotency_key)
            return service.customer_projection(task)
        except Exception as exc:
            _fail(exc)

    @router.get("/{task_id}")
    async def get_task(task_id: str, request: Request, response: Response):
        actor = owner(request)
        try:
            result = service.customer_projection(service.repository.get_task(task_id, actor))
            response.headers["ETag"] = result["etag"]
            return result
        except Exception as exc:
            _fail(exc)

    @router.get("/{task_id}/events")
    async def events(task_id: str, request: Request, after_sequence: int = Query(0, ge=0)):
        actor = owner(request)
        try:
            return {"items": [{"event_id": event.event_id, "task_id": event.task_id,
                "sequence": event.sequence, "type": event.type, "status": event.status,
                "step_key": event.step_key, "safe_data": event.safe_data,
                "occurred_at": event.occurred_at} for event in
                service.repository.events(task_id, after_sequence, actor)]}
        except Exception as exc:
            _fail(exc)

    @router.post("/{task_id}/commands/{command}")
    async def command(task_id: str, command: str, request: Request,
                      x_csrf_token: str | None = Header(None),
                      if_match: str | None = Header(None),
                      idempotency_key: str | None = Header(None)):
        if command not in {"pause", "resume", "cancel", "retry"}:
            raise HTTPException(404, "task_command_invalid")
        if not idempotency_key:
            raise HTTPException(400, "task_idempotency_key_required")
        actor = owner(request, x_csrf_token)
        try:
            return service.customer_projection(await service.command(
                task_id, command, actor, if_match, idempotency_key=idempotency_key))
        except Exception as exc:
            _fail(exc)

    @router.post("/{task_id}/instructions")
    async def instruct(task_id: str, payload: InstructionPayload, request: Request,
                       x_csrf_token: str | None = Header(None),
                       idempotency_key: str | None = Header(None)):
        if not idempotency_key:
            raise HTTPException(400, "task_idempotency_key_required")
        actor = owner(request, x_csrf_token)
        try:
            return service.customer_projection(await service.add_instruction(
                task_id, actor, payload.content, changes=payload.changes, run=True,
                idempotency_key=idempotency_key))
        except Exception as exc:
            _fail(exc)

    @router.post("/{task_id}/actions/{action_id}/decision")
    async def decide(task_id: str, action_id: str, payload: DecisionPayload, request: Request,
                     x_csrf_token: str | None = Header(None),
                     idempotency_key: str | None = Header(None)):
        if not idempotency_key:
            raise HTTPException(400, "task_idempotency_key_required")
        actor = owner(request, x_csrf_token)
        try:
            return service.customer_projection(await service.resolve_action(
                task_id, action_id, actor, payload.decision, payload.comment,
                idempotency_key=idempotency_key))
        except Exception as exc:
            _fail(exc)

    @router.get("/{task_id}/artifacts/{artifact_id}")
    async def artifact(task_id: str, artifact_id: str, request: Request):
        actor = owner(request)
        try:
            return _artifact_response(service, service.repository.get_task(task_id, actor),
                                      artifact_id, customer=True)
        except HTTPException:
            raise
        except Exception as exc:
            _fail(exc)

    return router
