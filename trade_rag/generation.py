from __future__ import annotations

from .contracts import Citation, SearchResult


def answer_with_citations(query: str, results: list[SearchResult], status: str) -> dict:
    if status != "HIGH_CONFIDENCE":
        return {
            "status": status,
            "answer": "无法基于已批准且有权限的知识确定回答，请补充范围或转人工。",
            "citations": [],
        }
    selected = results[:3]
    citations = []
    for result in selected:
        metadata = result.child.metadata
        citations.append(Citation(
            result.source.document_id,
            result.source.version,
            result.child.location,
            result.child.child_id,
            image_id=metadata.get("image_id"),
            image_index=metadata.get("image_index"),
            page_number=metadata.get("page_number"),
        ).__dict__)
    return {
        "status": "ANSWERED",
        "answer": "\n\n".join(
            result.parent.text if result.parent is not None else result.child.text
            for result in selected
        ),
        "citations": citations,
    }
