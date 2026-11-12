"""Utilitários de texto para a memória de longo prazo.

Nada aqui chama rede ou banco: são funções puras usadas para normalizar
entidades, tokenizar consultas e estimar o custo em tokens do contexto.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

_WORD_RE = re.compile(r"[0-9a-zA-ZÀ-ÖØ-öø-ÿ_]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
_SUBJECT_SPLIT_RE = re.compile(r"[,;|/]+")

# Palavras muito comuns não ajudam a diferenciar memórias e só gastam tokens.
_STOPWORDS = {
    "a", "à", "ao", "aos", "as", "com", "como", "da", "das", "de", "do",
    "dos", "e", "é", "ela", "ele", "eles", "elas", "em", "entre", "era",
    "essa", "esse", "esta", "este", "eu", "foi", "há", "isso", "isto",
    "já", "mais", "mas", "me", "mesmo", "meu", "minha", "muito", "na",
    "nas", "não", "no", "nos", "nós", "o", "os", "ou", "para", "pela",
    "pelo", "por", "qual", "que", "se", "sem", "seu", "sua", "também",
    "te", "tem", "ter", "um", "uma", "você", "vocês", "the", "of", "and",
    "to", "in", "is", "it", "that", "this",
}


def strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def compact_whitespace(text: str) -> str:
    return _SPACE_RE.sub(" ", (text or "").strip())


def normalize_entity(text: str) -> str:
    """Nome de entidade comparável: sem acento, caixa baixa e pontuação."""
    value = strip_accents(compact_whitespace(text)).casefold()
    value = re.sub(r"[^0-9a-z _-]+", " ", value)
    return compact_whitespace(value)


def split_subjects(subject: str) -> list[str]:
    """Um subject pode conter mais de uma entidade: 'João, Maria'."""
    return [part.strip() for part in _SUBJECT_SPLIT_RE.split(subject or "") if part.strip()]


def words(text: str) -> list[str]:
    return [match.group(0).casefold() for match in _WORD_RE.finditer(text or "")]


def tokens(text: str) -> list[str]:
    """Tokens normalizados (sem acento) para busca lexical e embeddings."""
    result: list[str] = []
    for word in words(strip_accents(text)):
        if len(word) >= 3 and word not in _STOPWORDS:
            result.append(word)
    return result


def token_set(text: str) -> set[str]:
    return set(tokens(text))


def estimate_tokens(text: str) -> int:
    """Estimativa conservadora para português (~4 caracteres por token)."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def truncate(text: str, max_chars: int) -> str:
    text = compact_whitespace(text)
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    cut = text[: max(0, max_chars - 1)].rstrip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


def overlap_score(query_tokens: Iterable[str], text: str) -> float:
    query = {token for token in query_tokens if token}
    if not query:
        return 0.0
    found = query & token_set(text)
    return len(found) / float(len(query))
