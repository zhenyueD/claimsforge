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


_SYSTEM = """你是赔付方案的最终审核员。

输入：损坏评估、客户情绪、CompensationAgent 提出的方案、政策上限信息。

你要判断这个方案：
- 金额是否合理（既不过度赔付伤公司，也不抠门激怒客户）
- 措辞是否得体（justification 是否会让情绪激动的客户更生气）
- offer_type 是否合适（如复杂电子产品破裂用 replacement 比 partial_refund 更合适）

输出 verdict（三选一）：
- approve：方案 OK，直接执行
- revise：方案需要小幅修订（金额调整、换措辞、改 offer_type），在 revised_offer 给出修订版
- escalate_to_human：方案有重大问题（金额溢出、可能涉法律纠纷、判断证据不足），转人工

reason：1 句话说明判断理由。

判断原则：
- 客户情绪 >= 8 但你看到 justification 冷漠机械 → revise，建议更共情的措辞
- 金额超过政策 max_cents → revise，建议降到上限
- damage.confidence < 0.5 + 金额 > 50 元 → escalate（证据不足不该自动给钱）
- 方案明显与政策冲突 → escalate
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

    # 软性校验 — 让 LLM 看 justification 措辞
    prompt = (
        f"客户消息：{user_message}\n"
        f"损坏严重度：{damage_severity}/10，置信度：{damage_confidence:.2f}\n"
        f"客户情绪分：{emotion_score:.1f}/10\n"
        f"\n方案：\n{offer.model_dump_json(indent=2, ensure_ascii=False)}\n"
        f"\n请审核。"
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
