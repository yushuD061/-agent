"""Persistent, privacy-safe workflow run/event observation for trade tools."""

from __future__ import annotations

import inspect
import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable


TRADE_NODES = [
    "extract_rfq", "search_product", "check_inventory", "calculate_quote",
    "create_quote", "approve_message", "create_followup",
]
ANALYTICS_NODES = ["intent", "contract", "authorize", "execute", "audit"]
ALIASES = {
    "extract_rfq": "extract_rfq", "search_product": "search_product",
    "search_products": "search_product", "check_inventory": "check_inventory",
    "calculate_quote": "calculate_quote", "create_quote": "create_quote",
    "create_quote_version": "create_quote", "approve_message": "approve_message",
    "approve_quote": "approve_message", "create_followup": "create_followup",
    "schedule_followup": "create_followup",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class WorkflowRun:
    run_id: str
    conversation_id: str
    owner_id: str = "local"
    workflow_type: str = "foreign_trade_quote"
    status: str = "running"
    current_node: str = "extract_rfq"
    completed_nodes: list[str] = field(default_factory=list)
    waiting_reason: str | None = None
    last_error_code: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    last_sequence: int = 0
    version: int = 1


@dataclass
class WorkflowEvent:
    run_id: str
    conversation_id: str
    sequence: int
    type: str
    node: str | None
    status: str
    occurred_at: str
    safe_data: dict[str, Any] = field(default_factory=dict)
    protocol_version: int = 2


class WorkflowError(Exception):
    def __init__(self, code: str, status_code: int = 400):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


Listener = Callable[[WorkflowEvent], Awaitable[None] | None]


class WorkflowService:
    """Atomic run index plus append-only event logs for one local process."""

    def __init__(self, root: str | Path = "workspace/workflows") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "runs.json"
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []
        if not self.index_path.exists():
            self._write_runs([])

    def subscribe(self, listener: Listener) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def _read_runs(self) -> list[WorkflowRun]:
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
            return [WorkflowRun(**item) for item in payload.get("runs", [])]
        except (OSError, ValueError, TypeError):
            raise WorkflowError("workflow_index_invalid", 500)

    def _write_runs(self, runs: list[WorkflowRun]) -> None:
        temporary = self.index_path.with_suffix(f".tmp-{uuid.uuid4().hex}")
        temporary.write_text(json.dumps({"schema_version": 1, "runs": [asdict(run) for run in runs]},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.index_path)

    @staticmethod
    def _conversation_id(session_key: str) -> str | None:
        parts = session_key.split(":")
        if len(parts) == 3 and parts[:2] == ["web", "local"]:
            try:
                return str(uuid.UUID(parts[2]))
            except ValueError:
                return None
        return None

    @staticmethod
    def _node(tool_name: str) -> str | None:
        bare = tool_name.rsplit("__", 1)[-1]
        return ALIASES.get(bare)

    def _find(self, runs: list[WorkflowRun], run_id: str) -> WorkflowRun:
        try:
            normalized = str(uuid.UUID(run_id))
        except (ValueError, TypeError):
            raise WorkflowError("workflow_run_not_found", 404)
        for run in runs:
            if run.run_id == normalized and run.owner_id == "local":
                return run
        raise WorkflowError("workflow_run_not_found", 404)

    def list_runs(self, conversation_id: str) -> list[WorkflowRun]:
        try:
            normalized = str(uuid.UUID(conversation_id))
        except ValueError:
            raise WorkflowError("conversation_not_found", 404)
        with self._lock:
            runs = [run for run in self._read_runs() if run.conversation_id == normalized]
        return sorted(runs, key=lambda run: run.updated_at, reverse=True)

    def events(self, run_id: str, after_sequence: int = 0) -> list[WorkflowEvent]:
        with self._lock:
            run = self._find(self._read_runs(), run_id)
            path = self.root / f"{run.run_id}.jsonl"
            result: list[WorkflowEvent] = []
            if path.is_file():
                try:
                    for line in path.read_text(encoding="utf-8").splitlines():
                        try:
                            event = WorkflowEvent(**json.loads(line))
                            if event.sequence > after_sequence:
                                result.append(event)
                        except (ValueError, TypeError):
                            continue
                except OSError:
                    raise WorkflowError("workflow_events_unavailable", 500)
            return result

    def snapshot(self, conversation_id: str) -> dict[str, Any]:
        return {"type": "workflow.snapshot", "protocol_version": 2,
                "conversation_id": conversation_id,
                "runs": [asdict(run) for run in self.list_runs(conversation_id)]}

    async def _append(self, run: WorkflowRun, event_type: str, node: str | None,
                      status: str, safe_data: dict[str, Any] | None = None) -> WorkflowEvent:
        with self._lock:
            runs = self._read_runs()
            stored = self._find(runs, run.run_id)
            stored.last_sequence += 1
            stored.updated_at = _now()
            event = WorkflowEvent(stored.run_id, stored.conversation_id, stored.last_sequence,
                                  event_type, node, status, stored.updated_at, safe_data or {})
            path = self.root / f"{stored.run_id}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
            self._write_runs(runs)
            run.__dict__.update(stored.__dict__)
        for listener in list(self._listeners):
            try:
                value = listener(event)
                if inspect.isawaitable(value):
                    await value
            except Exception:
                continue
        return event

    async def observe_tool(self, session_key: str, tool_name: str, result_text: str) -> None:
        conversation_id, node = self._conversation_id(session_key), self._node(tool_name)
        if not conversation_id or not node:
            return
        try:
            result = json.loads(result_text)
        except (ValueError, TypeError):
            result = {"error": "tool_failed", "error_code": "tool_result_invalid"} if str(result_text).startswith(("错误:", "Error:")) else {}
        with self._lock:
            runs = self._read_runs()
            active = [run for run in runs if run.conversation_id == conversation_id and run.status in {"running", "waiting_confirmation"}]
            if node == "extract_rfq" or not active:
                run = WorkflowRun(str(uuid.uuid4()), conversation_id)
                runs.append(run)
                self._write_runs(runs)
                created = True
            else:
                run = sorted(active, key=lambda item: item.updated_at)[-1]
                created = False
        if created:
            await self._append(run, "workflow.run.created", node, "running")
        await self._append(run, "workflow.node.started", node, "running")

        is_error = isinstance(result, dict) and bool(result.get("error"))
        missing = []
        if node == "extract_rfq" and isinstance(result, dict):
            extraction = result.get("extraction") or {}
            missing = extraction.get("missing_fields") or []
        with self._lock:
            runs = self._read_runs()
            stored = self._find(runs, run.run_id)
            stored.current_node = node
            if is_error:
                stored.status = "failed"
                stored.last_error_code = str(result.get("error_code") or "tool_failed")[:80]
            elif missing:
                stored.status = "waiting_confirmation"
                stored.waiting_reason = "missing_required_fields"
            else:
                if node not in stored.completed_nodes:
                    stored.completed_nodes.append(node)
                stored.status = "completed" if node == "create_followup" else "running"
                stored.waiting_reason = None
            stored.updated_at = _now()
            self._write_runs(runs)
            run = stored
        if is_error:
            await self._append(run, "workflow.node.failed", node, "failed",
                               {"error_code": run.last_error_code})
        elif missing:
            await self._append(run, "workflow.node.waiting_confirmation", node, "waiting_confirmation",
                               {"missing_field_count": len(missing)})
        else:
            safe = {key: result[key] for key in ("rfq_id", "quote_id", "version", "followup_id")
                    if isinstance(result, dict) and key in result and isinstance(result[key], (str, int))}
            await self._append(run, "workflow.node.completed", node, run.status, safe)
            if run.status == "completed":
                await self._append(run, "workflow.run.completed", node, "completed")

    async def cancel(self, run_id: str) -> WorkflowRun:
        with self._lock:
            runs = self._read_runs()
            run = self._find(runs, run_id)
            if run.status in {"completed", "failed", "cancelled"}:
                raise WorkflowError("workflow_not_cancellable", 409)
            run.status = "cancelled"
            run.updated_at = _now()
            self._write_runs(runs)
        await self._append(run, "workflow.run.cancelled", run.current_node, "cancelled")
        return run

    async def observe_analytics(self, session_key: str, result_text: str) -> None:
        """Record the deterministic M4 fixed-query state machine."""
        conversation_id = self._conversation_id(session_key)
        if not conversation_id:
            return
        try:
            result = json.loads(result_text)
        except (ValueError, TypeError):
            result = {"error": "tool_result_invalid", "error_code": "tool_result_invalid"}
        run = WorkflowRun(str(uuid.uuid4()), conversation_id, workflow_type="trade_data_analysis",
                          current_node="intent")
        with self._lock:
            runs = self._read_runs()
            runs.append(run)
            self._write_runs(runs)
        await self._append(run, "workflow.run.created", "intent", "running")
        for node in ANALYTICS_NODES[:3]:
            await self._append(run, "workflow.node.started", node, "running")
            with self._lock:
                runs = self._read_runs(); stored = self._find(runs, run.run_id)
                stored.current_node = node
                if node not in stored.completed_nodes:
                    stored.completed_nodes.append(node)
                self._write_runs(runs); run = stored
            await self._append(run, "workflow.node.completed", node, "running")
        if result.get("error"):
            with self._lock:
                runs = self._read_runs(); stored = self._find(runs, run.run_id)
                stored.current_node = "execute"; stored.status = "failed"
                stored.last_error_code = str(result.get("error_code") or "query_failed")[:80]
                self._write_runs(runs); run = stored
            await self._append(run, "workflow.node.failed", "execute", "failed",
                               {"error_code": run.last_error_code})
            return
        for node in ANALYTICS_NODES[3:]:
            await self._append(run, "workflow.node.started", node, "running")
            with self._lock:
                runs = self._read_runs(); stored = self._find(runs, run.run_id)
                stored.current_node = node
                if node not in stored.completed_nodes:
                    stored.completed_nodes.append(node)
                stored.status = "completed" if node == "audit" else "running"
                self._write_runs(runs); run = stored
            safe = {}
            if node == "audit":
                safe = {key: result[key] for key in ("query_id", "query_code", "sql_hash", "row_count")
                        if key in result}
            await self._append(run, "workflow.node.completed", node, run.status, safe)
        await self._append(run, "workflow.run.completed", "audit", "completed")
