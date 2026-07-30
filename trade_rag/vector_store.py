from __future__ import annotations

from .config import RagVectorConfig, load_rag_vector_config
from .stores import InMemoryVectorStore, VectorStore


def create_vector_store(config: RagVectorConfig | None = None) -> VectorStore:
    """Create the configured store without silently falling back on failures."""
    current = config or load_rag_vector_config()
    current.validate()
    if current.backend == "memory":
        return InMemoryVectorStore()
    if current.backend == "sqlite":
        from .sqlite_store import SqliteVectorStore
        store = SqliteVectorStore(current)
        store.check_ready()
        return store
    if current.backend == "pgvector":
        # Lazy import keeps the default memory path independent of PostgreSQL.
        from .pgvector_store import PgvectorStore
        store = PgvectorStore(current)
    else:
        # Milvus and its transitive SDK dependencies remain opt-in.
        from .milvus_store import MilvusStore
        store = MilvusStore(current)
    store.check_ready()
    return store
