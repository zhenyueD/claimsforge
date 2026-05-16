#!/usr/bin/env python3
"""
Batch evaluation of DamageAgent on a labeled dataset.

Inputs:
  - eval/dataset/*.jpg  : labeled damage images (filename pattern: <category>_<true_type>_<true_severity>_<id>.jpg)
  - or eval/dataset.json: explicit ground-truth list
Outputs:
  - eval/results/last_run.json   : per-image prediction
  - eval/results/last_run.md     : human-readable confusion matrix + failure cases
  - eval/results/CHANGELOG.md    : appended summary per run for tracking progression

Usage:
  python eval/run_damage_eval.py
  python eval/run_damage_eval.py --concurrency 4 --limit 20
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))

from damage_agent import assess  # noqa: E402

DATASET_DIR = Path(__file__).resolve().parent / "dataset"
DATASET_JSON = Path(__file__).resolve().parent / "dataset.json"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


def load_dataset(limit: int | None = None) -> list[dict]:
    """Prefer dataset.json (richer labels). Fall back to filename convention."""
    if DATASET_JSON.exists():
        rows = json.loads(DATASET_JSON.read_text(encoding="utf-8"))
        if isinstance(rows, dict):
            rows = rows.get("items", [])
        for r in rows:
            r["path"] = DATASET_DIR / r["file"]
        rows = [r for r in rows if r["path"].exists()]
    else:
        rows = []
        for p in sorted(DATASET_DIR.glob("*.jpg")) + sorted(DATASET_DIR.glob("*.png")):
            # filename: <cat>_<type>_<sev>_<id>.jpg
            stem = p.stem.split("_")
            if len(stem) < 4:
                continue
            cat, ttype, sev_s, _id = stem[0], stem[1], stem[2], "_".join(stem[3:])
            try:
                rows.append({
                    "id": _id,
                    "file": p.name,
                    "path": p,
                    "category": cat,
                    "true_damage_type": ttype,
                    "true_severity_min": max(0, int(sev_s) - 1),
                    "true_severity_max": min(10, int(sev_s) + 1),
                    "user_message": f"My {cat} arrived with damage; please review and refund.",
                })
            except ValueError:
                continue
    if limit:
        rows = rows[:limit]
    return rows


def evaluate_one(row: dict) -> dict:
    img_bytes = Path(row["path"]).read_bytes()
    t0 = time.monotonic()
    try:
        result = assess(user_message=row["user_message"], image_bytes=img_bytes)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        type_ok = result.damage_type.value == row["true_damage_type"]
        sev = result.severity
        sev_ok = row["true_severity_min"] <= sev <= row["true_severity_max"]
        return {
            "id": row["id"],
            "file": row["file"],
            "category": row.get("category"),
            "true_type": row["true_damage_type"],
            "true_sev_range": [row["true_severity_min"], row["true_severity_max"]],
            "pred_type": result.damage_type.value,
            "pred_severity": sev,
            "pred_confidence": result.confidence,
            "pred_reasoning": result.reasoning,
            "type_ok": type_ok,
            "sev_ok": sev_ok,
            "ok": type_ok and sev_ok,
            "elapsed_ms": elapsed_ms,
        }
    except Exception as e:
        return {
            "id": row["id"],
            "file": row["file"],
            "error": str(e),
            "ok": False,
            "type_ok": False,
            "sev_ok": False,
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=3, help="parallel requests (mind Gemini RPM)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out-prefix", default="last_run")
    args = ap.parse_args()

    rows = load_dataset(limit=args.limit)
    if not rows:
        print(f"No dataset items found at {DATASET_DIR} or {DATASET_JSON}")
        return 1

    print(f"Evaluating {len(rows)} items with concurrency={args.concurrency}…")
    results: list[dict] = []
    t_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {ex.submit(evaluate_one, r): r for r in rows}
        for i, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            results.append(res)
            mark = "✓" if res.get("ok") else "✗"
            print(f"  [{i:3d}/{len(rows)}] {mark} {res['file']:35s}  pred={res.get('pred_type','—'):10s} sev={res.get('pred_severity','?')}", flush=True)
    total_s = time.monotonic() - t_start

    # Aggregate
    n = len(results)
    ok = sum(1 for r in results if r.get("ok"))
    type_ok = sum(1 for r in results if r.get("type_ok"))
    sev_ok = sum(1 for r in results if r.get("sev_ok"))
    errs = [r for r in results if "error" in r]

    confusion = Counter()
    for r in results:
        if "pred_type" in r:
            confusion[(r["true_type"], r["pred_type"])] += 1

    summary = {
        "total": n,
        "passed": ok,
        "type_accuracy": round(type_ok / n, 3) if n else 0,
        "severity_accuracy": round(sev_ok / n, 3) if n else 0,
        "joint_accuracy": round(ok / n, 3) if n else 0,
        "errors": len(errs),
        "elapsed_s": round(total_s, 1),
        "p50_latency_ms": sorted(r.get("elapsed_ms", 0) for r in results if "elapsed_ms" in r)[n // 2] if n else 0,
    }

    # Write JSON
    out_json = RESULTS_DIR / f"{args.out_prefix}.json"
    out_json.write_text(json.dumps({"summary": summary, "items": results}, indent=2, ensure_ascii=False))

    # Write Markdown
    out_md = RESULTS_DIR / f"{args.out_prefix}.md"
    md = []
    md.append(f"# DamageAgent Eval · {time.strftime('%Y-%m-%d %H:%M')}")
    md.append("")
    md.append("## Summary")
    md.append(f"- Items evaluated: **{n}**")
    md.append(f"- Joint accuracy (type + severity): **{summary['joint_accuracy']*100:.1f}%**")
    md.append(f"- Type accuracy: **{summary['type_accuracy']*100:.1f}%**")
    md.append(f"- Severity ±1 accuracy: **{summary['severity_accuracy']*100:.1f}%**")
    md.append(f"- p50 latency: **{summary['p50_latency_ms']}ms**")
    md.append(f"- Errors: {summary['errors']}")
    md.append("")
    md.append("## Confusion matrix (true → predicted, top 10)")
    md.append("")
    md.append("| true | predicted | count |")
    md.append("|------|-----------|-------|")
    for (t, p), c in sorted(confusion.items(), key=lambda x: -x[1])[:10]:
        mark = "✅" if t == p else "❌"
        md.append(f"| {t} | {p} {mark} | {c} |")
    md.append("")
    fails = [r for r in results if not r.get("ok") and "pred_type" in r]
    if fails:
        md.append(f"## Failed cases ({len(fails)})")
        for r in fails:
            md.append(f"- **{r['file']}** · true=({r['true_type']}, {r['true_sev_range']}) → "
                      f"pred=({r['pred_type']}, sev={r['pred_severity']}, conf={r['pred_confidence']:.2f})")
            md.append(f"  - reasoning: {r['pred_reasoning'][:160]}")
    out_md.write_text("\n".join(md), encoding="utf-8")

    # Append to CHANGELOG
    cl = RESULTS_DIR / "CHANGELOG.md"
    line = f"- **{time.strftime('%Y-%m-%d %H:%M')}** · n={n} · joint={summary['joint_accuracy']*100:.1f}% · type={summary['type_accuracy']*100:.1f}% · sev={summary['severity_accuracy']*100:.1f}% · p50={summary['p50_latency_ms']}ms\n"
    if not cl.exists():
        cl.write_text("# DamageAgent eval CHANGELOG\n\n", encoding="utf-8")
    with cl.open("a", encoding="utf-8") as f:
        f.write(line)

    print()
    print(f"=== SUMMARY ===")
    print(f"  Joint accuracy: {summary['joint_accuracy']*100:.1f}%  ({ok}/{n})")
    print(f"  Type accuracy:  {summary['type_accuracy']*100:.1f}%")
    print(f"  Sev ±1:         {summary['severity_accuracy']*100:.1f}%")
    print(f"  p50 latency:    {summary['p50_latency_ms']}ms")
    print(f"  Elapsed:        {total_s:.1f}s")
    print()
    print(f"  Wrote: {out_json}")
    print(f"  Wrote: {out_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
