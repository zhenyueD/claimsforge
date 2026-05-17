"""
Record each demo shot as a separate .webm via Playwright.

Outputs:
  /tmp/cf-demo/shot4-clean.webm           (~20s)
  /tmp/cf-demo/shot5-phash-replay.webm    (~20s)
  /tmp/cf-demo/shot6-exif-stale.webm      (~15s)
  /tmp/cf-demo/shot7-multimodal.webm      (~15s)
  /tmp/cf-demo/shot8-tier2-toggle.webm    (~20s)
  /tmp/cf-demo/shot9-sft-download.webm    (~15s)

Each webm is silent (Playwright doesn't record audio).
The ffmpeg compositor later overlays macOS `say` voice-over.

Requires:
  pip install playwright && playwright install chromium
  prod reset (data/image_fingerprints.jsonl cleared) so shot 4 approves
  clean before shot 5 replays the same image.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

DEMO_URL = "http://45.32.154.255"
DEMO_IMAGES = Path(__file__).resolve().parent.parent / "data" / "demo_images"
OUT_DIR = Path("/tmp/cf-demo")
VIEWPORT = {"width": 1920, "height": 1080}


def _reset_dir():
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)


async def _new_page(browser, name: str):
    ctx = await browser.new_context(
        viewport=VIEWPORT,
        record_video_dir=str(OUT_DIR / name),
        record_video_size=VIEWPORT,
    )
    page = await ctx.new_page()
    return ctx, page


async def _close_and_rename(ctx, name: str, target: Path):
    page = ctx.pages[0]
    video = page.video
    await ctx.close()
    if video:
        path = await video.path()
        if path:
            Path(path).rename(target)
            print(f"  saved → {target.name}")


async def shot4_clean(browser):
    print("[shot 4] clean claim · expects Trust ~100")
    ctx, page = await _new_page(browser, "shot4")
    try:
        await page.goto(DEMO_URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1500)
        # Click "Cracked mug $24" scenario chip
        await page.get_by_text("Cracked mug", exact=False).first.click(timeout=10000)
        await page.wait_for_timeout(1500)
        # Click send
        await page.locator("#sendBtn, button:has-text('Send')").first.click(timeout=5000)
        # Wait for trust score card to appear
        try:
            await page.wait_for_selector(".trust-card, .result-card.trust", timeout=25000)
        except Exception:
            await page.wait_for_timeout(15000)
        await page.wait_for_timeout(2500)
    finally:
        await _close_and_rename(ctx, "shot4", OUT_DIR / "shot4-clean.webm")


async def shot5_phash(browser):
    print("[shot 5] same image, new session · expects pHash deny")
    ctx, page = await _new_page(browser, "shot5")
    try:
        await page.goto(DEMO_URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1500)
        # Click clear (if a chat already exists) — fresh page so probably no-op
        try:
            await page.locator("button:has-text('Clear')").first.click(timeout=2000)
        except Exception:
            pass
        await page.wait_for_timeout(800)
        # Same scenario again — same image, new session_id
        await page.get_by_text("Cracked mug", exact=False).first.click(timeout=10000)
        await page.wait_for_timeout(1500)
        await page.locator("#sendBtn, button:has-text('Send')").first.click(timeout=5000)
        try:
            await page.wait_for_selector(
                ".result-card.escalated, .result-card.trust", timeout=25000
            )
        except Exception:
            await page.wait_for_timeout(15000)
        await page.wait_for_timeout(2500)
    finally:
        await _close_and_rename(ctx, "shot5", OUT_DIR / "shot5-phash-replay.webm")


async def shot6_exif(browser):
    print("[shot 6] EXIF stale-photo (old_mug_2024.jpg)")
    ctx, page = await _new_page(browser, "shot6")
    try:
        await page.goto(DEMO_URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1500)
        # Type message
        await page.locator("#msgInput, textarea").first.fill(
            "My mug arrived cracked, please refund"
        )
        # Upload old EXIF image
        async with page.expect_file_chooser() as fc_info:
            await page.locator(".input-attach, button:has-text('📎')").first.click(timeout=5000)
        fc = await fc_info.value
        await fc.set_files(str(DEMO_IMAGES / "old_mug_2024.jpg"))
        await page.wait_for_timeout(1500)
        await page.locator("#sendBtn, button:has-text('Send')").first.click(timeout=5000)
        try:
            await page.wait_for_selector(".result-card.trust", timeout=25000)
        except Exception:
            await page.wait_for_timeout(15000)
        await page.wait_for_timeout(2500)
    finally:
        await _close_and_rename(ctx, "shot6", OUT_DIR / "shot6-exif-stale.webm")


async def shot7_multimodal(browser):
    print("[shot 7] text says 'mug' + laptop image")
    ctx, page = await _new_page(browser, "shot7")
    try:
        await page.goto(DEMO_URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1500)
        await page.locator("#msgInput, textarea").first.fill(
            "My ceramic mug arrived cracked, please refund"
        )
        async with page.expect_file_chooser() as fc_info:
            await page.locator(".input-attach, button:has-text('📎')").first.click(timeout=5000)
        fc = await fc_info.value
        await fc.set_files(str(DEMO_IMAGES / "laptop_scratch.jpg"))
        await page.wait_for_timeout(1500)
        await page.locator("#sendBtn, button:has-text('Send')").first.click(timeout=5000)
        try:
            await page.wait_for_selector(".result-card.escalated", timeout=25000)
        except Exception:
            await page.wait_for_timeout(15000)
        await page.wait_for_timeout(2500)
    finally:
        await _close_and_rename(ctx, "shot7", OUT_DIR / "shot7-multimodal.webm")


async def shot8_tier2(browser):
    print("[shot 8] admin Tier-2 toggle + methodologies tab")
    ctx, page = await _new_page(browser, "shot8")
    try:
        await page.goto(f"{DEMO_URL}/admin", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2500)
        # Scroll down to Tier-2 panel
        await page.evaluate("window.scrollTo({top: document.body.scrollHeight * 0.6, behavior: 'smooth'})")
        await page.wait_for_timeout(2500)
        # Toggle HR-LUXURY off then on
        try:
            checkbox = page.locator("input[type='checkbox']").nth(-1)  # last = HR-LUXURY usually
            await checkbox.click(timeout=3000)
            await page.wait_for_timeout(2000)
            await checkbox.click(timeout=3000)  # re-enable
            await page.wait_for_timeout(1500)
        except Exception as e:
            print(f"  toggle skipped: {e}")
        # Navigate to methodologies
        await page.goto(f"{DEMO_URL}/methodologies", wait_until="networkidle", timeout=20000)
        await page.wait_for_timeout(3000)
    finally:
        await _close_and_rename(ctx, "shot8", OUT_DIR / "shot8-tier2-toggle.webm")


async def shot9_sft(browser):
    print("[shot 9] SFT dataset download from admin")
    ctx, page = await _new_page(browser, "shot9")
    try:
        await page.goto(f"{DEMO_URL}/admin", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2500)
        # Scroll to SFT panel
        await page.evaluate("window.scrollTo({top: document.body.scrollHeight * 0.45, behavior: 'smooth'})")
        await page.wait_for_timeout(2500)
        # Select format = openai
        try:
            await page.locator("#sft-format").select_option("openai", timeout=3000)
            await page.wait_for_timeout(1500)
        except Exception:
            pass
        # Click download
        try:
            async with page.expect_download(timeout=10000) as dl:
                await page.locator("#sft-download-btn").click(timeout=3000)
            d = await dl.value
            print(f"  download started: {d.suggested_filename}")
        except Exception as e:
            print(f"  click only: {e}")
        await page.wait_for_timeout(3000)
    finally:
        await _close_and_rename(ctx, "shot9", OUT_DIR / "shot9-sft-download.webm")


async def main():
    _reset_dir()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            await shot4_clean(browser)
            await shot5_phash(browser)
            await shot6_exif(browser)
            await shot7_multimodal(browser)
            await shot8_tier2(browser)
            await shot9_sft(browser)
        finally:
            await browser.close()
    print()
    print("=" * 60)
    for f in sorted(OUT_DIR.rglob("*.webm")):
        size_mb = f.stat().st_size / 1024 / 1024
        print(f"  {f.name}  ({size_mb:.1f}MB)")


if __name__ == "__main__":
    asyncio.run(main())
