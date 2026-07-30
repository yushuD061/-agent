"""Web 渠道实现模块，提供 WebSocket 接口。"""

import asyncio
import hashlib
import ipaddress
from datetime import datetime, timezone
import json
import os
import secrets
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from bus import InboundMessage, MessageBus, OutboundMessage
from .base import Channel
from .web_protocol import encode_assistant_message
from privacy import safe_print as print
from session.conversation import ConversationError, ConversationService
from agent.workflow import WorkflowError, WorkflowService
from agent.business.email_account_repository import EmailAccountRepositoryError
from agent.business.email_account_service import (
    EmailAccountService,
    EmailAccountServiceError,
    create_default_email_account_service,
    _map_repository_error,
)
from agent.business.email_secret_store import EmailSecretStoreError
from agent.business.email_delivery_service import (
    EmailDeliveryService,
    EmailDeliveryServiceError,
    create_default_email_delivery_service,
)
from agent.business.email_review_service import (
    EmailReviewService,
    EmailReviewServiceError,
    create_default_email_review_service,
)
from agent.business.email_quote_workflow import (
    EmailQuoteWorkflowError,
    EmailQuoteWorkflowService,
)
from agent.business.email_config import load_email_config
from channels.email.admin_contracts import EmailAdminErrorCode, public_provider_contract
from trade_rag.knowledge_repository import (
    KnowledgeRepository,
    decode_text_document,
    public_document_record,
)
from trade_rag.pipeline import RagPipeline
from trade_rag.pdf_ingestion import PdfIngestionCoordinator
from trade_rag.vector_runtime import ManagedVectorStore, vector_runtime_state
from config import load_config
from channels.trade_workbench_api import build_trade_workbench_router
from channels.task_runtime_api import create_workspace_task_router
from agent.business.task_runtime_repository import TaskOwner, TaskRuntimeError
from agent.business.task_runtime_service import TaskRuntimeService


def _is_loopback_bind(host: str) -> bool:
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip()).is_loopback
    except ValueError:
        return False


def create_app() -> FastAPI:
    """Application factory for local UI previews and HTTP integration tests."""
    channel = WebChannel(MessageBus(), host="127.0.0.1")
    channel._app = FastAPI(title="NanoClaw Web")
    channel._register_routes()
    return channel._app

class WebChannel(Channel):
    """Web 渠道，通过 WebSocket 接收和发送消息。"""

    def __init__(self, bus: MessageBus, host: str = "0.0.0.0", port: int = 8080,
                 conversation_service: ConversationService | None = None,
                 workflow_service: WorkflowService | None = None,
                 email_admin_token: str | None = None,
                 email_admin_allowed_origins: tuple[str, ...] | None = None,
                 email_account_service: EmailAccountService | None = None,
                 email_delivery_service: EmailDeliveryService | None = None,
                 email_review_service: EmailReviewService | None = None,
                 email_quote_workflow_service: EmailQuoteWorkflowService | None = None,
                 email_reviewer_id: str | None = None,
                 knowledge_pipeline: RagPipeline | None = None,
                 knowledge_repository: KnowledgeRepository | None = None,
                 pdf_ingestion: PdfIngestionCoordinator | None = None,
                 workspace_memory_router=None,
                 task_runtime_service: TaskRuntimeService | None = None,
                 workspace_task_owner: TaskOwner | None = None, *, channel_name: str = "web",
                 root_page: str = "workspace"):
        """初始化 Web 渠道。

        Args:
            bus: 消息总线实例
            host: 监听地址
            port: 监听端口
        """
        if root_page not in {"landing", "workspace"}:
            raise ValueError("web_root_page_invalid")
        super().__init__(name=channel_name, bus=bus)
        self.host = host
        self.port = port
        self.root_page = root_page
        self._connections: dict[str, WebSocket] = {}  # client_id -> WebSocket
        self._bindings: dict[str, str] = {}  # client_id -> conversation_id
        self._active_requests: dict[str, set[str]] = {}
        self._seen_requests: dict[str, set[str]] = {}
        self._request_hashes: dict[str, dict[str, str]] = {}
        self._app: FastAPI | None = None
        self._server: uvicorn.Server | None = None
        self._knowledge_repository = knowledge_repository or KnowledgeRepository()
        self._knowledge_pipeline = knowledge_pipeline or RagPipeline()
        self._pdf_ingestion = pdf_ingestion
        self._workspace_memory_router = workspace_memory_router
        self._conversation_service = conversation_service or ConversationService()
        self._workflow_service = workflow_service or WorkflowService()
        self._workflow_service.subscribe(self._broadcast_workflow_event)
        self._task_runtime_service = task_runtime_service
        self._workspace_task_owner = workspace_task_owner
        if self._task_runtime_service is not None:
            self._task_runtime_service.subscribe(self._broadcast_task_event)
        config = load_config()
        self._multi_conversation_enabled = config.web_multi_conversation_enabled
        self._knowledge_admin_enabled = config.knowledge_admin_enabled
        self._workflow_events_enabled = config.workflow_events_enabled
        self._customer_portal_enabled = config.customer_portal_enabled
        self._customer_portal_host = config.customer_portal_host
        self._customer_portal_port = config.customer_portal_port
        self._customer_portal_public_url = config.customer_portal_public_url
        self._email_admin_token = config.email_admin_token if email_admin_token is None else email_admin_token
        origins = (config.email_admin_allowed_origins if email_admin_allowed_origins is None
                   else email_admin_allowed_origins)
        self._email_admin_allowed_origins = tuple(origin.rstrip("/") for origin in origins if origin)
        self._email_account_service = email_account_service
        self._email_delivery_service = email_delivery_service
        self._email_review_service = email_review_service
        self._email_quote_workflow_service = email_quote_workflow_service
        self._email_reviewer_id = (os.environ.get("NANOCLAW_EMAIL_REVIEWER_ID", "")
                                   if email_reviewer_id is None else email_reviewer_id)
        self._rag_observability_token = os.environ.get("RAG_OBSERVABILITY_TOKEN", "")
        self._email_smtp_worker_enabled = config.email_smtp_worker_enabled
        ingestion_config = load_email_config()
        self._email_managed_ingestion_enabled = ingestion_config.managed_accounts_enabled
        self._email_remote_extraction_approved = ingestion_config.remote_extraction_approved

    def _pdf_ingestion_coordinator(self) -> PdfIngestionCoordinator:
        coordinator = self._pdf_ingestion
        if (coordinator is None
                or coordinator.repository is not self._knowledge_repository
                or coordinator.pipeline is not self._knowledge_pipeline):
            coordinator = PdfIngestionCoordinator(
                self._knowledge_repository, self._knowledge_pipeline,
            )
            self._pdf_ingestion = coordinator
        return coordinator

    async def start(self) -> None:
        """启动 FastAPI + WebSocket 服务。"""
        self._validate_workspace_bind()
        coordinator = self._pdf_ingestion_coordinator()
        await asyncio.to_thread(coordinator.reconcile_indexes_once)
        coordinator.start()
        # 创建 FastAPI 应用
        self._app = FastAPI(title="NanoClaw Web")

        # 注册路由
        self._register_routes()

        print(f"[WebChannel] 正在启动 Web 服务: http://{self.host}:{self.port}")

        # 使用 uvicorn.Server.serve() 启动（异步方式，不阻塞事件循环）
        config = uvicorn.Config(
            self._app,
            host=self.host,
            port=self.port,
            log_level="info",
        )
        self._server = uvicorn.Server(config)
        await self._server.serve()

    def _validate_workspace_bind(self) -> None:
        if self.name == "workspace_web" and not _is_loopback_bind(self.host):
            raise ValueError("trade_workbench_requires_loopback_or_formal_operator_auth")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._pdf_ingestion is not None:
            self._pdf_ingestion.stop()

    def _register_routes(self) -> None:
        """注册 FastAPI 路由。"""
        if self._app is None:
            return

        ui_dir = Path(__file__).parent / "web_ui"
        static_dir = ui_dir / "static"
        if static_dir.exists():
            self._app.mount("/static", StaticFiles(directory=static_dir), name="static")
        if self._workspace_memory_router is not None:
            self._app.include_router(self._workspace_memory_router)
        if self.root_page == "workspace" and _is_loopback_bind(self.host):
            # The old campaign runner remains inspectable but cannot accept new writes.
            self._app.include_router(build_trade_workbench_router(read_only=True))
            if self._task_runtime_service is not None and self._workspace_task_owner is not None:
                self._app.include_router(create_workspace_task_router(
                    self._task_runtime_service, self._workspace_task_owner,
                    conversation_guard=self._conversation_service.get))

        # GET / -> 返回 index.html
        @self._app.get("/")
        async def index():
            """返回 Web UI 页面。"""
            index_path = ui_dir / (
                "landing.html" if self.root_page == "landing" else "index.html"
            )
            if index_path.exists():
                return FileResponse(index_path, media_type="text/html")
            else:
                return HTMLResponse(content="<h1>landing.html not found</h1>")

        @self._app.get("/workspace")
        async def workspace():
            page = ui_dir / "index.html"
            return FileResponse(page, media_type="text/html") if page.exists() else HTMLResponse("<h1>index.html not found</h1>", status_code=404)

        @self._app.get("/customer")
        async def customer_portal():
            page = ui_dir / "customer.html"
            return FileResponse(page, media_type="text/html") if page.exists() else HTMLResponse("<h1>customer.html not found</h1>", status_code=404)

        @self._app.get("/product")
        async def product_page():
            page = ui_dir / "product.html"
            return FileResponse(page, media_type="text/html") if page.exists() else HTMLResponse("<h1>product.html not found</h1>", status_code=404)

        @self._app.get("/api/ui/config")
        async def ui_config(request: Request):
            if self._customer_portal_public_url:
                customer_url = self._customer_portal_public_url
            else:
                host = self._customer_portal_host
                if host in {"0.0.0.0", "::", ""}:
                    host = request.url.hostname or "127.0.0.1"
                customer_url = f"{request.url.scheme}://{host}:{self._customer_portal_port}"
            return {
                "customer_portal_enabled": self._customer_portal_enabled,
                "customer_portal_url": customer_url if self._customer_portal_enabled else None,
            }

        async def bounded_upload(request: Request, max_bytes: int) -> bytes:
            length = request.headers.get("content-length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise HTTPException(status_code=413, detail="file_too_large")
            chunks: list[bytes] = []
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(status_code=413, detail="file_too_large")
                chunks.append(chunk)
            return b"".join(chunks)

        def upload_name(request: Request) -> str:
            value = request.headers.get("x-file-name", "")
            if not value:
                raise HTTPException(status_code=400, detail="missing_file_name")
            return unquote(value)

        def conversation_payload(item):
            return {
                "conversation_id": item.conversation_id, "owner_id": item.owner_id,
                "title": item.title, "channel": item.channel, "created_at": item.created_at,
                "updated_at": item.updated_at, "deleted_at": item.deleted_at, "version": item.version,
            }

        def conversation_error(exc: ConversationError):
            raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc

        def require_feature(enabled: bool, code: str) -> None:
            if not enabled:
                raise HTTPException(status_code=404, detail=code)

        def require_email_admin(request: Request) -> None:
            """Allow the local workspace without a prompt; keep remote access fail-closed."""
            client_host = (request.client.host if request.client else "").split("%", 1)[0]
            local_workspace = client_host in {"127.0.0.1", "::1"}
            if not local_workspace:
                if not self._email_admin_token:
                    raise HTTPException(
                        status_code=503,
                        detail=EmailAdminErrorCode.AUTH_NOT_CONFIGURED.value,
                    )
                scheme, separator, supplied = request.headers.get("authorization", "").partition(" ")
                if (not separator or scheme.casefold() != "bearer" or not supplied
                        or not secrets.compare_digest(supplied, self._email_admin_token)):
                    raise HTTPException(
                        status_code=401,
                        detail=EmailAdminErrorCode.UNAUTHORIZED.value,
                        headers={"WWW-Authenticate": "Bearer"},
                    )
            origin = request.headers.get("origin")
            if origin:
                request_origin = f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
                allowed = {request_origin.rstrip("/"), *self._email_admin_allowed_origins}
                if origin.rstrip("/") not in allowed:
                    raise HTTPException(
                        status_code=403,
                        detail=EmailAdminErrorCode.ORIGIN_FORBIDDEN.value,
                    )

        def require_rag_observer(request: Request) -> None:
            client_host = (request.client.host if request.client else "").split("%", 1)[0]
            if client_host in {"127.0.0.1", "::1"}:
                return
            scheme, separator, supplied = request.headers.get("authorization", "").partition(" ")
            if not self._rag_observability_token:
                raise HTTPException(status_code=503, detail="rag_observability_not_configured")
            if (not separator or scheme.casefold() != "bearer" or not supplied
                    or not secrets.compare_digest(supplied, self._rag_observability_token)):
                raise HTTPException(status_code=401, detail="rag_observability_unauthorized")

        @self._app.get("/healthz")
        async def healthz():
            return {"status": "ok", "service": "nanoclaw-web"}

        @self._app.get("/readyz")
        async def readyz(response: Response):
            store = self._knowledge_pipeline.store
            if isinstance(store, ManagedVectorStore):
                store.refresh(force=True)
            keyword_ready = True
            keyword_check = getattr(self._knowledge_pipeline.keyword_store, "check_ready", None)
            if keyword_check is not None:
                try:
                    await asyncio.to_thread(keyword_check)
                except Exception:
                    keyword_ready = False
            ready = bool(vector_runtime_state.snapshot()["ready"]) and keyword_ready
            if not ready:
                response.status_code = 503
            return {"status": "ready" if ready else "not_ready"}

        @self._app.get("/api/rag/metrics")
        async def rag_metrics(request: Request):
            require_rag_observer(request)
            return vector_runtime_state.snapshot(include_audit=True)

        @self._app.get("/metrics/rag", response_class=PlainTextResponse)
        async def rag_prometheus_metrics(request: Request):
            require_rag_observer(request)
            return vector_runtime_state.prometheus()

        def account_service() -> EmailAccountService:
            if self._email_account_service is None:
                self._email_account_service = create_default_email_account_service()
            return self._email_account_service

        def delivery_service() -> EmailDeliveryService:
            if self._email_delivery_service is None:
                self._email_delivery_service = create_default_email_delivery_service()
            return self._email_delivery_service

        def review_service() -> EmailReviewService:
            if self._email_review_service is None:
                self._email_review_service = create_default_email_review_service(self._email_reviewer_id)
            return self._email_review_service

        def quote_workflow_service() -> EmailQuoteWorkflowService:
            if self._email_quote_workflow_service is None:
                self._email_quote_workflow_service = EmailQuoteWorkflowService(
                    review_service().repository.connection
                )
            return self._email_quote_workflow_service

        def email_review_error(exc: Exception) -> None:
            if isinstance(exc, EmailReviewServiceError):
                raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
            raise HTTPException(status_code=500, detail="email_review_internal_error") from exc

        def email_delivery_error(exc: Exception) -> None:
            if isinstance(exc, EmailDeliveryServiceError):
                raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
            raise HTTPException(status_code=500, detail="email_delivery_internal_error") from exc

        def email_quote_workflow_error(exc: Exception) -> None:
            if isinstance(exc, EmailQuoteWorkflowError):
                raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
            if isinstance(exc, EmailReviewServiceError):
                raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
            raise HTTPException(status_code=500, detail="email_quote_workflow_internal_error") from exc

        async def email_json(request: Request) -> dict:
            length = request.headers.get("content-length")
            if length and length.isdigit() and int(length) > 65536:
                raise HTTPException(status_code=413, detail="email_request_too_large")
            try:
                payload = await request.json()
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise HTTPException(status_code=400, detail="email_invalid_json") from exc
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="email_invalid_account_config")
            return payload

        def email_account_error(exc: Exception):
            if isinstance(exc, EmailAccountRepositoryError):
                exc = _map_repository_error(exc)
            if isinstance(exc, EmailSecretStoreError):
                raise HTTPException(status_code=503, detail="email_secret_store_unavailable") from exc
            if isinstance(exc, EmailAccountServiceError):
                raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
            raise exc

        @self._app.get("/api/capabilities")
        async def capabilities():
            return {
                "web_multi_conversation": self._multi_conversation_enabled,
                "knowledge_admin": self._knowledge_admin_enabled,
                "workflow_events": self._workflow_events_enabled,
            }

        @self._app.get("/api/email/providers")
        async def email_providers():
            """Public, non-secret provider metadata used by the future M2 UI."""
            return public_provider_contract()

        @self._app.get("/api/email/accounts")
        async def email_accounts(request: Request):
            require_email_admin(request)
            try:
                return {"items": account_service().list_accounts()}
            except Exception as exc:
                email_account_error(exc)

        @self._app.post("/api/email/accounts", status_code=201)
        async def create_email_account(request: Request):
            require_email_admin(request)
            payload = await email_json(request)
            try:
                return account_service().create_account(payload)
            except Exception as exc:
                email_account_error(exc)

        @self._app.patch("/api/email/accounts/{account_id}")
        async def update_email_account(account_id: str, request: Request):
            require_email_admin(request)
            payload = await email_json(request)
            try:
                return account_service().update_account(account_id, payload)
            except Exception as exc:
                email_account_error(exc)

        @self._app.post("/api/email/accounts/{account_id}/test")
        async def test_email_account(account_id: str, request: Request):
            require_email_admin(request)
            try:
                return account_service().test_connection(account_id)
            except Exception as exc:
                email_account_error(exc)

        @self._app.post("/api/email/accounts/{account_id}/enable")
        async def enable_email_account(account_id: str, request: Request):
            require_email_admin(request)
            try:
                return account_service().set_enabled(account_id, True)
            except Exception as exc:
                email_account_error(exc)

        @self._app.post("/api/email/accounts/{account_id}/disable")
        async def disable_email_account(account_id: str, request: Request):
            require_email_admin(request)
            try:
                return account_service().set_enabled(account_id, False)
            except Exception as exc:
                email_account_error(exc)

        @self._app.get("/api/email/inbound")
        async def list_inbound_email(request: Request):
            require_email_admin(request)
            try:
                raw_limit = request.query_params.get("limit", "20")
                return review_service().list_reviews(
                    status=request.query_params.get("status", "all"),
                    account_id=request.query_params.get("account_id") or None,
                    cursor=request.query_params.get("cursor") or None,
                    limit=int(raw_limit),
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="email_review_invalid_request") from exc
            except Exception as exc:
                email_review_error(exc)

        @self._app.get("/api/email/inbound/{email_id}")
        async def get_inbound_email(email_id: int, request: Request, response: Response):
            require_email_admin(request)
            try:
                payload, etag = review_service().detail(email_id)
                response.headers["ETag"] = etag
                return payload
            except Exception as exc:
                email_review_error(exc)

        @self._app.post("/api/email/inbound/{email_id}/review-preview")
        async def preview_inbound_email(email_id: int, request: Request):
            require_email_admin(request)
            payload = await email_json(request)
            try:
                result = review_service().preview(email_id, payload)
                result.pop("change_meta", None)
                result.pop("base_extraction_hash", None)
                return result
            except Exception as exc:
                email_review_error(exc)

        @self._app.post("/api/email/inbound/{email_id}/confirm")
        async def confirm_inbound_email(email_id: int, request: Request):
            require_email_admin(request)
            payload = await email_json(request)
            try:
                result = review_service().confirm(
                    email_id, payload,
                    idempotency_key=request.headers.get("idempotency-key", ""),
                    if_match=request.headers.get("if-match", ""),
                )
                result["quote_workflow"] = quote_workflow_service().materialize_confirmed(
                    email_id=email_id, review_id=result["review_id"], review_hash=result["review_hash"]
                )
                return result
            except Exception as exc:
                email_quote_workflow_error(exc)

        @self._app.get("/api/email/sendable-quotes")
        async def sendable_email_quotes(request: Request):
            require_email_admin(request)
            try:
                return {"items": quote_workflow_service().list_work_items(request.query_params.get("limit", 100))}
            except Exception as exc:
                email_quote_workflow_error(exc)

        @self._app.post("/api/email/approvals/{approval_key}/decision")
        async def decide_email_quote_approval(approval_key: int, request: Request):
            require_email_admin(request)
            payload = await email_json(request)
            if set(payload) - {"action", "comment"}:
                raise HTTPException(status_code=400, detail="email_approval_invalid_request")
            try:
                return quote_workflow_service().decide(
                    approval_key,
                    action=str(payload.get("action", "")),
                    reviewer=self._email_reviewer_id or "local-business-operator",
                    comment=str(payload.get("comment", "")),
                )
            except Exception as exc:
                email_quote_workflow_error(exc)

        @self._app.post("/api/email/deliveries")
        async def create_email_delivery(request: Request):
            require_email_admin(request)
            payload = await email_json(request)
            try:
                return delivery_service().queue(payload)
            except Exception as exc:
                email_delivery_error(exc)

        @self._app.get("/api/email/deliveries")
        async def list_email_deliveries(request: Request):
            require_email_admin(request)
            try:
                return {"items": delivery_service().list_deliveries(request.query_params.get("limit", 100))}
            except Exception as exc:
                email_delivery_error(exc)

        @self._app.get("/api/email/metrics")
        async def email_delivery_metrics(request: Request):
            require_email_admin(request)
            try:
                work_items = quote_workflow_service().list_work_items(100)
                reviews = [item for item in work_items if item.get("work_status") == "awaiting_field_review"]
                sendable = [item for item in work_items if item.get("work_status") == "approved"]
                deliveries = delivery_service().list_deliveries(100)
                metrics = delivery_service().metrics()
                status_counts = metrics.get("status_counts", {})
                today = datetime.now(timezone.utc).date().isoformat()
                accepted_today = sum(
                    item.get("status") == "accepted"
                    and str(item.get("updated_at") or item.get("smtp_accepted_at") or "").startswith(today)
                    for item in deliveries
                )
                active_delivery = sum(
                    int(status_counts.get(status, 0))
                    for status in ("pending", "sending", "retry_wait")
                )
                return {
                    **metrics,
                    "agent_review_queue": len(reviews),
                    "sendable_approvals": len(sendable),
                    "active_deliveries": active_delivery,
                    "accepted_today": accepted_today,
                }
            except Exception as exc:
                email_delivery_error(exc)

        @self._app.post("/api/email/deliveries/{delivery_id}/retry")
        async def retry_email_delivery(delivery_id: str, request: Request):
            require_email_admin(request)
            try:
                return delivery_service().retry(delivery_id)
            except Exception as exc:
                email_delivery_error(exc)

        @self._app.get("/api/email/runtime")
        async def email_runtime(request: Request):
            require_email_admin(request)
            return {
                "smtp_worker_enabled": self._email_smtp_worker_enabled,
                "smtp_transport": "smtp_ssl" if self._email_smtp_worker_enabled else "disabled",
                "queue_requires_explicit_operator": True,
                "agent_smtp_tool_registered": False,
                "managed_ingestion_enabled": self._email_managed_ingestion_enabled,
                "inbound_scope": "foreign_trade_rfq_only",
                "extraction_mode": (
                    "remote_llm" if self._email_remote_extraction_approved else "deterministic_local"
                ),
            }

        @self._app.post("/api/conversations", status_code=201)
        async def create_conversation(request: Request):
            require_feature(self._multi_conversation_enabled, "multi_conversation_disabled")
            try:
                body = await request.json() if request.headers.get("content-length") not in {None, "0"} else {}
                return conversation_payload(self._conversation_service.create(body.get("title", "New conversation")))
            except ConversationError as exc:
                conversation_error(exc)

        @self._app.get("/api/conversations")
        async def list_conversations(search: str = "", offset: int = Query(0, ge=0),
                                     limit: int = Query(50, ge=1, le=100), include_deleted: bool = False):
            require_feature(self._multi_conversation_enabled, "multi_conversation_disabled")
            try:
                items, total = self._conversation_service.list(
                    search=search, offset=offset, limit=limit, include_deleted=include_deleted)
                return {"items": [conversation_payload(item) for item in items], "total": total,
                        "offset": offset, "limit": limit}
            except ConversationError as exc:
                conversation_error(exc)

        @self._app.get("/api/conversations/{conversation_id}")
        async def get_conversation(conversation_id: str):
            require_feature(self._multi_conversation_enabled, "multi_conversation_disabled")
            try:
                return conversation_payload(self._conversation_service.get(conversation_id))
            except ConversationError as exc:
                conversation_error(exc)

        @self._app.get("/api/conversations/{conversation_id}/messages")
        async def get_conversation_messages(conversation_id: str, offset: int = Query(0, ge=0),
                                            limit: int = Query(100, ge=1, le=500)):
            require_feature(self._multi_conversation_enabled, "multi_conversation_disabled")
            try:
                items, total = self._conversation_service.messages(conversation_id, offset=offset, limit=limit)
                return {"items": items, "total": total, "offset": offset, "limit": limit}
            except ConversationError as exc:
                conversation_error(exc)

        @self._app.patch("/api/conversations/{conversation_id}")
        async def rename_conversation(conversation_id: str, request: Request):
            require_feature(self._multi_conversation_enabled, "multi_conversation_disabled")
            try:
                body = await request.json()
                return conversation_payload(self._conversation_service.rename(conversation_id, body.get("title", "")))
            except ConversationError as exc:
                conversation_error(exc)

        @self._app.delete("/api/conversations/{conversation_id}")
        async def delete_conversation(conversation_id: str):
            require_feature(self._multi_conversation_enabled, "multi_conversation_disabled")
            try:
                item = self._conversation_service.delete(conversation_id)
                for client_id, bound_id in list(self._bindings.items()):
                    if bound_id == item.conversation_id:
                        self._bindings.pop(client_id, None)
                await self.bus.publish_inbound(InboundMessage(
                    channel="web", sender_id=f"local:{item.conversation_id}", chat_id="",
                    content="", raw={"event": "conversation_deleted", "conversation_id": item.conversation_id},
                ))
                return conversation_payload(item)
            except ConversationError as exc:
                conversation_error(exc)

        @self._app.post("/api/conversations/{conversation_id}/restore")
        async def restore_conversation(conversation_id: str):
            require_feature(self._multi_conversation_enabled, "multi_conversation_disabled")
            try:
                return conversation_payload(self._conversation_service.restore(conversation_id))
            except ConversationError as exc:
                conversation_error(exc)

        def workflow_error(exc: WorkflowError):
            raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc

        @self._app.get("/api/conversations/{conversation_id}/workflow-runs")
        async def list_workflow_runs(conversation_id: str):
            require_feature(self._workflow_events_enabled, "workflow_events_disabled")
            try:
                self._conversation_service.get(conversation_id)
                return {"items": [asdict(run) for run in self._workflow_service.list_runs(conversation_id)]}
            except ConversationError as exc:
                conversation_error(exc)
            except WorkflowError as exc:
                workflow_error(exc)

        @self._app.get("/api/workflow-runs/{run_id}/events")
        async def list_workflow_events(run_id: str, after_sequence: int = Query(0, ge=0)):
            require_feature(self._workflow_events_enabled, "workflow_events_disabled")
            try:
                return {"items": [asdict(event) for event in self._workflow_service.events(run_id, after_sequence)]}
            except WorkflowError as exc:
                workflow_error(exc)

        @self._app.post("/api/workflow-runs/{run_id}/cancel")
        async def cancel_workflow_run(run_id: str):
            require_feature(self._workflow_events_enabled, "workflow_events_disabled")
            try:
                return asdict(await self._workflow_service.cancel(run_id))
            except WorkflowError as exc:
                workflow_error(exc)

        @self._app.post("/api/files/read")
        async def read_file_for_conversation(request: Request):
            """Decode a bounded text file; the browser owns its conversation lifecycle."""
            max_bytes = 512 * 1024
            payload = await bounded_upload(request, max_bytes)
            try:
                name, content, content_type = decode_text_document(
                    upload_name(request), payload, max_bytes=max_bytes)
            except ValueError as exc:
                code = str(exc)
                status = 413 if code == "file_too_large" else 400
                raise HTTPException(status_code=status, detail=code) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=500, detail="knowledge_index_failed") from exc
            return {"name": name, "content": content, "content_type": content_type, "size": len(payload)}

        @self._app.post("/api/knowledge/import")
        async def import_knowledge(request: Request, response: Response):
            """Persist a validated document; PDFs continue in the durable worker."""
            require_feature(self._knowledge_admin_enabled, "knowledge_admin_disabled")
            payload = await bounded_upload(request, self._knowledge_repository.max_bytes)
            try:
                classification = request.headers.get("X-Knowledge-Classification", "internal").strip().lower()
                name = upload_name(request)
                if Path(name).suffix.lower() == ".pdf":
                    result = await asyncio.to_thread(
                        self._knowledge_repository.stage_pdf, name, payload,
                        classification=classification,
                        content_type=request.headers.get("Content-Type"),
                    )
                    if (result.get("duplicate") and "failed" in {
                            result.get("parse_status"), result.get("chunk_status"),
                            result.get("index_status")}):
                        result = await asyncio.to_thread(
                            self._knowledge_repository.prepare_pdf_retry,
                            result["document_id"],
                        )
                        result["duplicate"] = True
                    self._pdf_ingestion_coordinator().enqueue(result["document_id"])
                    response.status_code = 202
                else:
                    result = await asyncio.to_thread(
                        self._knowledge_repository.import_bytes, name, payload,
                        classification=classification,
                        content_type=request.headers.get("Content-Type"),
                    )
                duplicate = bool(result.get("duplicate", False))
                if (str(result.get("content_type", "")).startswith("image/")
                      and result.get("index_status") == "pending"
                      and result.get("status") == "published"):
                    result = await asyncio.to_thread(
                        self._knowledge_repository.index_image,
                        result["document_id"], index_document=self._knowledge_pipeline.index,
                    )
                result["duplicate"] = duplicate
            except ValueError as exc:
                code = str(exc)
                status = 413 if code == "file_too_large" else 400
                raise HTTPException(status_code=status, detail=code) from exc
            return {
                "document_id": result["document_id"],
                "name": result["original_name"],
                "status": result["status"],
                "duplicate": result["duplicate"],
                "parent_count": result.get("parent_count"),
                "child_count": result.get("child_count"),
                "index_status": result.get("index_status"),
                "classification": result.get("classification", "internal"),
                "content_type": result.get("content_type"),
                "parser": result.get("parser"),
                "parser_version": result.get("parser_version"),
                "pages": result.get("pages"),
                "text_chars": result.get("text_chars"),
                "text_page_ratio": result.get("text_page_ratio"),
                "needs_ocr": result.get("needs_ocr", False),
                "image_count": result.get("image_count", 0),
                "ocr_image_count": result.get("ocr_image_count", 0),
                "ocr_no_text_count": result.get("ocr_no_text_count", 0),
                "ingestion_route": result.get("ingestion_route"),
                "parse_status": result.get("parse_status"),
                "chunk_status": result.get("chunk_status"),
                "indexed_count": result.get("indexed_count"),
                "source_hash": result.get("source_hash"),
                "content_hash": result.get("content_hash"),
                "warnings": result.get("parse_warnings", []),
            }

        @self._app.patch("/api/knowledge/documents/{document_id}/classification")
        async def classify_knowledge_document(document_id: str, request: Request):
            """Require an explicit internal/public decision before customer retrieval."""
            require_feature(self._knowledge_admin_enabled, "knowledge_admin_disabled")
            try:
                payload = await request.json()
                classification = str(payload.get("classification", "")).strip().lower()
                result = await asyncio.to_thread(
                    self._knowledge_repository.set_classification, document_id, classification)
                if (result.get("content_type") == "application/pdf"
                        and result.get("index_status") == "indexed"):
                    await asyncio.to_thread(
                        self._knowledge_pipeline.delete_by_document,
                        document_id, int(result.get("version", 1)),
                    )
                    result = await asyncio.to_thread(
                        self._knowledge_repository.index_pdf,
                        document_id, index_prepared=self._knowledge_pipeline.index_prepared,
                    )
                elif (str(result.get("content_type", "")).startswith("image/")
                      and result.get("index_status") == "indexed"
                      and int(result.get("child_count") or 0) > 0):
                    await asyncio.to_thread(
                        self._knowledge_pipeline.delete_by_document,
                        document_id, int(result.get("version", 1)),
                    )
                    result = await asyncio.to_thread(
                        self._knowledge_repository.index_image,
                        document_id, index_document=self._knowledge_pipeline.index,
                    )
                return {"document_id": document_id, "classification": result["classification"]}
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except (ValueError, AttributeError, TypeError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=500, detail="knowledge_index_failed") from exc

        @self._app.get("/api/knowledge/documents")
        async def list_knowledge_documents(search: str = "", status: str = "",
                                           offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                                           include_deleted: bool = False):
            require_feature(self._knowledge_admin_enabled, "knowledge_admin_disabled")
            try:
                items, total, summary = self._knowledge_repository.list_documents(
                    search=search, status=status, offset=offset, limit=limit,
                    include_deleted=include_deleted)
                safe_items = [public_document_record(item) for item in items]
                return {"items": safe_items, "total": total, "summary": summary,
                        "offset": offset, "limit": limit}
            except RuntimeError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc

        @self._app.get("/api/knowledge/documents/{document_id}")
        async def get_knowledge_document(document_id: str):
            require_feature(self._knowledge_admin_enabled, "knowledge_admin_disabled")
            try:
                return self._knowledge_repository.get_document(document_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="knowledge_document_not_found") from exc

        @self._app.get("/api/knowledge/documents/{document_id}/chunks-preview")
        async def preview_knowledge_document_chunks(
                document_id: str, limit: int = Query(12, ge=1, le=50)):
            """Return a bounded PDF chunk preview without storage paths or full-document export."""
            require_feature(self._knowledge_admin_enabled, "knowledge_admin_disabled")
            try:
                return await asyncio.to_thread(
                    self._knowledge_repository.preview_pdf_chunks,
                    document_id, limit=limit,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="knowledge_document_not_found") from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc

        @self._app.get("/api/knowledge/documents/{document_id}/images")
        async def preview_knowledge_document_images(
                document_id: str, limit: int = Query(100, ge=1, le=300)):
            """Return bounded OCR image records with stable indexes and no storage paths."""
            require_feature(self._knowledge_admin_enabled, "knowledge_admin_disabled")
            try:
                return self._knowledge_repository.preview_images(document_id, limit=limit)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="knowledge_document_not_found") from exc

        @self._app.post("/api/knowledge/documents/{document_id}/approve-review")
        async def approve_knowledge_document_review(document_id: str):
            """Approve a safe manual-review item, then publish its OCR/text chunks."""
            require_feature(self._knowledge_admin_enabled, "knowledge_admin_disabled")
            try:
                current = await asyncio.to_thread(
                    self._knowledge_repository.approve_review, document_id
                )
                if current.get("content_type") == "application/pdf":
                    item = await asyncio.to_thread(
                        self._knowledge_repository.index_pdf,
                        document_id,
                        index_prepared=self._knowledge_pipeline.index_prepared,
                    )
                else:
                    item = await asyncio.to_thread(
                        self._knowledge_repository.index_image,
                        document_id,
                        index_document=self._knowledge_pipeline.index,
                    )
                return public_document_record(item)
            except KeyError as exc:
                raise HTTPException(
                    status_code=404, detail="knowledge_document_not_found"
                ) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc

        @self._app.delete("/api/knowledge/documents/{document_id}")
        async def delete_knowledge_document(document_id: str):
            require_feature(self._knowledge_admin_enabled, "knowledge_admin_disabled")
            try:
                item = self._knowledge_repository.revoke(
                    document_id, delete_index=self._knowledge_pipeline.delete_by_document)
                return public_document_record(item)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="knowledge_document_not_found") from exc

        @self._app.post("/api/knowledge/documents/{document_id}/retry-index")
        async def retry_knowledge_index(document_id: str, response: Response):
            require_feature(self._knowledge_admin_enabled, "knowledge_admin_disabled")
            try:
                current = self._knowledge_repository.get_document(document_id)
                if current.get("status") == "revoke_pending":
                    item = self._knowledge_repository.retry_revoke(
                        document_id, delete_index=self._knowledge_pipeline.delete_by_document)
                elif (current.get("content_type") == "application/pdf"
                      and "failed" in {current.get("parse_status"), current.get("chunk_status"),
                                       current.get("index_status")}):
                    item = self._knowledge_repository.prepare_pdf_retry(document_id)
                    self._pdf_ingestion_coordinator().enqueue(document_id)
                    response.status_code = 202
                else:
                    item = self._knowledge_repository.retry_index(
                        document_id, index_document=(
                            self._knowledge_pipeline.index_prepared
                            if current.get("content_type") == "application/pdf"
                            else self._knowledge_pipeline.index
                        ))
                return public_document_record(item)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="knowledge_document_not_found") from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        # WebSocket /ws
        @self._app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            """WebSocket 连接处理。"""
            # 接收连接
            await websocket.accept()

            # 生成 client_id
            client_id = str(uuid.uuid4())
            self._connections[client_id] = websocket

            print(f"[WebChannel] 新连接: client_id={client_id}")

            try:
                # 循环接收消息
                while True:
                    data = await websocket.receive_text()

                    # 跳过空消息
                    if not data.strip():
                        continue

                    event = "message"
                    conversation_id = self._bindings.get(client_id)
                    request_id = None
                    try:
                        payload = json.loads(data)
                        if isinstance(payload, dict) and payload.get("type") == "conversation.bind":
                            if payload.get("protocol_version") != 2:
                                await websocket.send_json({"type": "error", "protocol_version": 2, "code": "unsupported_protocol"})
                                continue
                            try:
                                item = self._conversation_service.get(str(payload.get("conversation_id", "")))
                            except ConversationError as exc:
                                await websocket.send_json({"type": "error", "protocol_version": 2, "code": exc.code})
                                continue
                            conversation_id = item.conversation_id
                            self._bindings[client_id] = conversation_id
                            await websocket.send_json({"type": "conversation.bound", "protocol_version": 2,
                                                       "conversation_id": conversation_id})
                            if self._workflow_events_enabled:
                                await websocket.send_json(self._workflow_service.snapshot(conversation_id))
                            if self._task_runtime_service is not None and self._workspace_task_owner is not None:
                                await websocket.send_json(self._task_runtime_service.snapshot(
                                    self._workspace_task_owner, conversation_id))
                            continue
                        if isinstance(payload, dict) and payload.get("type") == "chat.message":
                            if payload.get("protocol_version") != 2 or payload.get("conversation_id") != conversation_id:
                                await websocket.send_json({"type": "error", "protocol_version": 2, "code": "conversation_not_bound"})
                                continue
                            request_id = str(payload.get("request_id", ""))
                            content = payload.get("content")
                            if not request_id or not isinstance(content, str) or not content.strip():
                                await websocket.send_json({"type": "error", "protocol_version": 2, "code": "invalid_message"})
                                continue
                            seen = self._seen_requests.setdefault(conversation_id, set())
                            digest = hashlib.sha256(content.strip().encode("utf-8")).hexdigest()
                            if request_id in seen:
                                expected = self._request_hashes.get(conversation_id, {}).get(request_id)
                                event = ({"type": "error", "protocol_version": 2,
                                          "code": "request_id_conflict"}
                                         if expected is not None and expected != digest else
                                         {"type": "chat.duplicate", "protocol_version": 2})
                                await websocket.send_json({**event, "conversation_id": conversation_id,
                                                           "request_id": request_id})
                                continue
                            seen.add(request_id)
                            self._request_hashes.setdefault(conversation_id, {})[request_id] = digest
                            self._active_requests.setdefault(client_id, set()).add(request_id)
                            selected_task_id = payload.get("task_id")
                            if self._task_runtime_service is not None and self._workspace_task_owner is not None:
                                try:
                                    binding = await self._task_runtime_service.bind_conversation_message(
                                        self._workspace_task_owner, conversation_id, content.strip(),
                                        str(selected_task_id) if selected_task_id else None,
                                        idempotency_key=request_id)
                                except TaskRuntimeError as exc:
                                    await websocket.send_json({"type": "error", "protocol_version": 2,
                                        "conversation_id": conversation_id, "request_id": request_id,
                                        "code": exc.code})
                                    self._active_requests.get(client_id, set()).discard(request_id)
                                    continue
                                if binding:
                                    language = str(payload.get("language", "zh"))
                                    binding_type = str(binding.get("binding"))
                                    acknowledgement = self._task_runtime_service.acknowledgement(
                                        binding_type, language)
                                    self._conversation_service.append_message(
                                        conversation_id, role="user", content=content.strip())
                                    self._conversation_service.append_message(
                                        conversation_id, role="assistant", content=acknowledgement)
                                    if binding_type == "selection_required":
                                        await websocket.send_json({"type": "task.selection_required",
                                            "protocol_version": 2, "conversation_id": conversation_id,
                                            "request_id": request_id, "task_ids": binding["task_ids"]})
                                    else:
                                        await websocket.send_json(self._task_runtime_service.snapshot(
                                            self._workspace_task_owner, conversation_id))
                                    await websocket.send_text(encode_assistant_message(
                                        acknowledgement, conversation_id=conversation_id,
                                        request_id=request_id))
                                    self._active_requests.get(client_id, set()).discard(request_id)
                                    continue
                            data = content
                        elif isinstance(payload, dict) and payload.get("type") == "clear_conversation":
                            # Compatibility event no longer deletes persistent history.
                            await websocket.send_json({"type": "error", "protocol_version": 2, "code": "clear_conversation_retired"})
                            continue
                    except json.JSONDecodeError:
                        pass

                    if conversation_id is None:
                        await websocket.send_json({"type": "error", "protocol_version": 2, "code": "conversation_not_bound"})
                        continue

                    try:
                        self._conversation_service.touch(conversation_id)
                    except ConversationError as exc:
                        await websocket.send_json({"type": "error", "protocol_version": 2, "code": exc.code})
                        continue

                    print(f"[WebChannel] 收到事件: client_id={client_id} event={event}")

                    # 构造入站消息（chat_id = client_id，用于找回连接发送回复）
                    message = InboundMessage(
                        channel=self.name,
                        sender_id=f"local:{conversation_id}",
                        chat_id=client_id,
                        content=data,
                        raw={
                            "client_id": client_id,
                            "event": event,
                            "conversation_id": conversation_id,
                            "request_id": request_id,
                        },
                    )

                    # 发布到总线
                    await self.bus.publish_inbound(message)

            except Exception as e:
                # 连接断开
                print(f"[WebChannel] 连接断开: client_id={client_id} reason={e}")

            finally:
                # 清理连接
                self._connections.pop(client_id, None)
                self._bindings.pop(client_id, None)
                self._active_requests.pop(client_id, None)

    async def send(self, message: OutboundMessage) -> None:
        """发送消息到 WebSocket 客户端。

        Args:
            message: 出站消息实例
        """
        # 通过 chat_id 找回 WebSocket 连接
        ws = self._connections.get(message.chat_id)

        if ws is None:
            # 连接不存在（用户已断开）
            print(f"[WebChannel] 连接不存在: chat_id={message.chat_id}")
            return

        if message.conversation_id and self._bindings.get(message.chat_id) != message.conversation_id:
            print(f"[WebChannel] 忽略非当前对话回复: chat_id={message.chat_id}")
            return
        if message.request_id and message.request_id not in self._active_requests.get(message.chat_id, set()):
            print(f"[WebChannel] 忽略非当前请求回复: chat_id={message.chat_id}")
            return

        try:
            if message.event_type != "assistant.message":
                payload = {
                    "type": message.event_type, "protocol_version": 2,
                    "conversation_id": message.conversation_id,
                    "request_id": message.request_id,
                }
                if message.error_code:
                    payload["code"] = message.error_code
                await ws.send_json(payload)
            else:
                await ws.send_text(encode_assistant_message(
                    message.content, conversation_id=message.conversation_id,
                    request_id=message.request_id,
                ))
            print(f"[WebChannel] 消息已发送: chat_id={message.chat_id}")
            if message.request_id:
                active = self._active_requests.get(message.chat_id)
                if active is not None:
                    active.discard(message.request_id)
                    if not active: self._active_requests.pop(message.chat_id, None)

        except Exception as e:
            # 发送失败，清理连接
            print(f"[WebChannel] 发送失败: chat_id={message.chat_id} reason={e}")
            self._connections.pop(message.chat_id, None)

    async def _broadcast_workflow_event(self, event) -> None:
        """Broadcast only to sockets currently bound to the event conversation."""
        if not self._workflow_events_enabled:
            return
        payload = {"type": "workflow.event", "protocol_version": 2,
                   "conversation_id": event.conversation_id, "event": asdict(event)}
        for client_id, websocket in list(self._connections.items()):
            if self._bindings.get(client_id) != event.conversation_id:
                continue
            try:
                await websocket.send_json(payload)
            except Exception:
                self._connections.pop(client_id, None)
                self._bindings.pop(client_id, None)

    async def _broadcast_task_event(self, event) -> None:
        """Task events are scoped to the socket's currently bound conversation."""
        if self._task_runtime_service is None:
            return
        try:
            task = self._task_runtime_service.repository.get_task(event.task_id)
        except Exception:
            return
        if (self._workspace_task_owner is None
                or task["tenant_id"] != self._workspace_task_owner.tenant_id):
            return
        payload = {"type": "task.event", "protocol_version": 2,
                   "conversation_id": task["conversation_id"], "event": asdict(event)}
        for client_id, websocket in list(self._connections.items()):
            if self._bindings.get(client_id) != task["conversation_id"]:
                continue
            try:
                await websocket.send_json(payload)
            except Exception:
                self._connections.pop(client_id, None)
                self._bindings.pop(client_id, None)

    async def stop(self) -> None:
        """停止 Web 服务。"""
        if self._server is not None:
            # 通知 server 停止
            self._server.should_exit = True
            self._server = None

        # 关闭所有 WebSocket 连接
        for client_id, ws in list(self._connections.items()):
            try:
                await ws.close()
            except Exception:
                pass
        self._connections.clear()
        self._bindings.clear()

        print("[WebChannel] 服务已停止")
