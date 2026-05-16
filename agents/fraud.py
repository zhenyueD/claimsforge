"""
Zero-trust visual fraud gate — perceptual-hash based image-replay detection.

Why this exists:
  Multimodal claim systems have a well-known attack: customer A reuses
  the same damage photo across two accounts (or two sessions on the same
  account after the first refund), or photoshops a single photo and
  resubmits with minor crops/recompression. The LLM cannot detect this
  because it judges the image semantically, not by identity.

  pHash collapses every approved-claim image to a 64-bit fingerprint and
  Hamming-distances new uploads against it. <=5 bits different on a
  64-bit hash is the standard "perceptually identical" threshold (handles
  JPEG recompression, mild crop, watermark removal, color rebalance).

Storage:
  data/image_fingerprints.jsonl — append-only. Each row is
  {image_id, phash, status: "uploaded" | "approved", session_id, ts}.
  Brute-force scan suffices below ~100K rows; at scale you'd swap in
  Milvus / Qdrant (the API stays the same).

This file is intentionally framework-free — pure Python + Pillow +
imagehash. Lives in agents/ for the supervisor.py import.
"""
from __future__ import annotations

import io
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_FP_PATH = Path(__file__).resolve().parent.parent / "data" / "image_fingerprints.jsonl"
_fp_lock = threading.Lock()

# Hamming-distance threshold below which two pHashes are considered the
# same image. 5 bits on a 64-bit hash ~ tolerates recompression + crop.
PHASH_COLLISION_THRESHOLD = 5


def compute_phash(image_bytes: bytes) -> Optional[str]:
    """Return a 16-hex-char (64-bit) pHash, or None if Pillow/imagehash
    can't open the bytes (corrupt upload, unsupported format)."""
    try:
        import imagehash  # local import — keeps optional dependency optional
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes))
        return str(imagehash.phash(img))
    except Exception as e:
        logger.warning("compute_phash failed: %s", e)
        return None


def _hex_to_bits(h: str) -> int:
    """Treat the 16-hex pHash as a 64-bit integer for fast xor/popcount."""
    return int(h, 16)


def _hamming(a: str, b: str) -> int:
    """Bits-different between two 16-hex pHashes."""
    return bin(_hex_to_bits(a) ^ _hex_to_bits(b)).count("1")


def record_upload(image_id: str, phash: str, session_id: str) -> None:
    """Log a fresh upload. We separate this from `approved` so that an
    uploaded-but-rejected image doesn't poison the collision set —
    only approved claims become future collision anchors."""
    _append({
        "image_id": image_id, "phash": phash, "status": "uploaded",
        "session_id": session_id, "ts": datetime.now().isoformat(),
    })


def record_approved(image_id: str, phash: str, session_id: str) -> None:
    """Promote a previously-uploaded image to the collision anchor set
    after compensation auto-approves it. Called by orchestrator."""
    _append({
        "image_id": image_id, "phash": phash, "status": "approved",
        "session_id": session_id, "ts": datetime.now().isoformat(),
    })


def _append(row: dict) -> None:
    """Atomic append behind a lock — protects against partial writes when
    two pipelines complete simultaneously."""
    try:
        _FP_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _fp_lock:
            with _FP_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("fingerprint append failed: %s", e)


def find_collision(
    phash: str,
    current_session_id: str,
    threshold: int = PHASH_COLLISION_THRESHOLD,
) -> Optional[dict]:
    """Scan the approved-image set for the closest match within `threshold`
    Hamming bits. Returns the matching row or None.

    Cross-session collisions (current_session_id != stored session_id) are
    the high-confidence fraud signal. Same-session collisions are usually
    the customer accidentally re-uploading the same image — we still flag
    them so the supervisor can decide.
    """
    if not _FP_PATH.exists():
        return None
    best: Optional[dict] = None
    best_dist = threshold + 1
    try:
        with _FP_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("status") != "approved":
                    continue
                stored = row.get("phash")
                if not stored or len(stored) != len(phash):
                    continue
                d = _hamming(phash, stored)
                if d <= threshold and d < best_dist:
                    best_dist = d
                    row["_hamming_distance"] = d
                    row["_cross_session"] = row.get("session_id") != current_session_id
                    best = row
    except Exception as e:
        logger.warning("find_collision scan failed: %s", e)
    return best


def stats() -> dict:
    """Lightweight counts for the admin dashboard."""
    if not _FP_PATH.exists():
        return {"approved": 0, "uploaded": 0, "total": 0}
    approved = uploaded = 0
    try:
        with _FP_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("status") == "approved":
                    approved += 1
                elif row.get("status") == "uploaded":
                    uploaded += 1
    except Exception:
        pass
    return {"approved": approved, "uploaded": uploaded, "total": approved + uploaded}
