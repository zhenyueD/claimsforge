#!/usr/bin/env python3
"""
Bulk-ingest a directory of mixed-format SOP/knowledge files.

Logic:
  - Walk the directory recursively
  - Skip junk (.DS_Store, images)
  - For each file, try local parser; fall back to Gemini File API for legacy formats
  - After all files processed, run embedding indexer once
  - Print a per-folder summary so user knows what landed

Usage:
  python scripts/bulk_ingest.py ./uploads_inbox/
  python scripts/bulk_ingest.py ./uploads_inbox/ --skip-ext doc,ppt  # skip slow legacy
  python scripts/bulk_ingest.py ./uploads_inbox/ --limit 20          # smoke test
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))

from ingestion import ingest_document  # noqa: E402
from embedding_index import index_all  # noqa: E402

SUPPORTED_EXTS = {"pdf", "docx", "doc", "pptx", "ppt", "xlsx", "xls", "txt", "md", "json", "csv"}
SKIP_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
SKIP_EXTS_IMG = {"jpg", "jpeg", "png", "gif", "bmp", "webp", "tiff", "svg"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", help="directory to walk recursively")
    ap.add_argument("--skip-ext", default="", help="comma-separated extensions to skip (e.g. 'doc,ppt')")
    ap.add_argument("--only-ext", default="", help="comma-separated extensions to include exclusively")
    ap.add_argument("--limit", type=int, default=0, help="stop after N files (testing)")
    ap.add_argument("--contributor", default="bulk_import")
    ap.add_argument("--rebuild-embeddings", action="store_true", help="rebuild embeddings only at the end")
    args = ap.parse_args()

    base = Path(args.dir).expanduser().resolve()
    if not base.exists():
        print(f"❌ directory not found: {base}")
        return 1

    skip_exts = {e.strip().lower().lstrip(".") for e in args.skip_ext.split(",") if e.strip()}
    only_exts = {e.strip().lower().lstrip(".") for e in args.only_ext.split(",") if e.strip()}

    # Collect files
    files = []
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        if p.name in SKIP_FILES:
            continue
        ext = p.suffix.lstrip(".").lower()
        if ext in SKIP_EXTS_IMG:
            continue
        if ext not in SUPPORTED_EXTS:
            continue
        if skip_exts and ext in skip_exts:
            continue
        if only_exts and ext not in only_exts:
            continue
        files.append(p)

    files.sort()
    if args.limit:
        files = files[: args.limit]

    print(f"📂 base: {base}")
    print(f"📄 files to ingest: {len(files)}")
    if not files:
        return 0

    by_folder = defaultdict(lambda: {"files": 0, "entries": 0, "skipped": 0, "errors": []})
    t_start = time.monotonic()

    for i, p in enumerate(files, 1):
        rel = p.relative_to(base)
        folder = str(rel.parts[0]) if len(rel.parts) > 1 else "(root)"
        blob = p.read_bytes()
        size_kb = len(blob) / 1024
        domain_hint = None
        # Heuristic: take folder name as a coarse domain hint
        if "客服" in folder or "service" in folder.lower():
            domain_hint = "customer_service"
        if "售前" in p.name or "presale" in p.name.lower():
            domain_hint = "presale"
        elif "售后" in p.name or "after" in p.name.lower():
            domain_hint = "aftersales"
        elif "话术" in p.name or "script" in p.name.lower():
            domain_hint = "script_template"
        elif "kpi" in p.name.lower() or "考核" in p.name or "绩效" in p.name:
            domain_hint = "kpi"
        elif "培训" in p.name or "training" in p.name.lower():
            domain_hint = "training"

        print(f"  [{i:3d}/{len(files)}] {p.name[:50]:50s} ({size_kb:6.1f} KB)…", end="", flush=True)
        try:
            # Skip embedding step per-file; rebuild once at end
            r = ingest_document(
                p.name, blob,
                contributor=args.contributor,
                domain_hint=domain_hint,
                rebuild_embeddings=False,
            )
            by_folder[folder]["files"] += 1
            by_folder[folder]["entries"] += r.entries_written
            by_folder[folder]["skipped"] += r.skipped
            print(f"  ✓ {r.entries_written}e {r.skipped}s ({r.elapsed_s}s)")
        except Exception as e:
            by_folder[folder]["errors"].append(f"{p.name}: {e}")
            print(f"  ✗ {e}")

    # Rebuild embeddings once for all new entries
    print()
    print("⚡ rebuilding embeddings…")
    idx = index_all(rate_limit_sleep=0.03)
    print(f"   indexed: {idx.get('indexed', 0)}  total in index: {idx.get('total_in_index', 0)}")

    dt = time.monotonic() - t_start
    print()
    print(f"===== SUMMARY ({dt:.1f}s) =====")
    total_e = sum(d["entries"] for d in by_folder.values())
    total_s = sum(d["skipped"] for d in by_folder.values())
    total_err = sum(len(d["errors"]) for d in by_folder.values())
    print(f"Total files: {sum(d['files'] for d in by_folder.values())}")
    print(f"Total entries written: {total_e}")
    print(f"Total chunks skipped: {total_s}")
    print(f"Total errors: {total_err}")
    print()
    for folder, d in sorted(by_folder.items()):
        print(f"  📁 {folder}")
        print(f"     files: {d['files']}  entries: {d['entries']}  skipped: {d['skipped']}  errors: {len(d['errors'])}")
        for err in d["errors"][:3]:
            print(f"     ⚠ {err}")


if __name__ == "__main__":
    sys.exit(main() or 0)
