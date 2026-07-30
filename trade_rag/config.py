from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_PROJECT_ENV = Path(__file__).resolve().parent.parent / ".env"

def _load_project_env() -> None:
    """读取项目根目录简单 KEY=VALUE；不覆盖进程或容器环境变量。"""
    if not _PROJECT_ENV.is_file(): return
    try:
        original_keys = set(os.environ)
        for raw in _PROJECT_ENV.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            key, value = line.split("=", 1)
            if key.strip() not in original_keys:
                os.environ[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        return

def _positive_int(name: str, default: int) -> int:
    try: value = int(os.environ.get(name, str(default)))
    except ValueError as exc: raise ValueError(f"{name} must be an integer") from exc
    if value <= 0: raise ValueError(f"{name} must be greater than 0")
    return value

def _positive_float(name: str, default: float) -> float:
    try: value = float(os.environ.get(name, str(default)))
    except ValueError as exc: raise ValueError(f"{name} must be a number") from exc
    if value <= 0: raise ValueError(f"{name} must be greater than 0")
    return value

def _ratio(name: str, default: float) -> float:
    value = _positive_float(name, default)
    if value > 1:
        raise ValueError(f"{name} must be less than or equal to 1")
    return value

@dataclass(frozen=True)
class RemoteApiConfig:
    api_key: str; base_url: str; model: str; timeout_seconds: float
    @property
    def configured(self) -> bool: return bool(self.api_key and self.base_url and self.model)

@dataclass(frozen=True)
class RagApiConfig:
    embedding: RemoteApiConfig
    embedding_dimensions: int
    embedding_batch_size: int
    rerank: RemoteApiConfig
    rerank_max_candidates: int
    remote_data_transfer_approved: bool

    def validate_remote_ready(self) -> None:
        missing=[]
        if not self.embedding.configured: missing.append("RAG_EMBEDDING_API_KEY/BASE_URL/MODEL")
        if not self.rerank.configured: missing.append("RAG_RERANK_API_KEY/BASE_URL/MODEL")
        if missing: raise ValueError("missing RAG API configuration: " + ", ".join(missing))
        if not self.remote_data_transfer_approved: raise ValueError("RAG_REMOTE_DATA_TRANSFER_APPROVED must be true before remote calls")


@dataclass(frozen=True)
class RagVectorConfig:
    backend: str
    dimensions: int
    host: str = ""
    port: int = 5432
    database: str = ""
    user: str = ""
    password: str = ""
    sslmode: str = "prefer"
    connect_timeout_seconds: int = 5
    milvus_uri: str = ""
    milvus_token: str = ""
    milvus_database: str = "nanoclaw_vector_docker"
    milvus_alias: str = "trade_knowledge_active"
    milvus_connect_timeout_seconds: int = 5
    sqlite_path: str = "workspace/knowledge_base/rag_index.db"

    def validate(self) -> None:
        if self.backend not in {"memory", "sqlite", "pgvector", "milvus"}:
            raise ValueError("RAG_VECTOR_BACKEND must be memory, sqlite, pgvector or milvus")
        if self.dimensions != 64:
            raise ValueError("M2/M3 requires RAG_VECTOR_DIMENSIONS=64")
        if self.backend == "memory":
            return
        if self.backend == "sqlite":
            if not self.sqlite_path.strip():
                raise ValueError("RAG_SQLITE_INDEX_PATH is required for sqlite")
            return
        if self.backend == "milvus":
            missing = [name for name, value in (
                ("RAG_MILVUS_URI", self.milvus_uri),
                ("RAG_MILVUS_TOKEN", self.milvus_token),
                ("RAG_MILVUS_DATABASE", self.milvus_database),
                ("RAG_MILVUS_COLLECTION_ALIAS", self.milvus_alias),
            ) if not value]
            if missing:
                raise ValueError("missing Milvus configuration: " + ", ".join(missing))
            if self.milvus_alias != "trade_knowledge_active":
                raise ValueError("M3 requires RAG_MILVUS_COLLECTION_ALIAS=trade_knowledge_active")
            return
        missing = [name for name, value in (
            ("RAG_PGVECTOR_HOST", self.host),
            ("RAG_PGVECTOR_DATABASE", self.database),
            ("RAG_PGVECTOR_USER", self.user),
            ("RAG_PGVECTOR_PASSWORD", self.password),
        ) if not value]
        if missing:
            raise ValueError("missing pgvector configuration: " + ", ".join(missing))
        if self.sslmode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
            raise ValueError("RAG_PGVECTOR_SSLMODE is invalid")

    @property
    def connection_kwargs(self) -> dict[str, object]:
        self.validate()
        if self.backend != "pgvector":
            raise ValueError("pgvector connection requested while backend is not pgvector")
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.database,
            "user": self.user,
            "password": self.password,
            "sslmode": self.sslmode,
            "connect_timeout": self.connect_timeout_seconds,
        }

    @property
    def milvus_connection_kwargs(self) -> dict[str, object]:
        self.validate()
        if self.backend != "milvus":
            raise ValueError("Milvus connection requested while backend is not milvus")
        return {
            "uri": self.milvus_uri,
            "token": self.milvus_token,
            "db_name": self.milvus_database,
            "timeout": self.milvus_connect_timeout_seconds,
        }


@dataclass(frozen=True)
class RagKeywordConfig:
    backend: str
    url: str = ""
    username: str = ""
    password: str = ""
    index: str = "trade_knowledge_child_v1"
    timeout_seconds: float = 10.0
    sqlite_path: str = "workspace/knowledge_base/rag_index.db"

    def validate(self) -> None:
        if self.backend not in {"memory", "sqlite", "elasticsearch"}:
            raise ValueError("RAG_KEYWORD_BACKEND must be memory, sqlite or elasticsearch")
        if self.backend == "memory":
            return
        if self.backend == "sqlite":
            if not self.sqlite_path.strip():
                raise ValueError("RAG_SQLITE_INDEX_PATH is required for sqlite")
            return
        missing = [name for name, value in (
            ("RAG_ELASTICSEARCH_URL", self.url),
            ("RAG_ELASTICSEARCH_USERNAME", self.username),
            ("RAG_ELASTICSEARCH_PASSWORD", self.password),
            ("RAG_ELASTICSEARCH_INDEX", self.index),
        ) if not value]
        if missing:
            raise ValueError("missing Elasticsearch configuration: " + ", ".join(missing))
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("RAG_ELASTICSEARCH_URL must use http or https")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", self.index):
            raise ValueError("RAG_ELASTICSEARCH_INDEX is invalid")

@dataclass(frozen=True)
class PdfIngestionConfig:
    """M0 resource and OCR-gate thresholds; M1 parsers must enforce these limits."""
    max_bytes: int
    max_pages: int
    max_chars_per_page: int
    max_total_chars: int
    parse_timeout_seconds: float
    max_concurrent_parses: int
    min_text_page_ratio: float
    min_average_chars_per_text_page: int
    max_consecutive_blank_pages: int

    def __post_init__(self) -> None:
        if self.max_total_chars < self.max_chars_per_page:
            raise ValueError("RAG_PDF_MAX_TOTAL_CHARS must be greater than or equal to RAG_PDF_MAX_CHARS_PER_PAGE")

@dataclass(frozen=True)
class OcrConfig:
    enabled: bool
    provider: str
    language: str
    device: str
    min_confidence: float
    max_images_per_document: int
    max_image_bytes: int
    max_image_pixels: int
    max_ocr_chars_per_image: int
    pdf_dpi: int
    min_embedded_width: int
    min_embedded_height: int

    def __post_init__(self) -> None:
        if self.provider != "paddleocr":
            raise ValueError("RAG_OCR_PROVIDER must be paddleocr")
        if self.device not in {"cpu", "gpu"}:
            raise ValueError("RAG_OCR_DEVICE must be cpu or gpu")

def load_rag_api_config() -> RagApiConfig:
    _load_project_env()
    embedding=RemoteApiConfig(os.environ.get("RAG_EMBEDDING_API_KEY", ""), os.environ.get("RAG_EMBEDDING_BASE_URL", "").rstrip("/"), os.environ.get("RAG_EMBEDDING_MODEL", ""), _positive_float("RAG_EMBEDDING_TIMEOUT_SECONDS", 30))
    rerank=RemoteApiConfig(os.environ.get("RAG_RERANK_API_KEY", ""), os.environ.get("RAG_RERANK_BASE_URL", "").rstrip("/"), os.environ.get("RAG_RERANK_MODEL", ""), _positive_float("RAG_RERANK_TIMEOUT_SECONDS", 30))
    return RagApiConfig(embedding, _positive_int("RAG_EMBEDDING_DIMENSIONS", 1024), _positive_int("RAG_EMBEDDING_BATCH_SIZE", 32), rerank, _positive_int("RAG_RERANK_MAX_CANDIDATES", 30), os.environ.get("RAG_REMOTE_DATA_TRANSFER_APPROVED", "false").strip().lower() in {"1", "true", "yes", "on"})


def load_rag_vector_config() -> RagVectorConfig:
    _load_project_env()
    backend = os.environ.get("RAG_VECTOR_BACKEND", "memory").strip().lower()
    config = RagVectorConfig(
        backend=backend,
        dimensions=_positive_int("RAG_VECTOR_DIMENSIONS", 64),
        host=os.environ.get("RAG_PGVECTOR_HOST", "").strip(),
        port=_positive_int("RAG_PGVECTOR_PORT", 5432),
        database=os.environ.get("RAG_PGVECTOR_DATABASE", "").strip(),
        user=os.environ.get("RAG_PGVECTOR_USER", "").strip(),
        password=os.environ.get("RAG_PGVECTOR_PASSWORD", ""),
        sslmode=os.environ.get("RAG_PGVECTOR_SSLMODE", "prefer").strip().lower(),
        connect_timeout_seconds=_positive_int("RAG_PGVECTOR_CONNECT_TIMEOUT_SECONDS", 5),
        milvus_uri=os.environ.get("RAG_MILVUS_URI", "").strip(),
        milvus_token=os.environ.get("RAG_MILVUS_TOKEN", ""),
        milvus_database=os.environ.get("RAG_MILVUS_DATABASE", "nanoclaw_vector_docker").strip(),
        milvus_alias=os.environ.get("RAG_MILVUS_COLLECTION_ALIAS", "trade_knowledge_active").strip(),
        milvus_connect_timeout_seconds=_positive_int("RAG_MILVUS_CONNECT_TIMEOUT_SECONDS", 5),
        sqlite_path=os.environ.get(
            "RAG_SQLITE_INDEX_PATH", "workspace/knowledge_base/rag_index.db"
        ).strip(),
    )
    config.validate()
    return config


def load_rag_keyword_config() -> RagKeywordConfig:
    _load_project_env()
    config = RagKeywordConfig(
        backend=os.environ.get("RAG_KEYWORD_BACKEND", "memory").strip().lower(),
        url=os.environ.get("RAG_ELASTICSEARCH_URL", "").strip().rstrip("/"),
        username=os.environ.get("RAG_ELASTICSEARCH_USERNAME", "").strip(),
        password=os.environ.get("RAG_ELASTICSEARCH_PASSWORD", ""),
        index=os.environ.get(
            "RAG_ELASTICSEARCH_INDEX", "trade_knowledge_child_v1"
        ).strip().lower(),
        timeout_seconds=_positive_float("RAG_ELASTICSEARCH_TIMEOUT_SECONDS", 10),
        sqlite_path=os.environ.get(
            "RAG_SQLITE_INDEX_PATH", "workspace/knowledge_base/rag_index.db"
        ).strip(),
    )
    config.validate()
    return config

def load_pdf_ingestion_config() -> PdfIngestionConfig:
    _load_project_env()
    return PdfIngestionConfig(
        max_bytes=_positive_int("RAG_PDF_MAX_BYTES", 20 * 1024 * 1024),
        max_pages=_positive_int("RAG_PDF_MAX_PAGES", 300),
        max_chars_per_page=_positive_int("RAG_PDF_MAX_CHARS_PER_PAGE", 200_000),
        max_total_chars=_positive_int("RAG_PDF_MAX_TOTAL_CHARS", 5_000_000),
        parse_timeout_seconds=_positive_float("RAG_PDF_PARSE_TIMEOUT_SECONDS", 30),
        max_concurrent_parses=_positive_int("RAG_PDF_MAX_CONCURRENT_PARSES", 2),
        min_text_page_ratio=_ratio("RAG_PDF_MIN_TEXT_PAGE_RATIO", 0.8),
        min_average_chars_per_text_page=_positive_int("RAG_PDF_MIN_AVERAGE_CHARS_PER_TEXT_PAGE", 50),
        max_consecutive_blank_pages=_positive_int("RAG_PDF_MAX_CONSECUTIVE_BLANK_PAGES", 3),
    )

def load_ocr_config() -> OcrConfig:
    _load_project_env()
    return OcrConfig(
        enabled=os.environ.get("RAG_OCR_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
        provider=os.environ.get("RAG_OCR_PROVIDER", "paddleocr").strip().lower(),
        language=os.environ.get("RAG_OCR_LANGUAGE", "ch").strip() or "ch",
        device=os.environ.get("RAG_OCR_DEVICE", "cpu").strip().lower(),
        min_confidence=_ratio("RAG_OCR_MIN_CONFIDENCE", 0.60),
        max_images_per_document=_positive_int("RAG_OCR_MAX_IMAGES_PER_DOCUMENT", 300),
        max_image_bytes=_positive_int("RAG_OCR_MAX_IMAGE_BYTES", 20 * 1024 * 1024),
        max_image_pixels=_positive_int("RAG_OCR_MAX_IMAGE_PIXELS", 40_000_000),
        max_ocr_chars_per_image=_positive_int("RAG_OCR_MAX_CHARS_PER_IMAGE", 20_000),
        pdf_dpi=_positive_int("RAG_OCR_PDF_DPI", 200),
        min_embedded_width=_positive_int("RAG_OCR_MIN_EMBEDDED_WIDTH", 64),
        min_embedded_height=_positive_int("RAG_OCR_MIN_EMBEDDED_HEIGHT", 64),
    )
