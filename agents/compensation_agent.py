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
    OfferType,
)

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
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    返回 (适用的政策, 触发的强制升级原因)。

    强制升级原因列表非空 → orchestrator 直接 escalate，不走 LLM。
    """
    policies = load_policies()["policies"]
    matched: list[dict[str, Any]] = []
    escalate_reasons: list[str] = []

    for p in policies:
        cond = p.get("applies_when", {})

        # 损坏严重度区间
        sev_min = cond.get("damage_severity_min", -1)
        sev_max = cond.get("damage_severity_max", 10)
        if not (sev_min <= damage.severity <= sev_max):
            continue

        # 损坏类型白名单（如有）
        if "damage_types" in cond and damage.damage_type.value not in cond["damage_types"]:
            continue

        # has_image 条件（P-LMT-01）
        if "has_image" in cond and cond["has_image"] != has_image:
            continue

        # 情绪条件（P-EMO-01 / P-EMO-02）
        emo_min = cond.get("emotion_score_min")
        if emo_min is not None:
            if emotion is None or emotion.score < emo_min:
                continue

        # 金额阈值（P-LMT-01）
        amt_min = cond.get("claim_amount_min_cents")
        if amt_min is not None and estimated_value_cents < amt_min:
            continue

        # 关键词条件（P-EMO-02）
        # 注：需要 ctx.user_message，这里简化为 emotion.label 中的关键词
        # 在 orchestrator 里更精确处理

        matched.append(p)
        if p.get("force_escalate"):
            escalate_reasons.append(f"{p['id']}: {p['title']}")

    return matched, escalate_reasons


# ─────────────────────────────────────────────────────────
#  Compensation 计算（结构化提案）
# ─────────────────────────────────────────────────────────
_SYSTEM = """你是电商售后理赔的赔付方案制定者。

输入：客户的损坏评估 + 适用的政策清单 + 商品估值 + 客户情绪。

任务：从适用政策中选最合适的一条（通常是最贴合损坏类型/严重度的），按其规则计算赔付金额，并写出向客户解释的 justification。

金额计算规则：
- amount_basis=order_value：amount_cents = 商品估值（受 max_cents 封顶）
- amount_basis=percentage：amount_cents = 商品估值 × amount_percent / 100（受 max_cents 封顶）
- amount_basis=fixed：amount_cents = 政策里的 amount_cents
- amount_basis=manual：amount_cents 设为 0，offer_type 设为 store_credit，让 verifier 升级

情绪上浮：
- 如果适用政策里含 P-EMO-01（情绪 >= 8），把最终 amount_cents × 1.2（不超过原始 max_cents）

输出：
- offer_type：选择的赔付方式
- amount_cents：最终金额（分为单位）
- justification：1-2 句话给客户解释为什么是这个方案（不要透露内部政策 ID）
- policy_ids：引用的政策 ID 列表
- requires_return：根据政策填

注意：
- justification 用温和、共情的语气，不要冷冰冰报数字
- 不要替客户决定他不需要的方案（比如客户要退款，你不要给 replacement 除非政策强制）
"""


def propose(
    damage: DamageAssessment,
    emotion: Optional[Emotion],
    has_image: bool,
    estimated_value_cents: int = 5000,  # 默认 50 元商品估值（demo 用）
) -> tuple[Optional[CompensationOffer], list[str]]:
    """
    返回 (offer, escalate_reasons)。
    如果有强制升级原因，offer 仍会出（作为参考），但 orchestrator 会让 verifier escalate。
    """
    matched, escalate_reasons = filter_policies(damage, emotion, has_image, estimated_value_cents)

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

    prompt = (
        f"损坏评估：\n{damage.model_dump_json(indent=2)}\n\n"
        f"商品估值（分）：{estimated_value_cents}\n\n"
        f"客户情绪：{emotion.model_dump_json() if emotion else 'null'}\n\n"
        f"适用政策：\n{json.dumps(policy_summaries, ensure_ascii=False, indent=2)}\n\n"
        f"请输出赔付方案。"
    )

    try:
        offer = structured(
            prompt=prompt,
            schema=CompensationOffer,
            system=_SYSTEM,
            temperature=0.2,
            max_tokens=512,
        )
        return offer, escalate_reasons
    except GeminiError as e:
        logger.warning("CompensationAgent fallback: %s", e)
        return None, ["llm_failed"] + escalate_reasons


def run(ctx: ClaimContext, estimated_value_cents: int = 5000) -> ClaimContext:
    if ctx.damage is None:
        ctx.add_trace(AgentName.COMPENSATION, status="error", summary="no damage assessment")
        return ctx

    t0 = time.monotonic()
    offer, escalate_reasons = propose(
        damage=ctx.damage,
        emotion=ctx.emotion,
        has_image=ctx.image_bytes is not None,
        estimated_value_cents=estimated_value_cents,
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
