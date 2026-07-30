"""Single-worker, restart-safe PDF ingestion coordination for the local Web services."""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

from .knowledge_repository import KnowledgeRepository


class PdfIngestionCoordinator:
    """Own one daemon worker shared by both local Web listeners."""

    def __init__(self, repository: KnowledgeRepository, pipeline: Any) -> None:
        self.repository = repository
        self.pipeline = pipeline
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._queued: set[str] = set()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._reconcile_lock = threading.Lock()
        self._reconciled = False

    def reconcile_indexes_once(self) -> None:
        """Run the shared startup reconciliation once for both local Web listeners."""
        with self._reconcile_lock:
            if self._reconciled:
                return
            from .config import load_rag_keyword_config, load_rag_vector_config
            vector_config = load_rag_vector_config()
            keyword_config = load_rag_keyword_config()
            if vector_config.backend == "sqlite" and keyword_config.backend == "sqlite":
                from .sqlite_index import reconcile_sqlite_index
                reconcile_sqlite_index(
                    self.repository, self.pipeline, apply=True,
                )
            self._reconciled = True

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(
                target=self._run, name="nanoclaw-pdf-ingestion", daemon=True,
            )
            self._thread.start()
        for document_id in self.repository.list_pdf_processing_candidates():
            self.enqueue(document_id)

    def enqueue(self, document_id: str) -> bool:
        self.start()
        with self._lock:
            if self._stopping or document_id in self._queued:
                return False
            self._queued.add(document_id)
            self._queue.put(document_id)
            return True

    def _run(self) -> None:
        while True:
            document_id = self._queue.get()
            if document_id is None:
                self._queue.task_done()
                return
            try:
                self._process(document_id)
            finally:
                with self._lock:
                    self._queued.discard(document_id)
                self._queue.task_done()
            with self._lock:
                if self._stopping:
                    return

    def _process(self, document_id: str) -> None:
        try:
            current = self.repository.get_document(document_id)
            if current.get("status") in {"revoked", "revoke_pending", "review_required"}:
                return
            if (current.get("parse_status") != "ready"
                    or current.get("chunk_status") != "ready"):
                current = self.repository.process_staged_pdf(document_id)
            if current.get("status") in {"revoked", "revoke_pending", "review_required"}:
                return
            if (current.get("parse_status") == "ready"
                    and current.get("chunk_status") == "ready"
                    and current.get("ingestion_route") == "index"
                    and current.get("index_status") != "indexed"):
                current = self.repository.index_pdf(
                    document_id, index_prepared=self.pipeline.index_prepared,
                )
                if current.get("status") != "published":
                    self.pipeline.delete_by_document(
                        document_id, int(current.get("version", 1)),
                    )
        except (KeyError, ValueError):
            # Revocation and explicit review can race a running worker; both are terminal here.
            return
        except Exception:
            # Repository stage methods persist a safe error code before propagating.
            return

    def stop(self) -> None:
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            self._queue.put(None)
            thread = self._thread
        if thread is not None:
            thread.join(timeout=0.2)

    def wait_for_idle(self, timeout: float = 10.0) -> bool:
        """Bounded test/diagnostic helper; production callers should poll document state."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._queued and self._queue.unfinished_tasks == 0:
                    return True
            time.sleep(0.01)
        return False
