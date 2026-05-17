#!/usr/bin/env python3
"""Batch-ingest a list of files from uploads_inbox into the unified KB.

Reads file paths from a text file (one per line) and runs each through
agents.ingestion.ingest_document. Skips images. Skips files already
present in unified_kb.jsonl by source_doc match. Concurrency=2 (Gemini
quota-friendly). Resumable: writes a progress log so a restart can
skip what's done.

Usage:
    python scripts/batch_ingest_inbox.py /tmp/today_new.txt
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))

from ingestion import ingest_document  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("batch-ingest")

PROGRESS_LOG = ROOT / "data" / "_ingest_progress.jsonl"
SKIP_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg"}
CONCURRENCY = 2  # Gemini-quota friendly


def _already_done() -> set[str]:
    """Read progress log; returns the set of paths that succeeded."""
    done = set()
    if PROGRESS_LOG.exists():
        for line in PROGRESS_LOG.read_text().splitlines():
            try:
                row = json.loads(line)
                if row.get("ok"):
                    done.add(row["path"])
            except Exception:
                pass
    return done


def _record(path: str, ok: bool, **extra) -> None:
    PROGRESS_LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {"path": path, "ok": ok, "ts": time.time(), **extra}
    with PROGRESS_LOG.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def ingest_one(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"path": path, "ok": False, "err": "missing"}
    name = p.name
    try:
        blob = p.read_bytes()
        # Domain hint from top-level folder name
        parts = p.parts
        try:
            i = parts.index("uploads_inbox")
            domain = parts[i + 1] if len(parts) > i + 1 else "general"
        except ValueError:
            domain = "general"
        # Strip emoji-style colons that crash on some filesystems
        domain = domain.replace(":", "-")[:40]

        report = ingest_document(
            filename=name,
            blob=blob,
            contributor="inbox-batch-2026-05-17",
            domain_hint=domain,
            rebuild_embeddings=False,  # rebuild once at the end
        )
        return {
            "path": path,
            "ok": True,
            "raw": report.raw_chars,
            "chunks": report.chunks,
            "written": report.entries_written,
            "errors": len(report.errors),
        }
    except Exception as e:
        return {"path": path, "ok": False, "err": str(e)[:200]}


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: batch_ingest_inbox.py <filelist.txt>")
        return 1
    filelist = Path(sys.argv[1])
    if not filelist.exists():
        print(f"filelist not found: {filelist}")
        return 1

    paths = [
        line.strip() for line in filelist.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    paths = [p for p in paths if Path(p).suffix.lower() not in SKIP_EXT]

    done = _already_done()
    todo = [p for p in paths if p not in done]
    log.info("inbox: %d total | %d done | %d todo", len(paths), len(done), len(todo))
    if not todo:
        log.info("nothing to do")
        return 0

    t0 = time.monotonic()
    written_total = 0
    chunks_total = 0
    errors: list[str] = []
    completed = 0

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {ex.submit(ingest_one, p): p for p in todo}
        for fut in as_completed(futures):
            r = fut.result()
            completed += 1
            _record(r["path"], r["ok"], **{k: v for k, v in r.items() if k not in ("path", "ok")})
            if r["ok"]:
                written_total += r.get("written", 0)
                chunks_total += r.get("chunks", 0)
                log.info(
                    "[%d/%d] %s  → %d chunks / %d entries",
                    completed, len(todo), Path(r["path"]).name[:50],
                    r.get("chunks", 0), r.get("written", 0),
                )
            else:
                errors.append(f"{r['path']}: {r.get('err','?')}")
                log.warning("[%d/%d] FAIL %s — %s", completed, len(todo),
                            Path(r["path"]).name[:50], r.get("err", "?")[:100])

    elapsed = time.monotonic() - t0
    log.info("=" * 60)
    log.info("DONE in %.0fs · %d files · %d chunks · %d entries written · %d errors",
             elapsed, completed, chunks_total, written_total, len(errors))
    if errors:
        log.warning("errors (first 5):")
        for e in errors[:5]:
            log.warning("  %s", e[:120])
    return 0


if __name__ == "__main__":
    sys.exit(main())
