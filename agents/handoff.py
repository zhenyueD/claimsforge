"""
Handoff Summary — when a claim escalates to a human, build a structured
briefing so the agent doesn't have to re-read the whole transcript.

Why this exists:
  Without this, a human takes ~3-5 min to context-switch into a claim:
  scroll the chat, infer what the bot decided, guess why it escalated.
  HandoffSummary compresses that into a 30-second skim:
    - what the customer wants + how angry they are
    - what the model concluded (intent / damage / supervisor verdict)
    - what was offered (if anything) and what was blocked
    - recommended human action

Storage:
  Append-only JSONL at data/handoff_queue.jsonl, newest at the bottom.
  Agents drain via GET /api/admin/handoff-queue and resolve via
  POST /api/admin/handoff/{handoff_id}/resolve (future).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from schemas import ClaimContext

logger = logging.getLogger(__name__)

QUEUE_PATH = Path(__file__).resolve().parent.parent / "data" / "handoff_queue.jsonl"


class HandoffPriority(str, Enum):
    P0 = "p0"  # legal / regulator / threats — human in <5 min
    P1 = "p1"  # high emotion or repeat complainer — human in <30 min
    P2 = "p2"  # normal escalation — same business day
    P3 = "p3"  # informational handoff (e.g. successful auto-close audit)


class HandoffSummary(BaseModel):
    """Structured handoff briefing for a human agent.

    Designed to be readable in <30 seconds. Each field is a single
    short string; recommended_action is the only multi-sentence block.
    """
    handoff_id: str
    session_id: str
    created_at: str
    priority: HandoffPriority

    # ── Customer state
    customer_message: str = Field(description="The triggering message, truncated to 240 chars")
    customer_language: str = Field(description="'zh' or 'en' — what to reply in")
    emotion_label: Optional[str] = None
    emotion_score: Optional[float] = None
    escalation_signals: list[str] = Field(default_factory=list)

    # ── Model reasoning chain (one-liners)
    intent_label: Optional[str] = None
    intent_order_id: Optional[str] = None
    intent_product_hint: Optional[str] = None
    damage_summary: Optional[str] = Field(default=None, description="e.g. 'crack severity 8/10 conf=0.9'")

    # ── What the bot decided
    proposed_offer: Optional[str] = Field(default=None, description="e.g. 'full_refund $24'")
    policy_ids: list[str] = Field(default_factory=list)
    supervisor_verdict: Optional[str] = None
    blocked_rules: list[str] = Field(default_factory=list)

    # ── Conversation context
    prior_turn_count: int = 0
    last_assistant_decision: Optional[str] = Field(
        default=None, description="What we last told the customer in this session"
    )

    # ── Action items for the human
    escalation_reason: str = Field(description="One-line WHY this hit a human")
    recommended_action: str = Field(description="Concrete next step for the human agent")
    status: str = Field(default="open", description="open | claimed | resolved")
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None
    resolution_note: Optional[str] = None


def _classify_priority(ctx: ClaimContext) -> HandoffPriority:
    """Pick a priority band based on what tripped the escalation.

    Order matters — first match wins. P0 always takes precedence over
    high emotion because legal/regulator beats angry-but-non-litigious.
    """
    # P0: legal / regulator threats — any escalation signal mentioning these
    if ctx.emotion and ctx.emotion.escalation_signals:
        signals_blob = " ".join(s.lower() for s in ctx.emotion.escalation_signals)
        for needle in ("lawyer", "court", "12315", "消协", "consumer protection", "media", "媒体", "律师"):
            if needle in signals_blob:
                return HandoffPriority.P0

    # P0: Supervisor force-escalated on a hard rule (water+electronics, luxury)
    sup = ctx.supervisor_decision or {}
    if sup.get("verdict") == "force_escalate":
        return HandoffPriority.P0

    # P1: high emotion (score >= 8) or HIGH/CRITICAL risk band
    if ctx.emotion:
        if ctx.emotion.score >= 8 or ctx.emotion.risk.value in {"HIGH", "CRITICAL"}:
            return HandoffPriority.P1

    # P1: repeat complainer (5+ history turns and still escalating)
    if len(ctx.history) >= 5:
        return HandoffPriority.P1

    return HandoffPriority.P2


def _build_escalation_reason(ctx: ClaimContext) -> str:
    """One-line WHY. Reads from supervisor decision when present, else
    falls back to inferring from offer / damage / confidence."""
    sup = ctx.supervisor_decision or {}
    if sup.get("blocked_rules"):
        return f"Supervisor blocked: {sup['blocked_rules'][0]}"
    if sup.get("reasons") and sup.get("verdict") in {"force_escalate"}:
        return sup["reasons"][0][:160]

    if ctx.verification and ctx.verification.verdict.value == "escalate_to_human":
        return f"Verifier escalated: {ctx.verification.reason[:160]}"

    if ctx.damage and ctx.damage.confidence < 0.2:
        return "Damage evidence too weak to auto-decide"

    if ctx.offer is None:
        return "No policy matched — fallback to human"

    return "Bot chose to defer to human (default)"


def _build_recommended_action(ctx: ClaimContext, priority: HandoffPriority) -> str:
    """A specific next step, not a generic 'review this'. Phrased as an
    instruction so the agent can act without rereading."""
    is_zh = any("一" <= c <= "鿿" for c in (ctx.user_message or ""))
    reply_lang = "Chinese" if is_zh else "English"

    if priority == HandoffPriority.P0:
        sup = ctx.supervisor_decision or {}
        if "lawyer" in str(sup).lower() or "12315" in str(sup) or "消协" in str(sup):
            return (
                f"LEGAL THREAT — do NOT make a unilateral offer. Loop in compliance/legal first. "
                f"Reply in {reply_lang}. Acknowledge receipt within 1 hour."
            )
        if "luxury" in str(sup).lower():
            return (
                f"Luxury category — verify SKU + authenticity record, then offer per category SOP. "
                f"Reply in {reply_lang}."
            )
        if "electronics" in str(sup).lower() and "water" in str(sup).lower():
            return (
                f"Water damage on electronics — check warranty coverage manually. Most claims of this "
                f"type are out of warranty. Reply in {reply_lang}."
            )

    if ctx.offer:
        amt = ctx.offer.amount_cents / 100
        cur = ctx.offer.currency
        return (
            f"Bot proposed {ctx.offer.offer_type.value} {cur}{amt:.2f} (policies: "
            f"{', '.join(ctx.offer.policy_ids)}). Review then either approve as-is, adjust, "
            f"or escalate. Reply in {reply_lang}."
        )

    if ctx.damage and ctx.damage.confidence < 0.2:
        return (
            f"Evidence is weak (no clear photo or text is vague). Ask the customer for a clearer "
            f"photo / order number, then re-run via the dashboard. Reply in {reply_lang}."
        )

    return f"Review the conversation and decide. Reply in {reply_lang}."


def build_summary(ctx: ClaimContext) -> HandoffSummary:
    """Build a HandoffSummary from a fully-pipelined ClaimContext.

    Call this AFTER the orchestrator has finished and ctx.escalated_to_human
    is True. Safe to call on partially-populated ctx (degrades to None fields).
    """
    priority = _classify_priority(ctx)
    is_zh = any("一" <= c <= "鿿" for c in (ctx.user_message or ""))

    damage_summary = None
    if ctx.damage:
        damage_summary = (
            f"{ctx.damage.damage_type.value} severity {ctx.damage.severity}/10 "
            f"conf={ctx.damage.confidence:.2f}"
        )

    proposed_offer = None
    if ctx.offer:
        proposed_offer = (
            f"{ctx.offer.offer_type.value} {ctx.offer.currency}"
            f"{ctx.offer.amount_cents/100:.2f}"
        )

    last_assistant_decision = None
    for turn in reversed(ctx.history):
        if turn.role == "assistant" and turn.decision_summary:
            last_assistant_decision = turn.decision_summary
            break

    sup = ctx.supervisor_decision or {}

    return HandoffSummary(
        handoff_id=f"ho-{ctx.session_id}-{int(datetime.now().timestamp())}",
        session_id=ctx.session_id,
        created_at=datetime.now().isoformat(),
        priority=priority,
        customer_message=ctx.user_message[:240],
        customer_language="zh" if is_zh else "en",
        emotion_label=ctx.emotion.label if ctx.emotion else None,
        emotion_score=ctx.emotion.score if ctx.emotion else None,
        escalation_signals=list(ctx.emotion.escalation_signals) if ctx.emotion else [],
        intent_label=ctx.intent.label.value if ctx.intent else None,
        intent_order_id=ctx.intent.order_id if ctx.intent else None,
        intent_product_hint=ctx.intent.product_hint if ctx.intent else None,
        damage_summary=damage_summary,
        proposed_offer=proposed_offer,
        policy_ids=list(ctx.offer.policy_ids) if ctx.offer else [],
        supervisor_verdict=sup.get("verdict"),
        blocked_rules=list(sup.get("blocked_rules") or []),
        prior_turn_count=len(ctx.history),
        last_assistant_decision=last_assistant_decision,
        escalation_reason=_build_escalation_reason(ctx),
        recommended_action=_build_recommended_action(ctx, priority),
    )


# ─────────────────────────────────────────────────────────
#  Queue persistence
# ─────────────────────────────────────────────────────────
def enqueue(summary: HandoffSummary) -> None:
    """Append a summary to the JSONL queue."""
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with QUEUE_PATH.open("a", encoding="utf-8") as f:
        f.write(summary.model_dump_json() + "\n")


def list_queue(status: Optional[str] = None, limit: int = 50) -> list[HandoffSummary]:
    """Read the queue. Returns newest first. Optionally filter by status."""
    if not QUEUE_PATH.exists():
        return []
    summaries: list[HandoffSummary] = []
    with QUEUE_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                s = HandoffSummary.model_validate_json(line)
                if status is None or s.status == status:
                    summaries.append(s)
            except Exception as e:
                logger.warning("malformed handoff line: %s", e)
    summaries.sort(key=lambda s: s.created_at, reverse=True)
    return summaries[:limit]


def resolve(handoff_id: str, resolved_by: str, note: str = "") -> Optional[HandoffSummary]:
    """Mark a handoff as resolved. Rewrites the JSONL with the updated row.

    O(n) but n stays small (queue is drained continuously). Switch to sqlite
    if this becomes a hotspot.
    """
    if not QUEUE_PATH.exists():
        return None
    items: list[HandoffSummary] = []
    target: Optional[HandoffSummary] = None
    with QUEUE_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                s = HandoffSummary.model_validate_json(line)
            except Exception:
                continue
            if s.handoff_id == handoff_id and s.status != "resolved":
                s.status = "resolved"
                s.resolved_at = datetime.now().isoformat()
                s.resolved_by = resolved_by
                s.resolution_note = note[:500] if note else None
                target = s
            items.append(s)
    if target is None:
        return None
    with QUEUE_PATH.open("w", encoding="utf-8") as f:
        for s in items:
            f.write(s.model_dump_json() + "\n")
    return target


def queue_stats() -> dict:
    """Quick counts for the admin dashboard."""
    items = list_queue(limit=10000)
    by_status = {"open": 0, "claimed": 0, "resolved": 0}
    by_priority = {"p0": 0, "p1": 0, "p2": 0, "p3": 0}
    for s in items:
        by_status[s.status] = by_status.get(s.status, 0) + 1
        by_priority[s.priority.value] = by_priority.get(s.priority.value, 0) + 1
    return {
        "total": len(items),
        "by_status": by_status,
        "by_priority": by_priority,
        "oldest_open": next(
            (s.created_at for s in reversed(items) if s.status == "open"), None
        ),
    }
