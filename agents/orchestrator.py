"""
Orchestrator — 把 4 个 agent 按顺序拼成 ClaimsForge 主流水线。

流程：
  IntentAgent → (if claim) DamageAgent → CompensationAgent → VerifierAgent
                                          ↑                      ↓
                                          └──── revise (max 1x)──┘

不引入任何编排框架（LangChain / LangGraph 都太重），就一个函数。

调用方拿到 ClaimContext 后：
  - ctx.intent / damage / offer / verification 各 agent 的中间产物
  - ctx.final_offer 是最终方案（None 代表升级人工）
  - ctx.final_reply 是发给用户的文字
  - ctx.traces 是给前端流式展示的事件
  - ctx.escalated_to_human 是否最终升级

支持 on_trace 回调，让 WebSocket 一边跑一边推送。
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from schemas import (
    AgentName,
    AgentTrace,
    ClaimContext,
    Emotion,
    IntentLabel,
    Needs,
    VerifierVerdict,
)

import intent_agent
import emotion_agent
import damage_agent
import compensation_agent
import verifier_agent
from knowledge import append_learned_case

logger = logging.getLogger(__name__)


TraceCallback = Callable[[AgentTrace], None]


# ─────────────────────────────────────────────────────────
#  最终文字回复（不走 LLM，模板拼装）
# ─────────────────────────────────────────────────────────
def _format_final_reply(ctx: ClaimContext) -> str:
    """根据流水线结果生成发给客户的文字。"""
    if ctx.escalated_to_human:
        return (
            "您的情况我们非常重视，已经为您升级到人工客服。"
            "专员会在 30 分钟内联系您，给您一个满意的处理方案。"
        )

    if ctx.final_offer is None:
        return "抱歉，暂时无法自动处理您的诉求。我们已为您转人工客服，稍后联系您。"

    offer = ctx.final_offer
    type_label = {
        "full_refund": "全额退款",
        "partial_refund": "部分退款",
        "replacement": "免费换新",
        "store_credit": "店铺积分",
    }.get(offer.offer_type.value, "赔付")

    parts = [
        f"已为您办理：{type_label} ¥{offer.amount_cents/100:.2f}",
        offer.justification,
    ]
    if offer.requires_return:
        parts.append("请准备好商品，物流上门取件信息将另行发送。")
    else:
        parts.append("商品无需寄回。")
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────
#  主流水线
# ─────────────────────────────────────────────────────────
def run(
    ctx: ClaimContext,
    on_trace: Optional[TraceCallback] = None,
    estimated_value_cents: int = 5000,
) -> ClaimContext:
    """跑完整 pipeline。on_trace 回调每次 agent 完成后触发，可用于 WebSocket 流式。"""
    pipeline_start = time.monotonic()

    def emit(trace: AgentTrace) -> None:
        if on_trace:
            try:
                on_trace(trace)
            except Exception as e:
                logger.warning("on_trace callback raised: %s", e)

    # Stage 1: Intent
    intent_agent.run(ctx)
    emit(ctx.traces[-1])

    if ctx.intent is None or ctx.intent.label == IntentLabel.GENERAL_INQUIRY:
        ctx.final_reply = (
            "Hi — happy to help. Are you reporting a damaged or defective item, "
            "or do you have a general question about our return policy?"
        )
        return ctx

    # Stage 2: Emotion — grade the customer's affect on this turn.
    # Done early so DamageAgent + CompensationAgent can adapt.
    emotion_agent.run(ctx)
    emit(ctx.traces[-1])

    # Stage 3: Damage（即使无图也跑，只是 confidence 会低）
    damage_agent.run(ctx)
    emit(ctx.traces[-1])

    # 损坏证据极弱直接升级
    if ctx.damage and ctx.damage.confidence < 0.2:
        ctx.escalated_to_human = True
        ctx.final_reply = _format_final_reply(ctx)
        elapsed = int((time.monotonic() - pipeline_start) * 1000)
        logger.info("pipeline done (escalated, low confidence) in %dms", elapsed)
        return ctx

    # Stage 3: Compensation
    compensation_agent.run(ctx, estimated_value_cents=estimated_value_cents)
    emit(ctx.traces[-1])

    if ctx.offer is None:
        # compensation 无法出方案 → 升级
        ctx.escalated_to_human = True
        ctx.final_reply = _format_final_reply(ctx)
        return ctx

    # Stage 4: Verifier（含最多 1 次修订回路）
    verifier_agent.run(ctx)
    emit(ctx.traces[-1])

    # 如果 verifier revise 了，把修订版当成新方案再 verify 一次（最多 1 轮）
    if (
        ctx.verification
        and ctx.verification.verdict == VerifierVerdict.REVISE
        and ctx.verification.revised_offer
    ):
        # 把 revised 当成新 offer，重新 verify（不再 revise，只能 approve/escalate）
        ctx.offer = ctx.verification.revised_offer
        verifier_agent.run(ctx)
        emit(ctx.traces[-1])
        if ctx.verification.verdict == VerifierVerdict.REVISE:
            # 二次还要 revise → 直接接受第二轮提案，不再循环
            ctx.final_offer = ctx.offer

    # 最终文字
    ctx.final_reply = _format_final_reply(ctx)

    elapsed = int((time.monotonic() - pipeline_start) * 1000)
    logger.info("pipeline done in %dms (escalated=%s)", elapsed, ctx.escalated_to_human)

    # Learning loop: persist this resolved claim so future CompensationAgent calls
    # can retrieve it as recent precedent. Skip general_inquiry (no value).
    try:
        append_learned_case({
            "session_id": ctx.session_id,
            "user_message_preview": ctx.user_message[:200],
            "intent": ctx.intent.model_dump() if ctx.intent else None,
            "emotion": ctx.emotion.model_dump() if ctx.emotion else None,
            "damage": ctx.damage.model_dump() if ctx.damage else None,
            "final_offer": ctx.final_offer.model_dump() if ctx.final_offer else None,
            "verification": ctx.verification.model_dump() if ctx.verification else None,
            "escalated": ctx.escalated_to_human,
            "pipeline_ms": elapsed,
        })
    except Exception as e:
        logger.warning("learning loop write failed (non-fatal): %s", e)

    return ctx


# ─────────────────────────────────────────────────────────
#  从 conversation_engine 的输出适配 ClaimContext
# ─────────────────────────────────────────────────────────
def adapt_legacy_emotion(emotion_dict: dict) -> Emotion:
    return Emotion(
        score=emotion_dict.get("score", 5.0),
        risk=emotion_dict.get("risk", "MEDIUM"),
        label=emotion_dict.get("label", "中性"),
    )


def adapt_legacy_needs(needs_dict: dict) -> Needs:
    return Needs(
        surface_need=needs_dict.get("surface_need", ""),
        latent_need=needs_dict.get("latent_need", ""),
        emotional_need=needs_dict.get("emotional_need", ""),
        retention_score=needs_dict.get("retention_score", 0.0),
        suggested_tone=needs_dict.get("suggested_tone", ""),
    )
