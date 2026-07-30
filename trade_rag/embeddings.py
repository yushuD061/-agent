from __future__ import annotations
import hashlib, math
from typing import Protocol

class EmbeddingProvider(Protocol):
    model_id: str
    def embed(self, texts: list[str]) -> list[list[float]]: ...

class MockEmbeddingProvider:
    model_id = "mock-hash-v1"
    def __init__(self, dimensions: int = 64): self.dimensions = dimensions
    def embed(self, texts):
        out=[]
        for text in texts:
            vec=[0.0]*self.dimensions
            for token in text.lower().split(): vec[int(hashlib.sha256(token.encode()).hexdigest(),16)%self.dimensions]+=1
            norm=math.sqrt(sum(x*x for x in vec)) or 1; out.append([x/norm for x in vec])
        return out
