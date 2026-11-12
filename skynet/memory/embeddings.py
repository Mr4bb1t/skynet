"""Embeddings para o RAG de memória.

Estratégia:
  * Gemini (`gemini-embedding-001`) quando houver API key e internet;
  * fallback local determinístico (hashing de palavras + trigramas) quando a
    rede falhar ou os embeddings estiverem desativados.

O fallback garante que a memória continua gravando e buscando por termos
exatos mesmo offline. Cada vetor guarda o provider que o gerou, então vetores
de providers diferentes nunca são comparados entre si.
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass
from typing import Sequence

from .text import strip_accents, tokens

LOG = logging.getLogger("memory.embeddings")


class EmbeddingError(RuntimeError):
    pass


@dataclass
class EmbeddingBatch:
    provider: str
    vectors: list[list[float]]

    @property
    def dim(self) -> int:
        return len(self.vectors[0]) if self.vectors else 0


class BaseEmbedder:
    name = "base"

    def embed(self, texts: Sequence[str], *, query: bool = False) -> EmbeddingBatch:
        raise NotImplementedError


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0.0:
        return vector
    return [value / norm for value in vector]


class HashingEmbedder(BaseEmbedder):
    """Embedding lexical local: palavras + trigramas de caracteres."""

    name = "hash"

    def __init__(self, dim: int = 512) -> None:
        self.dim = max(64, int(dim))

    @property
    def provider(self) -> str:
        return f"{self.name}:{self.dim}"

    def _features(self, text: str) -> list[tuple[str, float]]:
        normalized = strip_accents(text or "").casefold()
        features: list[tuple[str, float]] = []
        for word in tokens(normalized):
            features.append((f"w:{word}", 2.0))
            padded = f"<{word}>"
            for index in range(len(padded) - 2):
                features.append((f"c:{padded[index:index + 3]}", 0.35))
        return features

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for feature, weight in self._features(text):
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            raw = int.from_bytes(digest, "big")
            index = raw % self.dim
            sign = 1.0 if (raw >> 8) & 1 else -1.0
            vector[index] += sign * weight
        return _normalize(vector)

    def embed(self, texts: Sequence[str], *, query: bool = False) -> EmbeddingBatch:
        return EmbeddingBatch(
            provider=self.provider,
            vectors=[self._one(text) for text in texts],
        )


class GeminiEmbedder(BaseEmbedder):
    """Embeddings semânticos via Gemini API, com fallback entre modelos."""

    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-embedding-001",
        dim: int = 768,
    ) -> None:
        if not api_key:
            raise EmbeddingError("GEMINI_API_KEY ausente para embeddings")
        self.api_key = api_key
        self.model = model
        self.dim = int(dim)
        self._client = None
        self._active_model = model
        self._models = []
        for candidate in (model, "gemini-embedding-2", "gemini-embedding-001"):
            if candidate and candidate not in self._models:
                self._models.append(candidate)

    @property
    def provider(self) -> str:
        return f"{self.name}:{self._active_model}:{self.dim}"

    def _ensure_client(self):
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def embed(self, texts: Sequence[str], *, query: bool = False) -> EmbeddingBatch:
        from google.genai import types

        client = self._ensure_client()
        task_type = "RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT"
        last_error: Exception | None = None
        for model in self._models:
            try:
                response = client.models.embed_content(
                    model=model,
                    contents=list(texts),
                    config=types.EmbedContentConfig(
                        task_type=task_type,
                        output_dimensionality=self.dim,
                    ),
                )
                vectors = []
                for embedding in response.embeddings or []:
                    values = list(embedding.values or [])
                    if values:
                        vectors.append(_normalize(values))
                if len(vectors) != len(texts):
                    raise EmbeddingError(
                        f"embedding incompleto: {len(vectors)}/{len(texts)}"
                    )
                self._active_model = model
                return EmbeddingBatch(provider=self.provider, vectors=vectors)
            except Exception as exc:  # pragma: no cover - depende da rede
                last_error = exc
                LOG.debug("Embedding com %s falhou: %s", model, exc)
        raise EmbeddingError(str(last_error) if last_error else "embedding falhou")


class ResilientEmbedder(BaseEmbedder):
    """Tenta o Gemini e cai para o hashing local sem interromper a memória."""

    def __init__(self, primary: BaseEmbedder | None, fallback: BaseEmbedder) -> None:
        self.primary = primary
        self.fallback = fallback
        self.name = getattr(primary, "name", "hash") if primary else fallback.name
        self._warned = False

    def embed(self, texts: Sequence[str], *, query: bool = False) -> EmbeddingBatch:
        if self.primary is not None:
            try:
                return self.primary.embed(texts, query=query)
            except Exception as exc:
                if not self._warned:
                    self._warned = True
                    LOG.warning(
                        "Embeddings do Gemini indisponíveis; usando busca lexical "
                        "local até a próxima tentativa: %s",
                        exc,
                    )
        return self.fallback.embed(texts, query=query)


def build_embedder(
    *,
    api_key: str | None,
    model: str,
    dim: int,
    enabled: bool = True,
) -> BaseEmbedder:
    fallback = HashingEmbedder(dim=512 if dim > 512 else dim)
    if not enabled:
        return fallback
    primary = None
    if api_key:
        try:
            primary = GeminiEmbedder(api_key=api_key, model=model, dim=dim)
        except Exception as exc:  # pragma: no cover - defensivo
            LOG.warning("Embeddings Gemini desativados: %s", exc)
    return ResilientEmbedder(primary, fallback)
