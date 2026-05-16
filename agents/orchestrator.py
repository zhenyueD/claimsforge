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

import asyncio
import logging
import threading
import time
from typing import Callable, Optional

from schemas import (
    AgentName,
    AgentTrace,
    ClaimContext,
    DamageAssessment,
    DamageType,
    Emotion,
    IntentLabel,
    Needs,
    VerifierVerdict,
)


import intent_agent
import emotion_agent
import needs_agent
import damage_agent
import compensation_agent
import supervisor
import verifier_agent
import handoff
from knowledge import append_learned_case

logger = logging.getLogger(__name__)


# Methodology synthesis: serialize trigger attempts to prevent N concurrent
# pipeline threads from each spawning their own synthesis on the same total.
# A 15-minute cooldown also prevents re-synthesizing the same case clusters
# back-to-back (synthesis is expensive — embeds + Gemini calls per cluster).
_SYNTHESIS_LOCK = threading.Lock()
_SYNTHESIS_COOLDOWN_SEC = 15 * 60
_last_synthesis_ts: float = 0.0


def _try_trigger_synthesis(total: int) -> None:
    """Fire a background synthesis if (a) total hit a 5-multiple, (b) no
    other synthesis is in-flight, and (c) the last one finished >15min ago.

    Returns immediately; the actual synthesis runs in a daemon thread so
    the pipeline reply isn't blocked. All failure paths are non-fatal.
    """
    global _last_synthesis_ts
    if total <= 0 or total % 5 != 0:
        return
    # Cheap non-blocking acquire — if another thread is already triggering or
    # running synthesis, just bail.
    if not _SYNTHESIS_LOCK.acquire(blocking=False):
        return
    try:
        now = time.monotonic()
        if now - _last_synthesis_ts < _SYNTHESIS_COOLDOWN_SEC:
            return
        _last_synthesis_ts = now
        from case_synthesizer import run_synthesis as _synthesize

        def _runner() -> None:
            try:
                _synthesize(min_cluster_size=3, rebuild_existing=False)
            except Exception as e:
                logger.warning("background synthesis failed: %s", e)

        threading.Thread(target=_runner, daemon=True).start()
        logger.info("triggered background methodology synthesis at total=%d", total)
    finally:
        _SYNTHESIS_LOCK.release()


def _placeholder_damage_for_followup(ctx: ClaimContext) -> DamageAssessment:
    """Followup turns rarely re-attach the image. Skip vision and synthesize a
    high-confidence carry-over damage so compensation_agent can renegotiate
    from the prior turn's evidence (which is already in history)."""
    return DamageAssessment(
        damage_type=DamageType.DEFECT,
        severity=5,
        affected_parts=[],
        confidence=0.85,
        reasoning="Follow-up turn — damage evidence carried over from prior conversation.",
        evidence_quote=None,
    )


def _maybe_enqueue_handoff(ctx: ClaimContext) -> None:
    """Build + enqueue a HandoffSummary if this turn escalated.

    Failures are non-fatal — the customer reply still goes out; only the
    human queue is degraded.
    """
    if not ctx.escalated_to_human:
        return
    try:
        summary = handoff.build_summary(ctx)
        handoff.enqueue(summary)
        ctx.handoff_summary = summary.model_dump()
        logger.info(
            "handoff enqueued: %s priority=%s reason=%s",
            summary.handoff_id, summary.priority.value, summary.escalation_reason,
        )
    except Exception as e:
        logger.warning("handoff enqueue failed (non-fatal): %s", e)


def _finalize_reply(ctx: ClaimContext) -> None:
    """Set ctx.final_reply AND enqueue a handoff if escalated.

    Replaces direct ctx.final_reply assignments so every exit path of the
    pipeline gets the queue side-effect for free.
    """
    ctx.final_reply = _format_final_reply(ctx)
    _maybe_enqueue_handoff(ctx)


TraceCallback = Callable[[AgentTrace], None]


# ─────────────────────────────────────────────────────────
#  最终文字回复 — 让 LLM 写好的 justification 直接面客
# ─────────────────────────────────────────────────────────
def _detect_language(text: str) -> str:
    """Return 'zh' if the text contains CJK characters, else 'en'.

    This is the cheapest reliable language signal — we only need it for
    fallback templates (when there's no LLM-written justification to use).
    """
    return "zh" if any("一" <= c <= "鿿" for c in text or "") else "en"


def _format_final_reply(ctx: ClaimContext) -> str:
    """Bilingual final reply that respects what CompensationAgent wrote.

    The previous implementation prepended a Chinese template ("已为您办理：...")
    that overrode CompensationAgent's carefully tuned bilingual justification
    (banned phrases, tone calibration, concrete next steps with timeline).
    English customers ended up reading mixed Chinese+English replies.

    New behavior:
      - Escalated / no-offer fallbacks use a SHORT template in the customer's
        own language (detected from user_message).
      - Clarification short-circuit returns the LLM's clarification_question
        verbatim (already in customer's language).
      - Normal path returns ctx.final_offer.justification directly — the LLM
        has already woven in the dollar amount, next steps, and return/no-return
        logic per its system prompt.

    Why this matters:
      - Honors language adaptation written into compensation_agent._SYSTEM
      - Removes "已为您办理 / 商品无需寄回" boilerplate that contradicts the
        "Be brief, 2-4 sentences" voice rule
      - Eliminates the "Chinese header + English body" UX bug
    """
    is_zh = _detect_language(ctx.user_message) == "zh"

    # Escalation — short template, in the customer's language
    if ctx.escalated_to_human:
        if is_zh:
            return (
                "您的情况我们已升级到人工专员，30 分钟内会通过原渠道联系您。"
                "感谢您的耐心。"
            )
        return (
            "I've routed this to a senior specialist who will reach back to you "
            "through this same channel within 30 minutes. Thanks for bearing with me."
        )

    # Clarification — use the LLM's question verbatim (already localized)
    if ctx.awaiting_clarification and ctx.clarification_question:
        return ctx.clarification_question

    # No-offer fallback
    if ctx.final_offer is None:
        if is_zh:
            return "抱歉，暂时无法自动处理。我们已转人工客服，稍后联系您。"
        return "I couldn't auto-resolve this. A human teammate will reach out shortly."

    # Normal path — trust the LLM's justification. It already includes:
    #   - the offer type + amount in human language
    #   - the concrete next step (refund timing, return/no-return)
    #   - tone calibrated to the customer's emotion
    #   - the language the customer wrote in
    return ctx.final_offer.justification


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

    if ctx.intent is None:
        ctx.final_reply = (
            "Sorry — we had trouble understanding that. Could you describe what went wrong with the order?"
        )
        return ctx

    if ctx.intent.label == IntentLabel.GENERAL_INQUIRY:
        ctx.final_reply = (
            "Hi — happy to help. Are you reporting a damaged or defective item, "
            "or do you have a general question about our return policy?"
        )
        return ctx

    if ctx.intent.label == IntentLabel.NEEDS_CLARIFICATION and ctx.intent.clarification_question:
        # Short-circuit: send the follow-up question and wait for the next turn.
        ctx.awaiting_clarification = True
        ctx.clarification_question = ctx.intent.clarification_question
        ctx.final_reply = ctx.intent.clarification_question
        elapsed = int((time.monotonic() - pipeline_start) * 1000)
        logger.info("pipeline short-circuit (clarification) in %dms", elapsed)
        return ctx

    # Stage 2: Emotion — grade the customer's affect on this turn.
    emotion_agent.run(ctx)
    emit(ctx.traces[-1])

    # Stage 3: Needs — surface latent needs + retention risk + offer bias
    needs_agent.run(ctx)
    emit(ctx.traces[-1])

    # Stage 4: Damage (skip Vision on followup-without-image; carry damage over)
    is_followup_no_image = (
        ctx.intent.label == IntentLabel.FOLLOWUP_ON_PRIOR_CLAIM and ctx.image_bytes is None
    )
    if is_followup_no_image:
        ctx.damage = _placeholder_damage_for_followup(ctx)
        ctx.add_trace(AgentName.DAMAGE, status="ok",
                      summary="followup carry-over (skipped vision)", elapsed_ms=0)
        emit(ctx.traces[-1])
    else:
        damage_agent.run(ctx)
        emit(ctx.traces[-1])

        # 损坏证据极弱直接升级 (followup carry-over has conf=0.85 so won't trigger)
        if ctx.damage and ctx.damage.confidence < 0.2:
            ctx.escalated_to_human = True
            _finalize_reply(ctx)
            elapsed = int((time.monotonic() - pipeline_start) * 1000)
            logger.info("pipeline done (escalated, low confidence) in %dms", elapsed)
            return ctx

    # Stage 5: Compensation (sees damage + emotion + needs + history)
    compensation_agent.run(ctx, estimated_value_cents=estimated_value_cents)
    emit(ctx.traces[-1])

    # Stage 5.5: Supervisor (hard pure-Python rules — Sierra-style safety gate)
    # Can:
    #   - FORCE_ESCALATE on water-damaged electronics / legal threats / luxury
    #   - UN_ESCALATE perishables wrongly held back by P-LMT-01 (no-image rule)
    #   - CAP_AMOUNT when offer exceeds MAX_CASH_PAYMENT_CENTS or 100% of order
    # Runs BEFORE Verifier so the LLM-driven verifier can only soft-revise the
    # already-bounded offer, not exceed the hard caps.
    supervisor.run(ctx, estimated_value_cents=estimated_value_cents)
    emit(ctx.traces[-1])

    if ctx.offer is None or ctx.escalated_to_human:
        # Supervisor or earlier stages decided this needs a human; skip Verifier.
        _finalize_reply(ctx)
        return ctx

    # Stage 6: Verifier（含最多 1 次修订回路）
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
            # 二次仍 REVISE：不再接受未经验证的方案，强制升级人工。
            # 旧实现 ctx.final_offer = ctx.offer 会直接放行一个 verifier 自己都
            # 说"还需修订"的 offer — 这是潜在金额风险（policy max_cents 越界、
            # tone 不达标、措辞会激怒客户 都已被 verifier 标记，不能放行）。
            logger.warning(
                "verifier requested 2nd revise — escalating instead of auto-accepting (session=%s)",
                ctx.session_id,
            )
            ctx.escalated_to_human = True
            ctx.final_offer = None

    # 最终文字
    _finalize_reply(ctx)

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
            "supervisor": ctx.supervisor_decision,
            "escalated": ctx.escalated_to_human,
            "pipeline_ms": elapsed,
        })
    except Exception as e:
        logger.warning("learning loop write failed (non-fatal): %s", e)

    # Methodology synthesis trigger — debounced by _try_trigger_synthesis
    # (lock + 15-min cooldown so concurrent pipeline threads don't double-fire).
    try:
        from knowledge import get_learning_stats as _stats
        _try_trigger_synthesis(_stats().get("total", 0))
    except Exception as e:
        logger.warning("synthesis trigger failed (non-fatal): %s", e)

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


# ═══════════════════════════════════════════════════════════════════
#  ASYNC PIPELINE — Emotion / Needs / Damage 三 agent 并行
# ═══════════════════════════════════════════════════════════════════
#
# Why this exists:
#   Original sync run() executes Intent → Emotion → Needs → Damage →
#   Compensation → Verifier strictly sequentially. But Emotion, Needs, and
#   Damage only depend on (user_message, image_bytes) — they don't read each
#   other's output. Running them concurrently cuts p50 latency ~40-50%
#   (measured: 3.9s → ~2.1s on demo scenarios).
#
# Why we kept run() too:
#   /api/training/* and any caller that wants synchronous behavior still
#   works. Migrate to run_async() only where awaitable context exists
#   (FastAPI handlers are async by default).
#
# Concurrency safety:
#   - ctx.add_trace appends to a Python list. CPython list.append is atomic
#     (GIL guards it), but the order of appends across the 3 threads is
#     non-deterministic. We capture pre-async len(traces) and emit() everything
#     added after, so on_trace receives traces in whatever real order they
#     completed — which is what the UI wants for a live timeline.
#   - asyncio.to_thread runs each agent in the default thread pool. Gemini
#     SDK calls are HTTP — concurrency just means three sockets open at once.

async def run_async(
    ctx: ClaimContext,
    on_trace: Optional[TraceCallback] = None,
    estimated_value_cents: int = 5000,
) -> ClaimContext:
    """Async pipeline. Runs Emotion / Needs / Damage in parallel after Intent.

    Stages:
      1. Intent              (serial — downstream routing depends on its label)
      2. Emotion ‖ Needs ‖ Damage   (parallel — all depend only on input)
      3. Compensation        (serial — needs all three above)
      4. Verifier            (serial — at most 1 revise loop, see Patch 2)

    Same fallback semantics as run(): Gemini failures degrade to safe defaults
    inside each agent; orchestrator escalates the whole claim if confidence
    or verification fails.
    """
    pipeline_start = time.monotonic()

    def emit(trace: AgentTrace) -> None:
        if on_trace:
            try:
                on_trace(trace)
            except Exception as e:
                logger.warning("on_trace callback raised: %s", e)

    # ─── Stage 1: Intent (serial)
    await asyncio.to_thread(intent_agent.run, ctx)
    emit(ctx.traces[-1])

    if ctx.intent is None:
        ctx.final_reply = (
            "Sorry — we had trouble understanding that. Could you describe what went wrong with the order?"
        )
        return ctx

    if ctx.intent.label == IntentLabel.GENERAL_INQUIRY:
        ctx.final_reply = (
            "Hi — happy to help. Are you reporting a damaged or defective item, "
            "or do you have a general question about our return policy?"
        )
        return ctx

    if ctx.intent.label == IntentLabel.NEEDS_CLARIFICATION and ctx.intent.clarification_question:
        ctx.awaiting_clarification = True
        ctx.clarification_question = ctx.intent.clarification_question
        ctx.final_reply = ctx.intent.clarification_question
        logger.info("pipeline_async short-circuit (clarification) for %s", ctx.session_id)
        return ctx

    # ─── Stage 2-4: Emotion ‖ Needs ‖ Damage (parallel)
    # Followup-without-image carries damage over from prior turn — no vision call.
    is_followup_no_image = (
        ctx.intent.label == IntentLabel.FOLLOWUP_ON_PRIOR_CLAIM and ctx.image_bytes is None
    )
    pre_len = len(ctx.traces)
    parallel_tasks = [
        asyncio.to_thread(emotion_agent.run, ctx),
        asyncio.to_thread(needs_agent.run, ctx),
    ]
    if is_followup_no_image:
        ctx.damage = _placeholder_damage_for_followup(ctx)
        ctx.add_trace(AgentName.DAMAGE, status="ok",
                      summary="followup carry-over (skipped vision)", elapsed_ms=0)
    else:
        parallel_tasks.append(asyncio.to_thread(damage_agent.run, ctx))
    await asyncio.gather(*parallel_tasks)
    # Emit traces in real completion order
    for tr in ctx.traces[pre_len:]:
        emit(tr)

    # Low-confidence short-circuit (followup carry-over has conf=0.85)
    if ctx.damage and ctx.damage.confidence < 0.2:
        ctx.escalated_to_human = True
        _finalize_reply(ctx)
        elapsed = int((time.monotonic() - pipeline_start) * 1000)
        logger.info("pipeline_async escalate-on-low-conf in %dms (%s)", elapsed, ctx.session_id)
        return ctx

    # ─── Stage 5: Compensation
    await asyncio.to_thread(compensation_agent.run, ctx, estimated_value_cents=estimated_value_cents)
    emit(ctx.traces[-1])

    # ─── Stage 5.5: Supervisor (pure-Python hard gate — see supervisor.py docstring)
    await asyncio.to_thread(supervisor.run, ctx, estimated_value_cents=estimated_value_cents)
    emit(ctx.traces[-1])

    if ctx.offer is None or ctx.escalated_to_human:
        _finalize_reply(ctx)
        return ctx

    # ─── Stage 6: Verifier (with at most 1 revise loop — see Patch 2 for 2nd-revise → ESCALATE)
    await asyncio.to_thread(verifier_agent.run, ctx)
    emit(ctx.traces[-1])

    if (
        ctx.verification
        and ctx.verification.verdict == VerifierVerdict.REVISE
        and ctx.verification.revised_offer
    ):
        ctx.offer = ctx.verification.revised_offer
        await asyncio.to_thread(verifier_agent.run, ctx)
        emit(ctx.traces[-1])
        if ctx.verification.verdict == VerifierVerdict.REVISE:
            # Patch 2 semantics: 2nd revise → ESCALATE, never accept unverified offer
            logger.warning(
                "verifier requested 2nd revise — escalating (session=%s)", ctx.session_id
            )
            ctx.escalated_to_human = True
            ctx.final_offer = None

    _finalize_reply(ctx)

    elapsed = int((time.monotonic() - pipeline_start) * 1000)
    logger.info("pipeline_async done in %dms (escalated=%s, session=%s)",
                elapsed, ctx.escalated_to_human, ctx.session_id)

    # Learning loop — same as run()
    try:
        append_learned_case({
            "session_id": ctx.session_id,
            "user_message_preview": ctx.user_message[:200],
            "intent": ctx.intent.model_dump() if ctx.intent else None,
            "emotion": ctx.emotion.model_dump() if ctx.emotion else None,
            "needs": ctx.needs.model_dump() if ctx.needs else None,
            "damage": ctx.damage.model_dump() if ctx.damage else None,
            "final_offer": ctx.final_offer.model_dump() if ctx.final_offer else None,
            "verification": ctx.verification.model_dump() if ctx.verification else None,
            "supervisor": ctx.supervisor_decision,
            "escalated": ctx.escalated_to_human,
            "pipeline_ms": elapsed,
        })
    except Exception as e:
        logger.warning("learning loop write failed (non-fatal): %s", e)

    # Methodology synthesis trigger — debounced (same as sync run())
    try:
        from knowledge import get_learning_stats as _stats
        _try_trigger_synthesis(_stats().get("total", 0))
    except Exception as e:
        logger.warning("synthesis trigger failed (non-fatal): %s", e)

    return ctx
