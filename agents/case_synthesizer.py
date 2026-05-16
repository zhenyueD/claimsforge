"""
Case synthesizer — meta-learning over the LEARNED_CASE entries.

When the same kind of claim shows up enough times, the system should stop
re-reasoning from scratch and write down the PATTERN as a reusable methodology.

Pipeline (run periodically or triggered by orchestrator):
  1. Pull all KBSource.LEARNED_CASE entries
  2. Cluster them by (damage_type, emotion bucket, offer outcome) keys
     plus embedding similarity
  3. For each cluster with >= MIN_CLUSTER_SIZE cases that doesn't already
     have a METHODOLOGY entry, ask Gemini to synthesize:
        - the recurring pattern
        - the decision rubric
        - the edge cases that broke the pattern
  4. Upsert as KBType.METHODOLOGY + embed

The result: every agent gets stronger over time, not just storing more cases
but distilling actionable wisdom. This is the loop that turns ClaimsForge
from "an LLM with a knowledge base" into "a system that learns".
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from typing import Optional

from pydantic import BaseModel, Field

from gemini_client import GeminiError, structured
from unified_kb import KBEntry, KBSource, KBType, _load_kb, upsert, make_id
from embedding_index import index_all, _cosine, _load_embeddings, embed_text

logger = logging.getLogger(__name__)

MIN_CLUSTER_SIZE = 3  # need ≥ 3 cases of a pattern before synthesizing
EMBEDDING_SIMILARITY_THRESHOLD = 0.72


class SynthesizedMethodology(BaseModel):
    title: str = Field(description="≤ 100 chars — the pattern name (e.g. 'Time-sensitive gift damage handling')")
    domain: str = Field(description="single word: damage / emotion / logistics / pricing / fraud / etc")
    pattern: str = Field(description="2-3 sentences describing the recurring situation across the cluster")
    decision_rubric: str = Field(description="The reusable IF-THEN guidance distilled from the cases. Be concrete.")
    rationale: str = Field(description="Why this rubric works — what underlying principle these cases share")
    edge_cases_to_watch: list[str] = Field(default_factory=list, description="Where this pattern broke or needed escalation")
    tags: list[str] = Field(default_factory=list, description="3-8 tags for retrieval")
    confidence: float = Field(default=0.7, ge=0, le=1, description="0=speculative, 1=strong pattern across diverse cases")


_SYS = """You are a customer-service operations analyst. You're given a CLUSTER of past
resolved claims that the system has handled. They share something in common — same damage
type, same emotional profile, similar outcome. Your job: distill the PATTERN into a
reusable methodology so the next agent (or human) doesn't have to re-reason from scratch.

Output a methodology that:
  - has a concise, useful title (not "Cluster 7")
  - states the pattern in plain language
  - gives a decision rubric the next agent can directly act on
  - explains WHY (the underlying principle)
  - notes edge cases where the pattern broke

Faithfulness > cleverness. If the cases actually disagree, say so honestly in
edge_cases_to_watch and lower the confidence score. Don't invent details.
"""


# ─────────────────────────────────────────────────────────
#  Clustering
# ─────────────────────────────────────────────────────────
def _bucket_key(case: KBEntry) -> str:
    """Coarse bucket — damage type + outcome family."""
    # cases store domain = damage_type for LEARNED_CASE
    dmg = case.domain or "unknown"
    # Tags include outcome words like full_refund / replacement / escalated
    outcome = "?"
    for t in case.tags:
        if t in ("full_refund", "partial_refund", "replacement", "store_credit"):
            outcome = t; break
        if "escalat" in t.lower():
            outcome = "escalated"; break
    return f"{dmg}::{outcome}"


def cluster_cases(min_size: int = MIN_CLUSTER_SIZE) -> dict[str, list[KBEntry]]:
    """Return {bucket_key: [cases…]} for buckets meeting size threshold.

    Within each bucket, we DON'T further sub-cluster by embedding — the
    bucket key is already specific enough at this scale. At 10k+ cases we'd
    do hierarchical clustering inside each bucket.
    """
    entries = _load_kb()
    cases = [e for e in entries if e.source == KBSource.LEARNED_CASE]
    buckets: dict[str, list[KBEntry]] = defaultdict(list)
    for c in cases:
        buckets[_bucket_key(c)].append(c)
    return {k: v for k, v in buckets.items() if len(v) >= min_size}


def existing_methodology_keys() -> set[str]:
    """What clusters do we already have a methodology for? Avoid duplicates."""
    entries = _load_kb()
    keys = set()
    for e in entries:
        if e.type == KBType.METHODOLOGY:
            for t in e.tags:
                if "::" in t:
                    keys.add(t)
    return keys


# ─────────────────────────────────────────────────────────
#  Synthesis
# ─────────────────────────────────────────────────────────
def synthesize_cluster(bucket_key: str, cases: list[KBEntry]) -> Optional[SynthesizedMethodology]:
    """Ask Gemini to distill a cluster of cases into a methodology."""
    cases_block = "\n\n".join(
        f"### Case {i+1}\n"
        f"Scenario: {c.scenario}\n"
        f"Decision: {c.decision}\n"
        f"Rationale: {c.rationale[:300]}"
        for i, c in enumerate(cases[:12])  # cap at 12 to stay within token budget
    )
    prompt = (
        f"## Cluster\n{bucket_key} ({len(cases)} cases)\n\n"
        f"## Cases\n{cases_block}\n\n"
        f"## Task\nSynthesize the recurring pattern into a reusable methodology."
    )
    try:
        return structured(
            prompt=prompt, schema=SynthesizedMethodology,
            system=_SYS, temperature=0.3, max_tokens=1000,
        )
    except GeminiError as e:
        logger.warning("methodology synthesis failed for %s: %s", bucket_key, e)
        return None


# ─────────────────────────────────────────────────────────
#  Main entry — run synthesis, write methodology entries, return summary
# ─────────────────────────────────────────────────────────
def run_synthesis(
    min_cluster_size: int = MIN_CLUSTER_SIZE,
    rebuild_existing: bool = False,
    dry_run: bool = False,
) -> dict:
    clusters = cluster_cases(min_size=min_cluster_size)
    existing = set() if rebuild_existing else existing_methodology_keys()

    summary = {
        "min_cluster_size": min_cluster_size,
        "total_clusters": len(clusters),
        "skipped_existing": 0,
        "synthesized": 0,
        "written_ids": [],
        "dry_run": dry_run,
        "per_cluster": [],
    }

    for bucket_key, cases in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        info = {"bucket": bucket_key, "case_count": len(cases)}
        if bucket_key in existing:
            info["skipped"] = "already synthesized"
            summary["skipped_existing"] += 1
            summary["per_cluster"].append(info)
            continue

        if dry_run:
            info["would_synthesize"] = True
            summary["per_cluster"].append(info)
            continue

        meth = synthesize_cluster(bucket_key, cases)
        if not meth:
            info["error"] = "synthesis returned None"
            summary["per_cluster"].append(info)
            continue

        entry_id = make_id(f"meth-{bucket_key}-{int(time.time())}")
        entry = KBEntry(
            id=entry_id,
            source=KBSource.LEARNED_CASE,  # promoted up the value chain
            type=KBType.METHODOLOGY,
            domain=meth.domain or "general",
            title=meth.title,
            scenario=meth.pattern,
            decision=meth.decision_rubric,
            rationale=meth.rationale + (
                "\n\nEdge cases to watch:\n" + "\n".join(f"- {e}" for e in meth.edge_cases_to_watch)
                if meth.edge_cases_to_watch else ""
            ),
            tags=meth.tags + [bucket_key, f"derived_from_{len(cases)}_cases", "methodology"],
            contributor="case_synthesizer",
            quality_score=meth.confidence,
        )
        upsert(entry)
        summary["synthesized"] += 1
        summary["written_ids"].append(entry_id)
        info["written_id"] = entry_id
        info["title"] = meth.title
        info["confidence"] = meth.confidence
        summary["per_cluster"].append(info)

    # Embed any new methodology entries
    if summary["synthesized"] > 0 and not dry_run:
        try:
            idx = index_all(rate_limit_sleep=0.05)
            summary["embedded"] = idx.get("indexed", 0)
        except Exception as e:
            summary["embed_error"] = str(e)

    return summary


# ─────────────────────────────────────────────────────────
#  Methodology browse helpers (for the UI)
# ─────────────────────────────────────────────────────────
def list_methodologies(limit: int = 100) -> list[KBEntry]:
    entries = _load_kb()
    methods = [e for e in entries if e.type == KBType.METHODOLOGY]
    methods.sort(key=lambda e: e.created_at or "", reverse=True)
    return methods[:limit]
