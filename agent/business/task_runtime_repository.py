"""Durable authority for human-intervenable NanoClaw task runs.

The repository owns state transitions, monotonic events, leases and immutable
checkpoints.  Agent prompts and browser state are deliberately not authoritative.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Callable, Iterator, Mapping, Protocol, runtime_checkable
import uuid

from agent.business.config import load_business_config


TASK_STATUSES = frozenset({
    "queued", "running", "pause_requested", "paused", "waiting_input",
    "waiting_review", "replanning", "retry_wait", "failed", "completed", "cancelled",
})
TERMINAL_TASK_STATUSES = frozenset({"completed", "cancelled"})
STEP_STATUSES = frozenset({
    "pending", "running", "waiting_input", "waiting_review", "retry_wait",
    "failed", "completed", "cancelled", "superseded",
})

TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "paused", "replanning", "cancelled"}),
    "running": frozenset({
        "queued", "pause_requested", "paused", "waiting_input", "waiting_review",
        "retry_wait", "failed", "completed", "cancelled", "replanning",
    }),
    "pause_requested": frozenset({"queued", "paused", "cancelled", "replanning"}),
    "paused": frozenset({"queued", "cancelled", "replanning"}),
    "waiting_input": frozenset({"replanning", "cancelled"}),
    "waiting_review": frozenset({"queued", "failed", "replanning", "cancelled"}),
    "replanning": frozenset({"queued", "waiting_input", "waiting_review", "cancelled"}),
    "retry_wait": frozenset({"running", "queued", "paused", "failed", "cancelled", "replanning"}),
    "failed": frozenset({"queued", "cancelled", "replanning"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid.uuid4())


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class TaskRuntimeError(RuntimeError):
    def __init__(self, code: str, status_code: int = 400):
        super().__init__(code)
        self.code, self.status_code = code, status_code


@dataclass(frozen=True)
class TaskOwner:
    tenant_id: str
    actor_type: str
    actor_id: str
    customer_account_id: str | None = None

    @property
    def is_customer(self) -> bool:
        return self.actor_type == "customer"


@dataclass(frozen=True)
class TaskEvent:
    event_id: str
    task_id: str
    sequence: int
    type: str
    status: str
    step_key: str | None
    safe_data: dict[str, Any]
    internal_data: dict[str, Any]
    occurred_at: str


@dataclass(frozen=True)
class ClaimedStep:
    task_id: str
    step_id: str
    plan_version: int
    step_key: str
    executor: str
    risk_level: str
    audience: str
    context: dict[str, Any]
    attempt_id: str
    attempt_no: int


@runtime_checkable
class TaskRuntimeRepository(Protocol):
    def create_task(self, owner: TaskOwner, conversation_id: str, title: str,
                    context: Mapping[str, Any], plan: list[dict[str, Any]]) -> dict[str, Any]: ...
    def get_task(self, task_id: str, owner: TaskOwner | None = None) -> dict[str, Any]: ...
    def list_tasks(self, owner: TaskOwner, conversation_id: str | None = None,
                   limit: int = 100) -> list[dict[str, Any]]: ...
    def events(self, task_id: str, after_sequence: int = 0,
               owner: TaskOwner | None = None) -> list[TaskEvent]: ...
    def claim_step(self, worker_id: str, *, lease_seconds: int = 60,
                   task_id: str | None = None) -> ClaimedStep | None: ...
    def close(self) -> None: ...


class SQLiteTaskRuntimeRepository:
    """Restart-safe local authority.  SQLite WAL remains the default backend."""

    def __init__(self, database_path: str | Path | None = None):
        configured = database_path or load_business_config().database_path
        self.database_path = str(Path(configured).resolve())
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.database_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        self._idempotency_lock = threading.RLock()
        self.migrate()

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def tx(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def migrate(self) -> None:
        migration = Path(__file__).with_name("migrations") / "007_task_runtime.sqlite.sql"
        with self._lock:
            self.connection.executescript(migration.read_text(encoding="utf-8"))
            self.connection.commit()

    @staticmethod
    def _owner_clause(owner: TaskOwner) -> tuple[str, list[Any]]:
        if owner.is_customer:
            return ("tenant_id=? AND owner_type='customer' AND customer_account_id=?",
                    [owner.tenant_id, owner.customer_account_id or owner.actor_id])
        return "tenant_id=?", [owner.tenant_id]

    def _require_task(self, db: sqlite3.Connection, task_id: str,
                      owner: TaskOwner | None = None) -> sqlite3.Row:
        try:
            task_id = str(uuid.UUID(task_id))
        except (ValueError, TypeError):
            raise TaskRuntimeError("task_not_found", 404) from None
        query, params = "SELECT * FROM task_instance WHERE task_id=?", [task_id]
        if owner is not None:
            clause, owner_params = self._owner_clause(owner)
            query += f" AND {clause}"
            params.extend(owner_params)
        row = db.execute(query, params).fetchone()
        if row is None:
            raise TaskRuntimeError("task_not_found", 404)
        return row

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in tuple(result):
            if key.endswith("_json"):
                result[key[:-5]] = json.loads(result.pop(key) or "{}")
        for key in ("pause_requested", "cancel_requested", "valid", "approved"):
            if key in result:
                result[key] = bool(result[key])
        return result

    def _append_event(self, db: sqlite3.Connection, task_id: str, event_type: str,
                      status: str, step_key: str | None = None,
                      safe: Mapping[str, Any] | None = None,
                      internal: Mapping[str, Any] | None = None) -> TaskEvent:
        row = self._require_task(db, task_id)
        sequence = int(row["last_sequence"]) + 1
        event = TaskEvent(new_id(), task_id, sequence, event_type, status, step_key,
                          dict(safe or {}), dict(internal or {}), now_utc())
        db.execute("""INSERT INTO task_event(event_id,task_id,sequence,type,status,step_key,
          safe_json,internal_json,occurred_at) VALUES(?,?,?,?,?,?,?,?,?)""",
          (event.event_id, event.task_id, event.sequence, event.type, event.status,
           event.step_key, canonical_json(event.safe_data), canonical_json(event.internal_data),
           event.occurred_at))
        db.execute("UPDATE task_instance SET last_sequence=?,updated_at=? WHERE task_id=?",
                   (sequence, event.occurred_at, task_id))
        return event

    @staticmethod
    def _assert_transition(current: str, next_status: str) -> None:
        if current == next_status:
            return
        if current not in TASK_STATUSES or next_status not in TASK_STATUSES:
            raise TaskRuntimeError("task_status_invalid", 500)
        if next_status not in TASK_TRANSITIONS[current]:
            raise TaskRuntimeError("task_transition_invalid", 409)

    @staticmethod
    def _input_snapshot(db: sqlite3.Connection, claim: ClaimedStep) -> dict[str, Any]:
        step = db.execute("SELECT dependencies_json FROM task_step WHERE step_id=?",
                          (claim.step_id,)).fetchone()
        dependencies = json.loads(step["dependencies_json"] or "[]") if step else []
        outputs: dict[str, Any] = {}
        for dependency in dependencies:
            row = db.execute("""SELECT output_json FROM task_step WHERE task_id=? AND
              plan_version=? AND step_key=? AND status='completed'""",
              (claim.task_id, claim.plan_version, dependency)).fetchone()
            if row is not None:
                outputs[dependency] = json.loads(row["output_json"] or "{}")
        return {"context": claim.context, "dependencies": outputs}

    def create_task(self, owner: TaskOwner, conversation_id: str, title: str,
                    context: Mapping[str, Any], plan: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            conversation_id = str(uuid.UUID(conversation_id))
        except (ValueError, TypeError):
            raise TaskRuntimeError("task_conversation_invalid", 422) from None
        title = " ".join(str(title).split())[:255]
        if not title:
            raise TaskRuntimeError("task_title_invalid", 422)
        task_id, now = new_id(), now_utc()
        plan_hash = content_hash(plan)
        with self.tx(immediate=True) as db:
            db.execute("""INSERT INTO task_instance(task_id,tenant_id,owner_type,owner_id,
              customer_account_id,conversation_id,title,template_id,template_version,status,
              context_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'rfq_quote_followup',1,
              'queued',?,?,?)""", (task_id, owner.tenant_id, owner.actor_type, owner.actor_id,
                owner.customer_account_id, conversation_id, title, canonical_json(dict(context)), now, now))
            db.execute("""INSERT INTO task_plan_revision(task_id,plan_version,instruction_id,
              plan_json,plan_hash,diff_json,created_at) VALUES(?,1,NULL,?,?,?,?)""",
              (task_id, canonical_json(plan), plan_hash,
               canonical_json({"created": [item["step_key"] for item in plan]}), now))
            for ordinal, item in enumerate(plan, 1):
                db.execute("""INSERT INTO task_step(step_id,task_id,plan_version,step_key,ordinal,
                  label_key,executor,risk_level,audience,dependencies_json,status,created_at,updated_at)
                  VALUES(?,?,1,?,?,?,?,?,?,?,'pending',?,?)""",
                  (new_id(), task_id, item["step_key"], ordinal, item["label_key"], item["executor"],
                   item["risk_level"], item["audience"], canonical_json(item.get("dependencies", [])), now, now))
            self._append_event(db, task_id, "task.created", "queued", safe={
                "title": title, "template_id": "rfq_quote_followup", "plan_version": 1,
            })
        return self.get_task(task_id, owner)

    def _steps(self, db: sqlite3.Connection, task_id: str, plan_version: int) -> list[dict[str, Any]]:
        rows = db.execute("""SELECT * FROM task_step WHERE task_id=? AND plan_version=?
          ORDER BY ordinal,step_id""", (task_id, plan_version)).fetchall()
        return [self._decode(row) for row in rows]

    def get_task(self, task_id: str, owner: TaskOwner | None = None) -> dict[str, Any]:
        with self._lock:
            row = self._require_task(self.connection, task_id, owner)
            task = self._decode(row)
            task["steps"] = self._steps(self.connection, task_id, int(row["active_plan_version"]))
            task["plan"] = self._decode(self.connection.execute(
                "SELECT * FROM task_plan_revision WHERE task_id=? AND plan_version=?",
                (task_id, row["active_plan_version"])).fetchone())
            task["human_actions"] = [self._decode(item) for item in self.connection.execute(
                "SELECT * FROM task_human_action WHERE task_id=? ORDER BY created_at", (task_id,)).fetchall()]
            task["artifacts"] = [self._decode(item) for item in self.connection.execute(
                "SELECT * FROM task_artifact WHERE task_id=? ORDER BY created_at", (task_id,)).fetchall()]
            task["checkpoints"] = [self._decode(item) for item in self.connection.execute(
                "SELECT * FROM task_checkpoint WHERE task_id=? ORDER BY created_at", (task_id,)).fetchall()]
            task["instructions"] = [self._decode(item) for item in self.connection.execute(
                "SELECT * FROM task_instruction WHERE task_id=? ORDER BY created_at", (task_id,)).fetchall()]
            task["etag"] = f'"task-{task_id}-{task["version"]}"'
            return task

    def list_tasks(self, owner: TaskOwner, conversation_id: str | None = None,
                   limit: int = 100) -> list[dict[str, Any]]:
        clause, params = self._owner_clause(owner)
        query = f"SELECT task_id FROM task_instance WHERE {clause}"
        if conversation_id:
            query += " AND conversation_id=?"
            params.append(conversation_id)
        query += " ORDER BY updated_at DESC,task_id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 100)))
        with self._lock:
            ids = [row["task_id"] for row in self.connection.execute(query, params).fetchall()]
        return [self.get_task(task_id, owner) for task_id in ids]

    def events(self, task_id: str, after_sequence: int = 0,
               owner: TaskOwner | None = None) -> list[TaskEvent]:
        with self._lock:
            self._require_task(self.connection, task_id, owner)
            rows = self.connection.execute("""SELECT * FROM task_event WHERE task_id=? AND
              sequence>? ORDER BY sequence""", (task_id, max(0, int(after_sequence)))).fetchall()
        return [TaskEvent(row["event_id"], row["task_id"], int(row["sequence"]), row["type"],
                          row["status"], row["step_key"], json.loads(row["safe_json"]),
                          json.loads(row["internal_json"]), row["occurred_at"]) for row in rows]

    def latest_event(self, task_id: str) -> TaskEvent | None:
        events = self.events(task_id, max(0, self.get_task(task_id)["last_sequence"] - 1))
        return events[-1] if events else None

    def idempotent(self, scope: str, key: str, payload: Mapping[str, Any],
                   action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        if not key or len(key) > 255:
            raise TaskRuntimeError("task_idempotency_key_required", 400)
        digest = content_hash(dict(payload))
        pending = {"__task_runtime_pending__": True}
        with self._idempotency_lock:
            try:
                with self.tx(immediate=True) as db:
                    row = db.execute("SELECT * FROM task_idempotency WHERE scope=? AND idempotency_key=?",
                                     (scope, key)).fetchone()
                    if row:
                        if row["payload_hash"] != digest:
                            raise TaskRuntimeError("task_idempotency_conflict", 409)
                        raw_response = row["response_json"]
                        response = (json.loads(raw_response)
                                    if isinstance(raw_response, str) else raw_response)
                        if response == pending:
                            raise TaskRuntimeError("task_idempotency_outcome_unknown", 409)
                        return response
                    db.execute("INSERT INTO task_idempotency VALUES(?,?,?,?,?)",
                               (scope, key, digest, canonical_json(pending), now_utc()))
            except TaskRuntimeError:
                raise
            except Exception:
                # A concurrent process may have won the unique-key reservation.
                row = self.connection.execute(
                    "SELECT * FROM task_idempotency WHERE scope=? AND idempotency_key=?",
                    (scope, key)).fetchone()
                if row is None:
                    raise
                if row["payload_hash"] != digest:
                    raise TaskRuntimeError("task_idempotency_conflict", 409) from None
                raw_response = row["response_json"]
                response = json.loads(raw_response) if isinstance(raw_response, str) else raw_response
                if response == pending:
                    raise TaskRuntimeError("task_idempotency_outcome_unknown", 409) from None
                return response
            try:
                value = action()
            except Exception:
                with self.tx(immediate=True) as db:
                    db.execute("""DELETE FROM task_idempotency WHERE scope=? AND
                      idempotency_key=? AND payload_hash=?""", (scope, key, digest))
                raise
            with self.tx(immediate=True) as db:
                db.execute("""UPDATE task_idempotency SET response_json=? WHERE scope=? AND
                  idempotency_key=? AND payload_hash=?""",
                  (canonical_json(value), scope, key, digest))
            return value

    @staticmethod
    def _require_etag(row: sqlite3.Row, expected_etag: str | None) -> None:
        if not expected_etag:
            raise TaskRuntimeError("task_precondition_required", 428)
        if expected_etag != f'"task-{row["task_id"]}-{row["version"]}"':
            raise TaskRuntimeError("task_version_conflict", 412)

    def command(self, task_id: str, command: str, owner: TaskOwner,
                expected_etag: str | None, actor_id: str) -> dict[str, Any]:
        if command not in {"pause", "resume", "cancel", "retry"}:
            raise TaskRuntimeError("task_command_invalid")
        with self.tx(immediate=True) as db:
            row = self._require_task(db, task_id, owner)
            self._require_etag(row, expected_etag)
            status, now = row["status"], now_utc()
            if status in TERMINAL_TASK_STATUSES:
                raise TaskRuntimeError("task_command_not_allowed", 409)
            if command == "pause":
                if status in {"paused", "pause_requested"}:
                    return self.get_task(task_id, owner)
                if status not in {"queued", "running", "retry_wait"}:
                    raise TaskRuntimeError("task_not_pausable", 409)
                running = db.execute("""SELECT 1 FROM task_step WHERE task_id=? AND
                  plan_version=? AND status='running' LIMIT 1""",
                  (task_id, row["active_plan_version"])).fetchone()
                next_status = "pause_requested" if running else "paused"
                self._assert_transition(status, next_status)
                db.execute("""UPDATE task_instance SET status=?,pause_requested=1,version=version+1,
                  updated_at=? WHERE task_id=?""", (next_status, now, task_id))
                self._append_event(db, task_id, "task.pause_requested" if running else "task.paused",
                                   next_status, safe={"actor_id": actor_id})
            elif command == "resume":
                if status not in {"paused", "pause_requested"}:
                    raise TaskRuntimeError("task_not_resumable", 409)
                self._assert_transition(status, "queued")
                db.execute("""UPDATE task_instance SET status='queued',pause_requested=0,error_code=NULL,
                  version=version+1,updated_at=? WHERE task_id=?""", (now, task_id))
                command_id = new_id()
                db.execute("""INSERT OR IGNORE INTO task_resume_outbox(command_id,task_id,command_type,
                  idempotency_key,payload_json,status,created_at,updated_at) VALUES(?,?, 'resume',?,?,
                  'pending',?,?)""", (command_id, task_id, f"resume:{task_id}:{row['version']}", "{}", now, now))
                self._append_event(db, task_id, "task.resumed", "queued", safe={"actor_id": actor_id})
            elif command == "cancel":
                self._assert_transition(status, "cancelled")
                db.execute("""UPDATE task_instance SET status='cancelled',cancel_requested=1,
                  pause_requested=0,version=version+1,updated_at=?,completed_at=? WHERE task_id=?""",
                  (now, now, task_id))
                db.execute("""UPDATE task_step SET status='cancelled',updated_at=? WHERE task_id=?
                  AND plan_version=? AND status IN ('pending','retry_wait','waiting_input','waiting_review','failed')""",
                  (now, task_id, row["active_plan_version"]))
                db.execute("UPDATE task_human_action SET status='cancelled',resolved_at=? WHERE task_id=? AND status='pending'",
                           (now, task_id))
                self._append_event(db, task_id, "task.cancelled", "cancelled", safe={"actor_id": actor_id})
            else:
                if status not in {"failed", "retry_wait"}:
                    raise TaskRuntimeError("task_no_retryable_step", 409)
                failed = db.execute("""SELECT * FROM task_step WHERE task_id=? AND plan_version=?
                  AND status IN ('failed','retry_wait') ORDER BY ordinal LIMIT 1""",
                  (task_id, row["active_plan_version"])).fetchone()
                if failed is None:
                    raise TaskRuntimeError("task_no_retryable_step", 409)
                self._assert_transition(status, "queued")
                db.execute("""UPDATE task_step SET status='pending',error_code=NULL,lease_owner=NULL,
                  lease_until=NULL,updated_at=? WHERE step_id=?""", (now, failed["step_id"]))
                db.execute("""UPDATE task_instance SET status='queued',error_code=NULL,version=version+1,
                  updated_at=? WHERE task_id=?""", (now, task_id))
                self._append_event(db, task_id, "task.retry_queued", "queued", failed["step_key"],
                                   safe={"actor_id": actor_id})
        return self.get_task(task_id, owner)

    def claim_step(self, worker_id: str, *, lease_seconds: int = 60,
                   task_id: str | None = None) -> ClaimedStep | None:
        now = now_utc()
        until = (datetime.now(timezone.utc) + timedelta(seconds=max(1, lease_seconds))).isoformat().replace("+00:00", "Z")
        with self.tx(immediate=True) as db:
            task_filter = " AND t.task_id=?" if task_id else ""
            parameters: list[Any] = [now, now]
            if task_id:
                parameters.append(task_id)
            candidates = db.execute("""SELECT s.*,t.context_json,t.status AS task_status,
              t.pause_requested,t.cancel_requested FROM task_step s JOIN task_instance t ON t.task_id=s.task_id
              WHERE s.plan_version=t.active_plan_version AND t.status IN ('queued','running','retry_wait')
              AND t.pause_requested=0 AND t.cancel_requested=0 AND
              ((s.status IN ('pending','retry_wait') AND (s.lease_until IS NULL OR s.lease_until<=?))
               OR (s.status='running' AND s.lease_until<?))
              """ + task_filter + " ORDER BY t.created_at,s.ordinal,s.step_id LIMIT 50",
              parameters).fetchall()
            selected = None
            for row in candidates:
                dependencies = json.loads(row["dependencies_json"])
                if dependencies:
                    completed = {item["step_key"] for item in db.execute("""SELECT step_key FROM task_step
                      WHERE task_id=? AND plan_version=? AND status='completed'""",
                      (row["task_id"], row["plan_version"])).fetchall()}
                    if not set(dependencies).issubset(completed):
                        continue
                selected = row
                break
            if selected is None:
                return None
            attempt_no = int(selected["attempt_count"]) + 1
            attempt_id = new_id()
            cursor = db.execute("""UPDATE task_step SET status='running',lease_owner=?,lease_until=?,
              attempt_count=?,started_at=COALESCE(started_at,?),updated_at=? WHERE step_id=? AND
              ((status IN ('pending','retry_wait') AND (lease_until IS NULL OR lease_until<=?))
               OR (status='running' AND lease_until<?))""",
              (worker_id, until, attempt_no, now, now, selected["step_id"], now, now))
            if cursor.rowcount != 1:
                return None
            db.execute("""UPDATE task_step_attempt SET status='lease_expired',
              error_code='task_step_lease_expired',completed_at=? WHERE step_id=? AND status='running'""",
              (now, selected["step_id"]))
            db.execute("""INSERT INTO task_step_attempt(attempt_id,task_id,step_id,attempt_no,
              worker_id,status,started_at) VALUES(?,?,?,?,?,'running',?)""",
              (attempt_id, selected["task_id"], selected["step_id"], attempt_no, worker_id, now))
            self._assert_transition(selected["task_status"], "running")
            db.execute("""UPDATE task_instance SET status='running',current_step_key=?,version=version+1,
              updated_at=? WHERE task_id=?""", (selected["step_key"], now, selected["task_id"]))
            self._append_event(db, selected["task_id"], "task.step.started", "running",
                               selected["step_key"], safe={"attempt": attempt_no})
            return ClaimedStep(selected["task_id"], selected["step_id"], int(selected["plan_version"]),
                               selected["step_key"], selected["executor"], selected["risk_level"],
                               selected["audience"], json.loads(selected["context_json"]),
                               attempt_id, attempt_no)

    def defer_claim_for_control(self, claim: ClaimedStep, worker_id: str) -> dict[str, Any] | None:
        """Release a just-claimed step when pause/cancel arrived before execution."""
        now = now_utc()
        with self.tx(immediate=True) as db:
            task, _step = self._assert_lease(db, claim, worker_id)
            if task["cancel_requested"]:
                db.execute("""UPDATE task_step SET status='cancelled',lease_owner=NULL,
                  lease_until=NULL,updated_at=? WHERE step_id=?""", (now, claim.step_id))
                db.execute("""UPDATE task_step_attempt SET status='cancelled',completed_at=?
                  WHERE attempt_id=?""", (now, claim.attempt_id))
                self._append_event(db, claim.task_id, "task.step.cancelled", "cancelled",
                                   claim.step_key)
                return self.get_task(claim.task_id)
            if task["pause_requested"]:
                self._assert_transition(task["status"], "paused")
                db.execute("""UPDATE task_step SET status='pending',lease_owner=NULL,
                  lease_until=NULL,updated_at=? WHERE step_id=?""", (now, claim.step_id))
                db.execute("""UPDATE task_step_attempt SET status='paused',completed_at=?
                  WHERE attempt_id=?""", (now, claim.attempt_id))
                db.execute("""UPDATE task_instance SET status='paused',version=version+1,
                  updated_at=? WHERE task_id=?""", (now, claim.task_id))
                self._append_event(db, claim.task_id, "task.paused", "paused", claim.step_key)
                return self.get_task(claim.task_id)
        return None

    def _assert_lease(self, db: sqlite3.Connection, claim: ClaimedStep,
                      worker_id: str) -> tuple[sqlite3.Row, sqlite3.Row]:
        task = self._require_task(db, claim.task_id)
        step = db.execute("SELECT * FROM task_step WHERE step_id=?", (claim.step_id,)).fetchone()
        if step is None or step["status"] != "running" or step["lease_owner"] != worker_id:
            raise TaskRuntimeError("task_step_lease_lost", 409)
        return task, step

    def complete_step(self, claim: ClaimedStep, worker_id: str, output: Mapping[str, Any],
                      artifacts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        artifacts, now = artifacts or [], now_utc()
        output_data = dict(output)
        output_digest = content_hash(output_data)
        checkpoint_id = new_id()
        with self.tx(immediate=True) as db:
            task, _step = self._assert_lease(db, claim, worker_id)
            if task["cancel_requested"]:
                db.execute("UPDATE task_step SET status='cancelled',lease_owner=NULL,lease_until=NULL,updated_at=? WHERE step_id=?",
                           (now, claim.step_id))
                db.execute("UPDATE task_step_attempt SET status='cancelled',completed_at=? WHERE attempt_id=?",
                           (now, claim.attempt_id))
                self._append_event(db, claim.task_id, "task.step.cancelled", "cancelled",
                                   claim.step_key)
                return self.get_task(claim.task_id)
            input_digest = content_hash(self._input_snapshot(db, claim))
            db.execute("""INSERT INTO task_checkpoint(checkpoint_id,task_id,step_id,plan_version,step_key,
              input_hash,output_hash,output_json,artifact_refs_json,executor_version,created_at)
              VALUES(?,?,?,?,?,?,?,?,?,'task-runtime-v1',?)""",
              (checkpoint_id, claim.task_id, claim.step_id, claim.plan_version, claim.step_key,
               input_digest, output_digest, canonical_json(output_data),
               canonical_json([item.get("artifact_id") for item in artifacts]), now))
            db.execute("""UPDATE task_step SET status='completed',input_hash=?,output_json=?,output_hash=?,
              checkpoint_id=?,lease_owner=NULL,lease_until=NULL,completed_at=?,updated_at=? WHERE step_id=?""",
              (input_digest, canonical_json(output_data), output_digest, checkpoint_id, now, now, claim.step_id))
            db.execute("""UPDATE task_step_attempt SET status='completed',input_hash=?,output_hash=?,
              completed_at=? WHERE attempt_id=?""", (input_digest, output_digest, now, claim.attempt_id))
            paused = bool(task["pause_requested"])
            remaining = db.execute("""SELECT 1 FROM task_step WHERE task_id=? AND plan_version=?
              AND status NOT IN ('completed','superseded','cancelled') LIMIT 1""",
              (claim.task_id, claim.plan_version)).fetchone()
            next_status = "paused" if paused else ("queued" if remaining else "completed")
            self._assert_transition(task["status"], next_status)
            completed_at = now if next_status == "completed" else None
            db.execute("""UPDATE task_instance SET status=?,current_step_key=?,pause_requested=?,
              version=version+1,updated_at=?,completed_at=? WHERE task_id=?""",
              (next_status, claim.step_key, int(paused), now, completed_at, claim.task_id))
            self._append_event(db, claim.task_id, "task.step.completed", next_status, claim.step_key,
                               safe={"checkpoint_id": checkpoint_id, "has_artifacts": bool(artifacts)},
                               internal={"output_hash": output_digest})
            if next_status == "paused":
                self._append_event(db, claim.task_id, "task.paused", "paused", claim.step_key)
            elif next_status == "completed":
                self._append_event(db, claim.task_id, "task.completed", "completed", claim.step_key)
        return self.get_task(claim.task_id)

    def wait_step(self, claim: ClaimedStep, worker_id: str, *, status: str,
                  prompt_key: str, missing: list[str] | None = None,
                  output: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if status not in {"waiting_input", "waiting_review"}:
            raise TaskRuntimeError("task_wait_status_invalid")
        now, action_id = now_utc(), new_id()
        audience = "customer" if status == "waiting_input" or claim.audience == "customer" else "workspace"
        with self.tx(immediate=True) as db:
            task, _step = self._assert_lease(db, claim, worker_id)
            self._assert_transition(task["status"], status)
            db.execute("""UPDATE task_step SET status=?,output_json=?,lease_owner=NULL,lease_until=NULL,
              updated_at=? WHERE step_id=?""", (status, canonical_json(dict(output or {})), now, claim.step_id))
            db.execute("UPDATE task_step_attempt SET status=?,completed_at=? WHERE attempt_id=?",
                       (status, now, claim.attempt_id))
            db.execute("""INSERT INTO task_human_action(action_id,task_id,step_key,audience,status,
              prompt_key,payload_json,created_at) VALUES(?,?,?,?, 'pending',?,?,?)""",
              (action_id, claim.task_id, claim.step_key, audience, prompt_key,
               canonical_json({"missing_fields": missing or []}), now))
            db.execute("""UPDATE task_instance SET status=?,current_step_key=?,version=version+1,
              updated_at=? WHERE task_id=?""", (status, claim.step_key, now, claim.task_id))
            self._append_event(db, claim.task_id, f"task.step.{status}", status, claim.step_key,
                               safe={"action_id": action_id, "prompt_key": prompt_key,
                                     "missing_fields": missing or []})
        return self.get_task(claim.task_id)

    def fail_step(self, claim: ClaimedStep, worker_id: str, error_code: str,
                  *, retryable: bool = True, max_attempts: int = 3) -> dict[str, Any]:
        now = now_utc()
        with self.tx(immediate=True) as db:
            task, _step = self._assert_lease(db, claim, worker_id)
            retry_wait = retryable and claim.attempt_no < max_attempts
            step_status, task_status = ("retry_wait", "retry_wait") if retry_wait else ("failed", "failed")
            self._assert_transition(task["status"], task_status)
            db.execute("""UPDATE task_step SET status=?,error_code=?,lease_owner=NULL,lease_until=NULL,
              updated_at=? WHERE step_id=?""", (step_status, error_code[:128], now, claim.step_id))
            db.execute("""UPDATE task_step_attempt SET status=?,error_code=?,completed_at=? WHERE attempt_id=?""",
                       (step_status, error_code[:128], now, claim.attempt_id))
            db.execute("""UPDATE task_instance SET status=?,error_code=?,version=version+1,
              updated_at=? WHERE task_id=?""", (task_status, error_code[:128], now, claim.task_id))
            self._append_event(db, claim.task_id, "task.step.retry_wait" if retry_wait else "task.step.failed",
                               task_status, claim.step_key, safe={"error_code": error_code[:128],
                               "attempt": claim.attempt_no})
        return self.get_task(claim.task_id)

    def apply_instruction(self, task_id: str, owner: TaskOwner, content: str,
                          changes: Mapping[str, Any], impacts: list[str], plan: list[dict[str, Any]]) -> dict[str, Any]:
        content = str(content).strip()
        if not content or len(content) > 10000:
            raise TaskRuntimeError("task_instruction_invalid", 422)
        instruction_id, now = new_id(), now_utc()
        with self.tx(immediate=True) as db:
            task = self._require_task(db, task_id, owner)
            if task["status"] in TERMINAL_TASK_STATUSES:
                raise TaskRuntimeError("task_instruction_not_allowed", 409)
            self._assert_transition(task["status"], "replanning")
            db.execute("UPDATE task_instance SET status='replanning',updated_at=? WHERE task_id=?",
                       (now, task_id))
            self._append_event(db, task_id, "task.replanning", "replanning", safe={
                "actor_type": owner.actor_type,
            })
            old_version, new_version = int(task["active_plan_version"]), int(task["active_plan_version"]) + 1
            context = json.loads(task["context_json"])
            context.update(dict(changes))
            db.execute("""INSERT INTO task_instruction(instruction_id,task_id,conversation_id,actor_type,
              actor_id,content,changes_json,impact_json,status,created_at) VALUES(?,?,?,?,?,?,?,?, 'applied',?)""",
              (instruction_id, task_id, task["conversation_id"], owner.actor_type, owner.actor_id,
               content, canonical_json(dict(changes)), canonical_json(impacts), now))
            old_steps = {row["step_key"]: row for row in db.execute("""SELECT * FROM task_step
              WHERE task_id=? AND plan_version=?""", (task_id, old_version)).fetchall()}
            reused, invalidated = [], []
            preserved_waiting: set[str] = set()
            for key, row in old_steps.items():
                if key in impacts and row["status"] == "completed":
                    invalidated.append(key)
                    if row["checkpoint_id"]:
                        db.execute("""UPDATE task_checkpoint SET valid=0,
                          invalidated_by_instruction_id=? WHERE checkpoint_id=?""",
                          (instruction_id, row["checkpoint_id"]))
                if row["status"] not in {"completed", "cancelled", "superseded"}:
                    db.execute("UPDATE task_step SET status='superseded',superseded_at=?,updated_at=? WHERE step_id=?",
                               (now, now, row["step_id"]))
                elif key in impacts and row["status"] == "completed":
                    db.execute("UPDATE task_step SET status='superseded',superseded_at=?,updated_at=? WHERE step_id=?",
                               (now, now, row["step_id"]))
            artifact_steps = set(impacts)
            if "internal_review" in artifact_steps:
                artifact_steps.add("quote_drafting")
            if artifact_steps:
                placeholders = ",".join("?" for _ in artifact_steps)
                db.execute(f"""UPDATE task_artifact SET approved=0,visibility='internal'
                  WHERE task_id=? AND step_key IN ({placeholders})""",
                  [task_id, *sorted(artifact_steps)])
            diff = {"instruction_id": instruction_id, "reused": reused,
                    "invalidated": invalidated, "impacted": impacts, "rerun": [],
                    "changes": dict(changes)}
            db.execute("""INSERT INTO task_plan_revision(task_id,plan_version,instruction_id,plan_json,
              plan_hash,diff_json,created_at) VALUES(?,?,?,?,?,?,?)""",
              (task_id, new_version, instruction_id, canonical_json(plan), content_hash(plan),
               canonical_json(diff), now))
            for ordinal, item in enumerate(plan, 1):
                old = old_steps.get(item["step_key"])
                can_reuse = bool(old and old["status"] in {"completed", "waiting_input", "waiting_review"}
                                 and item["step_key"] not in impacts)
                status = old["status"] if can_reuse else "pending"
                if can_reuse:
                    reused.append(item["step_key"])
                    if status in {"waiting_input", "waiting_review"}:
                        preserved_waiting.add(item["step_key"])
                db.execute("""INSERT INTO task_step(step_id,task_id,plan_version,step_key,ordinal,
                  label_key,executor,risk_level,audience,dependencies_json,status,input_hash,output_json,
                  output_hash,checkpoint_id,attempt_count,started_at,completed_at,created_at,updated_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (new_id(), task_id, new_version, item["step_key"], ordinal, item["label_key"],
                   item["executor"], item["risk_level"], item["audience"],
                   canonical_json(item.get("dependencies", [])), status,
                   old["input_hash"] if can_reuse else None,
                   old["output_json"] if can_reuse else "{}",
                   old["output_hash"] if can_reuse else None,
                   old["checkpoint_id"] if can_reuse else None,
                   int(old["attempt_count"]) if can_reuse else 0,
                   old["started_at"] if can_reuse else None,
                   old["completed_at"] if can_reuse else None, now, now))
            diff["reused"] = reused
            diff["rerun"] = [item["step_key"] for item in plan if item["step_key"] not in reused]
            db.execute("UPDATE task_plan_revision SET diff_json=? WHERE task_id=? AND plan_version=?",
                       (canonical_json(diff), task_id, new_version))
            for action in db.execute("""SELECT action_id,step_key FROM task_human_action
              WHERE task_id=? AND status='pending'""", (task_id,)).fetchall():
                if action["step_key"] not in preserved_waiting:
                    db.execute("""UPDATE task_human_action SET status='superseded',resolved_at=?
                      WHERE action_id=?""", (now, action["action_id"]))
            waiting_step = next((item for item in plan if item["step_key"] in preserved_waiting), None)
            next_status = old_steps[waiting_step["step_key"]]["status"] if waiting_step else "queued"
            next_step_key = (waiting_step["step_key"] if waiting_step else
                             next((item["step_key"] for item in plan if item["step_key"] not in reused),
                                  task["current_step_key"]))
            self._assert_transition("replanning", next_status)
            db.execute("""UPDATE task_instance SET status=?,active_plan_version=?,context_json=?,
              current_step_key=?,pause_requested=0,error_code=NULL,version=version+1,
              updated_at=? WHERE task_id=?""",
              (next_status, new_version, canonical_json(context), next_step_key, now, task_id))
            db.execute("UPDATE task_instruction SET applied_at=? WHERE instruction_id=?", (now, instruction_id))
            self._append_event(db, task_id, "task.plan.revised", next_status,
              waiting_step["step_key"] if waiting_step else None, safe={
                "plan_version": new_version, "reused": reused, "rerun": diff["rerun"],
            }, internal={"instruction_id": instruction_id, "changes": dict(changes)})
        return self.get_task(task_id, owner)

    def resolve_action(self, task_id: str, action_id: str, owner: TaskOwner,
                       decision: str, comment: str = "") -> dict[str, Any]:
        if decision not in {"approve", "reject", "confirm"}:
            raise TaskRuntimeError("task_decision_invalid", 422)
        now = now_utc()
        with self.tx(immediate=True) as db:
            task = self._require_task(db, task_id, owner)
            action = db.execute("SELECT * FROM task_human_action WHERE task_id=? AND action_id=?",
                                (task_id, action_id)).fetchone()
            if action is None or action["status"] != "pending":
                raise TaskRuntimeError("task_action_not_found", 404)
            if owner.is_customer and action["audience"] != "customer":
                raise TaskRuntimeError("task_action_not_found", 404)
            step = db.execute("""SELECT * FROM task_step WHERE task_id=? AND plan_version=? AND step_key=?""",
                              (task_id, task["active_plan_version"], action["step_key"])).fetchone()
            if step is None or step["status"] != "waiting_review":
                raise TaskRuntimeError("task_action_not_found", 404)
            allowed_decisions = ({"approve", "reject"} if step["step_key"] == "internal_review"
                                 else {"confirm", "reject"} if step["step_key"] == "customer_confirmation"
                                 else set())
            if decision not in allowed_decisions:
                raise TaskRuntimeError("task_decision_invalid", 422)
            db.execute("""UPDATE task_human_action SET status='resolved',decision_json=?,resolved_at=?,
              resolved_by=? WHERE action_id=?""",
              (canonical_json({"decision": decision, "comment": comment[:1000]}), now, owner.actor_id, action_id))
            if decision == "reject":
                self._assert_transition(task["status"], "failed")
                db.execute("UPDATE task_step SET status='failed',error_code='human_rejected',updated_at=? WHERE step_id=?",
                           (now, step["step_id"]))
                next_status = "failed"
            else:
                output = {"decision": decision, "resolved_by": owner.actor_id, "comment": comment[:1000]}
                digest = content_hash(output)
                checkpoint_id = new_id()
                db.execute("""INSERT INTO task_checkpoint(checkpoint_id,task_id,step_id,plan_version,step_key,
                  input_hash,output_hash,output_json,artifact_refs_json,executor_version,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,'human-decision-v1',?)""",
                  (checkpoint_id, task_id, step["step_id"], task["active_plan_version"], step["step_key"],
                   content_hash(json.loads(task["context_json"])), digest, canonical_json(output), "[]", now))
                db.execute("""UPDATE task_step SET status='completed',output_json=?,output_hash=?,checkpoint_id=?,
                  completed_at=?,updated_at=? WHERE step_id=?""",
                  (canonical_json(output), digest, checkpoint_id, now, now, step["step_id"]))
                if step["step_key"] == "internal_review" and decision == "approve":
                    db.execute("UPDATE task_artifact SET approved=1,visibility='customer' WHERE task_id=? AND step_key='quote_drafting'",
                               (task_id,))
                next_status = "queued"
                self._assert_transition(task["status"], next_status)
            db.execute("""UPDATE task_instance SET status=?,error_code=?,version=version+1,
              updated_at=? WHERE task_id=?""",
              (next_status, "human_rejected" if decision == "reject" else None, now, task_id))
            self._append_event(db, task_id, "task.human_action.resolved", next_status,
                               action["step_key"], safe={"action_id": action_id, "decision": decision})
        return self.get_task(task_id, owner)

    def save_artifact(self, task_id: str, step_key: str, plan_version: int,
                      visibility: str, kind: str, path: str | Path) -> dict[str, Any]:
        resolved = Path(path).resolve()
        data, now = resolved.read_bytes(), now_utc()
        record = {"artifact_id": new_id(), "task_id": task_id, "step_key": step_key,
                  "plan_version": int(plan_version), "visibility": visibility, "kind": kind,
                  "file_name": resolved.name, "storage_path": str(resolved), "byte_size": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(), "approved": False, "created_at": now}
        with self.tx(immediate=True) as db:
            self._require_task(db, task_id)
            db.execute("""INSERT INTO task_artifact(artifact_id,task_id,step_key,plan_version,
              visibility,kind,file_name,storage_path,byte_size,sha256,approved,created_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", tuple(record.values()))
        return record

    def acknowledge_resume_outbox(self, task_id: str) -> None:
        now = now_utc()
        with self.tx(immediate=True) as db:
            self._require_task(db, task_id)
            db.execute("""UPDATE task_resume_outbox SET status='completed',lease_owner=NULL,
              lease_until=NULL,updated_at=? WHERE task_id=? AND status='pending'""",
              (now, task_id))


def create_task_runtime_repository(database_path: str | Path | None = None) -> TaskRuntimeRepository:
    config = load_business_config()
    if config.database_backend == "mysql" and database_path is None:
        from agent.business.mysql_task_runtime_repository import MySQLTaskRuntimeRepository
        return MySQLTaskRuntimeRepository()
    return SQLiteTaskRuntimeRepository(database_path)
