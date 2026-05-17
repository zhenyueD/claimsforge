"""
Unified Knowledge Base — the shared brain across all ClaimsForge agents.

Design principle: ONE source of knowledge. Three contributors:
  1. HUMAN  — operations team uploads SOPs, policies, brand guidelines.
              Gemini auto-converts each chunk into typed KB entries.
  2. CURATED — pre-seeded merchant wisdom (Amazon/eBay/Shopify) shipped with the project.
  3. AI     — every resolved claim, every customer interaction, every flagged
              gap gets written back. The system learns from itself.

Each entry has a consistent schema (KBEntry) and an optional embedding for
semantic retrieval. Every agent reads from the same pool — sharing precedent,
sharing wisdom, sharing mistakes.

This is the substrate for the "collective intelligence" loop:
   humans seed it → AI uses + extends it → humans review + curate.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
UNIFIED_KB_PATH = DATA_DIR / "unified_kb.jsonl"
EMBEDDINGS_PATH = DATA_DIR / "unified_kb_embeddings.jsonl"
GAPS_PATH = DATA_DIR / "gaps.jsonl"
FEEDBACK_PATH = DATA_DIR / "feedback.jsonl"


# ─────────────────────────────────────────────────────────
#  Schema
# ─────────────────────────────────────────────────────────
class KBSource(str, Enum):
    HUMAN_SOP = "human_sop"            # uploaded by operations team
    HUMAN_POLICY = "human_policy"      # structured policy (the 26 rules)
    CURATED_WISDOM = "curated_wisdom"  # ships with the project (Amazon/eBay/...)
    LEARNED_CASE = "learned_case"      # AI wrote this after resolving a claim
    GAP = "gap"                        # AI flagged "I don't know how to handle this"
    USER_CORRECTION = "user_correction"  # human reviewer corrected an AI output


class KBType(str, Enum):
    RULE = "rule"              # if-then policy
    CASE = "case"              # specific past example
    PRINCIPLE = "principle"    # general guidance
    DECISION_LOG = "decision_log"  # what we did + outcome
    METHODOLOGY = "methodology"    # synthesized pattern across multiple cases


class KBEntry(BaseModel):
    """One unit of knowledge. All agents read this shape."""
    id: str
    source: KBSource
    type: KBType
    domain: str = Field(description="damage / emotion / logistics / luxury / fraud / etc")
    title: str = Field(description="<= 80 chars human-readable title")
    scenario: str = Field(description="When this applies — the trigger context")
    decision: str = Field(description="What to do / what we did")
    rationale: str = Field(description="Why. The model + a human can audit this.")
    tags: list[str] = Field(default_factory=list)
    customer_facing_name: Optional[str] = Field(default=None, description="Human-friendly name to cite to customers")
    # Optional English translation (populated by scripts/translate_kb_to_english.py).
    # Frontend prefers these when present so judges see English even for zh entries.
    title_en: Optional[str] = Field(default=None)
    scenario_en: Optional[str] = Field(default=None)
    decision_en: Optional[str] = Field(default=None)
    # Provenance
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    contributor: Optional[str] = Field(default=None, description="email / 'system' / 'gemini-ingest'")
    # Quality + usage
    quality_score: float = Field(default=0.5, ge=0, le=1, description="0=unverified, 1=human-approved gold")
    use_count: int = Field(default=0)
    upvotes: int = Field(default=0)
    downvotes: int = Field(default=0)
    # Source pointers
    source_doc: Optional[str] = Field(default=None, description="original filename if from SOP import")
    source_chunk: Optional[int] = Field(default=None)


# ─────────────────────────────────────────────────────────
#  Storage — append-only jsonl + optional embeddings sidecar
# ─────────────────────────────────────────────────────────
_kb_cache: Optional[list[KBEntry]] = None
_embeddings_cache: Optional[dict[str, list[float]]] = None
_write_lock = threading.Lock()


def _load_kb(force: bool = False) -> list[KBEntry]:
    global _kb_cache
    if _kb_cache is not None and not force:
        return _kb_cache
    entries: list[KBEntry] = []
    if UNIFIED_KB_PATH.exists():
        for line in UNIFIED_KB_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(KBEntry.model_validate_json(line))
            except Exception as e:
                logger.warning("skip malformed KB row: %s", e)
    # latest entry per id wins (allows updates by appending)
    by_id: dict[str, KBEntry] = {}
    for e in entries:
        by_id[e.id] = e
    _kb_cache = list(by_id.values())
    return _kb_cache


def _load_embeddings(force: bool = False) -> dict[str, list[float]]:
    global _embeddings_cache
    if _embeddings_cache is not None and not force:
        return _embeddings_cache
    out: dict[str, list[float]] = {}
    if EMBEDDINGS_PATH.exists():
        for line in EMBEDDINGS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                out[row["id"]] = row["embedding"]
            except Exception:
                continue
    _embeddings_cache = out
    return out


# ─────────────────────────────────────────────────────────
#  Writes
# ─────────────────────────────────────────────────────────
def make_id(seed: str) -> str:
    return "kb-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:14]


def upsert(entry: KBEntry) -> KBEntry:
    """Add or update a KB entry. Append-only on disk, latest wins on read."""
    entry.updated_at = datetime.now().isoformat()
    with _write_lock:
        UNIFIED_KB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with UNIFIED_KB_PATH.open("a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")
    # bust cache lazily
    global _kb_cache
    _kb_cache = None
    return entry


def record_use(entry_id: str) -> None:
    """Track when an entry is retrieved + used. Used for quality scoring.

    Performance note: the prior implementation called upsert(e), which busts
    _kb_cache. With KB at ~1500 entries and ~6 record_use calls per claim,
    every claim invalidated the cache 6× and the next read re-parsed the
    whole JSONL. This compounds as use_count append rows accumulate.

    Fix: mutate the cached entry in-place (entries returned by _load_kb are
    the same objects living in _kb_cache, so the bump is immediately visible)
    and append a single row to disk WITHOUT busting the cache. Read path
    stays O(1); write is a tiny append.
    """
    entries = _load_kb()
    for e in entries:
        if e.id == entry_id:
            e.use_count += 1
            e.updated_at = datetime.now().isoformat()
            try:
                with _write_lock:
                    UNIFIED_KB_PATH.parent.mkdir(parents=True, exist_ok=True)
                    with UNIFIED_KB_PATH.open("a", encoding="utf-8") as f:
                        f.write(e.model_dump_json() + "\n")
            except Exception as ex:
                logger.warning("record_use disk append failed: %s", ex)
            break


def record_uses(entry_ids: list[str]) -> None:
    """Batched record_use. Takes a single _write_lock for N entries — cuts
    the per-claim disk syscall count from N to 1. Cache stays valid throughout.
    """
    if not entry_ids:
        return
    entries = _load_kb()
    by_id = {e.id: e for e in entries}
    now = datetime.now().isoformat()
    dirty: list[KBEntry] = []
    for eid in entry_ids:
        e = by_id.get(eid)
        if e is None:
            continue
        e.use_count += 1
        e.updated_at = now
        dirty.append(e)
    if not dirty:
        return
    try:
        with _write_lock:
            UNIFIED_KB_PATH.parent.mkdir(parents=True, exist_ok=True)
            with UNIFIED_KB_PATH.open("a", encoding="utf-8") as f:
                for e in dirty:
                    f.write(e.model_dump_json() + "\n")
    except Exception as ex:
        logger.warning("record_uses disk append failed: %s", ex)


def write_embedding(entry_id: str, vec: list[float]) -> None:
    with _write_lock:
        EMBEDDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EMBEDDINGS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": entry_id, "embedding": vec}) + "\n")
    global _embeddings_cache
    _embeddings_cache = None


# ─────────────────────────────────────────────────────────
#  Retrieval — keyword fallback now; embedding added by embedding_index.py
# ─────────────────────────────────────────────────────────
_STOPWORDS = {"the", "a", "an", "is", "in", "on", "and", "or", "of", "to",
              "for", "with", "be", "was", "are", "this", "that", "it", "as"}


def _tokens(s: str) -> set[str]:
    s = s.lower().replace(".", " ").replace(",", " ").replace("/", " ").replace("-", " ")
    return {w for w in s.split() if w not in _STOPWORDS and len(w) > 2}


def _kw_score(qtok: set[str], e: KBEntry) -> float:
    haystack = " ".join([
        e.title, e.scenario, e.decision, e.rationale,
        " ".join(e.tags), e.domain,
        (e.customer_facing_name or ""),
    ])
    htok = _tokens(haystack)
    overlap = len(qtok & htok)
    if overlap == 0:
        return 0.0
    # tag exact-hit bonus
    tag_bonus = sum(1 for t in e.tags if t.lower() in qtok)
    # quality + use_count tie-breaker
    score = overlap + tag_bonus * 2 + e.quality_score * 1.5 + min(e.use_count, 10) * 0.05
    return score


def search(
    query: str,
    *,
    top_k: int = 5,
    source_filter: Optional[list[KBSource]] = None,
    domain_filter: Optional[str] = None,
    min_quality: float = 0.0,
) -> list[KBEntry]:
    """Return top-k entries by relevance. Falls back to keyword scoring;
    embedding search is added by embedding_index.search_embedding()."""
    entries = _load_kb()
    if source_filter:
        wanted = {s.value if isinstance(s, KBSource) else s for s in source_filter}
        entries = [e for e in entries if e.source.value in wanted]
    if domain_filter:
        entries = [e for e in entries if e.domain == domain_filter]
    if min_quality > 0:
        entries = [e for e in entries if e.quality_score >= min_quality]

    qtok = _tokens(query)
    scored = [(_kw_score(qtok, e), e) for e in entries]
    scored = [(s, e) for s, e in scored if s > 0]
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:top_k]]


# ─────────────────────────────────────────────────────────
#  Stats — UI surface
# ─────────────────────────────────────────────────────────
def get_kb_stats() -> dict[str, Any]:
    entries = _load_kb()
    from collections import Counter
    by_source = Counter(e.source.value for e in entries)
    by_domain = Counter(e.domain for e in entries)
    by_type = Counter(e.type.value for e in entries)
    total_uses = sum(e.use_count for e in entries)
    avg_quality = (sum(e.quality_score for e in entries) / len(entries)) if entries else 0
    return {
        "total_entries": len(entries),
        "by_source": dict(by_source),
        "by_domain": dict(by_domain),
        "by_type": dict(by_type),
        "total_use_count": total_uses,
        "avg_quality": round(avg_quality, 3),
        "has_embeddings": EMBEDDINGS_PATH.exists() and len(_load_embeddings()),
    }


# ─────────────────────────────────────────────────────────
#  Gap mining — when no entry was good enough
# ─────────────────────────────────────────────────────────
class Gap(BaseModel):
    id: str
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    user_message_excerpt: str
    damage_type: Optional[str] = None
    emotion_label: Optional[str] = None
    best_match_score: float = 0.0
    best_match_id: Optional[str] = None
    reason: str = Field(description="why this is a gap, e.g. 'low confidence' / 'escalated' / 'verifier rejected'")
    session_id: Optional[str] = None


def log_gap(g: Gap) -> None:
    with _write_lock:
        GAPS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with GAPS_PATH.open("a", encoding="utf-8") as f:
            f.write(g.model_dump_json() + "\n")


def list_gaps(limit: int = 50) -> list[dict[str, Any]]:
    if not GAPS_PATH.exists():
        return []
    rows = [json.loads(l) for l in GAPS_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    return rows[-limit:][::-1]  # newest first


# ─────────────────────────────────────────────────────────
#  Feedback — 👍 / 👎 from customers, written back to KB quality
# ─────────────────────────────────────────────────────────
class Feedback(BaseModel):
    id: str
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    session_id: str
    rating: int = Field(ge=-1, le=1, description="-1=👎, +1=👍, 0=neutral comment-only")
    comment: Optional[str] = None
    cited_entry_ids: list[str] = Field(default_factory=list)


def log_feedback(fb: Feedback) -> None:
    with _write_lock:
        FEEDBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with FEEDBACK_PATH.open("a", encoding="utf-8") as f:
            f.write(fb.model_dump_json() + "\n")
    # Adjust KB quality scores for cited entries
    for entry_id in fb.cited_entry_ids:
        entries = _load_kb()
        for e in entries:
            if e.id == entry_id:
                if fb.rating > 0:
                    e.upvotes += 1
                elif fb.rating < 0:
                    e.downvotes += 1
                # Bayesian-ish quality update
                total = e.upvotes + e.downvotes + 2  # smoothing
                e.quality_score = (e.upvotes + 1) / total
                upsert(e)
                break


def list_feedback(limit: int = 50) -> list[dict[str, Any]]:
    if not FEEDBACK_PATH.exists():
        return []
    rows = [json.loads(l) for l in FEEDBACK_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    return rows[-limit:][::-1]
