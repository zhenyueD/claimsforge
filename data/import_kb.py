"""
知识库导入脚本
将 CSV/JSON 数据导入 knowledge_base.json，并触发向量索引重建

用法：
  python import_kb.py                          # 导入默认 CSV（knowledge_base_real.csv）
  python import_kb.py --file my_data.csv       # 指定 CSV 文件
  python import_kb.py --file my_data.json      # 导入 JSON 文件
  python import_kb.py --reset                  # 清空并重建（危险：清空现有条目）
  python import_kb.py --rebuild-index-only     # 仅重建向量索引，不修改 KB
"""
import json
import sys
import csv
import argparse
import io
from pathlib import Path
from datetime import datetime

# Fix Windows GBK console encoding
if sys.stdout.encoding and sys.stdout.encoding.lower() in ('gbk', 'cp936', 'ascii'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

BASE    = Path(__file__).parent.parent
KB_PATH = BASE / "data" / "knowledge_base.json"
DEFAULT_CSV = BASE / "data" / "knowledge_base_real.csv"


def load_kb() -> dict:
    if KB_PATH.exists():
        with open(KB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"version": "1.0", "last_updated": "", "entries": [], "gaps": [], "update_log": []}


def save_kb(kb: dict):
    kb["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    with open(KB_PATH, "w", encoding="utf-8") as f:
        json.dump(kb, f, ensure_ascii=False, indent=2)


def parse_keywords(raw: str) -> list:
    """支持逗号分隔或 JSON 列表两种格式"""
    raw = raw.strip()
    if raw.startswith("["):
        try:
            return json.loads(raw)
        except Exception:
            pass
    return [k.strip() for k in raw.split(",") if k.strip()]


def import_from_csv(filepath: Path, kb: dict, reset: bool = False) -> dict:
    """从 CSV 导入条目，返回统计信息"""
    if reset:
        kb["entries"] = []
        print("⚠️  已清空现有条目（--reset 模式）")

    existing_ids = {e["id"] for e in kb["entries"]}
    added = updated = skipped = 0

    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    for row in rows:
        entry_id = row.get("id", "").strip()
        if not entry_id:
            skipped += 1
            continue

        entry = {
            "id":           entry_id,
            "category":     row.get("category", "").strip(),
            "question":     row.get("question", "").strip(),
            "answer":       row.get("answer", "").strip(),
            "keywords":     parse_keywords(row.get("keywords", "")),
            "source":       row.get("source", "manual").strip(),
            "last_updated": row.get("last_updated", datetime.now().strftime("%Y-%m-%d")).strip(),
        }

        if entry_id in existing_ids:
            # 更新已有条目
            for i, e in enumerate(kb["entries"]):
                if e["id"] == entry_id:
                    kb["entries"][i] = entry
                    break
            updated += 1
        else:
            kb["entries"].append(entry)
            existing_ids.add(entry_id)
            added += 1

        kb["update_log"].append({
            "action":    "ADD" if entry_id not in existing_ids else "UPDATE",
            "id":        entry_id,
            "timestamp": datetime.now().isoformat(),
            "source":    entry["source"]
        })

    return {"added": added, "updated": updated, "skipped": skipped, "total_rows": len(rows)}


def import_from_json(filepath: Path, kb: dict, reset: bool = False) -> dict:
    """从 JSON 导入条目"""
    if reset:
        kb["entries"] = []
        print("⚠️  已清空现有条目（--reset 模式）")

    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 支持两种结构：直接列表 或 {entries: [...]}
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict) and "entries" in data:
        rows = data["entries"]
    else:
        print("❌ JSON 格式错误，需为列表或含 entries 字段的对象")
        return {"added": 0, "updated": 0, "skipped": 0, "total_rows": 0}

    existing_ids = {e["id"] for e in kb["entries"]}
    added = updated = skipped = 0

    for row in rows:
        entry_id = row.get("id", "").strip()
        if not entry_id:
            skipped += 1
            continue

        entry = {
            "id":           entry_id,
            "category":     row.get("category", "").strip(),
            "question":     row.get("question", "").strip(),
            "answer":       row.get("answer", "").strip(),
            "keywords":     row.get("keywords", []),
            "source":       row.get("source", "manual"),
            "last_updated": row.get("last_updated", datetime.now().strftime("%Y-%m-%d")),
        }

        if entry_id in existing_ids:
            for i, e in enumerate(kb["entries"]):
                if e["id"] == entry_id:
                    kb["entries"][i] = entry
                    break
            updated += 1
        else:
            kb["entries"].append(entry)
            existing_ids.add(entry_id)
            added += 1

    return {"added": added, "updated": updated, "skipped": skipped, "total_rows": len(rows)}


def rebuild_vector_index(force: bool = True):
    """重建向量索引"""
    try:
        sys.path.insert(0, str(BASE / "agents"))
        from vector_store import build_index
        result = build_index(force=force)
        return result
    except Exception as e:
        return {"status": "error", "error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="知识库导入工具")
    parser.add_argument("--file", type=str, default=str(DEFAULT_CSV), help="CSV或JSON文件路径")
    parser.add_argument("--reset", action="store_true", help="清空已有条目后导入")
    parser.add_argument("--no-index", action="store_true", help="跳过向量索引重建")
    parser.add_argument("--rebuild-index-only", action="store_true", help="仅重建向量索引")
    args = parser.parse_args()

    print("=" * 55)
    print("  AI 客服知识库导入工具")
    print("=" * 55)

    if args.rebuild_index_only:
        print("\n⚙️  仅重建向量索引...")
        result = rebuild_vector_index(force=True)
        print(f"✅ 索引重建完成：{result}")
        return

    filepath = Path(args.file)
    if not filepath.exists():
        print(f"❌ 文件不存在：{filepath}")
        sys.exit(1)

    kb = load_kb()
    before_count = len(kb["entries"])
    print(f"\n📂 当前知识库：{before_count} 条条目")
    print(f"📥 导入文件：{filepath.name}")
    if args.reset:
        print("⚠️  模式：清空重建")
    else:
        print("🔄 模式：增量更新（已有条目将被覆盖）")

    # 执行导入
    suffix = filepath.suffix.lower()
    if suffix == ".csv":
        stats = import_from_csv(filepath, kb, reset=args.reset)
    elif suffix == ".json":
        stats = import_from_json(filepath, kb, reset=args.reset)
    else:
        print(f"❌ 不支持的文件格式：{suffix}（仅支持 .csv / .json）")
        sys.exit(1)

    save_kb(kb)
    after_count = len(kb["entries"])

    print(f"\n✅ 导入完成：")
    print(f"   新增：{stats['added']} 条")
    print(f"   更新：{stats['updated']} 条")
    print(f"   跳过：{stats['skipped']} 条")
    print(f"   处理行数：{stats['total_rows']}")
    print(f"   知识库总计：{before_count} → {after_count} 条")

    # 重建向量索引
    if not args.no_index:
        print("\n⚙️  重建向量索引（Chroma）...")
        result = rebuild_vector_index(force=True)
        if result.get("status") == "error":
            print(f"⚠️  向量索引重建失败（关键词检索仍可用）：{result['error']}")
        else:
            print(f"✅ 向量索引重建完成：{result.get('count', 0)} 条已索引")
    else:
        print("⏭️  已跳过向量索引重建（--no-index）")

    print("\n📊 知识库分类统计：")
    categories = {}
    for e in kb["entries"]:
        cat = e.get("category", "未分类")
        categories[cat] = categories.get(cat, 0) + 1
    for cat, cnt in sorted(categories.items()):
        print(f"   {cat}：{cnt} 条")
    print("=" * 55)


if __name__ == "__main__":
    main()
