"""
ClaimsForge schemas — Pydantic models 是所有 agent 间传递的单一 source of truth.

设计原则：
  - ClaimContext 像一个流水线上的工件，每个 agent 拿全量、附加自己的字段。
  - 字段都用 Optional 标，让 agent 早退也不会炸。
  - AgentTrace 是给前端流式显示的"思考过程"，独立于业务字段。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────
#  Enums
# ─────────────────────────────────────────────────────────────
class IntentLabel(str, Enum):
    CLAIM_WITH_IMAGE = "claim_with_image"
    CLAIM_TEXT_ONLY = "claim_text_only"
    GENERAL_INQUIRY = "general_inquiry"


class DamageType(str, Enum):
    CRACK = "crack"                # 裂纹（杯子/屏幕）
    SCRATCH = "scratch"            # 划痕（家电/笔电）
    TEAR = "tear"                  # 撕裂（布料/包装）
    DENT = "dent"                  # 凹陷（金属/塑料外壳）
    STAIN = "stain"                # 污渍
    MISSING_PART = "missing_part"  # 配件缺失
    WATER_DAMAGE = "water_damage"  # 进水/泡水
    DEFECT = "defect"              # 制造缺陷
    UNCLEAR = "unclear"            # 无法判断


class OfferType(str, Enum):
    FULL_REFUND = "full_refund"
    PARTIAL_REFUND = "partial_refund"
    REPLACEMENT = "replacement"
    STORE_CREDIT = "store_credit"


class VerifierVerdict(str, Enum):
    APPROVE = "approve"
    REVISE = "revise"
    ESCALATE = "escalate_to_human"


class AgentName(str, Enum):
    INTENT = "IntentAgent"
    EMOTION = "EmotionAgent"
    DAMAGE = "DamageAgent"
    COMPENSATION = "CompensationAgent"
    VERIFIER = "VerifierAgent"


class EmotionRisk(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"  # 涉及法律/媒体威胁


# ─────────────────────────────────────────────────────────────
#  Agent outputs
# ─────────────────────────────────────────────────────────────
class IntentResult(BaseModel):
    label: IntentLabel
    order_id: Optional[str] = Field(default=None, description="提取出的订单号，可能无")
    product_hint: Optional[str] = Field(default=None, description="用户提到的商品类别")
    confidence: float = Field(ge=0, le=1)


class DamageAssessment(BaseModel):
    """Gemini Vision 输出的结构化损坏评估。"""
    damage_type: DamageType
    severity: int = Field(ge=0, le=10, description="0=无损坏，10=完全损毁")
    affected_parts: list[str] = Field(default_factory=list, description="受损部位，如 '杯口', '屏幕左下角'")
    confidence: float = Field(ge=0, le=1)
    reasoning: str = Field(description="为什么这么判断（1-2 句话）")
    evidence_quote: Optional[str] = Field(default=None, description="如果用户文字与图片矛盾，引用矛盾点")


class CompensationOffer(BaseModel):
    offer_type: OfferType
    amount_cents: int = Field(ge=0, description="赔付金额（分），replacement 时为商品原价")
    currency: str = Field(default="CNY")
    justification: str = Field(description="为什么是这个金额（引用政策条款）")
    policy_ids: list[str] = Field(default_factory=list, description="引用的政策条目 ID")
    requires_return: bool = Field(default=False, description="是否需要客户寄回原件")


class VerificationResult(BaseModel):
    verdict: VerifierVerdict
    reason: str
    revised_offer: Optional[CompensationOffer] = Field(
        default=None, description="如果 verdict=revise，给出 verifier 建议的修订方案"
    )


# ─────────────────────────────────────────────────────────────
#  Emotion / Needs（沿用 conversation_engine.py 旧结构，只 typed 化）
# ─────────────────────────────────────────────────────────────
class Emotion(BaseModel):
    """Output of EmotionAgent — Gemini-graded customer affect on this turn."""
    score: float = Field(ge=0, le=10, description="0=happy, 5=neutral, 10=furious")
    risk: EmotionRisk
    label: str = Field(description="Single descriptive word: frustrated / anxious / angry / threatening / calm")
    triggers: list[str] = Field(default_factory=list, description="Specific words/phrases that drove the score")
    escalation_signals: list[str] = Field(default_factory=list, description="Legal threats / media mentions / repeat complaint markers")
    suggested_tone: str = Field(default="", description="Concrete guidance for the reply: apologetic / matter-of-fact / urgent / formal")


class Needs(BaseModel):
    surface_need: str = ""
    latent_need: str = ""
    emotional_need: str = ""
    retention_score: float = 0.0
    suggested_tone: str = ""


# ─────────────────────────────────────────────────────────────
#  Agent trace —— 给前端流式显示的事件
# ─────────────────────────────────────────────────────────────
class AgentTrace(BaseModel):
    agent: AgentName
    status: str  # "running" | "ok" | "error"
    summary: str  # 一行话给 UI 显示，比如 "classified as claim_with_image"
    elapsed_ms: Optional[int] = None
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


# ─────────────────────────────────────────────────────────────
#  ClaimContext —— 流水线上的核心工件
# ─────────────────────────────────────────────────────────────
class ClaimContext(BaseModel):
    """Orchestrator 把这个对象按顺序传给每个 agent。每个 agent 写入自己的字段。"""
    # 输入
    session_id: str
    user_message: str
    image_id: Optional[str] = Field(default=None, description="如果有上传，存的 image_id")
    image_bytes: Optional[bytes] = Field(default=None, exclude=True, description="不进 JSON，只内部传递")

    # 中间产物
    intent: Optional[IntentResult] = None
    damage: Optional[DamageAssessment] = None
    offer: Optional[CompensationOffer] = None
    verification: Optional[VerificationResult] = None

    # 复用现有
    emotion: Optional[Emotion] = None
    needs: Optional[Needs] = None

    # 最终结果（orchestrator 决定）
    final_offer: Optional[CompensationOffer] = None  # 经过 verifier 后的最终方案
    escalated_to_human: bool = False
    final_reply: str = ""  # 发给用户的最终文字

    # Trace
    traces: list[AgentTrace] = Field(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}

    def add_trace(self, agent: AgentName, status: str, summary: str, elapsed_ms: Optional[int] = None) -> None:
        self.traces.append(AgentTrace(agent=agent, status=status, summary=summary, elapsed_ms=elapsed_ms))


# ─────────────────────────────────────────────────────────────
#  API payloads
# ─────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str
    session_id: str
    image_id: Optional[str] = None  # 上传图片后由 /api/upload-image 返回


class UploadImageResponse(BaseModel):
    image_id: str
    width: int
    height: int
    bytes: int


class DemoScenario(BaseModel):
    id: str
    title: str            # "破裂马克杯"
    description: str      # 案例描述（给用户看）
    user_message: str     # 自动发送的消息
    image_filename: str   # data/demo_images/ 下的文件名
    expected_outcome: str # "$24 全退" 之类，给评委 spoiler


class ChatResponse(BaseModel):
    """扩展原 API 响应，向后兼容老前端。"""
    session_id: str
    reply: str
    used_llm: bool
    emotion: dict[str, Any]
    needs: dict[str, Any]
    escalation: dict[str, Any]
    kb_results: list[dict[str, Any]]
    confidence: float
    timestamp: str
    # ── 新增 ──
    claim: Optional[dict[str, Any]] = None  # 包含 intent / damage / offer / verification / traces
