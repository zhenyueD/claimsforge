"""
CONVERSATION ENGINE v2
整合：RESPONDER + ANALYZER(情绪评分+需求挖掘) + RAG检索 + LLM生成
单次调用完成对话响应，输出结构化事件

新增：
  - LLM 回复生成（OpenAI 兼容接口）
  - RAG 召回上下文喂给 LLM
  - 失败时自动降级至模板回复
  - 可通过 llm_config.py 调整所有参数
"""
import json
import re
import math
import time
from datetime import datetime
from pathlib import Path

from utils import safe_save_json, safe_load_json, update_json, extract_compensation_amount, has_injection_risk, is_trivial_message

BASE = Path(__file__).parent.parent
KB_PATH   = BASE / "data" / "knowledge_base.json"
STATE_PATH = BASE / "data" / "state.json"
GAPS_PATH  = BASE / "data" / "gaps.json"

# ── 升级触发词（中英文）──────────────────────────────────
ESCALATION_KEYWORDS  = [
    "赔偿", "起诉", "律师", "投诉平台", "曝光", "媒体", "12315", "消协", "骗子", "退钱",
    "sue", "lawyer", "attorney", "refund my money", "scam", "fraud",
    "compensate", "compensation", "reimburse",
]
COMPENSATION_KEYWORDS = [
    "赔偿", "补偿", "赔我", "赔款", "索赔",
    "compensate", "compensation", "reimburse", "pay me", "refund me",
]
HIGH_ANGER_WORDS      = [
    "垃圾", "坑爹", "骗人", "滚", "废物", "烂", "差劲", "无能", "混蛋", "气死", "骗子", "去你的",
    "garbage", "trash", "liar", "useless", "terrible", "awful", "scam",
]
NEGATIVE_WORDS        = [
    "不满", "失望", "差评", "问题", "错误", "故障", "无法", "不行", "不好", "烦", "急", "等了", "还没",
    "broken", "error", "problem", "issue", "bad",
]
POSITIVE_WORDS        = [
    "谢谢", "感谢", "好的", "明白", "解决了", "满意", "棒", "不错", "完美", "很好",
    "thanks", "thank you", "great", "perfect", "good", "nice",
]


# ─────────────────────────────────────────────────────────────
#  数据读写
# ─────────────────────────────────────────────────────────────
def load_kb():
    return safe_load_json(KB_PATH, {"entries": [], "gaps": [], "categories": {}})

def load_state():
    return safe_load_json(STATE_PATH, {"stats": {"total_conversations": 0, "kb_hits": 0, "kb_misses": 0, "escalations": 0}})

def save_state(state):
    safe_save_json(STATE_PATH, state)

def load_gaps():
    return safe_load_json(GAPS_PATH, [])

def save_gaps(gaps):
    safe_save_json(GAPS_PATH, gaps)


# ─────────────────────────────────────────────────────────────
#  RAG 检索（关键词 + 可被向量检索替代）
# ─────────────────────────────────────────────────────────────
def rag_search(query: str, top_k: int = 3):
    """先尝试向量检索（需要 vector_store.py），失败则降级关键词匹配"""
    try:
        from vector_store import vector_search
        results, confidence = vector_search(query, top_k=top_k)
        if results:
            return results, confidence
    except Exception:
        pass
    return _keyword_search(query, top_k)


def _keyword_search(query: str, top_k: int = 3):
    kb = load_kb()
    scores = []
    for entry in kb["entries"]:
        score = 0
        for kw in entry["keywords"]:
            if kw in query:
                score += 3
        q_words   = set(entry["question"])
        query_words = set(query)
        overlap   = len(q_words & query_words)
        score    += overlap * 0.5
        # 修复 R-03：使用 2-gram 词列表而非单字迭代
        bigrams = [query[i:i+2] for i in range(len(query)-1)]
        if any(w in entry["answer"] for w in bigrams):
            score += 1
        if score > 0:
            scores.append((score, entry))

    scores.sort(key=lambda x: x[0], reverse=True)
    results    = [e for _, e in scores[:top_k]]
    confidence = 0.0
    if scores:
        top_score  = scores[0][0]
        confidence = min(0.99, top_score / 10.0)
    return results, confidence


# ─────────────────────────────────────────────────────────────
#  情绪评分
# ─────────────────────────────────────────────────────────────
def analyze_emotion(text: str, history: list = None) -> dict:
    score = 5.0

    for w in HIGH_ANGER_WORDS:
        if w in text: score -= 2.0
    for w in NEGATIVE_WORDS:
        if w in text: score -= 0.8
    for w in POSITIVE_WORDS:
        if w in text: score += 1.0

    score -= text.count("！") * 0.5
    score -= text.count("!") * 0.5
    score -= text.count("？？") * 0.8
    score -= text.count("??") * 0.8

    # 修复 M-08：多轮情绪趋势（负面趋势递减锁定）
    if history:
        recent_scores = [h.get("emotion_score") for h in history[-4:]
                         if h.get("role") == "user" and h.get("emotion_score") is not None]
        if len(recent_scores) >= 2:
            avg = sum(recent_scores) / len(recent_scores)
            if avg < 4.5:        # 多轮低分 → 额外扣 1
                score -= 1.0
            if avg < 3.0:        # 多轮极低 → 再扣 1
                score -= 1.0
            # 负面趋势：连续三轮都低于 5
            if len(recent_scores) >= 3 and all(s <= 5 for s in recent_scores[-3:]):
                score -= 0.8

    score = max(0.0, min(10.0, score))
    risk  = "HIGH" if score <= 3.5 else ("MEDIUM" if score <= 6.0 else "LOW")
    return {
        "score": round(score, 1),
        "risk": risk,
        "label": "愤怒" if score <= 3.5 else ("焦虑" if score <= 6.0 else "平静")
    }


# ─────────────────────────────────────────────────────────────
#  需求挖掘
# ─────────────────────────────────────────────────────────────
def dig_needs(text: str, emotion: dict) -> dict:
    surface = "未知"
    latent  = "未知"
    emotional_need  = "希望得到帮助"
    retention_score = 5.0

    if any(w in text for w in ["退款", "退钱", "退费", "退订"]):
        surface = "申请退款"
        latent  = "对产品/服务不满，寻求补偿"
        emotional_need  = "希望感受到被重视和公平对待"
        retention_score = 3.0
    elif any(w in text for w in COMPENSATION_KEYWORDS):
        surface = "索要赔偿"
        latent  = "损失未被承认，寻求正式补偿"
        emotional_need  = "希望损失被认可并获得正式道歉"
        retention_score = 2.0
    elif any(w in text for w in ["无法", "登录", "故障", "错误", "崩溃", "卡"]):
        surface = "技术问题求助"
        latent  = "需要快速恢复正常使用"
        emotional_need  = "希望问题被高优先处理，不被敷衍"
        retention_score = 6.0
    elif any(w in text for w in ["支持", "可以", "能否", "功能", "如何", "怎么"]):
        surface = "功能咨询"
        latent  = "评估产品是否满足需求"
        emotional_need  = "希望获得准确专业的信息"
        retention_score = 7.5
    elif any(w in text for w in ["投诉", "差评", "举报", "曝光"]):
        surface = "正式投诉"
        latent  = "体验极差，寻求改变或发泄出口"
        emotional_need  = "希望被认真对待，而非被推诿"
        retention_score = 1.5

    if emotion["score"] <= 3.5:
        retention_score = max(1.0, retention_score - 2.0)
    elif emotion["score"] >= 7.0:
        retention_score = min(9.0, retention_score + 1.5)

    return {
        "surface_need":   surface,
        "latent_need":    latent,
        "emotional_need": emotional_need,
        "retention_score": round(retention_score, 1),
        "suggested_tone": "共情优先，先道歉再解决" if emotion["score"] <= 5 else "专业高效，直接给出解决方案"
    }


# ─────────────────────────────────────────────────────────────
#  升级检测
# ─────────────────────────────────────────────────────────────
def detect_escalation(text: str, emotion: dict) -> dict:
    need_escalate = False
    escalation_type = None
    compensation_amount_hint = None

    is_compensation = any(w in text for w in COMPENSATION_KEYWORDS)
    # 修复 S-03：精确提取金额（必须带货币/赔偿语义）
    if is_compensation:
        compensation_amount_hint = extract_compensation_amount(text)

    if is_compensation:
        need_escalate   = True
        escalation_type = "COMPENSATION"
    elif emotion["risk"] == "HIGH":
        need_escalate   = True
        escalation_type = "EMOTION_CRISIS"
    elif any(w in text for w in ESCALATION_KEYWORDS):
        need_escalate   = True
        escalation_type = "FORMAL_COMPLAINT"

    return {
        "need_escalate":    need_escalate,
        "type":             escalation_type,
        "compensation_hint": compensation_amount_hint,
        "sla_minutes":      2 if escalation_type == "COMPENSATION" else 5
    }


# ─────────────────────────────────────────────────────────────
#  LLM 回复生成（OpenAI 兼容，失败降级）
# ─────────────────────────────────────────────────────────────
def _build_llm_context(query: str, kb_results: list, emotion: dict, needs: dict) -> list:
    """将 RAG 召回内容 + 情绪/需求分析拼成 LLM 消息列表"""
    from llm_config import SYSTEM_PROMPT

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # 知识库上下文
    if kb_results:
        kb_ctx = "【相关知识库条目】\n"
        for entry in kb_results:
            kb_ctx += (
                f"- [{entry['id']}] {entry['category']}：{entry['question']}\n"
                f"  答案：{entry['answer']}\n"
            )
        messages.append({"role": "system", "content": kb_ctx})

    # 情绪 & 需求上下文
    ctx = (
        f"【当前用户状态】\n"
        f"情绪评分：{emotion['score']}/10（{emotion['label']}，风险：{emotion['risk']}）\n"
        f"表层需求：{needs['surface_need']}\n"
        f"潜在需求：{needs['latent_need']}\n"
        f"话术建议：{needs['suggested_tone']}\n"
    )
    messages.append({"role": "system", "content": ctx})
    messages.append({"role": "user", "content": query})
    return messages


def _call_llm(messages: list, max_retries: int = 2) -> str | None:
    """调用 LLM，成功返回文本，失败返回 None。带重试（M-09）"""
    from llm_config import (LLM_ENABLED, LLM_API_KEY, LLM_BASE_URL,
                            LLM_MODEL, LLM_TEMPERATURE, LLM_MAX_TOKENS, LLM_TIMEOUT)
    if not LLM_ENABLED or not LLM_API_KEY:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        print("[LLM] openai 库未安装，降级模板")
        return None

    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL, timeout=LLM_TIMEOUT)
    last_err = None
    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=LLM_MODEL,
                messages=messages,
                temperature=LLM_TEMPERATURE,
                max_tokens=LLM_MAX_TOKENS,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(0.5 * (attempt + 1))  # 指数退避
    print(f"[LLM] 重试 {max_retries} 次后失败，降级模板：{last_err}")
    return None


# ─────────────────────────────────────────────────────────────
#  模板回复（LLM 降级兜底）
# ─────────────────────────────────────────────────────────────
def _template_reply(query: str, kb_results: list, confidence: float,
                    emotion: dict, needs: dict, escalation: dict) -> str:

    if escalation["need_escalate"]:
        if escalation["type"] == "COMPENSATION":
            return (
                f"非常抱歉给您带来了困扰。您反映的赔偿诉求我们高度重视，"
                f"已为您转接专属客服组长处理，预计 {escalation['sla_minutes']} 分钟内响应。"
                f"请稍候，我们一定给您一个满意的答复。"
            )
        elif escalation["type"] == "EMOTION_CRISIS":
            return (
                f"非常抱歉让您有这样的体验，我深感歉意。"
                f"您的问题已被列为最高优先级，客服组长将在 {escalation['sla_minutes']} 分钟内直接联系您处理。"
                f"感谢您的耐心等待。"
            )
        else:
            return (
                f"您好，我理解您目前的感受，非常抱歉。"
                f"您的诉求已提交至专属处理团队，我们会尽快与您联系。"
            )

    if confidence < 0.3 or not kb_results:
        return (
            f"感谢您的提问。您咨询的问题我需要进一步确认，"
            f"已记录至待解答列表，将在1个工作日内回复您。"
            f"如有紧急需求请联系人工客服。"
        )

    best = kb_results[0]
    tone_prefix = ""
    if needs["suggested_tone"].startswith("共情"):
        tone_prefix = "非常理解您的情况，我来帮您解答。"

    reply = f"{tone_prefix}{best['answer']}"
    if len(kb_results) > 1:
        reply += f"\n\n如需了解更多相关信息（如{kb_results[1]['category']}），欢迎继续提问。"
    return reply


# ─────────────────────────────────────────────────────────────
#  generate_reply：LLM 优先，降级模板
# ─────────────────────────────────────────────────────────────
def generate_reply(query: str, kb_results: list, confidence: float,
                   emotion: dict, needs: dict, escalation: dict) -> tuple[str, bool]:
    """
    返回 (reply_text, used_llm)
    升级场景直接走模板，不消耗 LLM token
    """
    if escalation["need_escalate"]:
        return _template_reply(query, kb_results, confidence, emotion, needs, escalation), False

    # 尝试 LLM
    messages  = _build_llm_context(query, kb_results, emotion, needs)
    llm_reply = _call_llm(messages)
    if llm_reply:
        # 去掉 LLM 可能自己加的内部 ID 标识
        llm_reply = llm_reply.replace('📎 来源：', '').strip()
        return llm_reply, True

    # 降级
    return _template_reply(query, kb_results, confidence, emotion, needs, escalation), False


# ─────────────────────────────────────────────────────────────
#  主入口
# ─────────────────────────────────────────────────────────────
def process_message(session_id: str, user_message: str, history: list = None) -> dict:
    if history is None:
        history = []

    # 1. RAG检索（向量优先，降级关键词）
    kb_results, confidence = rag_search(user_message)

    # 2. 情绪分析
    emotion = analyze_emotion(user_message, history)

    # 3. 需求挖掘
    needs = dig_needs(user_message, emotion)

    # 4. 升级判断
    escalation = detect_escalation(user_message, emotion)

    # 5. 生成回复（LLM 优先，模板兜底）
    reply, used_llm = generate_reply(user_message, kb_results, confidence,
                                     emotion, needs, escalation)

    # 6. 原子更新统计（修复并发丢失：RMW 全程加锁）
    with update_json(STATE_PATH, default={"stats": {"total_conversations": 0, "kb_hits": 0, "kb_misses": 0, "escalations": 0}}) as state:
        state.setdefault("stats", {})
        s = state["stats"]
        s["total_conversations"] = s.get("total_conversations", 0) + 1
        if confidence >= 0.3:
            s["kb_hits"] = s.get("kb_hits", 0) + 1
        else:
            s["kb_misses"] = s.get("kb_misses", 0) + 1
        if escalation["need_escalate"]:
            s["escalations"] = s.get("escalations", 0) + 1

    # 7. 记录知识缺口（跳过寒暄/无意义词，修复污染问题）
    if confidence < 0.3 and not is_trivial_message(user_message):
        with update_json(GAPS_PATH, default=[]) as gaps:
            gaps.append({
                "query":      user_message,
                "session_id": session_id,
                "timestamp":  datetime.now().isoformat()
            })

    return {
        "session_id":  session_id,
        "reply":       reply,
        "used_llm":    used_llm,
        "emotion":     emotion,
        "needs":       needs,
        "escalation":  escalation,
        "kb_results":  [{"id": r["id"], "category": r["category"],
                         "question": r["question"]} for r in kb_results],
        "confidence":  round(confidence, 2),
        "timestamp":   datetime.now().isoformat()
    }


if __name__ == "__main__":
    result = process_message("test-001", "我要退款！等了3天还没到账，你们是骗子吗！！")
    print(json.dumps(result, ensure_ascii=False, indent=2))
