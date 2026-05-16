"""
EmotionAgent — Gemini-graded affect classifier.

Why this exists as a first-class agent (not buried in CompensationAgent):
  - Downstream agents need typed emotion to modulate decisions
    (offer uplift, escalation, reply tone). Keeping it explicit makes
    the policy graph debuggable and the UI honest about why a claim
    got the offer it got.
  - We want to *show* the human who reviews the case why we paid more.

Inputs:  user_message (the latest customer turn) + thin history hint
Outputs: typed Emotion {score, risk, label, triggers, signals, tone}

Cost: ~250 input tokens, ~150 output tokens, ~$0.0002/call on flash.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from gemini_client import GeminiError, structured
from schemas import AgentName, ClaimContext, Emotion, EmotionRisk

logger = logging.getLogger(__name__)


_SYSTEM = """You are an emotion-grading layer for an e-commerce claims pipeline.

Read the customer's latest message and grade their affect on these axes:

  score (0-10): a calibrated number, not a vibe.
    0  = happy / grateful ("thanks for the quick reply!")
    3  = mildly inconvenienced
    5  = neutral, transactional
    7  = clearly frustrated, expressive negativity
    9  = furious, accusatory, profanity
    10 = explicit threats (legal action, media, social blowback)

  risk:
    LOW       = score <= 4
    MEDIUM    = score 5-7
    HIGH      = score 8-9
    CRITICAL  = legal/media threats present (regardless of score)

  label: ONE word that a human supervisor could glance at — frustrated,
         anxious, angry, threatening, calm, grateful, resigned, etc.

  triggers: the exact 2-5 word phrases from the message that drove your score.
            Use the original language (do not translate).

  escalation_signals: short bullet list of legal/media markers — empty list if none.
            Examples: "mentions lawyer", "mentions 12315", "threatens chargeback",
            "third complaint about same order", "asking for manager"

  suggested_tone: one short sentence telling the reply writer how to open.
            Examples: "Lead with explicit apology + named ownership."
                      "Stay matter-of-fact; do not over-apologize."
                      "Acknowledge the wait time before anything else."

Calibration notes:
  - A short message can be HIGH risk. Length is irrelevant.
  - All-caps and exclamation marks are weak signals; word choice is strong.
  - Mentioning a regulator/lawyer/media is CRITICAL even if the tone is calm.
  - A polite request for refund is NOT high emotion. Stay calibrated.
"""


def grade(user_message: str, prior_score: Optional[float] = None) -> Emotion:
    """Grade one customer turn. prior_score (optional) helps detect escalation."""
    prompt = f"Customer message:\n\"\"\"\n{user_message.strip()}\n\"\"\""
    if prior_score is not None:
        prompt += f"\n\nFor context, the previous turn graded {prior_score:.1f}/10."

    try:
        result = structured(
            prompt=prompt,
            schema=Emotion,
            system=_SYSTEM,
            temperature=0.2,
            max_tokens=400,
        )
        assert isinstance(result, Emotion)
        # Promote CRITICAL whenever escalation_signals are non-empty
        if result.escalation_signals and result.risk != EmotionRisk.CRITICAL:
            result = result.model_copy(update={"risk": EmotionRisk.CRITICAL})
        return result
    except GeminiError as e:
        logger.warning("EmotionAgent fallback to MEDIUM/calm: %s", e)
        return Emotion(
            score=5.0,
            risk=EmotionRisk.MEDIUM,
            label="unknown",
            triggers=[],
            escalation_signals=[],
            suggested_tone="Stay neutral and professional; emotion grading unavailable.",
        )


def run(ctx: ClaimContext) -> ClaimContext:
    t0 = time.monotonic()
    prior = ctx.emotion.score if ctx.emotion else None
    ctx.emotion = grade(ctx.user_message, prior_score=prior)
    elapsed = int((time.monotonic() - t0) * 1000)
    summary = (
        f"{ctx.emotion.label} score={ctx.emotion.score:.1f} risk={ctx.emotion.risk.value}"
    )
    if ctx.emotion.escalation_signals:
        summary += f" ⚠ {len(ctx.emotion.escalation_signals)} escalation signal(s)"
    ctx.add_trace(AgentName.EMOTION, status="ok", summary=summary, elapsed_ms=elapsed)
    return ctx
