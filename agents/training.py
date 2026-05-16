"""
ClaimsForge Training Mode — flip the agents around to TRAIN human CS staff.

The same pipeline that resolves claims for customers is repurposed to:
  1. Generate a realistic, demanding customer persona (PersonaAgent)
  2. Simulate the customer's messages turn by turn (CustomerSimAgent)
  3. Grade the human trainee's replies on rubric dimensions (AssessmentAgent)
  4. Issue a final coaching report at the end of the conversation

This is the second product surface — sold to ops teams who need to
onboard / certify / continually upskill their CS staff. The same KB,
the same merchant wisdom, the same emotion calibration — but pointed
backwards at the humans, not the customers.

Coze ships this exact pattern as their "客服陪练" template. We do it
on the same multi-agent substrate as the main pipeline.
"""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from gemini_client import GeminiError, chat, structured
from unified_kb import KBEntry, KBSource, KBType, upsert, make_id
from embedding_index import hybrid_search

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
#  Personas — the universe of customer types we can simulate
# ─────────────────────────────────────────────────────────
class PersonaDifficulty(str, Enum):
    EASY = "easy"        # cooperative, simple complaint
    MEDIUM = "medium"    # frustrated, expects fairness
    HARD = "hard"        # confrontational, multi-issue, no patience
    EXPERT = "expert"    # legal threats, regulator mentions, knows tactics


class CustomerPersona(BaseModel):
    name: str
    archetype: str = Field(description="e.g. 'busy_professional' / 'first_time_buyer' / 'serial_returner' / 'gift_giver_in_a_rush'")
    difficulty: PersonaDifficulty
    backstory: str = Field(description="3-4 sentence character context — relationship to the brand, current life situation, why they're contacting support")
    opening_complaint: str = Field(description="The FIRST message they'll send to the agent (in their own voice, the language they speak)")
    hidden_goals: list[str] = Field(description="What they really want — sometimes not what they're saying")
    pain_points: list[str] = Field(description="What will set them off if mishandled")
    acceptance_criteria: list[str] = Field(description="What would resolve this in their eyes")
    language: str = Field(default="en", description="en / zh / etc")


# ─────────────────────────────────────────────────────────
#  PersonaAgent — fresh persona on demand
# ─────────────────────────────────────────────────────────
_PERSONA_SEEDS = [
    ("EASY", "First-time buyer, polite, just got a slightly damaged item, no rush, would like a refund."),
    ("EASY", "Returning customer, mug arrived chipped, simple ask, friendly tone."),
    ("MEDIUM", "Busy parent, ordered birthday gift, item arrived late AND damaged, mildly frustrated."),
    ("MEDIUM", "Small e-commerce reseller bought 5 units, 2 damaged, needs replacements fast for own customers."),
    ("MEDIUM", "Long-time customer surprised by a defective high-ticket purchase, expects VIP treatment."),
    ("HARD", "Third-time complaint same order, escalating tone, demands manager."),
    ("HARD", "Confrontational, refuses photo evidence request, threatens chargeback."),
    ("HARD", "Cross-border customer, customs problems blamed on seller, wants full refund + shipping."),
    ("EXPERT", "Mentions lawyer and consumer protection agency in first message."),
    ("EXPERT", "Threatens TikTok / social media exposure, professional tone, knows the rules."),
    ("EXPERT", "Cites the wrong policy back at you and demands compensation under it."),
]


def generate_persona(
    difficulty: Optional[PersonaDifficulty] = None,
    domain_hint: Optional[str] = None,
    language: str = "en",
) -> CustomerPersona:
    """Spin up a fresh customer persona for a training scenario."""
    seed_pool = _PERSONA_SEEDS
    if difficulty:
        seed_pool = [s for s in _PERSONA_SEEDS if s[0] == difficulty.value.upper()]
    seed_diff, seed_blurb = random.choice(seed_pool)

    domain_block = f"\nDomain focus: {domain_hint}" if domain_hint else ""
    lang_block = "\nLanguage: write the opening_complaint in Chinese." if language == "zh" else "\nLanguage: write the opening_complaint in English."

    system = (
        "You're designing a customer persona for a customer-service training simulation. "
        "Make the persona feel REAL — specific names, specific items, specific stakes. "
        "The trainee will reply to the persona; their job is to resolve the complaint while "
        "navigating the persona's actual emotional landscape. No cartoon villains; no easy wins. "
        "Calibrate to the requested difficulty."
    )
    prompt = (
        f"Difficulty: {seed_diff}\n"
        f"Inspiration: {seed_blurb}{domain_block}{lang_block}\n\n"
        f"Generate a fresh customer persona. Make the opening_complaint sound like a real first message a real human would send."
    )
    try:
        persona = structured(
            prompt=prompt,
            schema=CustomerPersona,
            system=system,
            temperature=0.9,  # we WANT variety
            max_tokens=800,
        )
        return persona
    except GeminiError as e:
        logger.warning("PersonaAgent fallback: %s", e)
        return CustomerPersona(
            name="Alex Chen",
            archetype="frustrated_buyer",
            difficulty=difficulty or PersonaDifficulty.MEDIUM,
            backstory="Recent customer, second issue this month, has a Twitter audience.",
            opening_complaint="Hi, my recent order arrived damaged. This is the second time. What are you going to do about it?",
            hidden_goals=["acknowledgement", "fair compensation"],
            pain_points=["being dismissed", "asked for redundant info"],
            acceptance_criteria=["explicit apology", "concrete next step"],
            language=language,
        )


# ─────────────────────────────────────────────────────────
#  CustomerSimAgent — keeps acting in character turn by turn
# ─────────────────────────────────────────────────────────
def simulate_next_customer_turn(
    persona: CustomerPersona,
    transcript: list[dict],  # [{role: 'customer'|'trainee', content: str}, ...]
) -> str:
    """Given the persona + prior transcript, generate the next customer message.
    Returns "" if the persona decides the conversation is over (satisfied or walked off)."""
    if not transcript:
        return persona.opening_complaint

    convo = "\n".join(f"{t['role']}: {t['content']}" for t in transcript)
    system = (
        f"You are playing the customer in a customer-service training simulation. STAY IN CHARACTER.\n\n"
        f"## Your character\n"
        f"Name: {persona.name}\n"
        f"Archetype: {persona.archetype}\n"
        f"Difficulty: {persona.difficulty.value}\n"
        f"Backstory: {persona.backstory}\n"
        f"Hidden goals (you DON'T necessarily state these explicitly): {', '.join(persona.hidden_goals)}\n"
        f"Pain points: {', '.join(persona.pain_points)}\n"
        f"What would actually satisfy you: {', '.join(persona.acceptance_criteria)}\n"
        f"Language: {'Chinese' if persona.language == 'zh' else 'English'}\n\n"
        f"Rules:\n"
        f"- Reply in 1-3 sentences, like a real customer texting support.\n"
        f"- If the trainee handled it well (apology + concrete action), soften slightly. If they handled it badly, escalate.\n"
        f"- If your acceptance criteria are clearly met and you're satisfied, end with a brief 'thanks' or 'fine, that works'.\n"
        f"- If the trainee gave you something concrete, you can pivot to a different angle (try to get more, ask follow-up, mention another item) — what real customers do.\n"
        f"- Output ONLY your customer message. No stage directions, no meta commentary."
    )
    prompt = f"## Conversation so far\n{convo}\n\n## Your next customer message:"
    try:
        return chat(prompt, system=system, temperature=0.8, max_tokens=300)
    except GeminiError as e:
        logger.warning("CustomerSim fallback: %s", e)
        return "I'm waiting for a real answer."


# ─────────────────────────────────────────────────────────
#  AssessmentAgent — grade the human trainee
# ─────────────────────────────────────────────────────────
class TurnAssessment(BaseModel):
    """Per-turn scoring."""
    empathy: int = Field(ge=0, le=10, description="Did the reply acknowledge the customer's feelings?")
    accuracy: int = Field(ge=0, le=10, description="Did the proposed action match what policy / KB suggests?")
    specificity: int = Field(ge=0, le=10, description="Concrete next steps + timeline vs vague promises?")
    professionalism: int = Field(ge=0, le=10, description="Tone calibration, no banned phrases, no over-apologizing?")
    deescalation: int = Field(ge=0, le=10, description="Did this turn defuse or escalate the customer?")
    notes: str = Field(description="2-3 sentences of coach feedback on THIS turn specifically.")
    improvement: Optional[str] = Field(default=None, description="One concrete suggestion for next time.")
    cited_kb_ids: list[str] = Field(default_factory=list, description="KB entries the reply correctly leveraged.")


class FinalReport(BaseModel):
    overall_score: int = Field(ge=0, le=100)
    avg_empathy: float
    avg_accuracy: float
    avg_specificity: float
    avg_professionalism: float
    avg_deescalation: float
    customer_outcome: str = Field(description="resolved_satisfied / resolved_grudging / churned / escalated_to_manager")
    headline: str = Field(description="One-sentence summary the trainee should remember.")
    strengths: list[str] = Field(description="What the trainee did well — be specific.")
    weaknesses: list[str] = Field(description="What needs work — be specific.")
    drills: list[str] = Field(description="3-5 concrete practice drills suggested next.")


def assess_trainee_turn(
    persona: CustomerPersona,
    transcript: list[dict],  # full transcript up to and including the latest trainee reply
    latest_trainee_reply: str,
    kb_evidence: list[KBEntry],
) -> TurnAssessment:
    """Score the trainee's most recent reply on 5 axes."""
    convo = "\n".join(f"{t['role']}: {t['content']}" for t in transcript)
    kb_block = "\n".join(
        f"  - [{e.source.value}] {e.title}: {e.decision[:120]}"
        for e in kb_evidence[:5]
    ) if kb_evidence else "  (no relevant KB entries — trainee was on their own)"

    system = (
        "You are a senior customer-service coach. Grade the trainee's latest reply on five axes (0-10 each), "
        "calibrated against the persona's expectations and what the company's KB / policy actually supports. "
        "Be honest, specific, kind. Reference the trainee's exact words when giving notes. "
        "If a KB entry was clearly the right answer and they used it, score accuracy high; if they missed it, score lower and note it."
    )
    prompt = (
        f"## Persona context\n"
        f"{persona.archetype} / {persona.difficulty.value}\n"
        f"Hidden goals: {persona.hidden_goals}\n"
        f"Acceptance criteria: {persona.acceptance_criteria}\n\n"
        f"## Full conversation\n{convo}\n\n"
        f"## Latest trainee reply (assess this one)\n{latest_trainee_reply}\n\n"
        f"## Relevant KB entries the trainee could have leveraged\n{kb_block}\n"
    )
    try:
        return structured(
            prompt=prompt,
            schema=TurnAssessment,
            system=system,
            temperature=0.3,
            max_tokens=800,
        )
    except GeminiError as e:
        logger.warning("Assess fallback: %s", e)
        return TurnAssessment(
            empathy=5, accuracy=5, specificity=5, professionalism=5, deescalation=5,
            notes="Assessment service unavailable; scored neutral.",
        )


def final_report(
    persona: CustomerPersona,
    transcript: list[dict],
    per_turn: list[TurnAssessment],
) -> FinalReport:
    """End-of-session coaching report."""
    if not per_turn:
        # Empty session — return a placeholder
        return FinalReport(
            overall_score=0,
            avg_empathy=0, avg_accuracy=0, avg_specificity=0,
            avg_professionalism=0, avg_deescalation=0,
            customer_outcome="incomplete",
            headline="No trainee turns recorded.",
            strengths=[], weaknesses=[],
            drills=["Run a full session to receive a real report."],
        )

    avg = lambda key: sum(getattr(a, key) for a in per_turn) / len(per_turn)
    avg_e, avg_a, avg_s, avg_p, avg_d = avg("empathy"), avg("accuracy"), avg("specificity"), avg("professionalism"), avg("deescalation")
    overall = int((avg_e + avg_a + avg_s + avg_p + avg_d) * 2)  # 0-100

    convo = "\n".join(f"{t['role']}: {t['content']}" for t in transcript)
    per_turn_block = "\n".join(
        f"Turn {i+1}: E={a.empathy} A={a.accuracy} S={a.specificity} P={a.professionalism} D={a.deescalation} — {a.notes}"
        for i, a in enumerate(per_turn)
    )

    system = (
        "You're writing the final coaching report after a CS training session. The trainee will read this and "
        "improve based on what you say. Be specific (cite exact phrases), kind, and actionable. "
        "Suggest 3-5 concrete drills they could practice based on the weaknesses you saw."
    )
    prompt = (
        f"## Persona\n{persona.archetype} / {persona.difficulty.value}\n"
        f"Acceptance criteria: {persona.acceptance_criteria}\n\n"
        f"## Full conversation\n{convo}\n\n"
        f"## Per-turn assessments\n{per_turn_block}\n\n"
        f"## Aggregate\nempathy={avg_e:.1f} accuracy={avg_a:.1f} specificity={avg_s:.1f} professionalism={avg_p:.1f} deescalation={avg_d:.1f}\n"
        f"Overall: {overall}/100"
    )
    try:
        report = structured(
            prompt=prompt,
            schema=FinalReport,
            system=system,
            temperature=0.4,
            max_tokens=1200,
        )
        # Override numerical fields from our actual computation
        report.overall_score = overall
        report.avg_empathy = round(avg_e, 1)
        report.avg_accuracy = round(avg_a, 1)
        report.avg_specificity = round(avg_s, 1)
        report.avg_professionalism = round(avg_p, 1)
        report.avg_deescalation = round(avg_d, 1)
        return report
    except GeminiError as e:
        logger.warning("FinalReport fallback: %s", e)
        return FinalReport(
            overall_score=overall,
            avg_empathy=round(avg_e, 1),
            avg_accuracy=round(avg_a, 1),
            avg_specificity=round(avg_s, 1),
            avg_professionalism=round(avg_p, 1),
            avg_deescalation=round(avg_d, 1),
            customer_outcome="unknown",
            headline="Report service unavailable; turn-level scores still saved.",
            strengths=[], weaknesses=[],
            drills=[],
        )


# ─────────────────────────────────────────────────────────
#  Session store + KB write-back of high-quality sessions
# ─────────────────────────────────────────────────────────
class TrainingSession(BaseModel):
    session_id: str
    persona: CustomerPersona
    transcript: list[dict] = Field(default_factory=list)  # [{role, content, timestamp}]
    assessments: list[TurnAssessment] = Field(default_factory=list)
    final: Optional[FinalReport] = None
    started_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    closed_at: Optional[str] = None


_sessions: dict[str, TrainingSession] = {}


def create_session(
    difficulty: Optional[PersonaDifficulty] = None,
    domain_hint: Optional[str] = None,
    language: str = "en",
) -> TrainingSession:
    persona = generate_persona(difficulty=difficulty, domain_hint=domain_hint, language=language)
    sid = make_id(f"train-{persona.name}-{time.time()}")
    session = TrainingSession(session_id=sid, persona=persona)
    session.transcript.append({
        "role": "customer",
        "content": persona.opening_complaint,
        "timestamp": datetime.now().isoformat(),
    })
    _sessions[sid] = session
    return session


def get_session(sid: str) -> Optional[TrainingSession]:
    return _sessions.get(sid)


def submit_trainee_reply(sid: str, reply: str) -> dict:
    """Trainee submits a reply → record it, assess it, simulate customer next turn."""
    s = _sessions.get(sid)
    if not s:
        raise ValueError(f"session {sid} not found")
    now = datetime.now().isoformat()
    s.transcript.append({"role": "trainee", "content": reply, "timestamp": now})

    # Retrieve relevant KB so the assessor can see what evidence the trainee should've used
    query = f"{s.persona.archetype} {reply[:200]} {' '.join(s.persona.hidden_goals)}"
    kb_results = hybrid_search(query, top_k=5, threshold=0.5)
    kb_entries = [e for e, _, _ in kb_results]

    assessment = assess_trainee_turn(s.persona, s.transcript, reply, kb_entries)
    s.assessments.append(assessment)

    # Simulate customer's next turn (may be empty if customer is "done")
    next_customer = simulate_next_customer_turn(s.persona, s.transcript)
    if next_customer:
        s.transcript.append({"role": "customer", "content": next_customer, "timestamp": datetime.now().isoformat()})

    return {
        "assessment": assessment.model_dump(),
        "next_customer_message": next_customer,
        "transcript_length": len(s.transcript),
        "cited_kb_ids": [e.id for e in kb_entries],
    }


def close_session(sid: str) -> FinalReport:
    s = _sessions.get(sid)
    if not s:
        raise ValueError(f"session {sid} not found")
    report = final_report(s.persona, s.transcript, s.assessments)
    s.final = report
    s.closed_at = datetime.now().isoformat()

    # If session was high-quality (≥75/100), write it back to unified_kb as a learned case
    if report.overall_score >= 75:
        try:
            entry = KBEntry(
                id=make_id(f"training-case-{sid}"),
                source=KBSource.LEARNED_CASE,
                type=KBType.CASE,
                domain="training",
                title=f"Trainee handled {s.persona.difficulty.value} {s.persona.archetype}: {report.overall_score}/100",
                scenario=s.persona.opening_complaint[:300],
                decision=report.headline,
                rationale=" · ".join(report.strengths[:3]),
                tags=["training", s.persona.difficulty.value, s.persona.archetype]
                     + (report.strengths[:3] if report.strengths else []),
                contributor="training_system",
                quality_score=report.overall_score / 100,
            )
            upsert(entry)
        except Exception as e:
            logger.warning("KB writeback failed: %s", e)

    return report
