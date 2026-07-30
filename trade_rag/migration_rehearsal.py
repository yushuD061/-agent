from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .config import RagVectorConfig
from .contracts import Actor, CanonicalDocument, ChildChunk, DocumentStatus
from .embeddings import MockEmbeddingProvider
from .milvus_admin import MilvusCollectionManager
from .milvus_store import ACTIVE_ALIAS, MilvusStore, collection_name
from .pgvector_store import PgvectorStore


ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "deploy" / "docker" / "compose.yaml"
DOCKER_ENV = ROOT / "deploy" / "docker" / ".env"
DEFAULT_ARTIFACT_ROOT = ROOT / ".tmp" / "docker-m5"
MIGRATIONS = (
    ROOT / "agent" / "business" / "migrations" / "001_trade_ops_core.mysql.sql",
    ROOT / "agent" / "business" / "migrations" / "003_email_accounts.mysql.sql",
    ROOT / "agent" / "business" / "migrations" / "004_email_delivery.mysql.sql",
)
RTO_SECONDS = 600.0
_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{5,39}$")
_GENERATION = re.compile(r"^trade_knowledge_v([1-9][0-9]*)$")
_SENSITIVE_PARTS = {"password", "token", "secret", "recipient", "body"}
_SENSITIVE_EXACT = {"embedding", "embeddings", "vector", "vectors"}


class RehearsalError(RuntimeError):
    pass


def validate_run_id(value: str) -> str:
    if not _RUN_ID.fullmatch(value):
        raise ValueError("run_id must be 6-40 lowercase letters, digits or hyphens")
    return value


def safe_database_name(prefix: str, run_id: str) -> str:
    validate_run_id(run_id)
    value = f"{prefix}_{run_id.replace('-', '_')}"
    if not re.fullmatch(r"[a-z][a-z0-9_]{5,63}", value):
        raise ValueError("unsafe restore database name")
    return value


def _normal(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite value cannot be hashed")
        return format(value, ".17g")
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime,)):
        return value.astimezone(timezone.utc).isoformat() if value.tzinfo else value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"sha256": hashlib.sha256(bytes(value)).hexdigest(), "bytes": len(value)}
    if isinstance(value, dict):
        return {str(key): _normal(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_normal(item) for item in value]
    return str(value)


def canonical_sha256(rows: Iterable[Any]) -> str:
    payload = json.dumps(_normal(list(rows)), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def redact(value: Any, key: str = "") -> Any:
    lowered = key.casefold()
    if lowered in _SENSITIVE_EXACT or any(part in lowered for part in _SENSITIVE_PARTS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def load_env(path: Path = DOCKER_ENV) -> dict[str, str]:
    if not path.is_file():
        raise RehearsalError("deploy/docker/.env is required")
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    required = {
        "DOCKER_MYSQL_ROOT_PASSWORD", "DOCKER_POSTGRES_PASSWORD",
        "DOCKER_MILVUS_ROOT_PASSWORD", "DOCKER_MILVUS_APP_USER",
        "DOCKER_MILVUS_APP_PASSWORD", "DOCKER_MINIO_ROOT_PASSWORD",
    }
    missing = sorted(name for name in required if not result.get(name))
    if missing:
        raise RehearsalError("missing Docker rehearsal configuration: " + ", ".join(missing))
    return result


def default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("m5-%Y%m%d-%H%M%S").lower()


@dataclass
class StageResult:
    status: str = "pending"
    duration_seconds: float | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class RehearsalState:
    schema_version: str
    run_id: str
    created_at: str
    cutoff_at: str | None
    rpo: int
    rto_limit_seconds: float
    source_generation: int | None
    restore_generation: int | None
    mysql_restore_database: str
    postgres_restore_database: str
    stages: dict[str, StageResult]

    @classmethod
    def new(cls, run_id: str, rto_seconds: float) -> "RehearsalState":
        return cls(
            schema_version="docker-m5-rehearsal-v1", run_id=run_id,
            created_at=datetime.now(timezone.utc).isoformat(), cutoff_at=None,
            rpo=0, rto_limit_seconds=rto_seconds, source_generation=None,
            restore_generation=None,
            mysql_restore_database=safe_database_name("trade_ops_m5", run_id),
            postgres_restore_database=safe_database_name("nanoclaw_vector_m5", run_id),
            stages={name: StageResult() for name in
                    ("prepare", "backup", "restore", "validate", "cutover", "rollback")},
        )

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "RehearsalState":
        payload = dict(payload)
        payload["stages"] = {name: StageResult(**item) for name, item in payload["stages"].items()}
        return cls(**payload)


class M5Rehearsal:
    def __init__(self, run_id: str, *, artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
                 rto_seconds: float = RTO_SECONDS, env: dict[str, str] | None = None):
        self.run_id = validate_run_id(run_id)
        if rto_seconds <= 0 or rto_seconds > RTO_SECONDS:
            raise ValueError("rto_seconds must be between 0 and 600")
        self.artifact_root = artifact_root.resolve()
        self.run_dir = self.artifact_root / run_id
        self.state_path = self.run_dir / "state.json"
        self.manifest_path = self.run_dir / "sha256-manifest.json"
        self.report_path = self.run_dir / "report.json"
        self.env = dict(env) if env is not None else load_env()
        self.rto_seconds = float(rto_seconds)

    def _compose(self, *args: str, input_text: str | None = None,
                 check: bool = True) -> subprocess.CompletedProcess[str]:
        command = ["docker", "compose", "--env-file", str(DOCKER_ENV),
                   "-f", str(COMPOSE), *args]
        return subprocess.run(command, cwd=ROOT, input=input_text, text=True,
                              encoding="utf-8", errors="replace",
                              capture_output=True, check=check)

    def _docker(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["docker", *args], cwd=ROOT, text=True,
                              encoding="utf-8", errors="replace",
                              capture_output=True, check=check)

    def _service_container(self, service: str) -> str:
        result = self._compose("ps", "-q", service).stdout.strip()
        if not result:
            raise RehearsalError(f"Docker service is not running: {service}")
        return result

    def _load_state(self, *, create: bool = False) -> RehearsalState:
        if self.state_path.exists():
            return RehearsalState.from_json(json.loads(self.state_path.read_text(encoding="utf-8")))
        if not create:
            raise RehearsalError("prepare must complete before this stage")
        self.run_dir.mkdir(parents=True, exist_ok=False)
        state = RehearsalState.new(self.run_id, self.rto_seconds)
        self._save_state(state)
        return state

    def _save_state(self, state: RehearsalState) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        payload = asdict(state)
        self.state_path.write_text(json.dumps(redact(payload), ensure_ascii=False,
                                              sort_keys=True, indent=2) + "\n", encoding="utf-8")

    @contextmanager
    def _lock(self):
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        lock = self.artifact_root / ".lock"
        try:
            lock.mkdir()
        except FileExistsError as exc:
            raise RehearsalError("another M5 rehearsal holds the lock") from exc
        try:
            yield
        finally:
            lock.rmdir()

    def _run_stage(self, name: str, action) -> RehearsalState:
        state = self._load_state(create=name == "prepare")
        current = state.stages[name]
        if current.status == "passed":
            return state
        started = time.monotonic()
        current.status = "running"
        self._save_state(state)
        try:
            details = action(state) or {}
            duration = time.monotonic() - started
            if name == "restore" and duration > state.rto_limit_seconds:
                raise RehearsalError("restore exceeded the configured RTO")
            current.status = "passed"
            current.duration_seconds = round(duration, 3)
            current.details = redact(details)
        except Exception as exc:
            current.status = "failed"
            current.duration_seconds = round(time.monotonic() - started, 3)
            current.details = {"error_code": type(exc).__name__}
            self._save_state(state)
            raise
        self._save_state(state)
        return state

    def _mysql_exec(self, sql: str, database: str | None = None) -> None:
        target = f" --database={database}" if database else ""
        self._compose("exec", "-T", "mysql", "sh", "-c",
                      f'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot{target}',
                      input_text=sql)

    def _pg_exec(self, sql: str, database: str | None = None) -> None:
        db = database or self.env.get("DOCKER_POSTGRES_DATABASE", "nanoclaw_vector_docker")
        self._compose("exec", "-T", "postgres", "psql", "-v", "ON_ERROR_STOP=1",
                      "-U", self.env.get("DOCKER_POSTGRES_USER", "nanoclaw_vector_docker"),
                      "-d", db, input_text=sql)

    def _mysql_connection(self, database: str):
        import pymysql
        return pymysql.connect(
            host="127.0.0.1", port=int(self.env.get("DOCKER_MYSQL_PORT", "3307")),
            user="root", password=self.env["DOCKER_MYSQL_ROOT_PASSWORD"],
            database=database, charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
        )

    def _mysql_database_exists(self, database: str) -> bool:
        import pymysql
        connection = pymysql.connect(
            host="127.0.0.1", port=int(self.env.get("DOCKER_MYSQL_PORT", "3307")),
            user="root", password=self.env["DOCKER_MYSQL_ROOT_PASSWORD"], charset="utf8mb4",
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME=%s",
                               (database,))
                return cursor.fetchone() is not None
        finally:
            connection.close()

    def _pg_database_exists(self, database: str) -> bool:
        import psycopg
        source = self._pg_config(self.env.get("DOCKER_POSTGRES_DATABASE",
                                               "nanoclaw_vector_docker"))
        with psycopg.connect(**source.connection_kwargs) as connection:
            return connection.execute("SELECT 1 FROM pg_database WHERE datname=%s",
                                      (database,)).fetchone() is not None

    def _pg_config(self, database: str) -> RagVectorConfig:
        return RagVectorConfig(
            backend="pgvector", dimensions=64, host="127.0.0.1",
            port=int(self.env.get("DOCKER_POSTGRES_PORT", "5433")), database=database,
            user=self.env.get("DOCKER_POSTGRES_USER", "nanoclaw_vector_docker"),
            password=self.env["DOCKER_POSTGRES_PASSWORD"], sslmode="disable",
            connect_timeout_seconds=5,
        )

    def _milvus_config(self) -> RagVectorConfig:
        return RagVectorConfig(
            backend="milvus", dimensions=64,
            milvus_uri=f"http://127.0.0.1:{self.env.get('DOCKER_MILVUS_PORT', '19530')}",
            milvus_token=f"{self.env['DOCKER_MILVUS_APP_USER']}:{self.env['DOCKER_MILVUS_APP_PASSWORD']}",
            milvus_database="nanoclaw_vector_docker", milvus_alias=ACTIVE_ALIAS,
            milvus_connect_timeout_seconds=10,
        )

    def _milvus_admin(self):
        from pymilvus import MilvusClient
        config = self._milvus_config()
        return MilvusClient(uri=config.milvus_uri,
                            token=f"root:{self.env['DOCKER_MILVUS_ROOT_PASSWORD']}",
                            db_name=config.milvus_database, timeout=10)

    @staticmethod
    def _load_generation(client, generation: int) -> None:
        from pymilvus import MilvusClient
        name = collection_name(generation)
        if "embedding_cosine" not in client.list_indexes(name):
            indexes = MilvusClient.prepare_index_params()
            indexes.add_index("embedding", index_type="AUTOINDEX", metric_type="COSINE",
                              index_name="embedding_cosine")
            client.create_index(name, indexes)
        client.load_collection(name)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            state = str(client.get_load_state(name).get("state", "")).casefold()
            if state in {"loaded", "loadstate.loaded"}:
                return
            time.sleep(0.5)
        raise RehearsalError("Milvus restored generation did not become loaded")

    def _fixtures(self) -> list[tuple[CanonicalDocument, list[ChildChunk], list[list[float]]]]:
        expires = datetime.now(timezone.utc) - timedelta(days=1)
        specs = (
            ("public", frozenset(), "trade", DocumentStatus.PUBLISHED, None, "public shipping guidance"),
            ("sales", frozenset({"sales"}), "trade", DocumentStatus.APPROVED, None, "sales quotation guidance"),
            ("expired", frozenset({"sales"}), "trade", DocumentStatus.PUBLISHED, expires, "expired guidance"),
            ("revoked", frozenset({"sales"}), "trade", DocumentStatus.REVOKED, None, "revoked guidance"),
            ("other", frozenset({"sales"}), "other", DocumentStatus.PUBLISHED, None, "other unit guidance"),
        )
        embedder = MockEmbeddingProvider(64)
        result = []
        for label, roles, unit, status, expires_at, text in specs:
            document_id = f"{self.run_id}-{label}"
            document = CanonicalDocument(
                document_id=document_id, version=1, source_uri=f"sample://{document_id}",
                title=f"M5 {label}", content=text,
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
                business_unit_id=unit, allowed_roles=roles, classification="internal",
                status=status, expires_at=expires_at, parser_version="m5-fixture-v1",
                metadata={"fixture": "docker-m5-v1"},
            )
            children = [ChildChunk(
                child_id=f"{document_id}-child", parent_id=f"{document_id}-parent",
                document_id=document_id, text=text, location="page-1",
                content_hash=hashlib.sha256((text + "-child").encode()).hexdigest(),
                metadata={"fixture": "docker-m5-v1"},
            )]
            result.append((document, children, embedder.embed([text])))
        return result

    def _seed_mysql(self) -> None:
        suffix = hashlib.sha256(self.run_id.encode()).hexdigest()
        uid = f"{suffix[:8]}-{suffix[8:12]}-4{suffix[13:16]}-a{suffix[17:20]}-{suffix[20:32]}"
        sql = f"""
USE trade_ops;
START TRANSACTION;
INSERT INTO ops_customer(customer_id,company_name_masked,country_code,owner_user_id,business_unit_id,created_at,updated_at)
VALUES ('m5-{self.run_id}','M5 *** Trading','DE','m5-operator','trade','2026-01-01','2026-01-01');
INSERT INTO ops_product(sku,name_cn,name_en,category_code,quantity_unit,moq,lead_time_days)
VALUES ('M5-{self.run_id}','脱敏夹具','Deidentified fixture','fixture','pcs',10,7);
INSERT INTO ops_rfq_request(rfq_id,customer_id,source_channel,source_hash,received_at)
VALUES ('rfq-{self.run_id}','m5-{self.run_id}','fixture','{suffix}','2026-01-01');
INSERT INTO ops_quote(quote_id,rfq_id,customer_id,current_version_no,status,created_at)
VALUES ('quote-{self.run_id}','rfq-{self.run_id}','m5-{self.run_id}',1,'approved','2026-01-01');
INSERT INTO ops_quote_version(quote_id,version_no,calculation_id,subtotal_amount,total_amount,currency_code,valid_until,content_hash,calculation_hash,created_by,created_at)
VALUES ('quote-{self.run_id}',1,'calc-{self.run_id}',100,100,'USD','2026-12-31','{suffix}','{suffix}','m5-operator','2026-01-01');
INSERT INTO ops_approval_record(quote_id,version_no,action,approval_status,required_role,reviewer_user_id,content_hash,calculation_hash,acted_at)
VALUES ('quote-{self.run_id}',1,'send','approved','sales_manager','m5-reviewer','{suffix}','{suffix}','2026-01-01');
SET @approval_key = LAST_INSERT_ID();
INSERT INTO ops_email_account(account_id,display_name,provider,address,secret_ref,allowed_senders_json,allowed_recipients_json,status,created_at,updated_at)
VALUES ('{uid}','M5 fixture {self.run_id}','smtp','m5-{self.run_id}@example.invalid','m5-secret-ref-{self.run_id}','[]','[\"allowed@example.invalid\"]','healthy','2026-01-01','2026-01-01');
INSERT INTO ops_email_delivery(delivery_id,idempotency_key,account_id,quote_id,quote_version,approval_key,recipient,subject_snapshot,body_snapshot,content_hash,snapshot_hash,status,smtp_message_id,created_by,created_at,updated_at)
SELECT UUID(),'{suffix}','{uid}',quote_key,1,@approval_key,'allowed@example.invalid','M5 fixture','Deidentified fixture body','{suffix}','{suffix}','pending','<m5-{self.run_id}@example.invalid>','m5-operator','2026-01-01','2026-01-01'
FROM ops_quote WHERE quote_id='quote-{self.run_id}';
COMMIT;
"""
        self._mysql_exec(sql)

    def _seed_vectors(self, pg_database: str, generation: int) -> None:
        pg = PgvectorStore(self._pg_config(pg_database))
        milvus = MilvusStore(self._milvus_config())
        admin = self._milvus_admin()
        try:
            manager = MilvusCollectionManager(admin)
            manager.create_generation(generation)
            manager.activate_generation(generation)
            for document, children, vectors in self._fixtures():
                pg.upsert(children, document, vectors)
                milvus.upsert(children, document, vectors)
        finally:
            admin.close()

    def prepare(self) -> RehearsalState:
        def action(state: RehearsalState):
            from .config import load_rag_vector_config
            if load_rag_vector_config().backend != "memory":
                raise RehearsalError("M5 requires the default vector backend to remain memory")
            raw_status = self._compose("--profile", "all", "ps", "--format", "json").stdout.strip()
            status = (json.loads(raw_status) if raw_status.startswith("[") else
                      [json.loads(line) for line in raw_status.splitlines() if line.strip()])
            services = {item.get("Service"): item for item in status}
            required = {"mysql", "postgres", "etcd", "minio", "milvus"}
            unhealthy = sorted(name for name in required if
                               name not in services or services[name].get("Health") != "healthy")
            if unhealthy:
                raise RehearsalError("Docker services are not healthy: " + ", ".join(unhealthy))
            self._mysql_exec(MIGRATIONS[0].read_text(encoding="utf-8"))
            for migration in MIGRATIONS[1:]:
                self._mysql_exec(migration.read_text(encoding="utf-8"), "trade_ops")
            self._seed_mysql()
            admin = self._milvus_admin()
            try:
                generations = [int(match.group(1)) for name in admin.list_collections()
                               if (match := _GENERATION.fullmatch(name))]
            finally:
                admin.close()
            source = max(generations, default=0) + 1
            state.source_generation = source
            state.restore_generation = source + 1
            self._seed_vectors(self.env.get("DOCKER_POSTGRES_DATABASE", "nanoclaw_vector_docker"), source)
            state.cutoff_at = datetime.now(timezone.utc).isoformat()
            return {"services_healthy": sorted(required), "fixture_set": "docker-m5-v1",
                    "write_frozen": True, "source_generation": source}
        with self._lock():
            return self._run_stage("prepare", action)

    def _copy_from(self, service: str, container_path: str, destination: Path) -> None:
        container = self._service_container(service)
        self._docker("cp", f"{container}:{container_path}", str(destination))

    def _backup_api(self, route: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        port = self.env.get("DOCKER_MILVUS_BACKUP_PORT", "18080")
        url = f"http://127.0.0.1:{port}/api/v1/{route}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data,
                                         headers={"Content-Type": "application/json"},
                                         method="GET" if data is None else "POST")
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RehearsalError("milvus backup API unavailable") from exc
        if int(result.get("code", 0)) != 0:
            raise RehearsalError("milvus backup API rejected the operation")
        return result

    def _start_backup_service(self) -> None:
        self._compose("--profile", "migration", "up", "-d", "milvus-backup")
        deadline = time.monotonic() + 60
        port = self.env.get("DOCKER_MILVUS_BACKUP_PORT", "18080")
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, TimeoutError):
                time.sleep(1)
        raise RehearsalError("milvus backup service did not become ready")

    def backup(self) -> RehearsalState:
        def action(state: RehearsalState):
            if state.cutoff_at is None or state.source_generation is None:
                raise RehearsalError("write cutoff is missing")
            mysql_file = self.run_dir / "mysql.sql"
            pg_file = self.run_dir / "postgres.dump"
            mysql_tmp = f"/tmp/{self.run_id}.sql"
            pg_tmp = f"/tmp/{self.run_id}.dump"
            self._compose("exec", "-T", "mysql", "sh", "-c",
                          f'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqldump --single-transaction --routines --events --triggers --no-tablespaces -uroot trade_ops > {mysql_tmp}')
            self._copy_from("mysql", mysql_tmp, mysql_file)
            self._compose("exec", "-T", "postgres", "pg_dump", "-Fc", "-f", pg_tmp,
                          "-U", self.env.get("DOCKER_POSTGRES_USER", "nanoclaw_vector_docker"),
                          self.env.get("DOCKER_POSTGRES_DATABASE", "nanoclaw_vector_docker"))
            self._copy_from("postgres", pg_tmp, pg_file)
            self._start_backup_service()
            backup_name = f"m5_{self.run_id.replace('-', '_')}"
            metadata = self._backup_api("create", {
                "async": False, "backup_name": backup_name,
                "collection_names": [
                    f"nanoclaw_vector_docker.{collection_name(state.source_generation)}"
                ],
            })
            (self.run_dir / "milvus-backup.json").write_text(
                json.dumps(redact(metadata), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8")
            self._write_manifest()
            return {"mysql_backup": mysql_file.name, "postgres_backup": pg_file.name,
                    "milvus_backup_name": backup_name}
        with self._lock():
            return self._run_stage("backup", action)

    def _verify_manifest(self) -> None:
        if not self.manifest_path.is_file():
            raise RehearsalError("backup checksum manifest is missing")
        payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        for name, expected in payload["files"].items():
            path = self.run_dir / name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RehearsalError("backup checksum verification failed")

    def _write_manifest(self) -> None:
        names = ("mysql.sql", "postgres.dump", "milvus-backup.json")
        files = {name: hashlib.sha256((self.run_dir / name).read_bytes()).hexdigest()
                 for name in names}
        self.manifest_path.write_text(json.dumps({"algorithm": "sha256", "files": files},
                                                  sort_keys=True, indent=2) + "\n", encoding="utf-8")

    def restore(self) -> RehearsalState:
        def action(state: RehearsalState):
            self._verify_manifest()
            mysql_db = state.mysql_restore_database
            pg_db = state.postgres_restore_database
            service_seconds: dict[str, float] = {}
            started = time.monotonic()
            if not self._mysql_database_exists(mysql_db):
                self._mysql_exec(f"CREATE DATABASE `{mysql_db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;")
                self._compose("exec", "-T", "mysql", "sh", "-c",
                              f'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot --database={mysql_db}',
                              input_text=(self.run_dir / "mysql.sql").read_text(encoding="utf-8"))
            service_seconds["mysql"] = round(time.monotonic() - started, 3)
            started = time.monotonic()
            pg_user = self.env.get("DOCKER_POSTGRES_USER", "nanoclaw_vector_docker")
            if not self._pg_database_exists(pg_db):
                self._compose("exec", "-T", "postgres", "createdb", "-U", pg_user, pg_db)
                pg_tmp = f"/tmp/{self.run_id}.dump"
                container = self._service_container("postgres")
                self._docker("cp", str(self.run_dir / "postgres.dump"), f"{container}:{pg_tmp}")
                self._compose("exec", "-T", "postgres", "pg_restore", "--exit-on-error",
                              "--no-owner", "--no-privileges", "-U", pg_user, "-d", pg_db, pg_tmp)
            service_seconds["postgres"] = round(time.monotonic() - started, 3)
            started = time.monotonic()
            self._start_backup_service()
            backup_name = f"m5_{self.run_id.replace('-', '_')}"
            renames = {
                f"nanoclaw_vector_docker.{collection_name(state.source_generation)}":
                f"nanoclaw_vector_docker.{collection_name(state.restore_generation)}"
            }
            self._backup_api("restore", {
                "async": False, "backup_name": backup_name,
                "collection_renames": renames,
            })
            service_seconds["milvus"] = round(time.monotonic() - started, 3)
            exceeded = sorted(name for name, seconds in service_seconds.items()
                              if seconds > state.rto_limit_seconds)
            if exceeded:
                raise RehearsalError("service restore exceeded the configured RTO")
            return {"mysql_database": mysql_db, "postgres_database": pg_db,
                    "restore_generation": state.restore_generation, "rpo": 0,
                    "service_rto_seconds": service_seconds}
        with self._lock():
            return self._run_stage("restore", action)

    @staticmethod
    def _mysql_snapshot(connection) -> dict[str, dict[str, Any]]:
        with connection.cursor() as cursor:
            cursor.execute("SHOW TABLES")
            tables = sorted(next(iter(row.values())) for row in cursor.fetchall())
            result = {}
            for table in tables:
                cursor.execute(f"SELECT * FROM `{table}`")
                rows = sorted((_normal(row) for row in cursor.fetchall()),
                              key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
                result[table] = {"rows": len(rows), "sha256": canonical_sha256(rows)}
            return result

    @staticmethod
    def _pg_snapshot(config: RagVectorConfig) -> dict[str, Any]:
        import psycopg
        with psycopg.connect(**config.connection_kwargs) as connection:
            rows = connection.execute("SELECT * FROM rag_child_vector ORDER BY child_id").fetchall()
            columns = [column.name for column in connection.execute(
                "SELECT * FROM rag_child_vector LIMIT 0").description]
        normalized = [dict(zip(columns, row)) for row in rows]
        return {"rows": len(normalized), "sha256": canonical_sha256(normalized)}

    def _gold(self, store) -> dict[str, list[str]]:
        embedder = MockEmbeddingProvider(64)
        query = embedder.embed(["sales quotation guidance"])[0]
        actors = {
            "sales": Actor("m5-sales", frozenset({"sales"}), "trade"),
            "finance": Actor("m5-finance", frozenset({"finance"}), "trade"),
            "other": Actor("m5-sales", frozenset({"sales"}), "other"),
        }
        return {name: [row.child.child_id for row in store.search(query, actor, 30)
                       if row.child.child_id.startswith(f"{self.run_id}-")]
                for name, actor in actors.items()}

    def validate(self) -> RehearsalState:
        def action(state: RehearsalState):
            with self._mysql_connection("trade_ops") as source, \
                    self._mysql_connection(state.mysql_restore_database) as restored:
                mysql_source = self._mysql_snapshot(source)
                mysql_restored = self._mysql_snapshot(restored)
            if mysql_source != mysql_restored:
                raise RehearsalError("MySQL restored data does not match the cutoff")
            source_pg = self._pg_config(self.env.get("DOCKER_POSTGRES_DATABASE", "nanoclaw_vector_docker"))
            restored_pg = self._pg_config(state.postgres_restore_database)
            if self._pg_snapshot(source_pg) != self._pg_snapshot(restored_pg):
                raise RehearsalError("PostgreSQL restored data does not match the cutoff")
            pg_gold = self._gold(PgvectorStore(restored_pg))
            admin = self._milvus_admin()
            try:
                self._load_generation(admin, state.restore_generation)
                MilvusCollectionManager(admin).activate_generation(state.restore_generation)
                milvus_gold = self._gold(MilvusStore(self._milvus_config()))
            finally:
                MilvusCollectionManager(admin).activate_generation(state.source_generation)
                admin.close()
            if {key: sorted(value) for key, value in pg_gold.items()} != \
                    {key: sorted(value) for key, value in milvus_gold.items()}:
                raise RehearsalError("vector backend gold results differ")
            return {"mysql": mysql_source, "postgres": self._pg_snapshot(source_pg),
                    "gold": pg_gold, "rpo": 0}
        with self._lock():
            return self._run_stage("validate", action)

    def cutover(self) -> RehearsalState:
        def action(state: RehearsalState):
            admin = self._milvus_admin()
            try:
                self._load_generation(admin, state.restore_generation)
                result = MilvusCollectionManager(admin).activate_generation(state.restore_generation)
            finally:
                admin.close()
            self._gold(PgvectorStore(self._pg_config(state.postgres_restore_database)))
            with self._mysql_connection(state.mysql_restore_database) as connection:
                if not self._mysql_snapshot(connection):
                    raise RehearsalError("restored MySQL database is not ready")
            return {"active_generation": result["active"], "temporary_targets_only": True}
        with self._lock():
            return self._run_stage("cutover", action)

    def rollback(self) -> RehearsalState:
        def action(state: RehearsalState):
            admin = self._milvus_admin()
            try:
                result = MilvusCollectionManager(admin).activate_generation(state.source_generation)
                active = MilvusCollectionManager(admin).check()["active_collection"]
            finally:
                admin.close()
            if active != collection_name(state.source_generation):
                raise RehearsalError("Milvus rollback did not restore the source generation")
            self._gold(PgvectorStore(self._pg_config(
                self.env.get("DOCKER_POSTGRES_DATABASE", "nanoclaw_vector_docker"))))
            with self._mysql_connection("trade_ops") as connection:
                self._mysql_snapshot(connection)
            return {"active_generation": result["active"], "restore_generation_retained": True}
        with self._lock():
            state = self._run_stage("rollback", action)
            self._write_report(state)
            return state

    def _write_report(self, state: RehearsalState) -> None:
        passed = all(stage.status == "passed" for stage in state.stages.values())
        payload = redact({**asdict(state), "passed": passed,
                          "default_vector_backend": "memory",
                          "production_ready": False})
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        lowered = serialized.casefold()
        if any(self.env[name] and self.env[name].casefold() in lowered for name in self.env if
               any(part in name.casefold() for part in ("password", "token", "secret"))):
            raise RehearsalError("report contains a configured secret")
        self.report_path.write_text(serialized, encoding="utf-8")

    def run(self, stage: str) -> RehearsalState:
        if stage == "full":
            state = None
            for name in ("prepare", "backup", "restore", "validate", "cutover", "rollback"):
                state = getattr(self, name)()
            assert state is not None
            return state
        return getattr(self, stage)()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the isolated Docker M5 migration rehearsal")
    parser.add_argument("stage", choices=("prepare", "backup", "restore", "validate",
                                          "cutover", "rollback", "full"))
    parser.add_argument("--run-id", default=default_run_id())
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--rto-seconds", type=float, default=RTO_SECONDS)
    args = parser.parse_args(argv)
    try:
        runner = M5Rehearsal(args.run_id, artifact_root=args.artifact_root,
                             rto_seconds=args.rto_seconds)
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
