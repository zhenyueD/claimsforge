#!/usr/bin/env python3
"""End-to-end claim eval — the true product KPI.

Loads eval/e2e_dataset.json, runs each case through the live orchestrator
(sync run() — that's what evaluates the same code path /api/claim uses),
and grades:
  - intent label match
  - emotion risk band match (if specified)
  - damage type + severity range (if specified)
  - offer_type membership in expected set
  - amount within tolerance window
  - escalation flag match
  - language match (reply language vs customer language)
  - security: prompt-injection messages should block or escalate

Writes:
  eval/results/e2e_last_run.json  — per-case + summary
  eval/results/e2e_last_run.md    — human-readable report
  eval/results/E2E_CHANGELOG.md   — appended one-line per run

Usage:
  python eval/run_e2e_eval.py
  python eval/run_e2e_eval.py --only EN-01,EN-05         # subset
  python eval/run_e2e_eval.py --concurrency 2            # safer for rate limits

Idempotent. Re-run after any prompt or policy change to see if accuracy
regressed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))

from schemas import ClaimContext, TurnRecord  # noqa: E402
import orchestrator  # noqa: E402

DATASET = Path(__file__).resolve().parent / "e2e_dataset.json"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


def _detect_language(text: str) -> str:
    return "zh" if any("一" <= c <= "鿿" for c in text or "") else "en"


def _load_image(path_str: Optional[str]) -> Optional[bytes]:
    if not path_str:
        return None
    candidates = [ROOT / "data" / path_str, ROOT / path_str, Path(path_str)]
    for p in candidates:
        if p.exists():
            return p.read_bytes()
    return None


def evaluate_case(case: dict) -> dict:
    msg = case["user_message"]
    image_bytes = _load_image(case.get("image"))

    # Multi-turn prelude (the EN-09 case)
    history: list[TurnRecord] = []
    for prelude in case.get("session_prelude", []):
        history.append(TurnRecord(
            role=prelude["role"],
            content=prelude["content"],
            timestamp="prelude",
        ))

    ctx = ClaimContext(
        session_id=f"e2e-{case['id']}",
        user_message=msg,
        image_bytes=image_bytes,
        history=history,
    )

    t0 = time.monotonic()
    try:
        orchestrator.run(ctx, estimated_value_cents=case["estimated_value_cents"])
        error = None
    except Exception as e:
        error = str(e)[:200]
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    expected = case.get("expected", {})
    checks: dict[str, bool] = {}
    notes: list[str] = []

    if error:
        return {"id": case["id"], "error": error, "elapsed_ms": elapsed_ms, "all_ok": False, "checks": {}}

    # --- Security: prompt injection blocked or escalated ---
    if expected.get("blocked_or_escalated"):
        blocked = ctx.escalated_to_human or (ctx.intent and ctx.intent.label.value == "general_inquiry")
        checks["security_blocked"] = bool(blocked)

    # --- Intent label ---
    if "intent" in expected and ctx.intent:
        checks["intent_label"] = (ctx.intent.label.value == expected["intent"])

    # --- Escalation flag ---
    if "should_escalate" in expected:
        checks["escalation"] = (ctx.escalated_to_human == expected["should_escalate"])

    # --- Language of reply ---
    if "language_in_reply" in expected:
        want = expected["language_in_reply"]
        got = _detect_language(ctx.final_reply or "")
        checks["language"] = (got == want)
        if not checks["language"]:
            notes.append(f"reply lang={got} expected={want}: {(ctx.final_reply or '')[:80]}")

    # --- Damage type ---
    if "damage_type" in expected and ctx.damage:
        checks["damage_type"] = (ctx.damage.damage_type.value == expected["damage_type"])

    # --- Damage severity range ---
    if "severity_range" in expected and ctx.damage:
        lo, hi = expected["severity_range"]
        checks["severity_range"] = (lo <= ctx.damage.severity <= hi)

    # --- Emotion ---
    if "emotion_min_score" in expected and ctx.emotion:
        checks["emotion_score"] = (ctx.emotion.score >= expected["emotion_min_score"])
    if "emotion_risk" in expected and ctx.emotion:
        checks["emotion_risk"] = (ctx.emotion.risk.value == expected["emotion_risk"])

    # --- Needs bias ---
    if "needs_bias" in expected and ctx.needs:
        checks["needs_bias"] = (ctx.needs.suggested_offer_bias == expected["needs_bias"])

    # --- Offer type (membership in list) ---
    if "offer_type" in expected and not expected.get("should_escalate"):
        want_types = expected["offer_type"]
        if isinstance(want_types, str):
            want_types = [want_types]
        got_type = ctx.final_offer.offer_type.value if ctx.final_offer else None
        checks["offer_type"] = (got_type in want_types)

    # --- Amount range ---
    if "amount_cents_range" in expected and ctx.final_offer:
        lo, hi = expected["amount_cents_range"]
        checks["amount_in_range"] = (lo <= ctx.final_offer.amount_cents <= hi)

    # --- Clarification has a question ---
    if expected.get("must_contain_question_mark"):
        text = ctx.final_reply or ""
        checks["has_question"] = ("?" in text or "？" in text)

    all_ok = all(checks.values()) if checks else False
    return {
        "id": case["id"],
        "category": case.get("category"),
        "rationale": case.get("rationale", ""),
        "all_ok": all_ok,
        "checks": checks,
        "notes": notes,
        "elapsed_ms": elapsed_ms,
        "intent_actual": ctx.intent.label.value if ctx.intent else None,
        "emotion_actual": (ctx.emotion.label, ctx.emotion.risk.value, ctx.emotion.score) if ctx.emotion else None,
        "damage_actual": (ctx.damage.damage_type.value, ctx.damage.severity, ctx.damage.confidence) if ctx.damage else None,
        "needs_bias_actual": ctx.needs.suggested_offer_bias if ctx.needs else None,
        "offer_actual": (ctx.final_offer.offer_type.value, ctx.final_offer.amount_cents) if ctx.final_offer else None,
        "escalated_actual": ctx.escalated_to_human,
        "reply_excerpt": (ctx.final_reply or "")[:160],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated case IDs to run (subset)")
    ap.add_argument("--concurrency", type=int, default=2,
                    help="parallel cases (be gentle with rate limits)")
    ap.add_argument("--out", default="e2e_last_run", help="output filename stem")
    args = ap.parse_args()

    data = json.loads(DATASET.read_text(encoding="utf-8"))
    cases = data["cases"]
    if args.only:
        wanted = {x.strip() for x in args.only.split(",")}
        cases = [c for c in cases if c["id"] in wanted]
    print(f"Evaluating {len(cases)} cases (concurrency={args.concurrency})…")

    results: list[dict] = []
    t_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        for res in ex.map(evaluate_case, cases):
            mark = "✓" if res["all_ok"] else "✗"
            checks_pass = sum(1 for v in res["checks"].values() if v)
            checks_tot = len(res["checks"])
            line = f"  [{mark}] {res['id']:<32s} {checks_pass}/{checks_tot} checks  ({res['elapsed_ms']}ms)"
            if not res["all_ok"]:
                failed = [k for k, v in res["checks"].items() if not v]
                line += f"   FAIL: {failed}"
            print(line, flush=True)
            results.append(res)
    elapsed = time.monotonic() - t_start

    n = len(results)
    ok = sum(1 for r in results if r["all_ok"])
    by_cat: dict[str, list[bool]] = {}
    for r in results:
        by_cat.setdefault(r.get("category") or "uncategorized", []).append(r["all_ok"])

    summary = {
        "n": n,
        "passed": ok,
        "joint_accuracy": round(ok / n, 3) if n else 0,
        "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in by_cat.items()},
        "elapsed_s": round(elapsed, 1),
        "p50_latency_ms": sorted(r["elapsed_ms"] for r in results)[n // 2] if n else 0,
    }

    out_json = RESULTS_DIR / f"{args.out}.json"
    out_json.write_text(json.dumps({"summary": summary, "items": results}, indent=2, ensure_ascii=False))

    md = [
        f"# E2E Eval · {time.strftime('%Y-%m-%d %H:%M')}",
        "",
        "## Summary",
        f"- Cases: **{n}**",
        f"- Joint accuracy: **{summary['joint_accuracy']*100:.1f}%** ({ok}/{n})",
        f"- p50 latency: **{summary['p50_latency_ms']}ms**",
        f"- Elapsed: {summary['elapsed_s']}s",
        "",
        "## By category",
    ]
    for k, v in summary["by_category"].items():
        md.append(f"- {k}: {v}")
    md.append("")
    fails = [r for r in results if not r["all_ok"]]
    if fails:
        md.append(f"## Failures ({len(fails)})")
        for r in fails:
            md.append(f"- **{r['id']}** — failed checks: {[k for k, v in r['checks'].items() if not v]}")
            md.append(f"  - intent: {r['intent_actual']}")
            md.append(f"  - emotion: {r['emotion_actual']}")
            md.append(f"  - damage: {r['damage_actual']}")
            md.append(f"  - offer: {r['offer_actual']} · escalated={r['escalated_actual']}")
            md.append(f"  - reply: {r['reply_excerpt']}")
            for note in r["notes"]:
                md.append(f"  - note: {note}")
            md.append("")
    out_md = RESULTS_DIR / f"{args.out}.md"
    out_md.write_text("\n".join(md), encoding="utf-8")

    cl = RESULTS_DIR / "E2E_CHANGELOG.md"
    line = f"- **{time.strftime('%Y-%m-%d %H:%M')}** · n={n} · joint={summary['joint_accuracy']*100:.1f}% · p50={summary['p50_latency_ms']}ms · cats={summary['by_category']}\n"
    if not cl.exists():
        cl.write_text("# E2E eval CHANGELOG\n\n", encoding="utf-8")
    with cl.open("a", encoding="utf-8") as f:
        f.write(line)

    print()
    print(f"===== SUMMARY =====")
    print(f"  Joint accuracy: {summary['joint_accuracy']*100:.1f}%  ({ok}/{n})")
    print(f"  p50 latency:    {summary['p50_latency_ms']}ms")
    print(f"  Wrote: {out_json}")
    print(f"  Wrote: {out_md}")
    return 0 if ok == n else 1


if __name__ == "__main__":
    sys.exit(main())
