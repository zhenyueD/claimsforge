"""
DamageAgent — 用 Gemini 2.5 Flash Vision 评估损坏程度。

输入：
  - user_message: 客户的文字描述
  - image_bytes: 商品照片（可选，无图时按文字推断）

输出：DamageAssessment（typed Pydantic）

策略：
  - 有图：image + text 一起喂给 Gemini，让模型多模态判断。
  - 无图：只看文字。模型很可能 confidence 偏低；这是符合预期的（policies.json 里 P-LMT-01 规定 >= 50 元且无图必须升级）。
  - 失败降级：返回 UNCLEAR + severity=0 + confidence=0，让上层决定升级到人工。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from gemini_client import GeminiError, structured
from schemas import AgentName, AgentTrace, ClaimContext, DamageAssessment, DamageType

logger = logging.getLogger(__name__)


# Few-shot 示例放进 system prompt，引导模型按 schema 输出
_SYSTEM = """你是电商售后理赔的损坏评估专家。

任务：根据客户提供的图片和文字描述，输出结构化的损坏评估。

评估准则：
- severity（0-10）：0=无损坏；3=轻微外观瑕疵；5=可使用但有明显缺陷；7=严重影响使用；10=完全损毁
- damage_type：从 enum 中选最贴近的一项；判断不清选 unclear
- confidence：你对判断的置信度，0-1
- affected_parts：具体部位（如"杯口"、"屏幕左下角"、"前轮挡泥板"）
- reasoning：1-2 句话解释你的判断依据
- evidence_quote：如果用户文字描述与图片证据矛盾，引用矛盾点；否则 null

判断原则：
- 严格基于证据。如果图片很模糊或不相关，confidence 给低分（< 0.4）。
- 不要替客户夸大或缩小损坏程度。
- 没有图片时主要凭客户描述，但 confidence 应较低（一般 < 0.5）。
- 如果用户描述明显与图片矛盾（如说"全坏了"但图里完好），在 evidence_quote 中点出。

以下是 2 个示例供参考：

示例 1（有图，明显裂纹）：
  用户："我的马克杯收到时杯口裂了一道口子，没法用了"
  图片：陶瓷马克杯，杯口可见约 2cm 裂纹
  输出：{damage_type:"crack", severity:8, affected_parts:["杯口"], confidence:0.9, reasoning:"杯口裂纹明显，长度约2cm，无法正常盛装液体", evidence_quote:null}

示例 2（无图，描述模糊）：
  用户："东西不太对劲，想退货"
  图片：无
  输出：{damage_type:"unclear", severity:0, affected_parts:[], confidence:0.1, reasoning:"客户未提供具体损坏描述或证据照片，无法评估", evidence_quote:null}
"""


def assess(
    user_message: str,
    image_bytes: Optional[bytes] = None,
    image_mime: str = "image/jpeg",
) -> DamageAssessment:
    """评估单条理赔。失败时返回 UNCLEAR 兜底。"""
    prompt = f"客户描述：{user_message.strip()}\n\n请评估损坏程度。"
    try:
        result = structured(
            prompt=prompt,
            schema=DamageAssessment,
            system=_SYSTEM,
            image_bytes=image_bytes,
            image_mime=image_mime,
            temperature=0.2,
            max_tokens=512,
        )
        assert isinstance(result, DamageAssessment)
        return result
    except GeminiError as e:
        logger.warning("DamageAgent fell back to UNCLEAR: %s", e)
        return DamageAssessment(
            damage_type=DamageType.UNCLEAR,
            severity=0,
            affected_parts=[],
            confidence=0.0,
            reasoning=f"自动评估失败：{e}。已降级，请人工核验。",
        )


def run(ctx: ClaimContext) -> ClaimContext:
    """流水线接口：从 ClaimContext 读输入，写 ctx.damage + ctx.traces。"""
    t0 = time.monotonic()
    ctx.damage = assess(
        user_message=ctx.user_message,
        image_bytes=ctx.image_bytes,
    )
    elapsed = int((time.monotonic() - t0) * 1000)
    summary = (
        f"{ctx.damage.damage_type.value} severity={ctx.damage.severity}/10 "
        f"conf={ctx.damage.confidence:.2f}"
    )
    ctx.add_trace(AgentName.DAMAGE, status="ok", summary=summary, elapsed_ms=elapsed)
    return ctx
