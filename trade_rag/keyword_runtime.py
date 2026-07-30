from __future__ import annotations

from .config import RagKeywordConfig, load_rag_keyword_config
from .elasticsearch_store import ElasticsearchKeywordStore
from .stores import InMemoryKeywordStore


def create_application_keyword_store(config: RagKeywordConfig | None = None):
    config = config or load_rag_keyword_config()
    config.validate()
    if config.backend == "memory":
        return InMemoryKeywordStore()
    if config.backend == "sqlite":
        from .sqlite_store import SqliteKeywordStore
        return SqliteKeywordStore(config)
    return ElasticsearchKeywordStore(config)
