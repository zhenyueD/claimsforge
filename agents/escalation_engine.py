"""
ESCALATION DECISION ENGINE
处理升级客诉、赔偿决策（<50元自主，>=50元推微信审批）
"""
import json
import re
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from utils import safe_save_json, safe_load_json, extract_compensation_amount

BASE = Path(__file__).parent.parent
STATE_PATH = BASE / "data" / "state.json"
TICKETS_PATH = BASE / "data" / "tickets.json"
NOTIFY_QUEUE_PATH = BASE / "data" / "notify_queue.json"

def _queue_wechat_notify(content: str):
    """写入微信通知队列，由API层通过EasyClaw wecom_mcp发送"""
    try:
        queue = safe_load_json(NOTIFY_QUEUE_PATH, [])
        queue.append({
            "content": content,
            "queued_at": datetime.now().isoformat(),
            "sent": False
        })
        safe_save_json(NOTIFY_QUEUE_PATH, queue)
    except Exception as e:
        print(f"[WARN] 通知队列写入失败: {e}", file=sys.stderr)

COMPENSATION_THRESHOLD = 50.0  # 元
COMP_LIMIT_PER_DAY = 1         # 同 session 每日最多自动赔偿次数（M-04）

def load_tickets():
    data = safe_load_json(TICKETS_PATH, [])
    if isinstance(data, dict):
        return data.get("tickets", [])
    return data if isinstance(data, list) else []

def save_tickets(tickets):
    safe_save_json(TICKETS_PATH, tickets)


def _check_compensation_limit(session_id: str, tickets: list) -> bool:
    """检查该 session 今日是否已达自动赔偿上限（M-04）"""
    today = datetime.now().date().isoformat()
    count = sum(1 for t in tickets
                if t.get("session_id") == session_id
                and t.get("created_at", "")[:10] == today
                and t.get("decision", {}).get("action") == "AUTO_APPROVE")
    return count < COMP_LIMIT_PER_DAY


def handle_escalation(session_id: str, user_message: str, escalation_info: dict, emotion: dict, needs: dict) -> dict:
    # 修复 M-05：工单 ID 加 uuid 后缀避免同秒碰撞
    ticket_id = f"TKT-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"

    # 修复 S-03：优先使用 escalation_info 中由 utils.extract_compensation_amount 提取的金额
    amount = escalation_info.get("compensation_hint")
    if not amount:
        amount = extract_compensation_amount(user_message)

    decision = {}
    tickets = load_tickets()

    if escalation_info["type"] == "COMPENSATION" and amount:
        # 频率限制检查（M-04）
        within_limit = _check_compensation_limit(session_id, tickets)

        if amount < COMPENSATION_THRESHOLD and within_limit:
            # AI自主决策
            decision = {
                "action": "AUTO_APPROVE",
                "amount": amount,
                "reason": f"金额{amount}元低于{COMPENSATION_THRESHOLD}元阈值，自动批准",
                "reply_to_user": f"您好，经核实您的诉求，我们决定给予您 ¥{amount} 的补偿，将在1-2个工作日内到账。感谢您的理解与支持。",
                "requires_human": False
            }
        elif not within_limit:
            # 超频转人工
            decision = {
                "action": "PENDING_APPROVAL",
                "amount": amount,
                "reason": f"该账号今日已达自动赔偿上限，转人工审核",
                "reply_to_user": "您的请求已提交审核，广服组长将在 5 分钟内与您联系。",
                "requires_human": True,
                "wechat_notify": True,
                "notify_content": f"⚠️ 重复赔偿请求\n工单：{ticket_id}\nsession：{session_id}\n金额：¥{amount}\n需人工核实"
            }
        else:
            # 需要人工审批（微信通知）
            decision = {
                "action": "PENDING_APPROVAL",
                "amount": amount,
                "reason": f"金额{amount}元超过{COMPENSATION_THRESHOLD}元阈值，需人工审批",
                "reply_to_user": "您的赔偿申请已提交给客服组长审核，预计2分钟内回复您，请稍候。",
                "requires_human": True,
                "wechat_notify": True,
                "notify_content": f"⚠️ 赔偿审批请求\n工单号：{ticket_id}\n用户诉求：{user_message[:80]}\n申请金额：¥{amount}\n情绪评分：{emotion['score']}/10\n请审批：[批准] [拒绝] [协商]"
            }
    elif escalation_info["type"] == "EMOTION_CRISIS":
        decision = {
            "action": "SUPERVISOR_TAKEOVER",
            "reason": f"情绪评分{emotion['score']}，高风险",
            "reply_to_user": "您的情绪我们完全理解，客服组长已介入，将给您最优先的处理。",
            "requires_human": True,
            "wechat_notify": emotion["score"] <= 2.0,  # 极端情绪才微信通知
            "notify_content": f"🚨 高风险客诉\n工单号：{ticket_id}\n情绪评分：{emotion['score']}/10\n用户消息：{user_message[:80]}\n需要介入处理"
        }
    else:
        decision = {
            "action": "FORMAL_COMPLAINT",
            "reason": "用户发起正式投诉",
            "reply_to_user": "您的投诉已正式受理，工单号：" + ticket_id + "，我们将在24小时内给出处理结果。",
            "requires_human": True,
            "wechat_notify": False
        }

    # 保存工单（复用上面 load_tickets 的结果，load_tickets 在上面已调用）
    ticket = {
        "id": ticket_id,
        "session_id": session_id,
        "type": escalation_info["type"],
        "message": user_message,
        "emotion": emotion,
        "needs": needs,
        "decision": decision,
        "status": "PENDING" if decision.get("requires_human") else "RESOLVED",
        "created_at": datetime.now().isoformat(),
        "resolved_at": None if decision.get("requires_human") else datetime.now().isoformat()
    }
    tickets.append(ticket)
    save_tickets(tickets)

    return {
        "ticket_id": ticket_id,
        "decision": decision,
        "ticket": ticket
    }

if __name__ == "__main__":
    result = handle_escalation(
        "sess-001",
        "你们服务中断导致我损失了200元，必须赔偿！",
        {"type": "COMPENSATION", "compensation_hint": 200.0, "sla_minutes": 2},
        {"score": 2.5, "risk": "HIGH", "label": "愤怒"},
        {"surface_need": "索要赔偿", "latent_need": "损失未被承认"}
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
