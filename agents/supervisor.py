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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from schemas import (
    AgentName,
    ClaimContext,
    CompensationOffer,
    DamageType,
    OfferType,
    SupervisorDecision,
    SupervisorLayer,
    SupervisorVerdict,
    TrustFactor,
    TrustFactorName,
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

# NEVER_AUTO_PAY_RULES — v5 lambda list, replaced in v6 by DENY_RULES below.
# (Brief Task B Phase 2 will move 3 of these into data/hard_rules.json.)


# SupervisorVerdict / SupervisorDecision / SupervisorLayer are now in
# schemas.py (Brief §0.2: Pydantic is the contract, types live there).


# ─────────────────────────────────────────────────────────
#  AWS-IAM-style rule definitions
# ─────────────────────────────────────────────────────────
@dataclass
class DenyRule:
    """Explicit-deny rule. If matcher(ctx) is True, force-escalate and
    short-circuit the rest of the supervisor. id is stable + human-readable
    for audit logs and Trust Score backlinks."""
    id: str
    matcher: Callable[[ClaimContext], bool]
    reason_fn: Callable[[ClaimContext], str]


@dataclass
class ExemptRule:
    """Explicit-exempt rule. Only consulted if a downstream agent already
    set ctx.escalated_to_human = True. If matcher fires, apply() un-escalates
    (and may synthesize a default offer)."""
    id: str
    matcher: Callable[[ClaimContext], bool]
    apply: Callable[[ClaimContext, int], None]
    reason_fn: Callable[[ClaimContext], str]


@dataclass
class CapRule:
    """Numerical clamp rule. apply() returns (original, capped) when a clamp
    happens, None when the rule didn't apply. Caps stack — running multiple
    in order keeps the strictest."""
    id: str
    apply: Callable[[ClaimContext, int], Optional[tuple[int, int]]]
    reason_fn: Callable[[ClaimContext], str]


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


# ─────────────────────────────────────────────────────────
#  Layer 1: DENY matchers — pure predicates over ctx
# ─────────────────────────────────────────────────────────
def _deny_multimodal_mismatch(ctx: ClaimContext) -> bool:
    if not (ctx.intent and ctx.intent.product_hint and ctx.damage and ctx.damage.detected_subject):
        return False
    hinted = _normalize_subject(ctx.intent.product_hint)
    detected = _normalize_subject(ctx.damage.detected_subject)
    return bool(hinted and detected and not _subjects_compatible(hinted, detected))


def _reason_multimodal_mismatch(ctx: ClaimContext) -> str:
    return (f"Text/image mismatch — customer described '{ctx.intent.product_hint}' "
            f"but image shows '{ctx.damage.detected_subject}'")


def _deny_fraud_replay(ctx: ClaimContext) -> bool:
    if not ctx.image_phash:
        return False
    try:
        import fraud as _fraud
        hit = _fraud.find_collision(ctx.image_phash, current_session_id=ctx.session_id)
        ctx.__dict__["_fraud_hit"] = hit if hit and hit.get("_cross_session") else None
        return bool(hit and hit.get("_cross_session"))
    except Exception as e:
        logger.warning("fraud gate scan failed (non-fatal): %s", e)
        return False


def _reason_fraud_replay(ctx: ClaimContext) -> str:
    hit = ctx.__dict__.get("_fraud_hit") or {}
    dist = hit.get("_hamming_distance", "?")
    prior_id = (hit.get("image_id") or "")[:16]
    return (f"Image pHash collision (cross-session, Hamming={dist} bits) with "
            f"previously-approved claim image {prior_id}…")


def _deny_duplicate_order(ctx: ClaimContext) -> bool:
    if not (ctx.intent and ctx.intent.order_id and ctx.history):
        return False
    oid = ctx.intent.order_id.upper()
    h = ctx.history
    accept_idx = next(
        (i for i, t in enumerate(h)
         if t.role == "user" and (t.decision_summary or "") == "resolve:accept"),
        -1,
    )
    if accept_idx <= 0:
        return False
    return any(t.role == "user" and oid in (t.content or "").upper()
               for t in h[:accept_idx])


def _reason_duplicate_order(ctx: ClaimContext) -> str:
    return f"Duplicate claim on order {ctx.intent.order_id.upper()} — prior refund already accepted"


def _deny_water_on_electronics(ctx: ClaimContext) -> bool:
    return bool(
        ctx.damage and ctx.damage.damage_type == DamageType.WATER_DAMAGE
        and ctx.intent and (ctx.intent.product_hint or "").lower() in
        {"laptop", "phone", "tablet", "electronics", "monitor", "headphones", "camera"}
    )


def _deny_legal_threat(ctx: ClaimContext) -> bool:
    if not (ctx.emotion and ctx.emotion.escalation_signals):
        return False
    return any(
        "lawyer" in s.lower() or "court" in s.lower() or "consumer protection" in s.lower()
        or "12315" in s.lower() or "消协" in s
        for s in ctx.emotion.escalation_signals
    )


def _deny_luxury(ctx: ClaimContext) -> bool:
    return bool(
        ctx.intent and (ctx.intent.product_hint or "").lower() in {"luxury", "jewelry", "watch", "designer"}
    )


def _deny_session_frequency(ctx: ClaimContext) -> bool:
    within, _ = _check_session_frequency(ctx.session_id)
    return not within


def _reason_session_frequency(ctx: ClaimContext) -> str:
    _, reason = _check_session_frequency(ctx.session_id)
    return reason or f"Session has exceeded {LIFETIME_CLAIM_LIMIT_PER_SESSION} claims"


# DENY layer — IAM-style: any match short-circuits the whole supervisor.
# Order matters only for which rule_id ends up in the audit log first;
# the verdict is the same.
DENY_RULES: list[DenyRule] = [
    DenyRule(
        id="RULE-MULTIMODAL-MISMATCH",
        matcher=_deny_multimodal_mismatch,
        reason_fn=_reason_multimodal_mismatch,
    ),
    DenyRule(
        id="RULE-FRAUD-REPLAY",
        matcher=_deny_fraud_replay,
        reason_fn=_reason_fraud_replay,
    ),
    DenyRule(
        id="RULE-DUPLICATE-ORDER",
        matcher=_deny_duplicate_order,
        reason_fn=_reason_duplicate_order,
    ),
    DenyRule(
        id="RULE-LIQUID-ELECTRONICS",
        matcher=_deny_water_on_electronics,
        reason_fn=lambda ctx: "Liquid damage on electronics requires manual warranty interpretation",
    ),
    DenyRule(
        id="RULE-LEGAL-THREAT",
        matcher=_deny_legal_threat,
        reason_fn=lambda ctx: "Legal / regulator threat detected — human must respond",
    ),
    DenyRule(
        id="RULE-LUXURY",
        matcher=_deny_luxury,
        reason_fn=lambda ctx: "Luxury category requires manual handling regardless of policy",
    ),
    DenyRule(
        id="RULE-SESSION-FREQ",
        matcher=_deny_session_frequency,
        reason_fn=_reason_session_frequency,
    ),
]


# ─────────────────────────────────────────────────────────
#  Layer 2: EXEMPT — un-escalate when category overrides P-LMT-01
# ─────────────────────────────────────────────────────────
def _exempt_perishable_match(ctx: ClaimContext) -> bool:
    if not (ctx.escalated_to_human and ctx.damage):
        return False
    product = (ctx.intent.product_hint or "").lower() if ctx.intent else ""
    msg_lower = (ctx.user_message or "").lower()
    return (
        any(d in product for d in NOIMAGE_EXEMPT_DOMAINS)
        or any(d in msg_lower for d in ["food", "drink", "beverage", "cosmetic", "skincare",
                                        "蛋糕", "食物", "饮料", "化妆品", "护肤"])
    )


def _exempt_perishable_apply(ctx: ClaimContext, estimated_value_cents: int) -> None:
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
    elif "P-PER-01-supervisor-exempt" not in ctx.offer.policy_ids:
        ctx.offer.policy_ids = list(ctx.offer.policy_ids) + ["P-PER-01-supervisor-exempt"]
    ctx.escalated_to_human = False


def _reason_perishable(ctx: ClaimContext) -> str:
    product = (ctx.intent.product_hint or "").lower() if ctx.intent else ""
    damage_domain = ctx.damage.damage_type.value if ctx.damage else ""
    return (f"Category exemption: {product or damage_domain} is no-image-exempt; "
            "P-LMT-01 escalation overridden, instant refund applied")


EXEMPT_RULES: list[ExemptRule] = [
    ExemptRule(
        id="RULE-PERISHABLE-EXEMPT",
        matcher=_exempt_perishable_match,
        apply=_exempt_perishable_apply,
        reason_fn=_reason_perishable,
    ),
]


# ─────────────────────────────────────────────────────────
#  Layer 3: CAP — clamp numerical fields, stack-safe
# ─────────────────────────────────────────────────────────
def _cap_max_cash(ctx: ClaimContext, estimated_value_cents: int) -> Optional[tuple[int, int]]:
    if ctx.offer and ctx.offer.amount_cents > MAX_CASH_PAYMENT_CENTS:
        original = ctx.offer.amount_cents
        ctx.offer.amount_cents = MAX_CASH_PAYMENT_CENTS
        return (original, MAX_CASH_PAYMENT_CENTS)
    return None


def _cap_pct_of_order(ctx: ClaimContext, estimated_value_cents: int) -> Optional[tuple[int, int]]:
    if not (ctx.offer and ctx.offer.offer_type in {OfferType.FULL_REFUND, OfferType.PARTIAL_REFUND}):
        return None
    ceiling = int(estimated_value_cents * MAX_PERCENT_OF_ORDER)
    if ctx.offer.amount_cents > ceiling:
        original = ctx.offer.amount_cents
        ctx.offer.amount_cents = ceiling
        return (original, ceiling)
    return None


CAP_RULES: list[CapRule] = [
    CapRule(
        id="RULE-MAX-CASH-PAYMENT",
        apply=_cap_max_cash,
        reason_fn=lambda ctx: (f"Hard cap: amount > MAX ${MAX_CASH_PAYMENT_CENTS/100:.0f}; "
                               f"capped to ${MAX_CASH_PAYMENT_CENTS/100:.0f}"),
    ),
    CapRule(
        id="RULE-MAX-PCT-ORDER",
        apply=_cap_pct_of_order,
        reason_fn=lambda ctx: ("Hard cap: refund exceeded 100% of order value; "
                               "capped to order value"),
    ),
]


# ─────────────────────────────────────────────────────────
#  IAM-style decision flow
# ─────────────────────────────────────────────────────────
def evaluate(ctx: ClaimContext, estimated_value_cents: int = 5000) -> SupervisorDecision:
    """AWS-IAM-style three-layer decision:

      1. DENY — any explicit-deny rule short-circuits to FORCE_ESCALATE
      2. EXEMPT — only if downstream agent escalated, an exempt rule can un-escalate
      3. CAP — clamp numerical fields (stacks: strictest wins)
      4. APPROVE — default-allow terminal

    Mutates ctx (escalated_to_human / offer.amount_cents) per rule application.
    Returns a typed SupervisorDecision the orchestrator records on
    ctx.supervisor_decision (as dict, for backwards compat).
    """
    matched_rules: list[str] = []
    reasons: list[str] = []

    # ── LAYER 1: DENY (explicit deny wins, IAM-style)
    for rule in DENY_RULES:
        try:
            if rule.matcher(ctx):
                reason = rule.reason_fn(ctx)
                matched_rules.append(rule.id)
                reasons.append(f"{rule.id}: {reason}")
                ctx.escalated_to_human = True
                ctx.final_offer = None
                return SupervisorDecision(
                    layer=SupervisorLayer.DENY,
                    verdict=SupervisorVerdict.FORCE_ESCALATE,
                    matched_rules=matched_rules,
                    reasons=reasons,
                    blocked_rules=list(matched_rules),  # backwards-compat
                )
        except Exception as e:
            logger.warning("DENY rule %s raised: %s", rule.id, e)

    # ── LAYER 2: EXEMPT (only if escalated; reverses prior escalation)
    if ctx.escalated_to_human:
        for rule in EXEMPT_RULES:
            try:
                if rule.matcher(ctx):
                    rule.apply(ctx, estimated_value_cents)
                    matched_rules.append(rule.id)
                    reasons.append(f"{rule.id}: {rule.reason_fn(ctx)}")
                    return SupervisorDecision(
                        layer=SupervisorLayer.EXEMPT,
                        verdict=SupervisorVerdict.UN_ESCALATE,
                        matched_rules=matched_rules,
                        reasons=reasons,
                        blocked_rules=list(matched_rules),
                    )
            except Exception as e:
                logger.warning("EXEMPT rule %s raised: %s", rule.id, e)

    # ── LAYER 3: CAP (stack — first cap wins on amount field per rule order)
    first_cap: Optional[tuple[int, int]] = None
    for rule in CAP_RULES:
        try:
            result = rule.apply(ctx, estimated_value_cents)
            if result:
                original, capped = result
                matched_rules.append(rule.id)
                reasons.append(f"{rule.id}: {rule.reason_fn(ctx)}")
                if first_cap is None:
                    first_cap = (original, capped)
        except Exception as e:
            logger.warning("CAP rule %s raised: %s", rule.id, e)

    if first_cap is not None:
        original, capped = first_cap
        return SupervisorDecision(
            layer=SupervisorLayer.CAP,
            verdict=SupervisorVerdict.CAP_AMOUNT,
            matched_rules=matched_rules,
            reasons=reasons,
            blocked_rules=list(matched_rules),
            original_amount_cents=original,
            capped_amount_cents=capped,
        )

    # ── DEFAULT: APPROVE (passed all layers)
    return SupervisorDecision(
        layer=SupervisorLayer.APPROVE,
        verdict=SupervisorVerdict.APPROVE,
        matched_rules=matched_rules,
        reasons=reasons or ["all hard rules passed"],
    )


# ─────────────────────────────────────────────────────────
#  Stripe-Radar-style trust score
# ─────────────────────────────────────────────────────────
# Factor weights — must sum to 1.0. Tuning these is the closest thing
# we have to "fraud model calibration"; changes should be reviewed.
TRUST_FACTOR_WEIGHTS = {
    TrustFactorName.IMAGE_UNIQUENESS:  0.25,
    TrustFactorName.AMOUNT_SANDBOX:    0.20,
    TrustFactorName.HISTORY_COHERENCE: 0.20,
    TrustFactorName.EMOTION_GATING:    0.15,
    TrustFactorName.EVIDENCE_QUALITY:  0.20,
}


def compute_trust_score(ctx: ClaimContext) -> tuple[int, list[TrustFactor]]:
    """Compute a 0-100 trust score + per-factor breakdown.

    Design:
      - Each factor 0-1, weighted by TRUST_FACTOR_WEIGHTS, sum * 100.
      - Any `fail` factor caps the total at 50 — UI signals "do not trust".
      - rule_id back-links a factor to the supervisor rule that drove it,
        so the audit log can show "Trust 32 because RULE-DUPLICATE-ORDER fired".
      - Pure read-only: never mutates ctx. Safe to call multiple times.
    """
    sup = ctx.supervisor_decision or {}
    matched = set(sup.get("matched_rules") or [])
    factors: list[TrustFactor] = []

    # ── F1: image_uniqueness (pHash fraud gate)
    if "RULE-FRAUD-REPLAY" in matched:
        factors.append(TrustFactor(
            name=TrustFactorName.IMAGE_UNIQUENESS,
            status="fail", score=0.0,
            weight=TRUST_FACTOR_WEIGHTS[TrustFactorName.IMAGE_UNIQUENESS],
            detail="pHash collision detected — cross-session image replay",
            rule_id="RULE-FRAUD-REPLAY",
        ))
    elif ctx.image_phash:
        try:
            import fraud as _fraud
            stats = _fraud.stats()
            factors.append(TrustFactor(
                name=TrustFactorName.IMAGE_UNIQUENESS,
                status="pass", score=1.0,
                weight=TRUST_FACTOR_WEIGHTS[TrustFactorName.IMAGE_UNIQUENESS],
                detail=f"no pHash collision in {stats.get('approved', 0)} approved anchors",
                rule_id=None,
            ))
        except Exception as e:
            logger.warning("trust F1 fraud stats failed: %s", e)
            factors.append(TrustFactor(
                name=TrustFactorName.IMAGE_UNIQUENESS,
                status="warn", score=0.5,
                weight=TRUST_FACTOR_WEIGHTS[TrustFactorName.IMAGE_UNIQUENESS],
                detail="fraud-gate check degraded",
            ))
    else:
        factors.append(TrustFactor(
            name=TrustFactorName.IMAGE_UNIQUENESS,
            status="warn", score=0.5,
            weight=TRUST_FACTOR_WEIGHTS[TrustFactorName.IMAGE_UNIQUENESS],
            detail="no image provided — uniqueness undetermined",
        ))

    # ── F2: amount_sandbox (Python clamp on LLM-emitted amount)
    if sup.get("verdict") == "cap_amount":
        original = sup.get("original_amount_cents") or 0
        capped = sup.get("capped_amount_cents") or 0
        ratio = (original - capped) / original if original > 0 else 0
        score = max(0.0, 1.0 - ratio)
        # Any cap is at least a `warn`; if LLM overshot >50% it's `fail`
        status = "fail" if ratio > 0.5 else "warn"
        rule_id = (sup.get("matched_rules") or [None])[0] if sup.get("matched_rules") else None
        factors.append(TrustFactor(
            name=TrustFactorName.AMOUNT_SANDBOX,
            status=status, score=score,
            weight=TRUST_FACTOR_WEIGHTS[TrustFactorName.AMOUNT_SANDBOX],
            detail=f"LLM proposed ${original/100:.2f}, sandbox capped to ${capped/100:.2f}",
            rule_id=rule_id,
        ))
    else:
        factors.append(TrustFactor(
            name=TrustFactorName.AMOUNT_SANDBOX,
            status="pass", score=1.0,
            weight=TRUST_FACTOR_WEIGHTS[TrustFactorName.AMOUNT_SANDBOX],
            detail="LLM amount within policy bounds, no clamp needed",
        ))

    # ── F3: history_coherence (duplicate-order check)
    if "RULE-DUPLICATE-ORDER" in matched:
        factors.append(TrustFactor(
            name=TrustFactorName.HISTORY_COHERENCE,
            status="fail", score=0.0,
            weight=TRUST_FACTOR_WEIGHTS[TrustFactorName.HISTORY_COHERENCE],
            detail="duplicate claim on already-resolved order",
            rule_id="RULE-DUPLICATE-ORDER",
        ))
    elif "RULE-SESSION-FREQ" in matched:
        factors.append(TrustFactor(
            name=TrustFactorName.HISTORY_COHERENCE,
            status="fail", score=0.0,
            weight=TRUST_FACTOR_WEIGHTS[TrustFactorName.HISTORY_COHERENCE],
            detail=f"session exceeded {LIFETIME_CLAIM_LIMIT_PER_SESSION}-claim limit",
            rule_id="RULE-SESSION-FREQ",
        ))
    else:
        factors.append(TrustFactor(
            name=TrustFactorName.HISTORY_COHERENCE,
            status="pass", score=1.0,
            weight=TRUST_FACTOR_WEIGHTS[TrustFactorName.HISTORY_COHERENCE],
            detail=f"no duplicate order ({len(ctx.history)} prior turns scanned)",
        ))

    # ── F4: emotion_gating (escalation signals + score)
    if ctx.emotion:
        if "RULE-LEGAL-THREAT" in matched:
            factors.append(TrustFactor(
                name=TrustFactorName.EMOTION_GATING,
                status="fail", score=0.0,
                weight=TRUST_FACTOR_WEIGHTS[TrustFactorName.EMOTION_GATING],
                detail="legal / regulator threat detected in emotion signals",
                rule_id="RULE-LEGAL-THREAT",
            ))
        elif ctx.emotion.escalation_signals:
            factors.append(TrustFactor(
                name=TrustFactorName.EMOTION_GATING,
                status="warn", score=0.3,
                weight=TRUST_FACTOR_WEIGHTS[TrustFactorName.EMOTION_GATING],
                detail=f"escalation signals present: {', '.join(ctx.emotion.escalation_signals[:2])}",
            ))
        elif ctx.emotion.score >= 8:
            factors.append(TrustFactor(
                name=TrustFactorName.EMOTION_GATING,
                status="warn", score=0.6,
                weight=TRUST_FACTOR_WEIGHTS[TrustFactorName.EMOTION_GATING],
                detail=f"high emotion score ({ctx.emotion.score:.1f}/10), within auto band",
            ))
        else:
            factors.append(TrustFactor(
                name=TrustFactorName.EMOTION_GATING,
                status="pass", score=1.0,
                weight=TRUST_FACTOR_WEIGHTS[TrustFactorName.EMOTION_GATING],
                detail=f"emotion {ctx.emotion.score:.1f}/10, no critical signals",
            ))
    else:
        factors.append(TrustFactor(
            name=TrustFactorName.EMOTION_GATING,
            status="warn", score=0.5,
            weight=TRUST_FACTOR_WEIGHTS[TrustFactorName.EMOTION_GATING],
            detail="emotion agent did not run",
        ))

    # ── F5: evidence_quality (DamageAgent confidence + multimodal mismatch)
    if "RULE-MULTIMODAL-MISMATCH" in matched:
        factors.append(TrustFactor(
            name=TrustFactorName.EVIDENCE_QUALITY,
            status="fail", score=0.0,
            weight=TRUST_FACTOR_WEIGHTS[TrustFactorName.EVIDENCE_QUALITY],
            detail="text/image subject mismatch — evidence incoherent",
            rule_id="RULE-MULTIMODAL-MISMATCH",
        ))
    elif ctx.damage:
        conf = ctx.damage.confidence
        if conf >= 0.8:
            status, score = "pass", 1.0
            detail = f"damage confidence {conf:.0%}, type={ctx.damage.damage_type.value}"
        elif conf >= 0.5:
            status, score = "warn", 0.7
            detail = f"damage confidence {conf:.0%} (moderate)"
        else:
            status, score = "fail", 0.3
            detail = f"damage confidence {conf:.0%} (too low to auto-decide)"
        factors.append(TrustFactor(
            name=TrustFactorName.EVIDENCE_QUALITY,
            status=status, score=score,
            weight=TRUST_FACTOR_WEIGHTS[TrustFactorName.EVIDENCE_QUALITY],
            detail=detail,
        ))
    else:
        factors.append(TrustFactor(
            name=TrustFactorName.EVIDENCE_QUALITY,
            status="warn", score=0.5,
            weight=TRUST_FACTOR_WEIGHTS[TrustFactorName.EVIDENCE_QUALITY],
            detail="no damage assessment (text-only)",
        ))

    # Weighted sum → 0-100
    weighted = sum(f.score * f.weight for f in factors)
    score = int(round(weighted * 100))

    # Any fail factor → cap at 50 (strong "don't trust" signal)
    if any(f.status == "fail" for f in factors):
        score = min(score, 50)

    return score, factors


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
