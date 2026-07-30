"""脱敏、可替换的企业知识库 RAG 骨架。

默认实现只依赖 Python 标准库；生产向量库和模型 API 通过适配器接入。
"""
from .contracts import Actor, CanonicalDocument, DocumentStatus, QueryRequest, SearchResult
from .pipeline import RagPipeline

__all__ = ["Actor", "CanonicalDocument", "DocumentStatus", "QueryRequest", "SearchResult", "RagPipeline"]
