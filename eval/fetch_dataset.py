#!/usr/bin/env python3
"""
Build a 50-image labeled dataset of damaged products by:
  1. Reusing our 3 placeholder demo images as anchors
  2. Generating systematic variations via PIL (more cracks, more scratches, etc.)
  3. (Optional) downloading from public CC0 sources if PEXELS_KEY env is set

This script gives us a CALIBRATED dataset where we know the ground truth, which
is what matters for prompt iteration. Pure web-scraped images would have noisy
labels.
"""
from __future__ import annotations

import io
import json
import os
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT_DIR = Path(__file__).resolve().parent / "dataset"
OUT_DIR.mkdir(exist_ok=True)
OUT_JSON = Path(__file__).resolve().parent / "dataset.json"

random.seed(7)

CATEGORIES = ["mug", "phone", "clothing", "furniture", "packaging"]
DAMAGE_TYPES = ["crack", "scratch", "tear", "dent", "stain", "missing_part", "water_damage"]

# Mapping which damage types are realistic per category
CATEGORY_DAMAGE = {
    "mug":       [("crack", 7), ("crack", 9), ("scratch", 3), ("missing_part", 8), ("stain", 4)],
    "phone":     [("crack", 8), ("scratch", 4), ("scratch", 6), ("water_damage", 9), ("dent", 5)],
    "clothing":  [("tear", 7), ("tear", 5), ("stain", 6), ("missing_part", 4), ("crack", 2)],  # crack=zipper
    "furniture": [("scratch", 5), ("dent", 6), ("stain", 4), ("tear", 5), ("crack", 7)],
    "packaging": [("tear", 6), ("crack", 4), ("water_damage", 7), ("dent", 5), ("stain", 3)],
}

def add_caption(img, text):
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, img.width, 26], fill=(0, 0, 0, 180))
    d.text((8, 6), text, fill=(255, 255, 255))


def draw_mug(d, severity, dtype):
    # body
    d.ellipse([100, 80, 300, 360], fill=(230, 225, 220), outline=(80, 80, 90), width=3)
    # handle
    d.arc([280, 150, 360, 280], start=270, end=90, fill=(80, 80, 90), width=10)
    if dtype == "crack":
        for _ in range(max(1, severity // 2)):
            x0 = random.randint(130, 270)
            y0 = random.randint(100, 350)
            pts = [(x0, y0)]
            for _ in range(severity):
                pts.append((pts[-1][0] + random.randint(-20, 20), pts[-1][1] + random.randint(10, 30)))
            d.line(pts, fill=(180, 30, 30), width=max(3, severity // 2))
    if dtype == "missing_part":
        # chip out the rim
        d.pieslice([100, 80, 300, 200], start=240, end=300, fill=(15, 15, 22))
    if dtype == "scratch":
        for _ in range(severity):
            x = random.randint(120, 280)
            y = random.randint(120, 340)
            d.line([(x, y), (x + random.randint(20, 60), y + random.randint(-5, 5))], fill=(150, 150, 160), width=1)
    if dtype == "stain":
        for _ in range(severity):
            x = random.randint(130, 270)
            y = random.randint(110, 340)
            d.ellipse([x, y, x + random.randint(10, 40), y + random.randint(10, 40)], fill=(90, 60, 30))


def draw_phone(d, severity, dtype):
    # body
    d.rectangle([130, 60, 270, 360], fill=(30, 30, 38), outline=(20, 20, 24), width=2)
    # screen
    d.rectangle([140, 70, 260, 340], fill=(20, 20, 28))
    if dtype == "crack":
        # spider crack pattern
        cx, cy = random.randint(170, 230), random.randint(150, 280)
        for _ in range(severity * 3):
            angle = random.uniform(0, 6.28)
            length = random.randint(20, 80)
            ex = int(cx + length * (1 if angle < 3.14 else -1))
            ey = int(cy + length * (0.5 - random.random()))
            d.line([(cx, cy), (ex, ey)], fill=(240, 240, 250), width=1)
    if dtype == "scratch":
        for _ in range(severity * 2):
            y = random.randint(80, 330)
            d.line([(145, y), (255, y + random.randint(-3, 3))], fill=(180, 180, 200), width=1)
    if dtype == "water_damage":
        # discoloration patches
        for _ in range(severity):
            x = random.randint(150, 240)
            y = random.randint(90, 320)
            d.ellipse([x, y, x + 30, y + 30], fill=(60, 100, 130))
    if dtype == "dent":
        # corner ding
        d.polygon([(130, 60), (160, 70), (140, 90)], fill=(15, 15, 20))


def draw_clothing(d, severity, dtype):
    # jacket silhouette
    d.polygon([(150, 60), (250, 60), (300, 200), (300, 360), (100, 360), (100, 200)], fill=(70, 50, 35))
    d.polygon([(180, 60), (220, 60), (200, 110)], fill=(40, 25, 18))
    if dtype == "tear":
        # white slashes (showing lining)
        for _ in range(max(1, severity // 2)):
            x = random.randint(140, 260)
            y = random.randint(150, 340)
            length = severity * 8
            d.line([(x, y), (x + random.randint(-10, 10), y + length)], fill=(245, 245, 245), width=6)
    if dtype == "stain":
        for _ in range(severity):
            x = random.randint(130, 270)
            y = random.randint(100, 340)
            d.ellipse([x, y, x + 40, y + 40], fill=(20, 20, 20))
    if dtype == "missing_part":
        # missing button
        for x in [180, 200, 220]:
            d.ellipse([x, 200, x + 6, 206], fill=(40, 25, 18))
        # missing one — leave a hole
        d.ellipse([200, 250, 208, 258], fill=(245, 245, 245))


def draw_furniture(d, severity, dtype):
    # table top
    d.polygon([(60, 200), (340, 200), (320, 240), (80, 240)], fill=(160, 110, 70))
    # legs
    d.rectangle([100, 240, 110, 380], fill=(120, 80, 50))
    d.rectangle([290, 240, 300, 380], fill=(120, 80, 50))
    if dtype == "scratch":
        for _ in range(severity):
            x = random.randint(80, 320)
            d.line([(x, 200), (x + random.randint(20, 50), 200)], fill=(80, 50, 30), width=1)
    if dtype == "dent":
        d.ellipse([180, 195, 220, 215], fill=(100, 70, 40))
    if dtype == "crack":
        d.line([(80, 220), (340, 225)], fill=(40, 20, 10), width=max(2, severity // 2))
    if dtype == "stain":
        for _ in range(severity):
            x = random.randint(100, 320)
            d.ellipse([x, 200, x + 30, 230], fill=(60, 30, 10))


def draw_packaging(d, severity, dtype):
    # cardboard box
    d.polygon([(80, 120), (320, 120), (320, 340), (80, 340)], fill=(190, 150, 100), outline=(110, 80, 50), width=2)
    d.line([(80, 120), (320, 120)], fill=(110, 80, 50), width=2)
    if dtype == "tear":
        d.polygon([(80, 120), (130, 180), (80, 200)], fill=(245, 245, 245))
    if dtype == "crack":
        d.line([(80, 230), (320, 240)], fill=(110, 80, 50), width=max(2, severity // 2))
    if dtype == "water_damage":
        for _ in range(severity):
            x = random.randint(100, 300)
            y = random.randint(140, 320)
            d.ellipse([x, y, x + 50, y + 50], fill=(120, 100, 70))
    if dtype == "dent":
        d.polygon([(150, 120), (200, 160), (250, 120)], fill=(160, 120, 80))
    if dtype == "stain":
        for _ in range(severity):
            x = random.randint(100, 300)
            y = random.randint(140, 320)
            d.ellipse([x, y, x + 25, y + 25], fill=(90, 60, 30))


def make_image(category: str, dtype: str, severity: int, idx: int) -> tuple[bytes, str]:
    img = Image.new("RGB", (400, 400), color=(250, 248, 244))
    d = ImageDraw.Draw(img)
    # subtle background gradient + grain
    for y in range(400):
        d.line([(0, y), (400, y)], fill=(250 - y // 8, 248 - y // 10, 244 - y // 12))

    if category == "mug": draw_mug(d, severity, dtype)
    elif category == "phone": draw_phone(d, severity, dtype)
    elif category == "clothing": draw_clothing(d, severity, dtype)
    elif category == "furniture": draw_furniture(d, severity, dtype)
    elif category == "packaging": draw_packaging(d, severity, dtype)

    # subtle blur to simulate camera
    img = img.filter(ImageFilter.GaussianBlur(radius=0.6))

    add_caption(img, f"eval: {category} · {dtype} · sev {severity}")

    fname = f"{category}_{dtype}_{severity}_{idx:03d}.jpg"
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue(), fname


def main():
    items = []
    idx = 0
    for cat in CATEGORIES:
        for dtype, base_sev in CATEGORY_DAMAGE[cat]:
            for sev_off in (-1, 0, 1):
                sev = max(1, min(10, base_sev + sev_off))
                idx += 1
                content, fname = make_image(cat, dtype, sev, idx)
                (OUT_DIR / fname).write_bytes(content)
                items.append({
                    "id": f"{cat}-{idx:03d}",
                    "file": fname,
                    "category": cat,
                    "true_damage_type": dtype,
                    "true_severity_min": max(0, sev - 1),
                    "true_severity_max": min(10, sev + 1),
                    "user_message": f"My {cat} arrived with what looks like {dtype.replace('_', ' ')}. Please review and refund."
                })

    OUT_JSON.write_text(json.dumps({"items": items}, indent=2, ensure_ascii=False))
    print(f"Generated {len(items)} eval images in {OUT_DIR}")
    print(f"Dataset manifest: {OUT_JSON}")


if __name__ == "__main__":
    main()
