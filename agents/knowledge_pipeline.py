"""
KNOWLEDGE PIPELINE
事件驱动的知识库自动更新管道
接收：对话缺口、满意度差评、组长指令
输出：更新后的知识库 + 变更日志
"""
import json
import uuid
from datetime import datetime
from pathlib import Path

from utils import safe_save_json, safe_load_json

BASE = Path(__file__).parent.parent
KB_PATH = BASE / "data" / "knowledge_base.json"
GAPS_PATH = BASE / "data" / "gaps.json"


def load_kb():
    return safe_load_json(KB_PATH, {"entries": [], "categories": {}, "update_log": []})


def save_kb(kb):
    safe_save_json(KB_PATH, kb)


def load_gaps():
    """优先读独立 gaps 文件，兼容旧版 kb['gaps']"""
    gaps = safe_load_json(GAPS_PATH, None)
    if gaps is not None:
        return gaps
    # 兼容旧版
    kb = load_kb()
    return kb.get("gaps", [])


def save_gaps(gaps):
    safe_save_json(GAPS_PATH, gaps)


def add_entry(category: str, question: str, answer: str, keywords: list, source: str = "manual") -> dict:
    kb = load_kb()
    # 修复 S-02：ID 改用 uuid 后缀，避免并发碰撞
    new_id = f"KB{datetime.now().strftime('%H%M%S')}{uuid.uuid4().hex[:4].upper()}"
    entry = {
        "id": new_id,
        "category": category,
        "question": question,
        "answer": answer,
        "keywords": keywords,
        "last_updated": datetime.now().strftime("%Y-%m-%d"),
        "source": source
    }
    kb["entries"].append(entry)
    kb.setdefault("update_log", []).append({
        "action": "ADD",
        "id": new_id,
        "timestamp": datetime.now().isoformat(),
        "source": source
    })
    save_kb(kb)
    return entry


def update_entry(entry_id: str, updates: dict, source: str = "manual") -> dict:
    kb = load_kb()
    for entry in kb["entries"]:
        if entry["id"] == entry_id:
            entry.update(updates)
            entry["last_updated"] = datetime.now().strftime("%Y-%m-%d")
            kb.setdefault("update_log", []).append({
                "action": "UPDATE",
                "id": entry_id,
                "changes": list(updates.keys()),
                "timestamp": datetime.now().isoformat(),
                "source": source
            })
            save_kb(kb)
            return entry
    return {}


def get_gaps_report() -> dict:
    gaps = load_gaps()
    query_count = {}
    for g in gaps:
        q = g.get("query", "")
        if not q:
            continue
        query_count[q] = query_count.get(q, 0) + 1
    sorted_gaps = sorted(query_count.items(), key=lambda x: x[1], reverse=True)
    return {
        "total_gaps": len(gaps),
        "unique_queries": len(sorted_gaps),
        "top_gaps": [{"query": q, "count": c} for q, c in sorted_gaps[:10]],
        "generated_at": datetime.now().isoformat()
    }


def clear_gaps():
    save_gaps([])


def get_kb_stats() -> dict:
    kb = load_kb()
    categories = {}
    for entry in kb["entries"]:
        cat = entry["category"]
        categories[cat] = categories.get(cat, 0) + 1
    gaps = load_gaps()
    update_log = kb.get("update_log", [])
    return {
        "total_entries": len(kb["entries"]),
        "categories": categories,
        "pending_gaps": len(gaps),
        "last_update": update_log[-1]["timestamp"] if update_log else None
    }


if __name__ == "__main__":
    print(json.dumps(get_kb_stats(), ensure_ascii=False, indent=2))
    print(json.dumps(get_gaps_report(), ensure_ascii=False, indent=2))
