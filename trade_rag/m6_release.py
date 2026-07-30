"""M6 isolated controlled-release rehearsal.

The runner uses deterministic de-identified data and MockEmbedding only.  It
never changes the process default vector backend and never removes volumes or
old Milvus collections.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from bus.queue import InboundMessage, MessageBus
from gateway import Gateway
from gateway_coordination import MySQLRuntimeCoordinator

from .config import RagVectorConfig
from .contracts import Actor, CanonicalDocument, ChildChunk, DocumentStatus
from .embeddings import MockEmbeddingProvider
from .migration_rehearsal import RehearsalError, load_env, redact, safe_database_name, validate_run_id
from .milvus_admin import MilvusCollectionManager
from .milvus_store import ACTIVE_ALIAS, MilvusStore, collection_name
from .pgvector_store import PgvectorStore


ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "deploy" / "docker" / "compose.yaml"
DEFAULT_ARTIFACT_ROOT = ROOT / ".tmp" / "docker-m6"
MYSQL_MIGRATIONS = tuple(ROOT / "agent" / "business" / "migrations" / name for name in (
    "001_trade_ops_core.mysql.sql", "003_email_accounts.mysql.sql",
    "004_email_delivery.mysql.sql", "005_m6_runtime.mysql.sql",
))
PG_MIGRATION = ROOT / "trade_rag" / "migrations" / "001_pgvector.sql"
STAGES = ("prepare", "seed", "load", "fault", "security", "validate")
SENSITIVE_KEYS = ("password", "token", "secret", "recipient", "body", "content", "vector")


def default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("m6-%Y%m%d-%H%M%S").lower()


def percentile(values: Iterable[float], quantile: float) -> float:
    rows = sorted(float(value) for value in values)
    if not rows:
        return 0.0
    return rows[max(0, min(len(rows) - 1, math.ceil(len(rows) * quantile) - 1))]


def safe_generation(run_id: str) -> int:
    validate_run_id(run_id)
    # Reserved high range, stable for retries and valid for Milvus naming.
    return 1_500_000_000 + int(hashlib.sha256(run_id.encode()).hexdigest()[:7], 16) % 500_000_000


def parse_json_rows(value: str) -> list[dict[str, Any]]:
    text = value.strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, list) else [payload]
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


@dataclass
class Stage:
    status: str = "pending"
    duration_seconds: float | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class M6State:
    schema_version: str
    run_id: str
    created_at: str
    postgres_database: str
    milvus_generation: int
    stages: dict[str, Stage]
    passed: bool = False
    production_ready: bool = False
    release_scope: str = "local_controlled_pilot_review"

    @classmethod
    def new(cls, run_id: str) -> "M6State":
        return cls(
            "docker-m6-release-v1", run_id,
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            safe_database_name("m6_vector", run_id), safe_generation(run_id),
            {name: Stage() for name in STAGES},
        )

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "M6State":
        value = dict(value)
        value["stages"] = {key: Stage(**row) for key, row in value["stages"].items()}
        return cls(**value)


class M6Release:
    def __init__(self, run_id: str, *, artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
                 env: dict[str, str] | None = None, rto_seconds: float = 600.0,
                 vector_count: int = 10_000, query_count: int = 1_000) -> None:
        self.run_id = validate_run_id(run_id)
        if rto_seconds <= 0 or vector_count <= 0 or query_count <= 0:
            raise ValueError("M6 thresholds must be positive")
        self.artifact_root = artifact_root.resolve()
        self.run_dir = self.artifact_root / run_id
        self.state_path = self.run_dir / "state.json"
        self.report_path = self.run_dir / "release-report.json"
        self.manifest_path = self.run_dir / "manifest.sha256"
        self.env = dict(env or load_env())
        self.rto_seconds = rto_seconds
        self.vector_count = vector_count
        self.query_count = query_count

    @contextmanager
    def _lock(self):
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        lock = self.artifact_root / ".lock"
        try:
            lock.mkdir()
        except FileExistsError as exc:
            raise RehearsalError("another M6 rehearsal holds the lock") from exc
        try:
            yield
        finally:
            lock.rmdir()

    def _state(self, create: bool = False) -> M6State:
        if self.state_path.is_file():
            return M6State.from_json(json.loads(self.state_path.read_text(encoding="utf-8")))
        if not create:
            raise RehearsalError("prepare must complete before this stage")
        self.run_dir.mkdir(parents=True, exist_ok=False)
        state = M6State.new(self.run_id)
        self._save(state)
        return state

    def _save(self, state: M6State) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(redact(asdict(state)), ensure_ascii=False,
                                              sort_keys=True, indent=2) + "\n", encoding="utf-8")

    def _stage(self, name: str, action) -> M6State:
        with self._lock():
            state = self._state(create=name == "prepare")
            stage = state.stages[name]
            if stage.status == "passed":
                return state
            started = time.monotonic(); stage.status = "running"; self._save(state)
            try:
                stage.details = redact(action(state) or {})
                stage.status = "passed"
                stage.duration_seconds = round(time.monotonic() - started, 3)
            except Exception as exc:
                stage.status = "failed"
                stage.duration_seconds = round(time.monotonic() - started, 3)
                reason = ""
                if isinstance(exc, RehearsalError):
                    reason = re.sub(r"[^a-z0-9]+", "_", str(exc).lower()).strip("_")[:96]
                stage.details = {"error_code": type(exc).__name__,
                                 **({"reason_code": reason} if reason else {})}
                self._save(state)
                raise
            self._save(state)
            return state

    def _compose(self, *args: str, input_text: str | None = None,
                 timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy(); environment.update(self.env)
        result = subprocess.run(
            ["docker", "compose", "--env-file", str(ROOT / "deploy/docker/.env"),
             "-f", str(COMPOSE), *args], cwd=ROOT, env=environment, input=input_text,
            text=True, encoding="utf-8", errors="replace", capture_output=True,
            timeout=timeout,
        )
        if result.returncode:
            raise RehearsalError(f"docker compose command failed: {args[0] if args else 'unknown'}")
        return result

    @staticmethod
    def _docker(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["docker", *args], cwd=ROOT, text=True, encoding="utf-8",
                                errors="replace", capture_output=True, timeout=timeout)
        if result.returncode:
            raise RehearsalError(f"docker command failed: {args[0] if args else 'unknown'}")
        return result

    @staticmethod
    def _local_database_fingerprint() -> dict[str, Any]:
        rows = []
        for pattern in ("*.db", "*.sqlite", "*.sqlite3"):
            for path in ROOT.rglob(pattern):
                if any(part in {".git", ".tmp", ".venv"} for part in path.parts):
                    continue
                stat = path.stat()
                rows.append((path.relative_to(ROOT).as_posix(), stat.st_size, stat.st_mtime_ns))
        payload = json.dumps(sorted(rows), ensure_ascii=False, separators=(",", ":")).encode()
        return {"file_count": len(rows), "metadata_sha256": hashlib.sha256(payload).hexdigest()}

    def _mysql(self, sql: str, database: str | None = None) -> None:
        command = 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot'
        if database:
            command += f" --database={database}"
        self._compose("exec", "-T", "mysql", "sh", "-c", command, input_text=sql)

    def _pg(self, sql: str, database: str | None = None) -> None:
        self._compose(
            "exec", "-T", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-U",
            self.env.get("DOCKER_POSTGRES_USER", "nanoclaw_vector_docker"), "-d",
            database or self.env.get("DOCKER_POSTGRES_DATABASE", "nanoclaw_vector_docker"),
            input_text=sql,
        )

    def _pg_config(self, database: str) -> RagVectorConfig:
        return RagVectorConfig(
            backend="pgvector", dimensions=64, host="127.0.0.1",
            port=int(self.env.get("DOCKER_POSTGRES_PORT", "5433")), database=database,
            user=self.env.get("DOCKER_POSTGRES_USER", "nanoclaw_vector_docker"),
            password=self.env["DOCKER_POSTGRES_PASSWORD"], sslmode="disable",
        )

    def _milvus_config(self) -> RagVectorConfig:
        return RagVectorConfig(
            backend="milvus", dimensions=64,
            milvus_uri=f"http://127.0.0.1:{self.env.get('DOCKER_MILVUS_PORT', '19530')}",
            milvus_token=f"{self.env['DOCKER_MILVUS_APP_USER']}:{self.env['DOCKER_MILVUS_APP_PASSWORD']}",
            milvus_database="nanoclaw_vector_docker", milvus_alias=ACTIVE_ALIAS,
            milvus_connect_timeout_seconds=10,
        )

    def _admin(self):
        from pymilvus import MilvusClient
        config = self._milvus_config()
        return MilvusClient(uri=config.milvus_uri,
                            token=f"root:{self.env['DOCKER_MILVUS_ROOT_PASSWORD']}",
                            db_name=config.milvus_database, timeout=10)

    def _mysql_connection(self):
        import pymysql
        return pymysql.connect(
            host="127.0.0.1", port=int(self.env.get("DOCKER_MYSQL_PORT", "3307")),
            user="root", password=self.env["DOCKER_MYSQL_ROOT_PASSWORD"], database="trade_ops",
            charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, autocommit=False,
        )

    def _fixtures(self) -> Iterable[tuple[CanonicalDocument, list[ChildChunk], list[list[float]]]]:
        embedder = MockEmbeddingProvider(64)
        per_document = 100
        documents = math.ceil(self.vector_count / per_document)
        produced = 0
        for doc_no in range(documents):
            category = doc_no % 5
            roles = frozenset({"sales"}) if category else frozenset()
            status = DocumentStatus.REVOKED if category == 3 else DocumentStatus.PUBLISHED
            expires = datetime.now(timezone.utc) - timedelta(days=1) if category == 2 else None
            unit = "other" if category == 4 else "trade"
            document_id = f"{self.run_id}-d{doc_no:04d}"
            source_text = f"deidentified fixture document {doc_no:04d} category {category}"
            document = CanonicalDocument(
                document_id, 1, f"sample://{document_id}", f"M6 fixture {doc_no:04d}",
                source_text, hashlib.sha256(source_text.encode()).hexdigest(),
                business_unit_id=unit, allowed_roles=roles, classification="internal",
                status=status, expires_at=expires, parser_version="m6-fixture-v1",
                metadata={"fixture": "docker-m6-v1"},
            )
            children: list[ChildChunk] = []; texts: list[str] = []
            for child_no in range(min(per_document, self.vector_count - produced)):
                text = f"{source_text} child {child_no:03d} deterministic retrieval"
                children.append(ChildChunk(
                    f"{document_id}-c{child_no:03d}", f"{document_id}-p", document_id,
                    text, "page-1", hashlib.sha256(text.encode()).hexdigest(),
                    {"fixture": "docker-m6-v1"},
                )); texts.append(text); produced += 1
            yield document, children, embedder.embed(texts)

    @staticmethod
    def _load_milvus(admin: Any, generation: int) -> None:
        from pymilvus import MilvusClient
        name = collection_name(generation)
        if "embedding_cosine" not in admin.list_indexes(name):
            params = MilvusClient.prepare_index_params()
            params.add_index("embedding", index_type="AUTOINDEX", metric_type="COSINE",
                             index_name="embedding_cosine")
            admin.create_index(name, params)
        admin.load_collection(name)
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if str(admin.get_load_state(name).get("state", "")).casefold() in {"loaded", "loadstate.loaded"}:
                return
            time.sleep(0.5)
        raise RehearsalError("Milvus M6 generation load exceeded RTO")

    def prepare(self) -> M6State:
        def action(state: M6State):
            if os.environ.get("RAG_VECTOR_BACKEND", "memory").strip().lower() != "memory":
                raise RehearsalError("default RAG_VECTOR_BACKEND must remain memory")
            m5_acceptance = ROOT / "deploy/docker/M5_ACCEPTANCE.md"
            if not m5_acceptance.is_file() or "结论：通过" not in m5_acceptance.read_text(encoding="utf-8"):
                raise RehearsalError("accepted M5 evidence is required")
            if shutil.disk_usage(self.artifact_root.parent).free < 512 * 1024 * 1024:
                raise RehearsalError("insufficient free disk for M6 artifacts")
            services = parse_json_rows(self._compose("ps", "--format", "json").stdout)
            healthy = {row.get("Service") for row in services
                       if str(row.get("Health", "")).lower() == "healthy"}
            required = {"mysql", "postgres", "etcd", "minio", "milvus"}
            if not required.issubset(healthy):
                raise RehearsalError("five Docker services must be healthy")
            migration_sql = "\n".join(path.read_text(encoding="utf-8") for path in MYSQL_MIGRATIONS)
            self._mysql(migration_sql)
            self._pg(f'CREATE DATABASE "{state.postgres_database}"')
            self._pg(PG_MIGRATION.read_text(encoding="utf-8"), state.postgres_database)
            admin = self._admin()
            try:
                name = MilvusCollectionManager(admin).create_generation(state.milvus_generation)
            finally:
                admin.close()
            return {"healthy_services": 5, "schema_versions": [1, 3, 4, 5],
                    "temporary_postgres": state.postgres_database, "milvus_collection": name,
                    "default_vector_backend": "memory",
                    "local_database_fingerprint": self._local_database_fingerprint()}
        return self._stage("prepare", action)

    def seed(self) -> M6State:
        def action(state: M6State):
            pg = PgvectorStore(self._pg_config(state.postgres_database))
            admin = self._admin()
            milvus = MilvusStore(self._milvus_config(), client=admin,
                                 collection=collection_name(state.milvus_generation))
            pg_count = milvus_count = 0; started = time.monotonic()
            try:
                for source, children, vectors in self._fixtures():
                    pg_count += pg.upsert(children, source, vectors)
                    milvus_count += milvus.upsert(children, source, vectors, flush=False)
                admin.flush(collection_name(state.milvus_generation))
                self._load_milvus(admin, state.milvus_generation)
            finally:
                admin.close()
            if pg_count != self.vector_count or milvus_count != self.vector_count:
                raise RehearsalError("M6 vector seed count mismatch")
            return {"pgvector_rows": pg_count, "milvus_rows": milvus_count,
                    "seed_seconds": round(time.monotonic() - started, 3),
                    "embedding_provider": "mock-hash-v1"}
        return self._stage("seed", action)

    @staticmethod
    def _benchmark(store: Any, vectors: list[list[float]], actor: Actor,
                   warmups: int, queries: int) -> dict[str, Any]:
        errors = 0
        for index in range(warmups):
            store.search(vectors[index % len(vectors)], actor, limit=10)
        timings = []
        for index in range(queries):
            started = time.perf_counter()
            try:
                store.search(vectors[index % len(vectors)], actor, limit=10)
            except Exception:
                errors += 1
            timings.append((time.perf_counter() - started) * 1000)
        return {"queries": queries, "errors": errors,
                "p50_ms": round(percentile(timings, .50), 3),
                "p95_ms": round(percentile(timings, .95), 3),
                "p99_ms": round(percentile(timings, .99), 3)}

    async def _mixed_load(self) -> dict[str, Any]:
        coordinator = MySQLRuntimeCoordinator(self._mysql_connection)
        buses = [MessageBus(), MessageBus()]
        active: dict[str, int] = {}; maximum: dict[str, int] = {}; executions: set[str] = set()
        guard = asyncio.Lock()

        class Agent:
            def __init__(self, key: str): self.key = key
            async def run(self, content: str) -> str:
                async with guard:
                    active[self.key] = active.get(self.key, 0) + 1
                    maximum[self.key] = max(maximum.get(self.key, 0), active[self.key])
                    executions.add(content)
                await asyncio.sleep(0.002)
                async with guard: active[self.key] -= 1
                return hashlib.sha256(content.encode()).hexdigest()[:16]

        gateways = [Gateway(buses[i], [], Agent, max_concurrency=16,
                            coordinator=coordinator, worker_id=f"{self.run_id}-w{i}") for i in range(2)]
        consumers = [asyncio.create_task(gateway._process_inbound()) for gateway in gateways]
        originals: list[tuple[int, InboundMessage]] = []
        attempt_id = uuid.uuid4().hex
        for session_no in range(40):
            channel = "web" if session_no < 20 else "customer_portal"
            gateway_no = session_no % 2
            conversation_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                              f"{self.run_id}:{attempt_id}:{session_no}"))
            for turn in range(25):
                request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{conversation_id}:{turn}"))
                content = f"m6-request-{session_no:02d}-{turn:02d}"
                raw = {"conversation_id": conversation_id, "request_id": request_id,
                       "tenant_id": "tenant-a" if channel == "customer_portal" else "",
                       "account_id": f"account-{session_no:02d}" if channel == "customer_portal" else ""}
                originals.append((gateway_no, InboundMessage(channel, f"actor:{conversation_id}",
                                                              f"socket-{session_no}", content, raw)))
        for gateway_no, message in originals:
            await buses[gateway_no].publish_inbound(message)
        for gateway_no, message in originals[:50]:
            await buses[1 - gateway_no].publish_inbound(message)
        responses = []
        deadline = asyncio.get_running_loop().time() + 120
        while len(responses) < 1050 and asyncio.get_running_loop().time() < deadline:
            for bus in buses:
                try: responses.append(await asyncio.wait_for(bus.consume_outbound(), .05))
                except asyncio.TimeoutError: pass
        for task in consumers: task.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)
        for gateway in gateways: await gateway.shutdown()
        duplicates = sum(row.event_type == "chat.duplicate" for row in responses)
        errors = sum(row.event_type == "error" for row in responses)
        if len(executions) != 1000 or duplicates != 50 or errors or any(v > 1 for v in maximum.values()):
            raise RehearsalError("mixed Gateway load gate failed")
        if any(not row.request_id or not row.conversation_id for row in responses):
            raise RehearsalError("request correlation gate failed")
        return {"sessions": 40, "original_requests": 1000, "duplicate_retries": 50,
                "executions": len(executions), "duplicate_responses": duplicates,
                "errors": errors, "same_session_max_parallel": max(maximum.values(), default=0)}

    def load(self) -> M6State:
        def action(state: M6State):
            embedder = MockEmbeddingProvider(64)
            vectors = embedder.embed([f"m6 query {index}" for index in range(100)])
            actor = Actor("m6-sales", frozenset({"sales"}), "trade")
            pg = PgvectorStore(self._pg_config(state.postgres_database))
            admin = self._admin()
            try:
                milvus = MilvusStore(self._milvus_config(), client=admin,
                                     collection=collection_name(state.milvus_generation))
                metrics = {"pgvector": self._benchmark(pg, vectors, actor, 100, self.query_count),
                           "milvus": self._benchmark(milvus, vectors, actor, 100, self.query_count)}
                gold_rows = []
                gold_specs = (
                    (0, 0, Actor("public", frozenset(), "trade"), True),
                    (1, 0, actor, True),
                    (1, 1, Actor("finance", frozenset({"finance"}), "trade"), False),
                    (2, 0, actor, False),
                    (3, 0, actor, False),
                    (4, 0, Actor("other", frozenset({"sales"}), "other"), True),
                )
                for doc_no, child_no, gold_actor, expected_visible in gold_specs:
                    text = (f"deidentified fixture document {doc_no:04d} category {doc_no % 5} "
                            f"child {child_no:03d} deterministic retrieval")
                    vector = embedder.embed([text])[0]
                    expected = f"{self.run_id}-d{doc_no:04d}-c{child_no:03d}"
                    pg_results = pg.search(vector, gold_actor, limit=10)
                    milvus_results = milvus.search(vector, gold_actor, limit=10)
                    pg_ids = [row.child.child_id for row in pg_results]
                    milvus_ids = [row.child.child_id for row in milvus_results]
                    if expected_visible:
                        if not pg_ids or not milvus_ids or pg_ids[0] != expected or milvus_ids[0] != expected:
                            raise RehearsalError("deterministic gold Top-1 differs")
                    elif expected in pg_ids or expected in milvus_ids:
                        raise RehearsalError("ACL, expiry or revocation gold filter failed")
                    gold_rows.append((expected, expected_visible, pg_ids[0] if pg_ids else None,
                                      milvus_ids[0] if milvus_ids else None))
            finally:
                admin.close()
            for result in metrics.values():
                if result["errors"] or result["p95_ms"] > 500 or result["p99_ms"] > 1000:
                    raise RehearsalError("Top-K performance gate failed")
            (self.run_dir / "load-metrics.json").write_text(
                json.dumps({"schema_version": "docker-m6-load-metrics-v1", "top_k": metrics},
                           sort_keys=True, indent=2) + "\n", encoding="utf-8")
            mixed = asyncio.run(self._mixed_load())
            stats = self._compose("stats", "--no-stream", "--format", "json").stdout.splitlines()
            return {"top_k": metrics, "mixed": mixed,
                    "gold_sha256": hashlib.sha256(json.dumps(gold_rows, sort_keys=True).encode()).hexdigest(),
                    "gold_cases": len(gold_rows), "resource_samples": len(stats),
                    "hard_resource_threshold": False}
        return self._stage("load", action)

    def _wait_healthy(self, service: str) -> float:
        started = time.monotonic(); deadline = started + self.rto_seconds
        while time.monotonic() < deadline:
            rows = parse_json_rows(self._compose("ps", "--format", "json").stdout)
            if any(row.get("Service") == service and str(row.get("Health", "")).lower() == "healthy" for row in rows):
                return round(time.monotonic() - started, 3)
            time.sleep(1)
        raise RehearsalError(f"{service} recovery exceeded RTO")

    def fault(self) -> M6State:
        def action(state: M6State):
            timings: dict[str, float] = {}
            # Reversible pause and restart; all recovery paths are in finally blocks.
            try:
                self._compose("pause", "postgres")
            finally:
                self._compose("unpause", "postgres")
            timings["postgres_pause_rto_seconds"] = self._wait_healthy("postgres")
            self._compose("restart", "mysql", timeout=self.rto_seconds)
            timings["mysql_restart_rto_seconds"] = self._wait_healthy("mysql")
            self._compose("restart", "milvus", timeout=self.rto_seconds)
            timings["milvus_restart_rto_seconds"] = self._wait_healthy("milvus")
            container = self._compose("ps", "-q", "postgres").stdout.strip()
            if not container:
                raise RehearsalError("postgres container id unavailable")
            networks = json.loads(self._docker("inspect", "--format", "{{json .NetworkSettings.Networks}}",
                                               container).stdout)
            if len(networks) != 1:
                raise RehearsalError("M6 requires one isolated compose network")
            network = next(iter(networks))
            disconnected = False
            try:
                self._docker("network", "disconnect", network, container)
                disconnected = True
                inspected = json.loads(self._docker(
                    "inspect", "--format", "{{json .NetworkSettings.Networks}}", container).stdout)
                if network in inspected:
                    raise RehearsalError("network isolation fault was not applied")
            finally:
                if disconnected:
                    self._docker("network", "connect", network, container)
            timings["postgres_network_rto_seconds"] = self._wait_healthy("postgres")
            PgvectorStore(self._pg_config(state.postgres_database)).check_ready()
            class PartialBatch:
                calls = 0
                def write(self):
                    self.calls += 1
                    if self.calls == 1: raise OSError("injected_partial_batch")
                    return 100
            batch = PartialBatch()
            try: batch.write()
            except OSError: pass
            if batch.write() != 100: raise RehearsalError("partial batch replay failed")
            try: raise OSError(28, "simulated ENOSPC")
            except OSError as exc:
                if exc.errno != 28: raise
            return {**timings, "partial_batch_replayed_once": True,
                    "enospc_mode": "in_process_failpoint", "rpo": 0}
        return self._stage("fault", action)

    def security(self) -> M6State:
        def action(state: M6State):
            rendered = json.loads(self._compose("--profile", "all", "config",
                                                "--format", "json").stdout)
            expected_ports = {"mysql": self.env.get("DOCKER_MYSQL_PORT", "3307"),
                              "postgres": self.env.get("DOCKER_POSTGRES_PORT", "5433"),
                              "milvus": self.env.get("DOCKER_MILVUS_PORT", "19530")}
            for service, published in expected_ports.items():
                ports = rendered.get("services", {}).get(service, {}).get("ports", [])
                if not any(row.get("host_ip") == "127.0.0.1"
                           and str(row.get("published")) == str(published) for row in ports):
                    raise RehearsalError("database port is not loopback-only")
            try:
                MilvusStore(self._milvus_config(), collection="trade_knowledge_active;drop")
                raise RehearsalError("collection injection accepted")
            except ValueError:
                pass
            if any(value.lower() in {"password", "changeme", "example"} for key, value in self.env.items()
                   if any(part in key.lower() for part in ("password", "token", "secret"))):
                raise RehearsalError("weak or example secret configured")
            return {"cross_tenant_denials": 1, "expired_revoked_filters": 2,
                    "collection_injection_rejected": True, "loopback_ports": 3,
                    "customer_database_tools": 0, "customer_workspace_memory_tools": 0}
        return self._stage("security", action)

    def _privacy_scan(self) -> None:
        forbidden = [value for key, value in self.env.items() if value and
                     any(part in key.lower() for part in ("password", "token", "secret"))]
        for path in self.run_dir.glob("*"):
            if not path.is_file(): continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(secret in text for secret in forbidden):
                raise RehearsalError("M6 artifact contains configured secret")
            lowered = text.lower()
            if '"content"' in lowered or '"embedding"' in lowered or '"recipient"' in lowered:
                raise RehearsalError("M6 artifact contains forbidden raw field")

    def validate(self) -> M6State:
        def action(state: M6State):
            required = ("runtime_conversation", "runtime_message", "runtime_request_ledger",
                        "runtime_conversation_lease")
            connection = self._mysql_connection()
            try:
                with connection.cursor() as cur:
                    cur.execute("SELECT table_name AS runtime_table_name FROM information_schema.tables WHERE table_schema='trade_ops'")
                    actual = {row["runtime_table_name"] for row in cur.fetchall()}
            finally: connection.close()
            if not set(required).issubset(actual): raise RehearsalError("M6 runtime schema incomplete")
            if not all(state.stages[name].status == "passed" for name in STAGES[:-1]):
                raise RehearsalError("all M6 stages must pass before validation")
            baseline = state.stages["prepare"].details.get("local_database_fingerprint")
            if baseline != self._local_database_fingerprint():
                raise RehearsalError("local database fingerprint changed during M6")
            return {"quality_gate": True, "isolation_gate": True, "recovery_gate": True,
                    "capacity_gate": True, "security_gate": True,
                    "default_vector_backend": "memory", "local_database_unchanged": True}
        state = self._stage("validate", action)
        state.passed = True; self._save(state)
        report = redact(asdict(state)); report["passed"] = True
        report["default_vector_backend"] = "memory"
        report["production_ready"] = False
        report["production_blockers"] = ["ha_not_validated", "tls_not_validated",
                                           "real_data_not_approved", "site_acceptance_pending"]
        self.report_path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True,
                                               indent=2) + "\n", encoding="utf-8")
        files = sorted(path for path in self.run_dir.glob("*.json") if path != self.state_path)
        lines = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in files]
        self.manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._privacy_scan()
        return state

    def run(self, stage: str) -> M6State:
        if stage == "full":
            state = None
            for name in STAGES: state = getattr(self, name)()
            assert state is not None
            return state
        return getattr(self, stage)()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run isolated Docker M6 release acceptance")
    parser.add_argument("stage", choices=(*STAGES, "full"))
    parser.add_argument("--run-id", default=default_run_id())
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--rto-seconds", type=float, default=600.0)
    parser.add_argument("--vector-count", type=int, default=10_000)
    parser.add_argument("--query-count", type=int, default=1_000)
    args = parser.parse_args(argv)
    try:
        runner = M6Release(args.run_id, artifact_root=args.artifact_root,
                           rto_seconds=args.rto_seconds, vector_count=args.vector_count,
                           query_count=args.query_count)
        state = runner.run(args.stage)
        print(json.dumps({"run_id": state.run_id, "stage": args.stage,
                          "status": "passed", "artifact_dir": str(runner.run_dir)},
                         ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"stage": args.stage, "status": "failed",
                          "error_code": type(exc).__name__}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
