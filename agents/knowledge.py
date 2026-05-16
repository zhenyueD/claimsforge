"""
Knowledge access — merchant KB (curated wisdom) + learned cases (live loop).

Two sources:
  1. data/merchant_kb.json — 93 entries from Amazon/eBay/Shopify/Reddit (curated)
  2. data/learned_cases.jsonl — append-only log written by orchestrator after each resolved claim

Retrieval is keyword + category match (no embedding model) — keeps the binary slim,
keeps the latency tiny (<5ms for full KB scan). Quality is good enough at this size.
At >5k entries we'd swap in an embedding model. Not today.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DATA = Path(__file__).resolve().parent.parent / "data"
KB_PATH = DATA / "merchant_kb.json"
LEARNED_PATH = DATA / "learned_cases.jsonl"

_kb_cache: Optional[list[dict[str, Any]]] = None
_learned_cache: Optional[list[dict[str, Any]]] = None
_write_lock = threading.Lock()


# ─────────────────────────────────────────────────────────
#  Loading (lazy, cached)
# ─────────────────────────────────────────────────────────
def _load_kb() -> list[dict[str, Any]]:
    global _kb_cache
    if _kb_cache is None:
        if KB_PATH.exists():
            _kb_cache = json.loads(KB_PATH.read_text(encoding="utf-8")).get("entries", [])
        else:
            _kb_cache = []
    return _kb_cache


def _load_learned() -> list[dict[str, Any]]:
    """Hot-reload every call — learned cases change live."""
    if not LEARNED_PATH.exists():
        return []
    rows = []
    for line in LEARNED_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


# ─────────────────────────────────────────────────────────
#  Retrieval
# ─────────────────────────────────────────────────────────
_STOPWORDS = {"the", "a", "an", "is", "in", "on", "and", "or", "of", "to", "for", "with"}


def _tokens(s: str) -> set[str]:
    return {w.lower() for w in s.replace(".", " ").replace(",", " ").split() if w.lower() not in _STOPWORDS and len(w) > 2}


def _score_entry(query_tokens: set[str], entry: dict[str, Any]) -> float:
    """Simple token-overlap score with category/tag boosts."""
    haystack = " ".join([
        entry.get("scenario", ""),
        entry.get("decision", ""),
        " ".join(entry.get("tags", [])),
        entry.get("category", ""),
    ])
    haystack_tokens = _tokens(haystack)
    overlap = len(query_tokens & haystack_tokens)
    if overlap == 0:
        return 0
    # tag exact matches are worth double
    tag_boost = sum(1 for t in entry.get("tags", []) if t.lower() in query_tokens)
    return overlap + tag_boost * 2


def retrieve_merchant_wisdom(
    *,
    damage_type: Optional[str] = None,
    emotion_label: Optional[str] = None,
    user_message: str = "",
    product_hint: Optional[str] = None,
    top_k: int = 4,
) -> list[dict[str, Any]]:
    """Return the most relevant KB entries (curated merchant wisdom)."""
    kb = _load_kb()
    if not kb:
        return []

    query_parts: list[str] = []
    if damage_type:
        query_parts.append(damage_type)
    if emotion_label:
        query_parts.append(emotion_label)
    if product_hint:
        query_parts.append(product_hint)
    if user_message:
        query_parts.append(user_message[:300])
    query_tokens = _tokens(" ".join(query_parts))

    scored = [(_score_entry(query_tokens, e), e) for e in kb]
    scored = [(s, e) for s, e in scored if s > 0]
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:top_k]]


def retrieve_recent_learned(
    *,
    damage_type: Optional[str] = None,
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """Recent live cases — used by CompensationAgent to ground decisions in actual recent precedent."""
    cases = _load_learned()
    if not cases:
        return []
    if damage_type:
        cases = [c for c in cases if c.get("damage", {}).get("damage_type") == damage_type]
    return cases[-top_k:][::-1]  # newest first


# ─────────────────────────────────────────────────────────
#  Learning loop — append-only write
# ─────────────────────────────────────────────────────────
def append_learned_case(record: dict[str, Any]) -> None:
    """Append a completed claim to the learning log. Idempotent on session_id."""
    record = dict(record)
    record.setdefault("learned_at", datetime.now().isoformat())

    with _write_lock:
        LEARNED_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LEARNED_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    logger.info("learned new case: %s damage=%s offer=%s",
                record.get("session_id"),
                record.get("damage", {}).get("damage_type"),
                record.get("final_offer", {}).get("offer_type"))


def get_learning_stats() -> dict[str, Any]:
    """Surface for the UI Learning Queue panel."""
    cases = _load_learned()
    if not cases:
        return {"total": 0, "by_damage_type": {}, "by_outcome": {}, "recent": []}
    from collections import Counter
    by_damage = Counter(c.get("damage", {}).get("damage_type", "unknown") for c in cases)
    by_outcome = Counter(
        "escalated" if c.get("escalated") else c.get("final_offer", {}).get("offer_type", "?")
        for c in cases
    )
    recent = [
        {
            "session_id": c.get("session_id"),
            "damage_type": c.get("damage", {}).get("damage_type"),
            "severity": c.get("damage", {}).get("severity"),
            "emotion": c.get("emotion", {}).get("label"),
            "offer_type": c.get("final_offer", {}).get("offer_type") if c.get("final_offer") else None,
            "amount_cents": c.get("final_offer", {}).get("amount_cents") if c.get("final_offer") else None,
            "escalated": c.get("escalated", False),
            "learned_at": c.get("learned_at"),
        }
        for c in cases[-10:][::-1]
    ]
    return {
        "total": len(cases),
        "by_damage_type": dict(by_damage),
        "by_outcome": dict(by_outcome),
        "recent": recent,
    }
