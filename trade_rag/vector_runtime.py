from __future__ import annotations

import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone

from .config import RagVectorConfig, load_rag_vector_config
from .vector_store import create_vector_store


class VectorBackendUnavailable(RuntimeError):
    pass


class VectorRuntimeState:
    """Process-local, content-free observability for the configured vector backend."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "_lock", threading.Lock()):
            self.backend = "unknown"
            self.ready = False
            self.error_code = "not_checked"
            self.last_check_at = ""
            self.last_success_at = ""
            self.operations: Counter[str] = Counter()
            self.failures: Counter[str] = Counter()
            self.total_duration_ms: Counter[str] = Counter()
            self.audit = deque(maxlen=100)

    def checked(self, backend: str, ready: bool, error_code: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.backend = backend if backend in {"memory", "sqlite", "pgvector", "milvus"} else "invalid"
            self.ready = ready
            self.error_code = error_code if not ready else ""
            self.last_check_at = now
            if ready:
                self.last_success_at = now
            self.audit.append({
                "timestamp": now, "event": "readiness_check", "backend": self.backend,
                "outcome": "ready" if ready else "not_ready",
                "error_code": self.error_code,
            })

    def operation(self, name: str, duration_ms: float, *, outcome: str,
                  error_code: str = "") -> None:
        if name not in {"upsert", "search", "delete"}:
            name = "unknown"
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.operations[name] += 1
            self.total_duration_ms[name] += max(0, int(duration_ms))
            if outcome != "success":
                self.failures[name] += 1
            self.audit.append({
                "timestamp": now, "event": "vector_operation", "backend": self.backend,
                "operation": name, "outcome": outcome, "error_code": error_code,
            })

    def snapshot(self, *, include_audit: bool = False) -> dict[str, object]:
        with self._lock:
            payload: dict[str, object] = {
                "backend": self.backend,
                "ready": self.ready,
                "error_code": self.error_code,
                "last_check_at": self.last_check_at,
                "last_success_at": self.last_success_at,
                "operations_total": dict(self.operations),
                "operation_failures_total": dict(self.failures),
                "operation_duration_ms_total": dict(self.total_duration_ms),
            }
            if include_audit:
                payload["audit_tail"] = list(self.audit)
            return payload

    def prometheus(self) -> str:
        snapshot = self.snapshot()
        backend = snapshot["backend"]
        lines = [
            "# HELP nanoclaw_vector_backend_ready Whether the configured vector backend is ready.",
            "# TYPE nanoclaw_vector_backend_ready gauge",
            f'nanoclaw_vector_backend_ready{{backend="{backend}"}} {1 if snapshot["ready"] else 0}',
            "# HELP nanoclaw_vector_operations_total Vector operations attempted.",
            "# TYPE nanoclaw_vector_operations_total counter",
        ]
        operations = snapshot["operations_total"]
        failures = snapshot["operation_failures_total"]
        for name in ("upsert", "search", "delete"):
            lines.append(f'nanoclaw_vector_operations_total{{backend="{backend}",operation="{name}"}} {operations.get(name, 0)}')
            lines.append(f'nanoclaw_vector_operation_failures_total{{backend="{backend}",operation="{name}"}} {failures.get(name, 0)}')
        return "\n".join(lines) + "\n"


vector_runtime_state = VectorRuntimeState()


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return "configuration_invalid"
    if isinstance(exc, RuntimeError):
        return "backend_not_ready"
    return "backend_unavailable"


class ManagedVectorStore:
    """Keeps the application alive without falling back to a different data backend."""

    def __init__(self, config: RagVectorConfig | None = None, *, retry_seconds: float = 5.0):
        self.config = config
        self.retry_seconds = retry_seconds
        self._delegate = None
        self._last_attempt = 0.0
        self.refresh(force=True)

    @property
    def backend(self) -> str:
        return self.config.backend if self.config is not None else vector_runtime_state.backend

    @property
    def _entries(self):
        """Compatibility view for existing in-memory diagnostics; remote stores stay opaque."""
        if self._delegate is not None and hasattr(self._delegate, "_entries"):
            return self._delegate._entries
        raise AttributeError("remote vector stores do not expose entries")

    def refresh(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self._last_attempt < self.retry_seconds:
            return self._delegate is not None
        self._last_attempt = now
        try:
            if self.config is None:
                self.config = load_rag_vector_config()
            self._delegate = create_vector_store(self.config)
            vector_runtime_state.checked(self.config.backend, True)
            return True
        except Exception as exc:
            backend = getattr(self.config, "backend", "invalid")
            self._delegate = None
            vector_runtime_state.checked(backend, False, _error_code(exc))
            return False

    def _call(self, operation: str, *args, **kwargs):
        if self._delegate is None and not self.refresh():
            vector_runtime_state.operation(operation, 0, outcome="failure",
                                           error_code="backend_not_ready")
            raise VectorBackendUnavailable("vector_backend_unavailable")
        started = time.perf_counter()
        try:
            result = getattr(self._delegate, operation if operation != "delete" else "delete_by_document")(
                *args, **kwargs)
            vector_runtime_state.operation(operation, (time.perf_counter() - started) * 1000,
                                           outcome="success")
            return result
        except ValueError:
            vector_runtime_state.operation(operation, (time.perf_counter() - started) * 1000,
                                           outcome="rejected", error_code="request_invalid")
            raise
        except Exception as exc:
            self._delegate = None
            vector_runtime_state.checked(self.backend, False, _error_code(exc))
            vector_runtime_state.operation(operation, (time.perf_counter() - started) * 1000,
                                           outcome="failure", error_code=_error_code(exc))
            raise VectorBackendUnavailable("vector_backend_unavailable") from exc

    def check_ready(self) -> None:
        if not self.refresh(force=True):
            raise VectorBackendUnavailable("vector_backend_unavailable")

    def upsert(self, children, source, vectors) -> int:
        return self._call("upsert", children, source, vectors)

    def search(self, vector, actor, limit: int = 30):
        return self._call("search", vector, actor, limit)

    def delete_by_document(self, document_id: str, version: int | None = None) -> int:
        return self._call("delete", document_id, version)


def create_application_vector_store(config: RagVectorConfig | None = None) -> ManagedVectorStore:
    return ManagedVectorStore(config)
