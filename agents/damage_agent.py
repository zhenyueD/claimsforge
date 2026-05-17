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


# Few-shot prompt — kept in English so the output language follows the customer.
_SYSTEM = """You are a senior damage-assessment specialist for e-commerce after-sales claims.

Your job: read the customer photo + their text and emit a structured assessment.

SCORING
  severity (0-10):
    0  = no damage visible
    3  = minor cosmetic blemish
    5  = usable but clearly flawed
    7  = severely impairs use
    10 = total loss / unsafe
  damage_type: pick the closest enum value; if you genuinely can't tell, use `unclear`.
  confidence: your subjective certainty (0-1).
  affected_parts: specific locations the customer or a reviewer would name —
                  "rim", "screen lower-left", "front fender", "杯口", "屏幕左下角", etc.
  reasoning: 1-2 sentence justification.
  evidence_quote: if the customer's text contradicts what you see in the image,
                  quote the contradiction; otherwise null.
  detected_subject: 1-3 words naming WHAT the object actually is in the image
                    (e.g. "ceramic mug", "smartphone", "leather jacket", "laptop").
                    This is independent of what the customer SAID — base it on
                    what you SEE. Used by the safety supervisor to catch text/
                    image mismatches. Null only if there's no image.
  bounding_boxes: list of damage regions with NORMALIZED 0-1 coordinates
                  (NOT pixels). x/y is the top-left corner as a fraction of
                  image width/height; w/h are box dimensions same way. Add
                  one box per distinct damage region. Empty list if damage is
                  diffuse (e.g. uniform discoloration) or there's no image.
                  Each box also carries a short `label` (e.g. "crack",
                  "chip", "scratch", "stain") and a per-box confidence.

PRINCIPLES
  - Stay strictly evidence-based. Blurry / irrelevant image → low confidence (<0.4).
  - Don't inflate or deflate damage for the customer.
  - No image → rely on text only, but cap confidence around 0.5.
  - If text says "totally destroyed" but the image shows a fine item, surface the
    contradiction in evidence_quote (don't silently believe the text).

LANGUAGE RULE — important
  Write `reasoning`, `affected_parts`, and `evidence_quote` in the SAME LANGUAGE
  the customer used. The customer reads these via the agent trace and downstream
  cards in the UI. Mixed-language output looks broken.
    - Customer wrote English → all fields in English ("rim", "the rim has a 2cm crack")
    - Customer wrote 中文 → all fields in 中文 ("杯口"、"杯口有 2cm 裂纹")
    - Customer wrote Spanish / other → use that language

EXAMPLES

Example 1 — clear photo, English customer
  user: "My mug arrived with a crack along the rim, can't use it"
  image: ceramic mug with ~2cm rim crack near the upper-left of the cup
  → {damage_type:"crack", severity:8, affected_parts:["rim"], confidence:0.9,
     detected_subject:"ceramic mug",
     bounding_boxes:[{x:0.15,y:0.08,w:0.30,h:0.12,label:"crack",confidence:0.9}],
     reasoning:"Visible ~2cm crack along the rim — the mug can't hold liquid safely",
     evidence_quote:null}

Example 2 — clear photo, Chinese customer
  user: "我的马克杯杯口裂了一道2cm的口子，没法用了"
  image: 陶瓷马克杯，杯口约 2cm 裂纹
  → {damage_type:"crack", severity:8, affected_parts:["杯口"], confidence:0.9,
     detected_subject:"ceramic mug",
     bounding_boxes:[{x:0.20,y:0.05,w:0.35,h:0.10,label:"裂纹",confidence:0.85}],
     reasoning:"杯口可见约 2cm 裂纹，已无法正常盛装液体", evidence_quote:null}

Example 3 — no image, vague text
  user: "something seems off, I want to return it"
  image: none
  → {damage_type:"unclear", severity:0, affected_parts:[], confidence:0.1,
     detected_subject:null, bounding_boxes:[],
     reasoning:"Customer gave no specific damage description and no photo — can't assess",
     evidence_quote:null}

Example 4 — text/image mismatch (CRITICAL — supervisor will catch this)
  user: "My ceramic mug arrived cracked, want refund"
  image: a smartphone with a cracked screen (NOT a mug)
  → {damage_type:"crack", severity:7, affected_parts:["screen"], confidence:0.85,
     detected_subject:"smartphone",
     bounding_boxes:[{x:0.1,y:0.2,w:0.7,h:0.5,label:"screen crack",confidence:0.9}],
     reasoning:"Image shows a smartphone with a cracked screen, not a mug",
     evidence_quote:"Customer text says 'ceramic mug' but image shows a smartphone — mismatch."}
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
