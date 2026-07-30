"""Build derived memory indexes from authoritative recallable rows."""

from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path

from .stores.keyword import KeywordMemoryIndex
from .stores.vector import VectorMemoryIndex


class MemoryIndexRebuilder:
    def __init__(self, authoritative_store, embedding):
        self.authoritative_store = authoritative_store
        self.embedding = embedding

    def rebuild(self, keyword_path: str | Path, vector_path: str | Path) -> int:
        keyword_path, vector_path = Path(keyword_path), Path(vector_path)
        if keyword_path.parent != vector_path.parent:
            raise ValueError("memory_index_rebuild_directory_mismatch")
        keyword_path.parent.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        temporary_keyword = keyword_path.with_name(f".{keyword_path.name}.{token}.tmp")
        temporary_vector = vector_path.with_name(f".{vector_path.name}.{token}.tmp")
        backup_keyword = keyword_path.with_name(f".{keyword_path.name}.{token}.old")
        backup_vector = vector_path.with_name(f".{vector_path.name}.{token}.old")
        keyword = vector = None
        try:
            items = self.authoritative_store.active_items_for_rebuild()
            keyword = KeywordMemoryIndex(temporary_keyword)
            vector = VectorMemoryIndex(temporary_vector, self.embedding)
            for item in items:
                keyword.upsert(item)
                vector.upsert(item)
            expected = {item.memory_id: item.version for item in items}
            keyword_rows = dict(keyword.connection.execute(
                "SELECT memory_id,source_version FROM memory_keyword_index"
            ).fetchall())
            vector_rows = dict(vector.connection.execute(
                "SELECT memory_id,source_version FROM memory_vector_index"
            ).fetchall())
            if keyword_rows != expected or vector_rows != expected:
                raise RuntimeError("memory_index_rebuild_verification_failed")
            keyword.connection.close()
            vector.connection.close()
            keyword = vector = None
            had_keyword, had_vector = keyword_path.exists(), vector_path.exists()
            try:
                if had_keyword:
                    os.replace(keyword_path, backup_keyword)
                if had_vector:
                    os.replace(vector_path, backup_vector)
                os.replace(temporary_keyword, keyword_path)
                os.replace(temporary_vector, vector_path)
            except Exception:
                keyword_path.unlink(missing_ok=True)
                vector_path.unlink(missing_ok=True)
                if had_keyword and backup_keyword.exists():
                    os.replace(backup_keyword, keyword_path)
                if had_vector and backup_vector.exists():
                    os.replace(backup_vector, vector_path)
                raise
            backup_keyword.unlink(missing_ok=True)
            backup_vector.unlink(missing_ok=True)
            return len(items)
        finally:
            if keyword is not None:
                keyword.connection.close()
            if vector is not None:
                vector.connection.close()
            temporary_keyword.unlink(missing_ok=True)
            temporary_vector.unlink(missing_ok=True)
