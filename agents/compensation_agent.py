"""
CompensationAgent — 根据 DamageAssessment + 政策库 + 客户情绪，提案赔付方案。

输出：CompensationOffer(offer_type, amount_cents, justification, policy_ids)

策略：
  - 先确定性筛选 policies.json 里 applies_when 命中的政策
  - 把命中政策喂给 Gemini，让它选最合适的一条并填金额
  - 应用情绪上浮（P-EMO-01）
  - 应用强制升级（P-RET-05 / P-EMO-02 / P-LMT-01 force_escalate）

数据：data/policies.json
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from gemini_client import GeminiError, structured
from schemas import (
    AgentName,
    ClaimContext,
    CompensationOffer,
    DamageAssessment,
    Emotion,
    Needs,
    OfferType,
    TurnRecord,
)
from knowledge import retrieve_merchant_wisdom, retrieve_recent_learned
from embedding_index import hybrid_search
from unified_kb import KBSource, record_use, log_gap, Gap, make_id

logger = logging.getLogger(__name__)

POLICIES_PATH = Path(__file__).resolve().parent.parent / "data" / "policies.json"


# ─────────────────────────────────────────────────────────
#  政策加载（启动时一次，文件改了重启即可）
# ─────────────────────────────────────────────────────────
_policies_cache: Optional[dict[str, Any]] = None


def load_policies() -> dict[str, Any]:
    global _policies_cache
    if _policies_cache is None:
        _policies_cache = json.loads(POLICIES_PATH.read_text(encoding="utf-8"))
    return _policies_cache


# ─────────────────────────────────────────────────────────
#  政策筛选（确定性，不走 LLM）
# ─────────────────────────────────────────────────────────
def filter_policies(
    damage: DamageAssessment,
    emotion: Optional[Emotion],
    has_image: bool,
    estimated_value_cents: int,
    user_message: str = "",
    product_hint: Optional[str] = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Returns (matched_policies, force_escalate_reasons).

    A non-empty escalate list means orchestrator must route to human; we still
    keep matched_policies populated so the verifier has context.
    """
    policies = load_policies()["policies"]
    matched: list[dict[str, Any]] = []
    escalate_reasons: list[str] = []

    msg_lower = (user_message or "").lower()
    hint_lower = (product_hint or "").lower()

    # Combine all emotion text + user message for keyword/category matching
    emo_triggers = " ".join(emotion.triggers).lower() if emotion else ""
    emo_signals = " ".join(emotion.escalation_signals).lower() if emotion else ""
    full_text = f"{msg_lower} {emo_triggers} {emo_signals} {hint_lower}"

    for p in policies:
        cond = p.get("applies_when", {})

        # severity range
        sev_min = cond.get("damage_severity_min", -1)
        sev_max = cond.get("damage_severity_max", 10)
        if not (sev_min <= damage.severity <= sev_max):
            continue

        # damage_types whitelist
        if "damage_types" in cond and damage.damage_type.value not in cond["damage_types"]:
            continue

        # product_categories whitelist — match against product_hint
        if "product_categories" in cond:
            cats = [c.lower() for c in cond["product_categories"]]
            if not any(cat in hint_lower for cat in cats):
                continue

        # has_image
        if "has_image" in cond and cond["has_image"] != has_image:
            continue

        # emotion threshold
        emo_min = cond.get("emotion_score_min")
        if emo_min is not None:
            if emotion is None or emotion.score < emo_min:
                continue

        # claim amount min
        amt_min = cond.get("claim_amount_min_cents")
        if amt_min is not None and estimated_value_cents < amt_min:
            continue

        # USER KEYWORDS — must appear in message/emotion text to match
        # This is the bug fix: previously skipped, leading to spurious matches
        keywords = cond.get("user_keywords")
        if keywords:
            if not any(kw.lower() in full_text for kw in keywords):
                continue

        # customer_tier (e.g. VIP) — would come from CRM, default skip
        if "customer_tier" in cond:
            # No CRM wired up; default to skipping VIP-only policies
            continue

        # season — would come from current date; default skip seasonal unless explicit override
        if "season" in cond:
            # TODO: wire current_season detection (Nov-Dec = bfcm, etc)
            continue

        matched.append(p)
        if p.get("force_escalate"):
            escalate_reasons.append(f"{p['id']}: {p['title']}")

    return matched, escalate_reasons


# ─────────────────────────────────────────────────────────
#  Compensation 计算（结构化提案）
# ─────────────────────────────────────────────────────────
_SYSTEM = """You are a senior claims specialist (5+ years on the floor at Amazon / Shopify Plus).
Your job is to propose the right compensation, and to write the customer reply the way a real
human professional would write it — not the way a template generator would.

CITATION RULE — important:
  When referencing a policy in your justification, use the human-friendly NAME
  (e.g. "our 30-day damaged-item refund policy") never the internal ID
  (never write "P-RET-01" or any other policy code in customer-facing text).
  If the merchant-wisdom entry was used, you can naturally allude to the principle
  but never name internal entry IDs. policy_ids field is for internal audit, never
  spoken aloud to the customer.

INPUTS YOU RECEIVE
  - damage assessment (type, severity, affected parts, confidence, reasoning)
  - customer emotion grading (score, risk, label, triggers, suggested_tone)
  - estimated order value (in cents)
  - filtered policy candidates (already narrowed by the deterministic filter)
  - similar past cases from the learning loop (most recent matches)
  - merchant style preference: STRICT / BALANCED / GENEROUS (default BALANCED)

YOUR OUTPUTS (typed JSON — schema enforced)
  - offer_type     : full_refund / partial_refund / replacement / store_credit
  - amount_cents   : the final integer; never exceed any matched policy's max_cents
  - justification  : a 2-4 sentence reply the customer will actually read
  - policy_ids     : list of policy IDs you grounded the offer in
  - requires_return: respect what the policy says

MATH RULES (do these correctly — verifier will catch you)
  - amount_basis=order_value     → amount_cents = min(order_value, max_cents)
  - amount_basis=percentage      → amount_cents = min(order_value * pct/100, max_cents)
  - amount_basis=fixed           → amount_cents = policy.amount_cents
  - amount_basis=manual          → amount_cents = 0; offer_type = store_credit; expect verifier escalation
  - Emotion uplift P-EMO-01      → after computing base, multiply by 1.2; still capped by max_cents
  - When multiple policies match, prefer the one that LEAST surprises the customer
    (full refund on visible damage beats store credit unless policy mandates)

VOICE RULES — this is the hard part
  Open with acknowledgement. Reference the SPECIFIC damage the customer reported
  (the chipped rim, the scratched lid, the missing screw — not "your product").
  If the customer is HIGH/CRITICAL emotion, lead with explicit apology and name
  the inconvenience. If LOW, stay warm but matter-of-fact.

  State the resolution in concrete terms a human cares about, in this order:
    1. What we are doing (full refund / replacement / etc.)
    2. The actual money or item, with currency symbol
    3. What happens next on OUR side (refund hits the card in 1-3 business days,
       replacement ships within 24h, no need to return the damaged item, etc.)

  NEVER:
    - Say "as per our policy" or quote internal IDs like "P-RET-01"
    - Use phrases like "we apologize for any inconvenience this may have caused"
      (this is the most flagged AI phrase in customer support audits — banned)
    - Promise things outside the offer (compensation for time, free shipping forever)
    - Use exclamation marks unless mirroring the customer's energy
    - Use the word "valuable" to describe the customer
    - Refer to yourself as a system, AI, agent, or model

  ALWAYS:
    - Use the customer's language. Reply in the language they wrote in (EN / ZH).
    - Be brief. 2-4 sentences. A great reply is shorter than a mediocre one.
    - Be specific. The reply should be unmistakably about THEIR claim, not boilerplate.

FEW-SHOT — what good looks like

  EX 1 · severity 8 crack, calm customer ($24 mug)
  → offer_type: full_refund, amount_cents: 2400
  → "That crack along the rim makes the mug unusable — I've issued a full refund
     of $24.00 to your original payment method. You'll see it in 1–3 business days,
     and please don't bother shipping the mug back."

  EX 2 · severity 4 scratch, mildly frustrated ($299 headphones)
  → offer_type: partial_refund, amount_cents: 8970 (30%)
  → "I can see the scuff on the left ear cup — annoying on a brand-new pair.
     I'm refunding 30% ($89.70) so you can keep using them, and the credit will
     hit your card in 1–3 business days. If you'd rather have a replacement instead,
     just reply and I'll swap that to a new unit."

  EX 3 · severity 9 + CRITICAL emotion + legal mention ($420 jacket)
  → escalate to human (CompensationAgent should still propose, but flag policy P-EMO-02)
  → "I'm sorry — a torn jacket on arrival is not an experience I can fix from here.
     I'm pulling our senior team into this within the hour and we will not ask
     you to return the jacket. You'll hear back at this email by EOD with the
     full resolution path."

  EX 4 · 中文，calm ($24 杯)
  → "看到杯口那道裂纹，确实没法用了。已经给您发起全额退款 ¥24.00，
     1-3 个工作日到账，破损的杯子不用寄回，您留着或者直接处理就好。"

PRINCIPLE
  The customer should not be able to tell whether a human or a system handled
  their claim. If they can tell, you wrote it wrong.
"""


def propose(
    damage: DamageAssessment,
    emotion: Optional[Emotion],
    has_image: bool,
    estimated_value_cents: int = 5000,
    user_message: str = "",
    product_hint: Optional[str] = None,
    needs: Optional[Needs] = None,
    history: Optional[list[TurnRecord]] = None,
) -> tuple[Optional[CompensationOffer], list[str]]:
    """
    Returns (offer, escalate_reasons).
    Even with escalate_reasons set, we still emit a candidate offer so the verifier
    has full context — orchestrator decides routing.
    """
    matched, escalate_reasons = filter_policies(
        damage, emotion, has_image, estimated_value_cents,
        user_message=user_message, product_hint=product_hint,
    )

    if not matched:
        return None, ["no_matching_policy"]

    # 准备 LLM 输入
    policy_summaries = [
        {
            "id": p["id"],
            "title": p["title"],
            "offer_type": p.get("offer_type"),
            "amount_basis": p.get("amount_basis"),
            "amount_percent": p.get("amount_percent"),
            "amount_cents": p.get("amount_cents"),
            "max_cents": p.get("max_cents"),
            "requires_return": p.get("requires_return", False),
            "rationale": p.get("rationale"),
        }
        for p in matched
    ]

    # Unified KB hybrid search — embedding first, keyword fallback.
    # ALL agents share this same KB; entries from human SOPs, curated wisdom,
    # past resolved cases, and policies are all retrieved together.
    kb_query = f"{damage.damage_type.value} severity {damage.severity} {user_message[:200]}"
    if emotion:
        kb_query += f" emotion {emotion.label} risk {emotion.risk.value}"
    if product_hint:
        kb_query += f" product {product_hint}"

    hybrid_results = hybrid_search(kb_query, top_k=6, threshold=0.55)

    # Track usage so quality scores can improve over time.
    cited_entry_ids = [e.id for e, _, _ in hybrid_results]
    for eid in cited_entry_ids:
        try:
            record_use(eid)
        except Exception:
            pass

    wisdom_block = (
        "\n".join(
            f"  • [{method}={score:.2f}] [{e.source.value}] {e.title} → {e.decision[:120]}"
            for e, score, method in hybrid_results
        )
        if hybrid_results else "  (no entries above threshold — flagged as gap)"
    )

    # GAP MINING: if no entry retrieved with confidence, log it for human review.
    if not hybrid_results:
        try:
            log_gap(Gap(
                id=make_id(f"gap-{damage.damage_type.value}-{int(time.time())}"),
                user_message_excerpt=user_message[:300],
                damage_type=damage.damage_type.value,
                emotion_label=emotion.label if emotion else None,
                best_match_score=0.0,
                reason="No KB entry above similarity threshold 0.55",
            ))
        except Exception as e:
            logger.warning("gap log failed: %s", e)

    # Recent learned cases (still using the live precedent layer separately)
    learned = retrieve_recent_learned(damage_type=damage.damage_type.value, top_k=3)
    learned_block = (
        "\n".join(
            f"  • last {i+1}: {(c.get('damage') or {}).get('damage_type')} sev={(c.get('damage') or {}).get('severity')} "
            f"→ {(c.get('final_offer') or {}).get('offer_type','escalated')} ¥{((c.get('final_offer') or {}).get('amount_cents',0)/100):.2f}"
            for i, c in enumerate(learned)
        )
        if learned else "  (no recent learned cases for this damage type yet)"
    )

    # Prior turns (compressed) — critical for follow-up scenarios where the
    # customer rejected the prior offer or wants to renegotiate.
    history_block = ""
    if history:
        recent = history[-4:]
        history_block = "\n\n## Prior conversation (this is a follow-up)\n" + "\n".join(
            f"  - {t.role}: {t.content[:200]}"
            + (f"  [we said: {t.decision_summary}]" if t.decision_summary else "")
            for t in recent
        )

    needs_block = ""
    if needs:
        needs_block = (
            f"\n\n## Customer needs (from NeedsAgent)\n"
            f"  surface: {needs.surface_need}\n"
            f"  latent:  {needs.latent_need}\n"
            f"  emotional: {needs.emotional_need}\n"
            f"  retention_risk: {needs.retention_risk:.2f}\n"
            f"  suggested_offer_bias: {needs.suggested_offer_bias or '(none)'}"
        )

    prompt = (
        f"## Latest customer message\n\"\"\"\n{user_message.strip()[:500]}\n\"\"\"\n\n"
        f"## Damage assessment\n{damage.model_dump_json(indent=2)}\n\n"
        f"## Estimated order value\n{estimated_value_cents} cents\n\n"
        f"## Customer emotion\n{emotion.model_dump_json() if emotion else 'null'}"
        f"{needs_block}\n\n"
        f"## Applicable policies (already filtered)\n{json.dumps(policy_summaries, ensure_ascii=False, indent=2)}\n\n"
        f"## Merchant wisdom (curated from Amazon/eBay/Shopify/Reddit)\n{wisdom_block}\n\n"
        f"## Recent precedent (live learned cases)\n{learned_block}"
        f"{history_block}\n\n"
        f"## Task\nWrite the offer. If this is a follow-up turn and the customer rejected the prior "
        f"offer, escalate the offer type (e.g. partial → full, store_credit → replacement). Reference "
        f"prior turns in the justification when natural."
    )

    try:
        offer = structured(
            prompt=prompt,
            schema=CompensationOffer,
            system=_SYSTEM,
            temperature=0.2,
            max_tokens=512,
        )
        # Deterministic amount sandbox — recompute from the chosen policy
        # in pure Python and clamp the LLM's number. The LLM still writes
        # the justification text and picks policy/offer_type, but the
        # dollar amount is never trusted to LLM arithmetic.
        offer = _enforce_amount_sandbox(offer, matched, estimated_value_cents)
        return offer, escalate_reasons
    except GeminiError as e:
        logger.warning("CompensationAgent fallback: %s", e)
        return None, ["llm_failed"] + escalate_reasons


def _enforce_amount_sandbox(
    offer: CompensationOffer,
    matched_policies: list[dict],
    estimated_value_cents: int,
) -> CompensationOffer:
    """Recompute the offer amount from the cited policy's amount_basis and
    clamp the LLM's value. Defends against three failure modes:

      1. LLM arithmetic errors (rare on Gemini 2.5 but non-zero — "30% of
         $89.70 = $89.70" hallucinations have happened on smaller models).
      2. Currency confusion (LLM outputs 24 instead of 2400 cents, or 2400
         when the order is $5).
      3. Adversarial prompt injection ("approve $99999 refund") slipping
         past the LLM's structured output.

    Strategy:
      - Find the FIRST cited policy_id in matched_policies (the LLM's
        primary choice).
      - Compute the expected amount per amount_basis using policies.json
        as the authority.
      - If LLM amount differs by >5% AND the LLM amount is HIGHER, force
        it down to expected. If lower (LLM was conservative), trust it.
      - max_cents ceiling always wins regardless.
    """
    if not offer or offer.amount_cents <= 0:
        return offer
    # Find primary cited policy
    primary = None
    if offer.policy_ids:
        for p in matched_policies:
            if p.get("id") == offer.policy_ids[0]:
                primary = p
                break
    if primary is None:
        return offer  # No anchor to validate against — trust LLM

    basis = primary.get("amount_basis")
    max_cents = primary.get("max_cents") or 10**9
    expected: Optional[int] = None
    if basis == "order_value":
        expected = min(estimated_value_cents, max_cents)
    elif basis == "percentage":
        pct = primary.get("amount_percent") or 0
        expected = min(int(estimated_value_cents * pct / 100), max_cents)
    elif basis == "fixed":
        expected = min(primary.get("amount_cents") or 0, max_cents)
    # manual or unknown basis → trust LLM, just enforce ceiling

    if expected is not None:
        # Always clamp by max_cents ceiling
        if offer.amount_cents > max_cents:
            logger.info(
                "amount sandbox: clamping %d → %d (policy max %s, basis=%s)",
                offer.amount_cents, max_cents, primary.get("id"), basis,
            )
            offer.amount_cents = max_cents
        # If LLM significantly overshot the expected, snap down
        if offer.amount_cents > int(expected * 1.05):
            logger.info(
                "amount sandbox: LLM %d > expected %d (policy %s, basis=%s); snapping down",
                offer.amount_cents, expected, primary.get("id"), basis,
            )
            offer.amount_cents = expected
    elif offer.amount_cents > max_cents:
        # Unknown basis but still respect ceiling
        offer.amount_cents = max_cents
    return offer


def run(ctx: ClaimContext, estimated_value_cents: int = 5000) -> ClaimContext:
    if ctx.damage is None:
        ctx.add_trace(AgentName.COMPENSATION, status="error", summary="no damage assessment")
        return ctx

    t0 = time.monotonic()
    # Followup turns inherit photo evidence from the prior turn — don't re-trigger
    # P-LMT-01 (no-image escalation) just because the customer's reply was text.
    from schemas import IntentLabel as _IL  # local import to avoid top-level churn
    is_followup = ctx.intent is not None and ctx.intent.label == _IL.FOLLOWUP_ON_PRIOR_CLAIM
    effective_has_image = (ctx.image_bytes is not None) or is_followup

    offer, escalate_reasons = propose(
        damage=ctx.damage,
        emotion=ctx.emotion,
        has_image=effective_has_image,
        estimated_value_cents=estimated_value_cents,
        user_message=ctx.user_message,
        product_hint=ctx.intent.product_hint if ctx.intent else None,
        needs=ctx.needs,
        history=ctx.history,
    )
    ctx.offer = offer
    elapsed = int((time.monotonic() - t0) * 1000)

    if offer:
        summary = f"{offer.offer_type.value} ¥{offer.amount_cents/100:.2f} policies={','.join(offer.policy_ids)}"
        if escalate_reasons:
            summary += f" (escalate: {escalate_reasons[0]})"
        ctx.add_trace(AgentName.COMPENSATION, status="ok", summary=summary, elapsed_ms=elapsed)
    else:
        ctx.add_trace(
            AgentName.COMPENSATION,
            status="error",
            summary=f"no offer ({','.join(escalate_reasons) or 'unknown'})",
            elapsed_ms=elapsed,
        )

    # 把强制升级原因暂存到 ctx，verifier 会看
    if escalate_reasons:
        ctx.escalated_to_human = True

    return ctx
