# ClaimsForge — Demo Video Script (2:30)

Target length: **2:30** (Milan AI Week judges have a lot to watch — keep it punchy).
Format: screen recording with voice-over.
Tone: confident, builder-to-builder, no marketing fluff.

---

## Pre-recording checklist

- [ ] Server running on Vultr URL (or `http://localhost:8000`)
- [ ] Browser at fresh state — `clearChat()` clicked once
- [ ] Quiet room, headset mic, water nearby
- [ ] Screen recording: macOS Quick Time → File → New Screen Recording → record only the browser window (1280×720 is plenty)
- [ ] Background: do not let other notifications pop up (turn on Do Not Disturb)

---

## Script (with timestamps)

### [0:00 – 0:15] **Hook**

*(Open on the ClaimsForge UI, scenario bar visible)*

> "E-commerce returns cost retailers seven hundred and forty-three billion dollars a year. Most low-value damage claims — a broken mug, a scratched screen — are still resolved by humans reading photos and arguing over twenty bucks. We automated it."

### [0:15 – 0:30] **Pain point**

*(Cursor moves to scenario bar, then highlights the purple "🔥 试 Claim Demo" button)*

> "Today I'll show you ClaimsForge — a multi-agent AI built on Google Gemini that handles the whole loop in eight seconds, end to end."

*(Click "🔥 试 Claim Demo" — modal opens with 3 scenario cards)*

### [0:30 – 1:45] **The walk-through** (THE money shot)

*(Hover over "破裂马克杯" card)*

> "Three pre-seeded scenarios. Let's do the cracked mug. The customer uploaded a photo and described the problem."

*(Click "破裂马克杯" → modal closes, message + 📎 appears in chat)*

> "Watch four agents collaborate."

*(Agent trace lines appear one by one — read each as they show up)*

> "First, **IntentAgent** classifies the request — claim with image evidence, order ID extracted."
>
> *(DamageAgent line appears)*
> "Then **DamageAgent** calls Gemini 2.5 Vision. It reads the photo and outputs structured JSON — crack, severity eight out of ten, affected part: cup rim, ninety percent confidence."
>
> *(Damage card with thumbnail appears)*
> "Here's the evidence card — image thumbnail, severity bar, and Gemini's own reasoning quoted back to us."
>
> *(CompensationAgent line appears)*
> "**CompensationAgent** looks up ten compensation policies via RAG. Finds P-RET-zero-one — severity ≥ seven means full refund."
>
> *(Offer card appears — ¥24.00 green)*
> "Twenty-four yuan, full refund, with an empathetic justification it wrote itself."
>
> *(VerifierAgent line appears, approve)*
> "Finally **VerifierAgent** hard-caps the amount against policy limits, reviews the tone, and approves."
>
> *(Green final reply card)*
> "Done. Twelve seconds. The customer is refunded. No human ever touched this ticket."

### [1:45 – 2:10] **Business value**

*(Cut to slide 7 — the business numbers)*

> "Per one thousand low-value claims this lifts auto-resolution from five percent to ninety percent. Three thousand seven hundred fifty dollars of agent time saved. Eighteen-point projected CSAT lift. And the human queue gets the edge cases that actually need humans — water damage on a laptop, fifty-yuan claim with no photo, customer threatening legal action — all auto-escalated."

### [2:10 – 2:30] **CTA**

*(Cut to slide 8 — the QR + URL)*

> "ClaimsForge. Open source MIT. Live on Vultr. Built on Gemini two-point-five Flash with native function calling, response schemas, and multimodal in one call. Fork it, ship it, and stop arguing over twenty bucks. Thank you."

---

## Recording tips

1. **First take is rough — record 3 takes, pick the best.** Don't try to nail it in one.
2. **Talk slightly slower than feels natural** — voice-overs always sound rushed.
3. **Cursor should be deliberate** — pause on important elements for ~1s.
4. **Don't fix mistakes mid-recording** — finish the take, you can re-record.
5. **Mute the system sound** — recording shouldn't pick up browser dings.
6. **Final cut**: use CapCut (free) or iMovie. Just trim head/tail, no fancy effects.

## Export settings

- Format: **MP4 (H.264)**
- Resolution: **1280×720** (fits YouTube unlisted just fine)
- Frame rate: **30 fps**
- Audio: **AAC 128kbps mono**
- File size target: **< 30 MB** for fast upload

## Upload

1. YouTube → Upload → **Unlisted**
2. Title: `ClaimsForge — Multi-agent claims resolution (Milan AI Week 2026)`
3. Description: paste short version of README intro
4. Copy share link → paste into lablab.ai submission form's "Video" field
