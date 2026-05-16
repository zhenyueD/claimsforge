"""
Embedding index over the unified KB.

Uses Gemini `text-embedding-004` (free tier 1500 RPM, no GPU cost on our side).
Embeddings are computed once, persisted to data/unified_kb_embeddings.jsonl,
loaded into RAM on demand for cosine-similarity retrieval.

This is the "semantic search" layer Coze + Spring AI articles both stress.
At ~150 entries it's overkill; at 10k+ it's necessary. We do it now because
it's a 50-line addition and it makes the system honest about what it knows.
"""
from __future__ import annotations

import logging
import math
import os
import time
from typing import Optional

from google import genai
from google.genai import types as gtypes

from unified_kb import (
    KBEntry,
    KBSource,
    _load_kb,
    _load_embeddings,
    write_embedding,
)
import gemini_client  # ensures .env loaded + key resolved

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "gemini-embedding-001"


def _client() -> genai.Client:
    return gemini_client.get_client()


def _entry_text_for_embedding(e: KBEntry) -> str:
    """Concatenate the entry into a single string the embedding model can chew on.
    Keeps it < 8k tokens. We weight title + scenario + decision (the user-facing
    bits) over rationale + tags."""
    parts = [
        e.title,
        f"Scenario: {e.scenario}",
        f"Decision: {e.decision}",
        f"Why: {e.rationale[:400]}" if e.rationale else "",
        f"Tags: {', '.join(e.tags)}" if e.tags else "",
        f"Domain: {e.domain}",
    ]
    return "\n".join(p for p in parts if p)


def embed_text(text: str) -> list[float]:
    """Single-shot embed. Returns 768-dim vector for text-embedding-004."""
    client = _client()
    resp = client.models.embed_content(
        model=EMBEDDING_MODEL,
        contents=text,
        config=gtypes.EmbedContentConfig(task_type="RETRIEVAL_QUERY"),
    )
    return list(resp.embeddings[0].values)


def index_all(force: bool = False, rate_limit_sleep: float = 0.05) -> dict:
    """Index every KB entry that doesn't have an embedding yet. Idempotent."""
    entries = _load_kb(force=True)
    existing = _load_embeddings(force=True)
    needed = [e for e in entries if force or e.id not in existing]
    if not needed:
        return {"indexed": 0, "skipped": len(entries), "total_in_index": len(existing)}

    logger.info("Embedding %d entries (skipping %d already indexed)…", len(needed), len(entries) - len(needed))
    client = _client()
    indexed = 0
    for entry in needed:
        text = _entry_text_for_embedding(entry)
        try:
            resp = client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=text,
                config=gtypes.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT"),
            )
            vec = list(resp.embeddings[0].values)
            write_embedding(entry.id, vec)
            indexed += 1
        except Exception as e:
            logger.warning("failed to embed %s: %s", entry.id, e)
        if rate_limit_sleep:
            time.sleep(rate_limit_sleep)

    return {
        "indexed": indexed,
        "skipped": len(entries) - len(needed),
        "total_in_index": indexed + len(existing),
    }


# ─────────────────────────────────────────────────────────
#  Retrieval
# ─────────────────────────────────────────────────────────
def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def search_embedding(
    query: str,
    *,
    top_k: int = 5,
    threshold: float = 0.55,
    source_filter: Optional[list[KBSource]] = None,
    domain_filter: Optional[str] = None,
) -> list[tuple[KBEntry, float]]:
    """Embed the query, return KB entries above the cosine threshold, sorted desc."""
    embeddings = _load_embeddings()
    if not embeddings:
        return []
    try:
        qvec = embed_text(query)
    except Exception as e:
        logger.warning("query embed failed, falling back to no results: %s", e)
        return []

    entries = _load_kb()
    by_id = {e.id: e for e in entries}

    if source_filter:
        wanted = {s.value if isinstance(s, KBSource) else s for s in source_filter}
        by_id = {k: v for k, v in by_id.items() if v.source.value in wanted}
    if domain_filter:
        by_id = {k: v for k, v in by_id.items() if v.domain == domain_filter}

    scored: list[tuple[KBEntry, float]] = []
    for eid, vec in embeddings.items():
        entry = by_id.get(eid)
        if entry is None:
            continue
        score = _cosine(qvec, vec)
        if score >= threshold:
            scored.append((entry, score))
    scored.sort(key=lambda x: -x[1])
    return scored[:top_k]


def hybrid_search(
    query: str,
    *,
    top_k: int = 5,
    threshold: float = 0.55,
    source_filter: Optional[list[KBSource]] = None,
    domain_filter: Optional[str] = None,
) -> list[tuple[KBEntry, float, str]]:
    """Embedding-first, keyword fallback if embedding returns nothing.
    Returns (entry, score, method) — method is 'embedding' or 'keyword'."""
    from unified_kb import search as kw_search

    emb_results = search_embedding(
        query, top_k=top_k, threshold=threshold,
        source_filter=source_filter, domain_filter=domain_filter,
    )
    if emb_results:
        return [(e, s, "embedding") for e, s in emb_results]

    # Fallback to keyword
    kw_results = kw_search(
        query, top_k=top_k,
        source_filter=source_filter, domain_filter=domain_filter,
    )
    # Pseudo-scores for keyword (1.0 / rank+1)
    return [(e, 1.0 / (i + 1), "keyword") for i, e in enumerate(kw_results)]
