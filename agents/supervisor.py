"""
SupervisorAgent — pure-Python safety gate that runs AFTER CompensationAgent
and BEFORE VerifierAgent.

Why this exists (Sierra-style):
  Letting an LLM be the only judge of whether a claim is safe to auto-pay
  has two well-known failure modes:
    1. Hallucinated policy ("I checked the policy and it allows $500" — except
       the policy doesn't exist)
    2. Tone over substance ("Verifier said approve because the writing was
       empathetic", ignoring that the amount was 3× the cap)
  The Supervisor enforces hard, pure-Python rules that the LLM cannot
  reason its way around. The LLM-driven VerifierAgent that runs after this
  only does soft checks (tone, fit) — it cannot un-escalate or raise an
  amount past the supervisor's cap.

Three categories of rules:
  HARD CAPS — absolute ceilings on cash payments / per-customer frequency
  EXEMPTIONS — when categorical policies (e.g. perishables) should override
               generic policies (e.g. "no image → escalate")
  NEVER_AUTO_PAY — patterns that always escalate regardless of what
                   CompensationAgent proposed

Decisions are typed (SupervisorDecision) and persisted on ctx so the UI /
audit log can see "why didn't this go through".
"""
from __future__ import annotations

import json
import logging
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from schemas import (
    AgentName,
    ClaimContext,
    CompensationOffer,
    DamageType,
    OfferType,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
#  Hard rule constants (live here, not in policies.json — these are the
#  absolutes nobody can override per-policy)
# ─────────────────────────────────────────────────────────
MAX_CASH_PAYMENT_CENTS = 50000          # $500 — anything above goes to a human, always
MAX_PERCENT_OF_ORDER = 1.0              # offer cannot exceed 100% of the order value
LIFETIME_CLAIM_LIMIT_PER_SESSION = 3    # within a session, escalate the 4th+ claim

# Perishable / cosmetic / consumable categories — these get a no-image exemption.
# (Customers can't reasonably photograph melted ice cream after the courier
# arrives 3h late, and asking them to is the worst-of-both-worlds CS UX.)
NOIMAGE_EXEMPT_DOMAINS = {"perishables", "perishable", "food", "beverage", "cosmetics", "skincare", "supplements"}

# Patterns that ALWAYS escalate — even if the model thinks otherwise.
# Each rule: (predicate fn, human-readable reason)
NEVER_AUTO_PAY_RULES = [
    (
        lambda ctx: bool(
            ctx.damage and ctx.damage.damage_type == DamageType.WATER_DAMAGE
            and ctx.intent and (ctx.intent.product_hint or "").lower() in
            {"laptop", "phone", "tablet", "electronics", "monitor", "headphones", "camera"}
        ),
        "Liquid damage on electronics requires manual warranty interpretation"
    ),
    (
        lambda ctx: bool(
            ctx.emotion and ctx.emotion.escalation_signals
            and any("lawyer" in s.lower() or "court" in s.lower() or "consumer protection" in s.lower()
                    or "12315" in s.lower() or "消协" in s for s in ctx.emotion.escalation_signals)
        ),
        "Legal / regulator threat detected — human must respond"
    ),
    (
        lambda ctx: bool(
            ctx.intent and (ctx.intent.product_hint or "").lower() in {"luxury", "jewelry", "watch", "designer"}
        ),
        "Luxury category requires manual handling regardless of policy"
    ),
]


class SupervisorVerdict(str, Enum):
    APPROVE = "approve"             # offer passes all hard rules — verifier may still soft-revise
    CAP_AMOUNT = "cap_amount"       # offer was over a hard cap — supervisor reduces it
    UN_ESCALATE = "un_escalate"     # compensation_agent escalated, but supervisor overrides
                                    # (e.g. perishable case that triggered P-LMT-01 wrongly)
    FORCE_ESCALATE = "force_escalate"  # hard rule says human only, no LLM can override


class SupervisorDecision(BaseModel):
    verdict: SupervisorVerdict
    reasons: list[str] = Field(default_factory=list, description="Why this verdict")
    blocked_rules: list[str] = Field(default_factory=list, description="Which hard rules / NEVER_AUTO_PAY triggered")
    original_amount_cents: Optional[int] = Field(default=None, description="If verdict=cap_amount, what it was before")
    capped_amount_cents: Optional[int] = Field(default=None, description="If verdict=cap_amount, what we capped it to")


# Session-level claim counter. Persisted to JSON so the frequency cap (the
# safety net against the same customer flooding claims) survives restarts.
# Plain dict + threading.Lock is fine here: claims-per-second is low, and a
# rare race that double-increments only over-counts (more conservative, fine).
_COUNTER_PATH = Path(__file__).resolve().parent.parent / "data" / "session_claim_counts.json"
_counter_lock = threading.Lock()


def _load_counts() -> dict[str, int]:
    """Read the persisted counter. Returns {} on any error."""
    try:
        if _COUNTER_PATH.exists():
            data = json.loads(_COUNTER_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}
    except Exception as e:
        logger.warning("session_claim_counts load failed: %s", e)
    return {}


_session_claim_counts: dict[str, int] = _load_counts()


def _save_counts() -> None:
    """Atomic-ish write: temp file + rename so a crash mid-write doesn't
    corrupt the JSON. Holds _counter_lock during serialize+swap."""
    try:
        _COUNTER_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _COUNTER_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_session_claim_counts), encoding="utf-8")
        tmp.replace(_COUNTER_PATH)
    except Exception as e:
        logger.warning("session_claim_counts save failed: %s", e)


# Equivalence classes for the multimodal consistency check (Rule 0.3).
# Each tuple is a synonym cluster — if hinted and detected words both live in
# the same cluster (or share a substring), they're considered the same item.
# Keep this tight: false positives here become legitimate claims that get
# wrongly escalated. False negatives (mismatches we miss) get caught by the
# verifier downstream.
_SUBJECT_SYNONYMS = [
    {"mug", "cup", "tumbler", "glass", "teacup", "杯", "杯子", "马克杯", "茶杯"},
    {"laptop", "notebook", "macbook", "computer", "笔记本", "电脑", "笔电"},
    {"phone", "smartphone", "iphone", "android", "mobile", "手机"},
    {"tablet", "ipad", "平板"},
    {"jacket", "coat", "hoodie", "sweater", "shirt", "tshirt", "dress", "外套", "衣服", "上衣", "夹克"},
    {"shoe", "shoes", "sneaker", "boot", "sandal", "鞋", "鞋子", "运动鞋"},
    {"bag", "backpack", "handbag", "purse", "suitcase", "luggage", "包", "背包", "手提包", "行李"},
    {"cake", "bread", "cookie", "food", "drink", "beverage", "fruit", "蛋糕", "面包", "食物", "饮料", "水果"},
    {"cosmetic", "skincare", "lipstick", "perfume", "makeup", "化妆品", "护肤品", "口红", "香水"},
    {"watch", "jewelry", "necklace", "ring", "bracelet", "手表", "首饰", "项链", "戒指"},
    {"headphones", "earbuds", "speaker", "soundbar", "耳机", "音箱", "蓝牙音箱"},
    {"camera", "lens", "tripod", "相机", "镜头"},
    {"toy", "doll", "lego", "玩具", "积木"},
    {"book", "magazine", "书", "杂志"},
    {"furniture", "chair", "table", "desk", "sofa", "shelf", "家具", "椅子", "桌子", "沙发"},
]


def _normalize_subject(s: str) -> str:
    """Lowercase, strip punctuation. Keeps CJK chars intact."""
    if not s:
        return ""
    return "".join(c for c in s.lower() if c.isalnum() or c in " 一二三四五六七八九十" or '一' <= c <= '鿿').strip()


def _subjects_compatible(a: str, b: str) -> bool:
    """True if two subject strings plausibly name the same physical item.
    Conservative — defaults to compatible when uncertain, so legitimate
    claims aren't escalated for borderline word choices."""
    if not a or not b:
        return True
    if a == b:
        return True
    # Substring match either way (e.g. "ceramic mug" contains "mug")
    if a in b or b in a:
        return True
    a_words = set(a.split())
    b_words = set(b.split())
    if a_words & b_words:
        return True
    # Synonym cluster lookup — any word from each lands in the same cluster?
    for cluster in _SUBJECT_SYNONYMS:
        a_hit = any(w in cluster for w in a_words) or any(c in a for c in cluster)
        b_hit = any(w in cluster for w in b_words) or any(c in b for c in cluster)
        if a_hit and b_hit:
            return True
    return False


def _check_session_frequency(session_id: str) -> tuple[bool, Optional[str]]:
    """Returns (within_limit, reason_if_exceeded)."""
    n = _session_claim_counts.get(session_id, 0)
    if n >= LIFETIME_CLAIM_LIMIT_PER_SESSION:
        return False, f"Session already has {n} claims (limit {LIFETIME_CLAIM_LIMIT_PER_SESSION})"
    return True, None


def _record_session_claim(session_id: str) -> None:
    with _counter_lock:
        _session_claim_counts[session_id] = _session_claim_counts.get(session_id, 0) + 1
        _save_counts()


def evaluate(ctx: ClaimContext, estimated_value_cents: int = 5000) -> SupervisorDecision:
    """Run the supervisor over the current ctx. Mutates ctx in three ways:
      - May set ctx.escalated_to_human = True (FORCE_ESCALATE)
      - May clear ctx.escalated_to_human (UN_ESCALATE for category exemptions)
      - May replace ctx.offer.amount_cents with a capped value (CAP_AMOUNT)

    Returns the decision so orchestrator/UI can show what happened.
    """
    reasons: list[str] = []
    blocked: list[str] = []
    verdict = SupervisorVerdict.APPROVE

    # ── Rule 0.3: multimodal consistency — text vs image subject mismatch
    # Customer says "my ceramic mug arrived cracked" but the image shows a
    # smartphone. The LLM correctly assessed the smartphone, but the claim
    # context is incoherent — either confused customer or active fraud.
    # Either way: human must adjudicate. This is Sierra-impossible (they
    # have no vision channel to cross-check against).
    if ctx.intent and ctx.intent.product_hint and ctx.damage and ctx.damage.detected_subject:
        hinted = _normalize_subject(ctx.intent.product_hint)
        detected = _normalize_subject(ctx.damage.detected_subject)
        if hinted and detected and not _subjects_compatible(hinted, detected):
            msg = (
                f"Text/image mismatch — customer described '{ctx.intent.product_hint}' "
                f"but image shows '{ctx.damage.detected_subject}'"
            )
            blocked.append(msg)
            reasons.append(f"MULTIMODAL_MISMATCH: {msg}")
            verdict = SupervisorVerdict.FORCE_ESCALATE
            ctx.escalated_to_human = True
            ctx.final_offer = None
            return SupervisorDecision(verdict=verdict, reasons=reasons, blocked_rules=blocked)

    # ── Rule 0: visual fraud replay (pHash collision against approved set)
    # Only CROSS-session collisions escalate. Same-session is the customer
    # accidentally re-uploading the same photo on a follow-up turn (or
    # re-running a demo) — not fraud.
    if ctx.image_phash:
        try:
            import fraud as _fraud
            hit = _fraud.find_collision(ctx.image_phash, current_session_id=ctx.session_id)
            if hit and hit.get("_cross_session"):
                dist = hit.get("_hamming_distance", "?")
                prior_id = (hit.get("image_id") or "")[:16]
                msg = (
                    f"Image pHash collision (cross-session, Hamming={dist} bits) with "
                    f"previously-approved claim image {prior_id}…"
                )
                blocked.append(msg)
                reasons.append(f"FRAUD_REPLAY: {msg}")
                verdict = SupervisorVerdict.FORCE_ESCALATE
                ctx.escalated_to_human = True
                ctx.final_offer = None
                return SupervisorDecision(verdict=verdict, reasons=reasons, blocked_rules=blocked)
        except Exception as e:
            logger.warning("fraud gate scan failed (non-fatal): %s", e)

    # ── Rule 0.5: duplicate-claim on already-resolved order
    # Customer accepts a refund, then re-opens the same order_id hoping the
    # bot's amnesia will double-pay. Pure-Python history scan — if any prior
    # user turn mentioned this order_id AND was followed by a 'resolve:accept'
    # marker, force-escalate.
    if ctx.intent and ctx.intent.order_id and ctx.history:
        oid = ctx.intent.order_id.upper()
        h = ctx.history
        accept_idx = next(
            (i for i, t in enumerate(h)
             if t.role == "user" and (t.decision_summary or "") == "resolve:accept"),
            -1,
        )
        if accept_idx > 0:
            mentioned_before = any(
                t.role == "user" and oid in (t.content or "").upper()
                for t in h[:accept_idx]
            )
            if mentioned_before:
                msg = f"Duplicate claim on order {oid} — prior refund already accepted"
                blocked.append(msg)
                reasons.append(f"DUPLICATE_CLAIM: {msg}")
                verdict = SupervisorVerdict.FORCE_ESCALATE
                ctx.escalated_to_human = True
                ctx.final_offer = None
                return SupervisorDecision(verdict=verdict, reasons=reasons, blocked_rules=blocked)

    # ── Rule 1: NEVER_AUTO_PAY patterns
    for predicate, msg in NEVER_AUTO_PAY_RULES:
        try:
            if predicate(ctx):
                blocked.append(msg)
                reasons.append(f"NEVER_AUTO_PAY: {msg}")
                verdict = SupervisorVerdict.FORCE_ESCALATE
        except Exception as e:
            logger.warning("supervisor predicate raised: %s", e)

    if verdict == SupervisorVerdict.FORCE_ESCALATE:
        ctx.escalated_to_human = True
        ctx.final_offer = None
        return SupervisorDecision(verdict=verdict, reasons=reasons, blocked_rules=blocked)

    # ── Rule 2: session frequency cap
    within, freq_reason = _check_session_frequency(ctx.session_id)
    if not within:
        blocked.append(freq_reason or "Session frequency limit")
        reasons.append(freq_reason or "")
        ctx.escalated_to_human = True
        ctx.final_offer = None
        return SupervisorDecision(
            verdict=SupervisorVerdict.FORCE_ESCALATE,
            reasons=reasons, blocked_rules=blocked
        )

    # ── Rule 3: category exemption — un-escalate perishables that hit P-LMT-01
    # Compensation_agent may have escalated because of no-image rule. If the
    # damage type or domain is no-image-exempt, supervisor reverses that
    # decision. If compensation already produced an offer, keep it; otherwise
    # synthesize a default full_refund using estimated_value_cents.
    if ctx.escalated_to_human and ctx.damage:
        product = (ctx.intent.product_hint or "").lower() if ctx.intent else ""
        damage_domain = (ctx.damage.damage_type.value or "").lower()
        msg_lower = (ctx.user_message or "").lower()
        is_exempt = (
            any(d in product for d in NOIMAGE_EXEMPT_DOMAINS)
            or any(d in msg_lower for d in ["food", "drink", "beverage", "cosmetic", "skincare",
                                            "蛋糕", "食物", "饮料", "化妆品", "护肤"])
        )
        if is_exempt:
            is_zh = any('一' <= c <= '鿿' for c in (ctx.user_message or ""))
            if ctx.offer is None:
                cap = min(estimated_value_cents, 15000)
                ctx.offer = CompensationOffer(
                    offer_type=OfferType.FULL_REFUND,
                    amount_cents=cap,
                    currency="¥" if is_zh else "USD",
                    justification=(
                        f"按生鲜/即食类商品惯例，已为您发起全额退款 ¥{cap/100:.2f}，"
                        "无需照片证据也无需寄回。我们会同步把这次的物流问题反馈给承运方。"
                    ) if is_zh else (
                        "Per perishables policy: you'll receive a full refund of "
                        f"${cap/100:.2f}, no photo or return required. Sorry for the trouble — "
                        "we'll also flag this with the carrier."
                    ),
                    policy_ids=["P-PER-01-supervisor-exempt"],
                    requires_return=False,
                )
            else:
                # Keep compensation_agent's offer, just add supervisor exemption tag
                if "P-PER-01-supervisor-exempt" not in ctx.offer.policy_ids:
                    ctx.offer.policy_ids = list(ctx.offer.policy_ids) + ["P-PER-01-supervisor-exempt"]
            ctx.escalated_to_human = False
            verdict = SupervisorVerdict.UN_ESCALATE
            reasons.append(
                f"Category exemption: {product or damage_domain} is no-image-exempt; "
                "P-LMT-01 escalation overridden, instant refund applied"
            )

    # ── Rule 4: hard cap on cash amount
    if ctx.offer and ctx.offer.amount_cents > MAX_CASH_PAYMENT_CENTS:
        original = ctx.offer.amount_cents
        ctx.offer.amount_cents = MAX_CASH_PAYMENT_CENTS
        verdict = SupervisorVerdict.CAP_AMOUNT
        reasons.append(f"Hard cap: amount {original/100:.2f} > MAX ${MAX_CASH_PAYMENT_CENTS/100:.0f}; capped")
        return SupervisorDecision(
            verdict=verdict, reasons=reasons, blocked_rules=blocked,
            original_amount_cents=original, capped_amount_cents=MAX_CASH_PAYMENT_CENTS,
        )

    # ── Rule 5: amount can't exceed 100% of order value
    if (
        ctx.offer
        and ctx.offer.offer_type in {OfferType.FULL_REFUND, OfferType.PARTIAL_REFUND}
        and ctx.offer.amount_cents > int(estimated_value_cents * MAX_PERCENT_OF_ORDER)
    ):
        original = ctx.offer.amount_cents
        capped = int(estimated_value_cents * MAX_PERCENT_OF_ORDER)
        ctx.offer.amount_cents = capped
        verdict = SupervisorVerdict.CAP_AMOUNT
        reasons.append(
            f"Hard cap: refund {original/100:.2f} > order value {estimated_value_cents/100:.2f}; capped to 100%"
        )
        return SupervisorDecision(
            verdict=verdict, reasons=reasons, blocked_rules=blocked,
            original_amount_cents=original, capped_amount_cents=capped,
        )

    return SupervisorDecision(verdict=verdict, reasons=reasons or ["all hard rules passed"])


def run(ctx: ClaimContext, estimated_value_cents: int = 5000) -> ClaimContext:
    """Orchestrator entry point. Records the decision on ctx.supervisor_decision
    and emits a trace so the UI can show what happened."""
    t0 = time.monotonic()
    decision = evaluate(ctx, estimated_value_cents=estimated_value_cents)
    elapsed = int((time.monotonic() - t0) * 1000)

    # Record on ctx for downstream agents + API response (dict to avoid circular import)
    ctx.supervisor_decision = decision.model_dump()

    # Track session-level claim count (only count auto-paid ones)
    if not ctx.escalated_to_human and ctx.offer:
        _record_session_claim(ctx.session_id)
        # Promote this image's pHash to the approved collision-anchor set so
        # any future submission with the same image gets caught by Rule 0.
        if ctx.image_phash and ctx.image_id:
            try:
                import fraud as _fraud
                _fraud.record_approved(ctx.image_id, ctx.image_phash, ctx.session_id)
            except Exception as e:
                logger.warning("fraud anchor write failed (non-fatal): %s", e)

    # Build a one-line summary for the trace
    summary = decision.verdict.value
    if decision.verdict == SupervisorVerdict.CAP_AMOUNT and decision.original_amount_cents and decision.capped_amount_cents:
        summary += f" ${decision.original_amount_cents/100:.0f}→${decision.capped_amount_cents/100:.0f}"
    elif decision.blocked_rules:
        summary += f" · {decision.blocked_rules[0][:60]}"

    ctx.add_trace(AgentName.SUPERVISOR, status="ok", summary=summary, elapsed_ms=elapsed)

    logger.info(
        "supervisor decision: %s for session=%s (reasons=%s)",
        decision.verdict.value, ctx.session_id, decision.reasons[:2],
    )
    return ctx
