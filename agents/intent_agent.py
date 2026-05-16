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
from schemas import AgentName, ClaimContext, IntentLabel, IntentResult

logger = logging.getLogger(__name__)


_SYSTEM = """你是电商客服的意图分类器。

任务：根据客户消息判断意图，并尽可能提取订单号和商品类别。

意图分类（三选一）：
- claim_with_image：客户在投诉商品损坏/缺陷/质量问题，且伴随了图片证据
- claim_text_only：客户在投诉商品损坏/缺陷/质量问题，但只有文字描述（无图）
- general_inquiry：一般咨询（询问政策、物流、推荐、寒暄等），不是理赔诉求

提取规则：
- order_id：识别"订单号"、"订单"、"ORD-xxx"、"#12345" 等格式；找不到返回 null
- product_hint：识别商品类别（杯子 / 笔电 / 外套 / 手机等），找不到返回 null
- confidence：你对意图分类的置信度，0-1

注意：
- "我想退款" 不算理赔，除非提到具体损坏或缺陷
- "刚收到货很满意" 是 general_inquiry
- 含"破/裂/坏/碎/损坏/缺/划/撕/裂纹"等明确损坏关键词 + 有图 → claim_with_image
- 同上但无图 → claim_text_only
"""


_ORDER_RE = re.compile(r"(?:ORD[-\s]?|订单号[:：\s]?|#)(\w{4,16})", re.IGNORECASE)


def _heuristic_order_id(text: str) -> str | None:
    """先用 regex 抢一次，可能比模型更稳。"""
    m = _ORDER_RE.search(text)
    return m.group(1).upper() if m else None


def classify(user_message: str, has_image: bool) -> IntentResult:
    msg = user_message.strip()
    prompt = (
        f"客户消息：{msg}\n"
        f"是否有图片：{'有' if has_image else '无'}\n\n"
        f"请输出意图分类。"
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
    ctx.intent = classify(ctx.user_message, has_image=ctx.image_bytes is not None)
    elapsed = int((time.monotonic() - t0) * 1000)
    summary = (
        f"{ctx.intent.label.value} conf={ctx.intent.confidence:.2f}"
        + (f" order={ctx.intent.order_id}" if ctx.intent.order_id else "")
    )
    ctx.add_trace(AgentName.INTENT, status="ok", summary=summary, elapsed_ms=elapsed)
    return ctx
