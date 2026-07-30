from __future__ import annotations

from .config import RagVectorConfig, load_rag_vector_config
from .stores import InMemoryParentStore


def create_application_parent_store(config: RagVectorConfig | None = None):
    current = config or load_rag_vector_config()
    current.validate()
    if current.backend != "sqlite":
        return InMemoryParentStore()
    from .sqlite_store import SqliteParentStore
    return SqliteParentStore(current)
