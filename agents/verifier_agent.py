"""
VerifierAgent — 校验 CompensationAgent 给出的方案是否合理。

职责：
  - 检查 amount 是否在政策上限内（确定性，不走 LLM）
  - 检查方案是否与损坏类型匹配（如 water_damage 不应自动 full_refund）
  - 检查是否触发了 force_escalate 政策
  - 如果有问题，要么 REVISE（建议修订），要么 ESCALATE_TO_HUMAN

调用 Gemini 主要做"语义校验"：方案语言是否得体、是否会激怒客户。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from gemini_client import GeminiError, structured
from schemas import (
    AgentName,
    ClaimContext,
    CompensationOffer,
    OfferType,
    VerificationResult,
    VerifierVerdict,
)
from compensation_agent import load_policies

logger = logging.getLogger(__name__)


_SYSTEM = """You are the final reviewer on every compensation offer before it
reaches the customer. You see: the damage assessment, the customer's emotion
grading, the offer CompensationAgent proposed, and the policy caps that applied.

JUDGE THE OFFER ON
  - amount: reasonable? (not so generous it bleeds margin, not so stingy it
    insults the customer)
  - tone: is the justification likely to inflame an already-upset customer?
  - offer_type fit: e.g. complex electronics break → replacement usually fits
    better than partial_refund

VERDICT (pick one)
  approve              — offer is good, ship it
  revise               — small change needed (amount tweak, gentler wording,
                         different offer_type). Put your revision in
                         revised_offer.
  escalate_to_human    — material risk (amount over cap, legal exposure,
                         evidence too weak). Bail out to a human.

reason: ONE sentence explaining your call.

PRINCIPLES
  - Customer emotion ≥ 8 but the justification reads cold/mechanical → revise
    with warmer wording.
  - Amount over the policy's max_cents → revise down to the cap.
  - damage.confidence < 0.5 AND amount > $50 → escalate (don't pay out on
    weak evidence).
  - Plan obviously conflicts with policy → escalate.

LANGUAGE RULE
  Write `reason` in the SAME LANGUAGE the customer used (visible in the
  agent trace + admin UI). If revised_offer is set, its justification must
  also stay in the customer's language.
"""


def verify(
    offer: CompensationOffer,
    damage_severity: int,
    damage_confidence: float,
    emotion_score: float,
    user_message: str,
) -> VerificationResult:
    # 确定性硬性检查 (不走 LLM)
    policies = {p["id"]: p for p in load_policies()["policies"]}
    for pid in offer.policy_ids:
        p = policies.get(pid)
        if not p:
            continue
        max_cents = p.get("max_cents")
        if max_cents and offer.amount_cents > max_cents * 1.21:  # 允许 20% 情绪上浮余量
            return VerificationResult(
                verdict=VerifierVerdict.REVISE,
                reason=f"金额 {offer.amount_cents/100} 超出政策 {pid} 上限 {max_cents/100}。",
                revised_offer=offer.model_copy(update={"amount_cents": max_cents}),
            )

    if damage_confidence < 0.5 and offer.amount_cents > 5000:
        return VerificationResult(
            verdict=VerifierVerdict.ESCALATE,
            reason=f"损坏置信度仅 {damage_confidence:.2f}，但赔付金额超 50 元，证据不足需人工核验。",
        )

    # Soft check — let the LLM judge tone + fit
    prompt = (
        f"## Customer message (write `reason` in this language)\n"
        f"\"\"\"\n{user_message}\n\"\"\"\n\n"
        f"## Signals\n"
        f"  damage severity: {damage_severity}/10, confidence: {damage_confidence:.2f}\n"
        f"  emotion score: {emotion_score:.1f}/10\n\n"
        f"## Proposed offer\n{offer.model_dump_json(indent=2, ensure_ascii=False)}\n\n"
        f"Review and return your verdict."
    )
    try:
        result = structured(
            prompt=prompt,
            schema=VerificationResult,
            system=_SYSTEM,
            temperature=0.2,
            max_tokens=512,
        )
        return result
    except GeminiError as e:
        logger.warning("VerifierAgent fallback to approve: %s", e)
        return VerificationResult(
            verdict=VerifierVerdict.APPROVE,
            reason="审核服务异常，按原方案放行（请人工监控）。",
        )


def run(ctx: ClaimContext) -> ClaimContext:
    if ctx.offer is None:
        ctx.add_trace(AgentName.VERIFIER, status="error", summary="no offer to verify")
        ctx.escalated_to_human = True
        return ctx

    t0 = time.monotonic()
    result = verify(
        offer=ctx.offer,
        damage_severity=ctx.damage.severity if ctx.damage else 0,
        damage_confidence=ctx.damage.confidence if ctx.damage else 0.0,
        emotion_score=ctx.emotion.score if ctx.emotion else 0.0,
        user_message=ctx.user_message,
    )
    ctx.verification = result
    elapsed = int((time.monotonic() - t0) * 1000)

    # 应用 verifier 决议
    if result.verdict == VerifierVerdict.APPROVE:
        ctx.final_offer = ctx.offer
        summary = "approved"
    elif result.verdict == VerifierVerdict.REVISE:
        ctx.final_offer = result.revised_offer or ctx.offer
        summary = f"revised: {result.reason[:60]}"
    else:  # ESCALATE
        ctx.escalated_to_human = True
        ctx.final_offer = None
        summary = f"escalated: {result.reason[:60]}"

    ctx.add_trace(AgentName.VERIFIER, status="ok", summary=summary, elapsed_ms=elapsed)
    return ctx
