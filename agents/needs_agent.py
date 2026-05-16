"""
NeedsAgent — Surface what the customer is really asking for.

The literal request is the tip. Below the waterline:
  - WHY they bought it (gift / replacement / professional use)
  - WHAT outcome they actually want (cash / working item / acknowledgement / story for friends)
  - HOW URGENTLY (the wedding is Friday vs. "next time you ship")
  - WHETHER they'd accept alternatives (replacement, store credit + bonus)

This is the layer that elevates a CS agent from "policy executor" to "advisor".
Output is consumed by CompensationAgent to bias the offer beyond what the
policy strictly mandates.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from gemini_client import GeminiError, structured
from schemas import AgentName, ClaimContext, Emotion, Needs, TurnRecord

logger = logging.getLogger(__name__)


_SYSTEM = """You are a needs-discovery layer for an e-commerce claims pipeline.

Read the customer's latest message + (if present) the prior conversation, and infer
the deeper needs behind the literal request.

Output fields (typed JSON — schema enforced):

  surface_need : ONE concise phrase — what they literally asked for.
                 e.g. "refund", "replacement", "discount", "apology"

  latent_need  : ONE sentence — the underlying business / personal driver.
                 The customer rarely states this directly; you infer from context.
                 e.g. "needs the mug for a birthday gift this weekend, can't wait for a refund"
                 e.g. "small business reseller — predictability of supply > one refund"
                 e.g. "international customer — return shipping is impractical"
                 If you genuinely can't infer, return "" (empty).

  emotional_need : What recognition / feeling they're seeking.
                 Common ones: acknowledgement, fairness, urgency, control,
                              status (VIP/loyalty), expertise (being taken seriously)

  retention_risk : 0.0-1.0 estimate of churn probability if poorly handled.
                 0.2 = first-time low-emotion customer with simple problem
                 0.5 = standard frustration
                 0.8 = repeat negative interaction, threatening language
                 1.0 = already mentions never buying again / leaving review

  upsell_signal : (optional) phrase if you detect openness to alternatives.
                 e.g. "open to similar product replacement"
                 e.g. "asked about other colors"
                 null if none.

  suggested_offer_bias : (optional) ONE of:
                 'lean_full_refund'     — speed + finality matters most
                 'lean_replacement'     — they want the working item, not the money
                 'lean_partial'         — keeping the item is OK, partial fairness
                 'lean_credit_with_bonus' — high retention value, store credit + sweetener
                 null if no clear bias.

Calibration:
  - Pay attention to deadlines, gift mentions, professional contexts.
  - "I'll just buy from somewhere else" is HIGH retention risk.
  - "This is the third time" raises retention risk regardless of current tone.
  - Don't over-infer. Empty / null fields are honest answers when context is thin.

Reply in the customer's language for surface_need / emotional_need if relevant.
"""


def discover(
    user_message: str,
    history: Optional[list[TurnRecord]] = None,
    emotion: Optional[Emotion] = None,
) -> Needs:
    hist_block = ""
    if history:
        recent = history[-4:]  # max 4 prior turns
        hist_block = "\n\n## Prior conversation\n" + "\n".join(
            f"{t.role}: {t.content[:200]}" for t in recent
        )

    emo_block = ""
    if emotion:
        emo_block = (
            f"\n\n## Emotion grading\n"
            f"score={emotion.score:.1f} risk={emotion.risk.value} label={emotion.label}\n"
            f"triggers={emotion.triggers}\nsignals={emotion.escalation_signals}"
        )

    prompt = f"## Latest customer message\n\"\"\"\n{user_message.strip()}\n\"\"\"{hist_block}{emo_block}"
    try:
        result = structured(
            prompt=prompt,
            schema=Needs,
            system=_SYSTEM,
            temperature=0.3,
            max_tokens=500,
        )
        return result
    except GeminiError as e:
        logger.warning("NeedsAgent fallback: %s", e)
        return Needs(
            surface_need="resolve damage claim",
            latent_need="",
            emotional_need="acknowledgement",
            retention_risk=0.5,
        )


def run(ctx: ClaimContext) -> ClaimContext:
    t0 = time.monotonic()
    ctx.needs = discover(ctx.user_message, history=ctx.history, emotion=ctx.emotion)
    elapsed = int((time.monotonic() - t0) * 1000)
    summary = (
        f"surface='{ctx.needs.surface_need}' retention_risk={ctx.needs.retention_risk:.2f}"
    )
    if ctx.needs.suggested_offer_bias:
        summary += f" bias={ctx.needs.suggested_offer_bias}"
    ctx.add_trace(AgentName.NEEDS, status="ok", summary=summary, elapsed_ms=elapsed)
    return ctx
