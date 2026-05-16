#!/usr/bin/env python3
"""
Migrate the legacy three KB stores into the single unified_kb.jsonl.

Idempotent — safe to re-run. Each source ID is preserved + namespaced.

Sources merged:
  - data/policies.json        → KBSource.HUMAN_POLICY  (26 rules)
  - data/merchant_kb.json     → KBSource.CURATED_WISDOM (93 entries)
  - data/learned_cases.jsonl  → KBSource.LEARNED_CASE   (live ML data)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))

from unified_kb import KBEntry, KBSource, KBType, upsert, _load_kb, get_kb_stats  # noqa: E402


def migrate_policies():
    p = ROOT / "data" / "policies.json"
    if not p.exists():
        print("- skip policies.json (not found)")
        return 0
    data = json.loads(p.read_text(encoding="utf-8"))
    n = 0
    for pol in data.get("policies", []):
        cond = pol.get("applies_when", {})
        scenario_parts = []
        if "damage_severity_min" in cond or "damage_severity_max" in cond:
            scenario_parts.append(f"severity {cond.get('damage_severity_min', 0)}-{cond.get('damage_severity_max', 10)}")
        if "damage_types" in cond:
            scenario_parts.append("damage types: " + ", ".join(cond["damage_types"]))
        if "product_categories" in cond:
            scenario_parts.append("categories: " + ", ".join(cond["product_categories"]))
        if "emotion_score_min" in cond:
            scenario_parts.append(f"emotion ≥ {cond['emotion_score_min']}")
        if "user_keywords" in cond:
            scenario_parts.append("keywords: " + ", ".join(cond["user_keywords"][:5]))
        if pol.get("force_escalate"):
            scenario_parts.append("⚠ force_escalate")

        decision_parts = [pol.get("offer_type", "?")]
        if pol.get("amount_basis"):
            decision_parts.append(f"basis={pol['amount_basis']}")
        if pol.get("max_cents"):
            decision_parts.append(f"max=${pol['max_cents']/100:.0f}")
        if pol.get("amount_percent"):
            decision_parts.append(f"pct={pol['amount_percent']}%")

        category = pol.get("category", "policy")
        domain = category.split(":")[1] if ":" in category else category

        entry = KBEntry(
            id=f"policy-{pol['id'].lower()}",
            source=KBSource.HUMAN_POLICY,
            type=KBType.RULE,
            domain=domain,
            title=pol.get("title", pol["id"]),
            customer_facing_name=pol.get("title"),  # default; humans can refine
            scenario=" · ".join(scenario_parts) or "any case",
            decision=" · ".join(decision_parts),
            rationale=pol.get("rationale", ""),
            tags=[pol["id"], category, pol.get("offer_type", "")] + ([pol["category"]] if pol.get("category") else []),
            contributor="human_policy_team",
            quality_score=1.0,  # human-curated = gold
        )
        upsert(entry)
        n += 1
    print(f"+ migrated {n} policies")
    return n


def migrate_merchant_wisdom():
    p = ROOT / "data" / "merchant_kb.json"
    if not p.exists():
        print("- skip merchant_kb.json")
        return 0
    data = json.loads(p.read_text(encoding="utf-8"))
    n = 0
    for it in data.get("entries", []):
        entry = KBEntry(
            id=f"wisdom-{it['id'].lower()}",
            source=KBSource.CURATED_WISDOM,
            type=KBType.PRINCIPLE,
            domain=it.get("category", "general"),
            title=f"[{it.get('source', '?')}] {it['scenario'][:60]}",
            scenario=it["scenario"],
            decision=it["decision"],
            rationale=f"Source: {it.get('source', 'curated')}",
            tags=it.get("tags", []) + [it.get("source", "")],
            contributor=f"curated:{it.get('source', '?')}",
            quality_score=0.85,
        )
        upsert(entry)
        n += 1
    print(f"+ migrated {n} curated wisdom entries")
    return n


def migrate_learned_cases():
    p = ROOT / "data" / "learned_cases.jsonl"
    if not p.exists():
        print("- skip learned_cases.jsonl")
        return 0
    n = 0
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            c = json.loads(line)
        except Exception:
            continue
        d = c.get("damage") or {}
        fo = c.get("final_offer") or {}
        em = c.get("emotion") or {}
        title = f"{d.get('damage_type', '?')} sev {d.get('severity', '?')} → {fo.get('offer_type', 'escalated')}"
        scenario = c.get("user_message_preview", "")[:200] or title
        decision = f"{fo.get('offer_type', 'escalated to human')} ${fo.get('amount_cents', 0)/100:.2f}"
        rationale = (fo.get("justification") or "")[:300]
        entry = KBEntry(
            id=f"case-{c.get('session_id', f'mig-{i}')}",
            source=KBSource.LEARNED_CASE,
            type=KBType.CASE,
            domain=d.get("damage_type", "general"),
            title=title,
            scenario=scenario,
            decision=decision,
            rationale=rationale,
            tags=[d.get("damage_type", ""), em.get("label", ""), em.get("risk", "")],
            contributor="ai_system",
            quality_score=0.6,
        )
        upsert(entry)
        n += 1
    print(f"+ migrated {n} learned cases")
    return n


if __name__ == "__main__":
    print("=== Migrating to unified_kb.jsonl ===")
    a = migrate_policies()
    b = migrate_merchant_wisdom()
    c = migrate_learned_cases()
    print()
    print(f"Total: {a+b+c} entries written")
    print()
    print("=== Unified KB stats ===")
    print(json.dumps(get_kb_stats(), indent=2))
