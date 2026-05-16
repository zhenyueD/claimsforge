#!/usr/bin/env python3
"""
CLI: ingest one or more SOP / knowledge documents into the unified KB.

Usage:
  python scripts/ingest.py path/to/sop.pdf
  python scripts/ingest.py path/to/sop.pdf path/to/policy.docx path/to/notes.md
  python scripts/ingest.py path/to/*.pdf --domain refund --contributor "ops_team"
  python scripts/ingest.py --dir ./my_sops/ --contributor "ops_team"

After ingestion, every agent in the pipeline can retrieve the new knowledge
via embedding_index.hybrid_search.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))

from ingestion import ingest_document  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", help="files to ingest")
    ap.add_argument("--dir", help="ingest every file in this directory recursively")
    ap.add_argument("--ext", default="pdf,docx,md,txt", help="comma-separated extensions to include when --dir is used")
    ap.add_argument("--domain", default=None, help="domain hint passed to KB entries (e.g. 'refund')")
    ap.add_argument("--contributor", default="cli_ingest", help="contributor name stored on entries")
    ap.add_argument("--max-chunks", type=int, default=None, help="cap chunks per file (testing)")
    args = ap.parse_args()

    files = list(args.paths)
    if args.dir:
        exts = {e.strip().lower().lstrip(".") for e in args.ext.split(",") if e.strip()}
        for p in Path(args.dir).rglob("*"):
            if p.is_file() and p.suffix.lstrip(".").lower() in exts:
                files.append(str(p))
    if not files:
        ap.error("no files given. Pass paths or --dir <folder>.")

    # Allow shell globs that didn't expand (rare on macos)
    expanded = []
    for f in files:
        if "*" in f or "?" in f:
            expanded.extend(glob.glob(f))
        else:
            expanded.append(f)
    files = expanded

    total = {"files": 0, "entries": 0, "skipped": 0, "embedded": 0, "errors": []}
    print(f"Ingesting {len(files)} file(s)…")
    print()
    for f in files:
        p = Path(f)
        if not p.exists():
            print(f"  ⚠ skip (not found): {f}")
            continue
        blob = p.read_bytes()
        print(f"  → {p.name}  ({len(blob)/1024:.1f} KB)…", end="", flush=True)
        try:
            r = ingest_document(
                p.name, blob,
                contributor=args.contributor,
                domain_hint=args.domain,
                max_chunks=args.max_chunks,
            )
            mark = "✓"
            print(f"  {mark} {r.entries_written} entries, {r.skipped} skipped, {r.embedded} embedded ({r.elapsed_s}s)")
            total["files"] += 1
            total["entries"] += r.entries_written
            total["skipped"] += r.skipped
            total["embedded"] += r.embedded
            total["errors"].extend(r.errors)
        except Exception as e:
            print(f"  ✗ FAILED: {e}")
            total["errors"].append(f"{p.name}: {e}")

    print()
    print("===== TOTAL =====")
    print(json.dumps(total, indent=2))


if __name__ == "__main__":
    sys.exit(main() or 0)
