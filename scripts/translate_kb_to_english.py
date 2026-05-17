"""
Batch-translate every Chinese KB entry's title / scenario / decision into
English using Gemini 2.5 Flash. Writes the English text into new fields
(title_en / scenario_en / decision_en) so the original Chinese remains
intact for Chinese-speaking users.

The frontend (methodologies.html, admin.html) reads *_en when present.

Runs in batches of 20 entries per Gemini call to stay under RPS limits.
~1200 zh entries → ~60 batched calls → 4-6 minutes total.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))

from gemini_client import structured
from pydantic import BaseModel, Field

KB_PATH = ROOT / "data" / "unified_kb.jsonl"
BATCH_SIZE = 20


def is_chinese(text: str) -> bool:
    return any("一" <= c <= "鿿" for c in (text or ""))


def needs_translation(entry: dict) -> bool:
    if entry.get("title_en") and entry.get("decision_en"):
        return False  # already done
    blob = (entry.get("title", "") + entry.get("scenario", "")
            + entry.get("decision", ""))
    return is_chinese(blob)


# ─────────────────────────────────────────────────────────
#  Gemini structured-output schema for one batch
# ─────────────────────────────────────────────────────────
class Translation(BaseModel):
    id: str = Field(description="Echo the entry id back so we can map results")
    title_en: str = Field(description="English translation of the title (concise)")
    scenario_en: str = Field(description="English translation of the WHEN/scenario field")
    decision_en: str = Field(description="English translation of the DO/decision field")


class TranslationBatch(BaseModel):
    items: list[Translation]


_SYSTEM = """You are a professional translator for e-commerce customer-service knowledge entries.
Translate Chinese title, scenario, and decision text into clear, concise English.

Rules:
  - Keep the meaning faithful; do not invent details.
  - Keep policy IDs (e.g. P-RET-01) and brand names (淘宝→Taobao, 天猫→Tmall) intact.
  - Strip greetings and filler ("亲爱的客户" type intros).
  - Output one Translation object per input entry, in the same order.
  - Empty input → empty string (don't hallucinate).
"""


def translate_batch(batch: list[dict]) -> dict[str, dict]:
    """Translate a batch of entries; returns {id: {title_en, scenario_en, decision_en}}."""
    prompt_items = [
        {
            "id": e["id"],
            "title": e.get("title", "")[:200],
            "scenario": e.get("scenario", "")[:400],
            "decision": e.get("decision", "")[:600],
        }
        for e in batch
    ]
    prompt = (
        "Translate these Chinese e-commerce KB entries to English. "
        "Return one Translation object per input, IDs echoed back.\n\n"
        f"{json.dumps(prompt_items, ensure_ascii=False, indent=2)}"
    )
    try:
        result = structured(
            prompt=prompt,
            schema=TranslationBatch,
            system=_SYSTEM,
            temperature=0.1,
            max_tokens=8000,
        )
        return {t.id: {
            "title_en": t.title_en,
            "scenario_en": t.scenario_en,
            "decision_en": t.decision_en,
        } for t in result.items}
    except Exception as e:
        print(f"  batch failed: {e}", flush=True)
        return {}


def main():
    # Read + dedup by id (latest wins, matches _load_kb)
    seen: dict[str, dict] = {}
    with KB_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                seen[r["id"]] = r
            except Exception:
                pass
    print(f"loaded {len(seen)} unique entries from KB")

    todo = [e for e in seen.values() if needs_translation(e)]
    print(f"needs translation: {len(todo)}")
    if not todo:
        print("nothing to do")
        return 0

    t0 = time.monotonic()
    done = 0
    for i in range(0, len(todo), BATCH_SIZE):
        batch = todo[i:i + BATCH_SIZE]
        translations = translate_batch(batch)
        for e in batch:
            tr = translations.get(e["id"])
            if tr:
                e["title_en"] = tr["title_en"]
                e["scenario_en"] = tr["scenario_en"]
                e["decision_en"] = tr["decision_en"]
                done += 1
        elapsed = time.monotonic() - t0
        eta = (elapsed / max(1, done)) * (len(todo) - done) if done > 0 else 0
        print(f"  [{done}/{len(todo)}] {elapsed:.0f}s elapsed · ETA {eta:.0f}s",
              flush=True)
        # Small pause to be RPM-friendly
        time.sleep(0.3)

    # Atomic rewrite: tmp + rename
    tmp = KB_PATH.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for entry in seen.values():
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    tmp.replace(KB_PATH)
    print(f"wrote {len(seen)} entries back to {KB_PATH.name}")
    print(f"translated {done} new · total elapsed {time.monotonic()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
