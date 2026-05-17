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
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────
#  Enums
# ─────────────────────────────────────────────────────────────
class IntentLabel(str, Enum):
    CLAIM_WITH_IMAGE = "claim_with_image"
    CLAIM_TEXT_ONLY = "claim_text_only"
    GENERAL_INQUIRY = "general_inquiry"
    NEEDS_CLARIFICATION = "needs_clarification"  # not enough info — ask a follow-up
    FOLLOWUP_ON_PRIOR_CLAIM = "followup_on_prior_claim"  # multi-turn continuation


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
    NEEDS = "NeedsAgent"
    DAMAGE = "DamageAgent"
    COMPENSATION = "CompensationAgent"
    SUPERVISOR = "SupervisorAgent"
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
    clarification_question: Optional[str] = Field(
        default=None,
        description="If label == needs_clarification, the ONE specific question to ask the customer "
                    "in their own language (e.g. 'Could you share your order number?'). null otherwise."
    )


class BoundingBox(BaseModel):
    """One damage region with normalized 0-1 coordinates so the frontend can
    overlay the box on any image size without knowing pixel dimensions."""
    x: float = Field(ge=0, le=1, description="Left edge, fraction of width")
    y: float = Field(ge=0, le=1, description="Top edge, fraction of height")
    w: float = Field(ge=0, le=1, description="Box width, fraction of image width")
    h: float = Field(ge=0, le=1, description="Box height, fraction of image height")
    label: str = Field(description="Short noun for the damage region (e.g. 'crack', 'chip', 'tear')")
    confidence: float = Field(default=0.7, ge=0, le=1)


class DamageAssessment(BaseModel):
    """Gemini Vision 输出的结构化损坏评估。"""
    damage_type: DamageType
    severity: int = Field(ge=0, le=10, description="0=无损坏，10=完全损毁")
    affected_parts: list[str] = Field(default_factory=list, description="受损部位，如 '杯口', '屏幕左下角'")
    confidence: float = Field(ge=0, le=1)
    reasoning: str = Field(description="为什么这么判断（1-2 句话）")
    evidence_quote: Optional[str] = Field(default=None, description="如果用户文字与图片矛盾，引用矛盾点")
    # Multimodal forensics fields — for visual evidence overlay + consistency gate
    detected_subject: Optional[str] = Field(
        default=None,
        description="The actual object Vision sees, in 1-3 words (e.g. 'ceramic mug', "
                    "'smartphone', 'leather jacket'). Used by SupervisorAgent to catch "
                    "text/image mismatches (customer claims 'mug' but image shows 'phone')."
    )
    bounding_boxes: list[BoundingBox] = Field(
        default_factory=list,
        description="Damage regions with normalized 0-1 coordinates. Empty if no clear "
                    "region (e.g. uniform discoloration). Frontend overlays these on the "
                    "uploaded image so the customer + evaluator see WHAT the AI saw."
    )


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
    """Output of NeedsAgent — surfaces what the customer is REALLY asking for,
    beyond the literal request. Used by CompensationAgent to choose offer type."""
    surface_need: str = Field(default="", description="What they literally asked for ('refund', 'replacement')")
    latent_need: str = Field(default="", description="The underlying business / personal need (e.g. 'fast resolution because she needs the mug for a gift on Friday')")
    emotional_need: str = Field(default="", description="Acknowledgement / fairness / urgency / control")
    retention_risk: float = Field(default=0.5, ge=0, le=1, description="Probability the customer churns if poorly handled")
    upsell_signal: Optional[str] = Field(default=None, description="If we detect cross-sell potential (e.g. 'open to replacement of similar item')")
    suggested_offer_bias: Optional[str] = Field(default=None, description="One of: 'lean_full_refund' / 'lean_replacement' / 'lean_partial' / 'lean_credit_with_bonus' / null")


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
#  SupervisorDecision —— AWS-IAM-style explicit layered decision
# ─────────────────────────────────────────────────────────────
class SupervisorLayer(str, Enum):
    """Which layer of the supervisor produced this decision. Mirrors the
    AWS IAM evaluation model: an explicit DENY at any layer is final, then
    EXEMPT can reverse a downstream-set escalation, then CAP clamps
    numerical fields. APPROVE is the default-allow terminal."""
    DENY = "deny"        # FORCE_ESCALATE — short-circuits everything below
    EXEMPT = "exempt"    # UN_ESCALATE — reverses an earlier escalation (e.g. perishables)
    CAP = "cap"          # CAP_AMOUNT — clamp amount_cents without escalating
    APPROVE = "approve"  # passed all layers


class SupervisorVerdict(str, Enum):
    APPROVE = "approve"
    CAP_AMOUNT = "cap_amount"
    UN_ESCALATE = "un_escalate"
    FORCE_ESCALATE = "force_escalate"


class SupervisorDecision(BaseModel):
    """Typed decision record produced by SupervisorAgent. Replaces the
    untyped dict that used to live on ctx.supervisor_decision (v5 had it
    as `dict` to dodge a circular import; that's no longer needed)."""
    layer: SupervisorLayer
    verdict: SupervisorVerdict
    matched_rules: list[str] = Field(default_factory=list,
        description="Rule IDs that fired. Audit-grade — every rule that participated.")
    reasons: list[str] = Field(default_factory=list,
        description="One human-readable reason per matched rule.")
    blocked_rules: list[str] = Field(default_factory=list,
        description="Backwards-compat alias of matched_rules for v5 callers.")
    original_amount_cents: Optional[int] = None
    capped_amount_cents: Optional[int] = None


# ─────────────────────────────────────────────────────────────
#  TrustScore —— Stripe-Radar-style weighted factor breakdown
# ─────────────────────────────────────────────────────────────
class TrustFactorName(str, Enum):
    IMAGE_UNIQUENESS = "image_uniqueness"
    IMAGE_PROVENANCE = "image_provenance"   # EXIF age / metadata authenticity
    AMOUNT_SANDBOX = "amount_sandbox"
    HISTORY_COHERENCE = "history_coherence"
    EMOTION_GATING = "emotion_gating"
    EVIDENCE_QUALITY = "evidence_quality"


class TrustFactor(BaseModel):
    """One factor in the trust score breakdown. UI renders these in the
    Trust Score card next to the final offer — the 'proof we weren't fooled'
    surface that Sierra/Decagon don't expose."""
    name: TrustFactorName
    status: Literal["pass", "warn", "fail"]
    score: float = Field(ge=0, le=1, description="Factor score, weighted into trust_score")
    weight: float = Field(ge=0, le=1, description="Factor weight; weights across all factors sum to 1.0")
    detail: str = Field(description="One-line human explanation (e.g. 'no pHash collision in 1247 anchors')")
    rule_id: Optional[str] = Field(default=None,
        description="Supervisor rule that drove this factor (audit-grade backlink)")


# ─────────────────────────────────────────────────────────────
#  ClaimContext —— 流水线上的核心工件
# ─────────────────────────────────────────────────────────────
class TurnRecord(BaseModel):
    """One historical turn in the conversation, kept lightweight for prompt injection."""
    role: str  # "user" | "assistant"
    content: str
    timestamp: str
    # for assistant turns we keep a compact decision summary so future agents can reason about what was offered
    decision_summary: Optional[str] = Field(default=None, description="e.g. 'offered full_refund $24, customer accepted'")
    emotion_score: Optional[float] = None
    offer_amount_cents: Optional[int] = None
    offer_type: Optional[str] = None


class ClaimContext(BaseModel):
    """Orchestrator 把这个对象按顺序传给每个 agent。每个 agent 写入自己的字段。"""
    # 输入
    session_id: str
    user_message: str
    image_id: Optional[str] = Field(default=None, description="如果有上传，存的 image_id")
    image_bytes: Optional[bytes] = Field(default=None, exclude=True, description="不进 JSON，只内部传递")
    image_phash: Optional[str] = Field(default=None, description="64-bit perceptual hash for fraud-replay detection")
    # Multi-turn history — populated by API layer from session store
    history: list[TurnRecord] = Field(default_factory=list, description="Prior turns (oldest first), excluding the current user_message")

    # 中间产物
    intent: Optional[IntentResult] = None
    emotion: Optional[Emotion] = None
    needs: Optional[Needs] = None
    damage: Optional[DamageAssessment] = None
    offer: Optional[CompensationOffer] = None
    verification: Optional[VerificationResult] = None

    # Indicates this turn is just a clarification — short-circuit pipeline
    awaiting_clarification: bool = False
    clarification_question: Optional[str] = None

    # SupervisorAgent decision (set between CompensationAgent and VerifierAgent).
    # v6: kept as dict on the wire for callers that already read .model_dump(),
    # but supervisor.py now produces a typed SupervisorDecision and dumps it
    # here. New code should construct SupervisorDecision and call .model_dump().
    supervisor_decision: Optional[dict] = None

    # HandoffSummary populated when escalated_to_human=True (dict not Pydantic to
    # avoid circular import; the actual model is handoff.HandoffSummary).
    handoff_summary: Optional[dict] = None

    # Trust Score (Stripe-Radar-style). Populated by supervisor.compute_trust_score
    # after the IAM-style evaluate() finishes. None on pipelines that short-circuit
    # before Supervisor runs (e.g. low-confidence damage early-exit).
    trust_score: Optional[int] = Field(default=None, ge=0, le=100,
        description="0-100 weighted trust score across 5 factors")
    trust_factors: list[TrustFactor] = Field(default_factory=list,
        description="Per-factor breakdown rendered in the UI's Trust Score card")

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
