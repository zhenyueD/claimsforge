"""
OBSERVER
持续监控全链路运行状态，每日生成报告
整合：INSPECTOR(全链路观察) + TRACKER(满意度) + COACH(案例分析)
"""
import json
from datetime import datetime, date
from pathlib import Path

BASE = Path(__file__).parent.parent
STATE_PATH = BASE / "data" / "state.json"
TICKETS_PATH = BASE / "data" / "tickets.json"
KB_PATH = BASE / "data" / "knowledge_base.json"
REPORTS_DIR = BASE / "reports"
SATISFACTION_PATH = BASE / "data" / "satisfaction.json"

def load_json(path, default=None):
    if not Path(path).exists():
        return default if default is not None else {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def submit_satisfaction(session_id: str, ticket_id: str, score: int, comment: str = "") -> dict:
    """用户提交满意度评分 1-5"""
    satisfactions = load_json(SATISFACTION_PATH, [])
    record = {
        "session_id": session_id,
        "ticket_id": ticket_id,
        "score": max(1, min(5, score)),
        "comment": comment,
        "timestamp": datetime.now().isoformat()
    }
    satisfactions.append(record)
    save_json(SATISFACTION_PATH, satisfactions)

    # 差评触发知识缺口分析
    if score <= 2:
        _flag_poor_rating(ticket_id, comment)

    # 更新平均满意度
    state = load_json(STATE_PATH)
    scores = [s["score"] for s in satisfactions]
    state["stats"]["avg_satisfaction"] = round(sum(scores) / len(scores), 2)
    save_json(STATE_PATH, state)

    return record

def _flag_poor_rating(ticket_id: str, comment: str):
    kb = load_json(KB_PATH)
    kb.setdefault("gaps", []).append({
        "query": f"[差评反馈] {comment}",
        "ticket_id": ticket_id,
        "timestamp": datetime.now().isoformat(),
        "source": "poor_rating"
    })
    save_json(KB_PATH, kb)

def get_health_status() -> dict:
    """获取各模块健康度"""
    state = load_json(STATE_PATH)
    tickets = load_json(TICKETS_PATH, [])
    kb = load_json(KB_PATH)
    satisfactions = load_json(SATISFACTION_PATH, [])

    stats = state.get("stats", {})
    total = stats.get("total_conversations", 0)
    escalations = stats.get("escalations", 0)
    kb_hits = stats.get("kb_hits", 0)
    kb_misses = stats.get("kb_misses", 0)
    avg_sat = stats.get("avg_satisfaction", 0)

    # 各模块评分
    hit_rate = kb_hits / max(1, kb_hits + kb_misses)
    escalation_rate = escalations / max(1, total)
    
    # 健康度判断
    def health(score):
        if score >= 0.8: return "🟢 健康"
        if score >= 0.5: return "🟡 注意"
        return "🔴 告警"

    pending_tickets = [t for t in tickets if t.get("status") == "PENDING"]

    return {
        "conversation_engine": {
            "status": health(hit_rate),
            "kb_hit_rate": f"{hit_rate:.1%}",
            "total_conversations": total
        },
        "knowledge_pipeline": {
            "status": health(1 - len(kb.get("gaps", [])) / max(1, total)),
            "total_entries": len(kb.get("entries", [])),
            "pending_gaps": len(kb.get("gaps", []))
        },
        "escalation_engine": {
            "status": health(1 - escalation_rate),
            "escalation_rate": f"{escalation_rate:.1%}",
            "pending_tickets": len(pending_tickets)
        },
        "satisfaction": {
            "status": health(avg_sat / 5),
            "avg_score": f"{avg_sat:.1f}/5.0",
            "total_ratings": len(satisfactions)
        }
    }

def generate_daily_report(send_email: bool = True) -> dict:
    """生成日报"""
    today = date.today().isoformat()
    state = load_json(STATE_PATH)
    tickets = load_json(TICKETS_PATH, [])
    kb = load_json(KB_PATH)
    satisfactions = load_json(SATISFACTION_PATH, [])
    health = get_health_status()

    # 今日工单
    today_tickets = [t for t in tickets if t.get("created_at", "").startswith(today)]
    resolved = [t for t in today_tickets if t.get("status") == "RESOLVED"]
    escalated = [t for t in today_tickets if t.get("type") in ["COMPENSATION", "EMOTION_CRISIS"]]

    # 差评根因分析
    poor_ratings = [s for s in satisfactions if s["score"] <= 2]
    poor_comments = [s["comment"] for s in poor_ratings if s["comment"]]

    # 生成COACH案例分析建议
    coach_suggestions = []
    if kb.get("gaps"):
        top_gaps = kb["gaps"][-5:]
        coach_suggestions.append(f"未命中问题TOP{len(top_gaps)}，建议补充知识库条目")
    if len(escalated) > 3:
        coach_suggestions.append("今日升级率偏高，建议分析升级触发词，优化RESPONDER话术")
    if poor_ratings:
        coach_suggestions.append(f"今日{len(poor_ratings)}条差评，建议在明日早会重点讨论")

    report = {
        "date": today,
        "generated_at": datetime.now().isoformat(),
        "summary": {
            "total_conversations": state.get("stats", {}).get("total_conversations", 0),
            "today_tickets": len(today_tickets),
            "resolved_tickets": len(resolved),
            "escalated_tickets": len(escalated),
            "avg_satisfaction": state.get("stats", {}).get("avg_satisfaction", 0)
        },
        "health_status": health,
        "knowledge_gaps": kb.get("gaps", [])[-10:],
        "poor_rating_comments": poor_comments,
        "coach_suggestions": coach_suggestions,
        "action_items": [
            {"priority": "HIGH", "item": s} for s in coach_suggestions
        ]
    }

    # 保存报告
    REPORTS_DIR.mkdir(exist_ok=True)
    report_path = REPORTS_DIR / f"{today}.json"
    save_json(report_path, report)

    # 同时生成Markdown版本
    md_content = _render_report_md(report)
    md_path = REPORTS_DIR / f"{today}.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    if send_email:
        _send_email_report(report, md_content)

    return report

def _render_report_md(report: dict) -> str:
    s = report["summary"]
    h = report["health_status"]
    lines = [
        f"# 📊 AI客服团队日报 — {report['date']}",
        "",
        "## 今日概览",
        f"- 总对话数：{s['total_conversations']}",
        f"- 今日工单：{s['today_tickets']}（已解决 {s['resolved_tickets']}）",
        f"- 升级工单：{s['escalated_tickets']}",
        f"- 平均满意度：{s['avg_satisfaction']:.1f}/5.0",
        "",
        "## 模块健康度",
        f"- 对话引擎：{h['conversation_engine']['status']} | 知识库命中率 {h['conversation_engine']['kb_hit_rate']}",
        f"- 知识管道：{h['knowledge_pipeline']['status']} | 待补充缺口 {h['knowledge_pipeline']['pending_gaps']} 条",
        f"- 升级引擎：{h['escalation_engine']['status']} | 升级率 {h['escalation_engine']['escalation_rate']}",
        f"- 满意度：{h['satisfaction']['status']} | 均分 {h['satisfaction']['avg_score']}",
        "",
    ]
    if report["coach_suggestions"]:
        lines += ["## 🎯 COACH改进建议"]
        for s in report["coach_suggestions"]:
            lines.append(f"- {s}")
        lines.append("")
    if report["knowledge_gaps"]:
        lines += ["## 📚 知识库缺口（最近10条）"]
        for g in report["knowledge_gaps"]:
            lines.append(f"- `{g.get('query', '')[:60]}`")
        lines.append("")
    lines += [
        "---",
        f"*由 OBSERVER 自动生成 · {report['generated_at']}*"
    ]
    return "\n".join(lines)

def _send_email_report(report: dict, md_content: str):
    """发送日报邮件（实际环境需配置SMTP）"""
    # 这里写入待发送队列，由API层处理实际发送
    email_queue_path = BASE / "data" / "email_queue.json"
    queue = load_json(email_queue_path, [])
    queue.append({
        "to": "23992721@qq.com",
        "subject": f"AI客服团队日报 {report['date']}",
        "body": md_content,
        "queued_at": datetime.now().isoformat(),
        "sent": False
    })
    save_json(email_queue_path, queue)

def generate_morning_brief() -> str:
    """生成早会晨报（每日08:00）"""
    today = date.today().isoformat()
    yesterday_reports = list(Path(REPORTS_DIR).glob("*.json"))
    yesterday_reports.sort()
    
    health = get_health_status()
    kb = load_json(KB_PATH)
    gaps = kb.get("gaps", [])

    brief_lines = [
        f"# ☀️ 早会晨报 — {today}",
        "",
        "## 昨日数据速览",
        f"- 知识库命中率：{health['conversation_engine']['kb_hit_rate']}",
        f"- 待处理工单：{health['escalation_engine']['pending_tickets']}",
        f"- 满意度均分：{health['satisfaction']['avg_score']}",
        f"- 知识缺口数：{len(gaps)}",
        "",
        "## 今日议程",
        "1. 📚 KNOWLEDGE：待补充缺口 " + str(len(gaps)) + " 条，讨论优先级",
        "2. 🤖 CONVERSATION ENGINE：昨日未命中问题复盘",
        "3. 🚨 ESCALATION：升级案例回顾，赔偿决策质量",
        "4. 📊 OBSERVER：数据看板解读，改进方向",
        "",
        "## 行动项",
    ]
    
    if gaps:
        brief_lines.append(f"- [ ] 补充知识库缺口（{len(gaps)}条）")
    if health["escalation_engine"]["pending_tickets"] != "0":
        brief_lines.append(f"- [ ] 处理待决工单")
    brief_lines.append("- [ ] 确认今日SLA目标")
    
    brief_lines += ["", "---", "*OBSERVER 自动生成 · 早会前5分钟推送*"]
    
    content = "\n".join(brief_lines)
    
    # 保存晨报
    REPORTS_DIR.mkdir(exist_ok=True)
    brief_path = REPORTS_DIR / f"{today}-morning-brief.md"
    with open(brief_path, "w", encoding="utf-8") as f:
        f.write(content)
    
    return content

if __name__ == "__main__":
    print(json.dumps(get_health_status(), ensure_ascii=False, indent=2))
