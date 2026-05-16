"""
IntentAgent — 分类用户意图 + 提取订单号。

Pipeline 第一站：决定后续要不要跑 DamageAgent / CompensationAgent。

输入：user_message + 是否有图
输出：IntentResult { label, order_id?, product_hint?, confidence }
"""
from __future__ import annotations

import logging
import re
import time

from gemini_client import GeminiError, structured
from schemas import AgentName, ClaimContext, IntentLabel, IntentResult, TurnRecord

logger = logging.getLogger(__name__)


_SYSTEM = """You classify the customer's intent on an e-commerce support channel.
This is a multi-turn conversation — read the prior history if present.

Output ONE label:

  claim_with_image          — customer reports damaged/defective/wrong product + image attached
  claim_text_only           — same but no image
  general_inquiry           — policy questions, shipping ETA, thanks, greetings, etc.
  needs_clarification       — customer raised a claim but is missing critical info
                              (no order number AND no clear damage description, OR vague
                              "something's wrong" with no specifics). When you choose this,
                              ALSO fill clarification_question with ONE specific question
                              in the customer's own language.
  followup_on_prior_claim   — there IS prior conversation history and the customer is
                              responding to / building on it (e.g. "I'd actually prefer a
                              replacement", "still no refund?", "the order number was
                              ORD-9999", "please cancel that and refund me instead"). Use
                              this when continuity matters more than starting fresh.

CRITICAL RULES (don't get this wrong)
  - A customer who mentions a lawyer, regulator (12315 / FTC / BBB / consumer
    protection / 消协), media exposure, chargeback, or "third time I have written"
    IS a claim — they're escalating an existing problem. NOT general_inquiry.
    If they reference a damaged item or a refund demand, classify as claim_text_only
    (or claim_with_image if attached). The downstream pipeline will route them
    to a human via emotion_agent + verifier.

  - A refund request without any mention of damage/defect/wrong-item is still
    general_inquiry (the customer just changed their mind). Don't auto-route to
    the claims pipeline.

  - A photo attached without ANY problem description → still
    general_inquiry (could be a product question / unboxing share). Don't assume.

EXTRACTION
  - order_id      — formats: ORD-XXX, #12345, "order number 88712", 订单号 XYZ.
                    null if not present. Don't guess.
  - product_hint  — single noun: mug, laptop, jacket, headphones, phone, ...
                    null if not present.
  - confidence    — your subjective certainty 0-1. Be honest; ≤0.5 on ambiguous.

The customer might write in English, Chinese, or other languages. Identify intent
regardless of language.
"""


_ORDER_RE = re.compile(r"(?:ORD[-\s]?|订单号[:：\s]?|#)(\w{4,16})", re.IGNORECASE)


def _heuristic_order_id(text: str) -> str | None:
    """先用 regex 抢一次，可能比模型更稳。"""
    m = _ORDER_RE.search(text)
    return m.group(1).upper() if m else None


def classify(
    user_message: str,
    has_image: bool,
    history: list[TurnRecord] | None = None,
) -> IntentResult:
    msg = user_message.strip()
    hist_block = ""
    if history:
        recent = history[-6:]
        hist_block = "\n\n## Prior conversation history\n" + "\n".join(
            f"{t.role}: {t.content[:240]}"
            + (f"  [decision: {t.decision_summary}]" if t.decision_summary else "")
            for t in recent
        )
    prompt = (
        f"## Latest customer message\n\"\"\"\n{msg}\n\"\"\"\n"
        f"## Has image attached: {'yes' if has_image else 'no'}"
        f"{hist_block}\n\n"
        f"Classify the intent."
    )
    try:
        result = structured(
            prompt=prompt,
            schema=IntentResult,
            system=_SYSTEM,
            temperature=0.1,
            max_tokens=256,
        )
    except GeminiError as e:
        logger.warning("IntentAgent fell back to general_inquiry: %s", e)
        return IntentResult(label=IntentLabel.GENERAL_INQUIRY, confidence=0.0)

    # 兜底：模型选了 claim_with_image 但实际没图 → 降级
    if result.label == IntentLabel.CLAIM_WITH_IMAGE and not has_image:
        result = result.model_copy(update={"label": IntentLabel.CLAIM_TEXT_ONLY})

    # 用 regex 重写 order_id（如果模型漏了）
    if not result.order_id:
        heur = _heuristic_order_id(msg)
        if heur:
            result = result.model_copy(update={"order_id": heur})

    return result


def run(ctx: ClaimContext) -> ClaimContext:
    t0 = time.monotonic()
    ctx.intent = classify(
        ctx.user_message,
        has_image=ctx.image_bytes is not None,
        history=ctx.history,
    )
    elapsed = int((time.monotonic() - t0) * 1000)
    summary = (
        f"{ctx.intent.label.value} conf={ctx.intent.confidence:.2f}"
        + (f" order={ctx.intent.order_id}" if ctx.intent.order_id else "")
    )
    if ctx.intent.label == IntentLabel.NEEDS_CLARIFICATION and ctx.intent.clarification_question:
        # short-circuit pipeline: orchestrator will ask the customer the question
        ctx.awaiting_clarification = True
        ctx.clarification_question = ctx.intent.clarification_question
    ctx.add_trace(AgentName.INTENT, status="ok", summary=summary, elapsed_ms=elapsed)
    return ctx
