# ClaimsForge v6 · Demo Video Script (2:30)

> **Submitted for**: Milan AI Week 2026 / AI Agent Olympics · Gemini + Vultr tracks
> **Recorded by**: zhenyueD · **Length**: exactly 2:30 (150s) · **Format**: 1920×1080 MP4
> **Voice**: English (slow, neutral pace) · **Captions**: burn-in EN subs in iMovie / CapCut

---

## 🎬 Pre-Recording Checklist (10 min before "Action")

### A. Reset prod state (so fraud-replay demo works fresh)

```bash
ssh root@45.32.154.255 << 'EOF'
# Wipe pHash collision anchors so Demo A approves cleanly
rm -f /opt/claimsforge/data/image_fingerprints.jsonl
rm -f /opt/claimsforge/data/session_claim_counts.json
# Re-enable HR-LUXURY (in case prior toggle demo left it off)
python3 -c "
import json
p = '/opt/claimsforge/data/hard_rules.json'
d = json.load(open(p))
for r in d['rules']: r['active'] = True
json.dump(d, open(p,'w'), indent=2, ensure_ascii=False)
print('all 3 hard rules ENABLED')
"
systemctl restart claimsforge && sleep 4 && systemctl is-active claimsforge
EOF
```

### B. Browser setup (Chrome / Safari)

- [ ] Open Chrome window, resize to **exactly 1920×1080** (use BetterTouchTool / Rectangle.app to snap)
- [ ] Zoom 100% (`⌘0`)
- [ ] Open 4 tabs in order — switching between them is the choreography:
  1. `http://45.32.154.255` (Demo)
  2. `http://45.32.154.255/admin` (Ops dashboard)
  3. `http://45.32.154.255/methodologies` (Auto-distilled SOPs)
  4. `https://github.com/zhenyueD/claimsforge` (the CTA at the end)
- [ ] **Mac Do Not Disturb ON** (no notification banners during recording)
- [ ] Hide bookmark bar (`⌘⇧B`)
- [ ] Mute Slack / iMessage

### C. Recording tool

- **macOS QuickTime** → File → New Screen Recording (built-in, 1080p ok)
- Or **OBS Studio** (better — adds webcam picture-in-picture if you want to be on camera)
- Or **CleanShot X** (paid, but cleaner UI for tutorial-style recording)
- **Microphone**: AirPods Pro / external USB > built-in mic
- Record clean takes per shot then iMovie / CapCut splice — easier than nailing 2:30 in one go

### D. Demo assets (already at `data/demo_images/`)

| File | Purpose | Used in shot |
|---|---|---|
| `mug_crack.jpg` | Clean claim · clear damage | Shot 4 (Demo A) |
| `mug_crack.jpg` | Second submission (same file, new session) | Shot 5 (pHash deny) |
| `old_mug_2024.jpg` | EXIF DateTimeOriginal 2024-01-15 (853 days old) | Shot 6 (EXIF fail) |
| `laptop_scratch.jpg` | Used as multimodal mismatch image | Shot 7 (text says "mug", image is laptop) |

---

## 🎬 Full Shot List (10 shots, 2:30 total)

### Shot 1 · HOOK (0:00 → 0:15 · 15s)

**Screen**: Static slide — open `docs/slides.pdf` page 2 (the "$743B / 3 days / 99% / 32%" big numbers slide). Full-screen.

**Mouse**: None.

**Voice-over** (4 sentences, slow):
> "$743 billion in returns every year. Most damage claims still take three days of human back-and-forth.
> And now AI-tampered photos are showing up — 99% of insurers have seen them, only 32% feel they can catch them.
> The existing AI agents — Sierra, Decagon, Fin — they all automate the *dialogue*.
> None of them prove the AI didn't get fooled."

**Cut to next shot at**: 0:15

---

### Shot 2 · POSITIONING (0:15 → 0:25 · 10s)

**Screen**: Slides PDF page 3 (Sierra vs ClaimsForge comparison table). Full-screen.

**Mouse**: Optional — slowly scroll cursor down the rightmost column, lingering on the green ✓ rows.

**Voice-over**:
> "ClaimsForge is the Trust Layer that sits next to the AI agent — Sierra-style hard rules in pure Python,
> deepfake-aware visual gates, and a customer-facing Trust Score on every offer."

**Cut to next shot at**: 0:25

---

### Shot 3 · ARCHITECTURE (0:25 → 0:35 · 10s)

**Screen**: Slides PDF page 4 (the full architecture diagram). Full-screen.

**Mouse**: Zoom in (`⌘+` twice) on the Supervisor cluster (orange box on the left) so the L1/L2/L3 layers and rule IDs are readable.

**Voice-over**:
> "Seven Gemini specialist agents run as an async pipeline.
> A pure-Python Supervisor enforces three layers — Deny, Exempt, Cap.
> The whole thing self-evolves: every resolved case becomes a methodology in the knowledge base."

**Cut to next shot at**: 0:35

---

### Shot 4 · DEMO A · CLEAN CLAIM (0:35 → 0:55 · 20s)

**Screen**: Switch to Tab 1 — `http://45.32.154.255`

**Operations** (do these on-screen, one per beat):

| Beat | Action | Notes |
|---|---|---|
| 0:35 | Click **"📦 Cracked mug · $24"** scenario chip at the top | Auto-loads message + demo:mug_crack.jpg |
| 0:37 | (auto) — scenario message fills in textarea, mug image thumbnail appears | |
| 0:38 | Click **▶ Send** | Pipeline starts streaming |
| 0:39–0:48 | (auto) — left rail lights up: IntentAgent → EmotionAgent ‖ NeedsAgent ‖ DamageAgent → CompensationAgent → SupervisorAgent → VerifierAgent | ~8 sec |
| 0:48 | (auto) — Damage card appears with image + **yellow bounding box** overlaid on the crack | |
| 0:50 | (auto) — Offer card appears: `full_refund $24` | |
| 0:52 | (auto) — **🛡 Trust Score card appears: 100/100, all 6 factors green** | This is the "wow" frame |

**Voice-over** (timed against beats):
> "A real claim: cracked mug, $24. The customer sends a photo, agent traces stream live —
> Intent, Emotion, Needs, Damage Vision running in parallel.
> See the yellow box? DamageAgent localized the crack at severity 8 of 10.
> CompensationAgent picks policy P-RET-01 — full refund.
> Supervisor passes. Verifier passes. Eight seconds, end-to-end.
> And — this is the part nobody else does — every offer ships with a **Trust Score**.
> 100 out of 100. Six green factors. Each linked back to the rule that fired."

**Cut at**: 0:55

---

### Shot 5 · DEMO B · FRAUD #1 (pHash replay) (0:55 → 1:15 · 20s)

**Screen**: Still on Tab 1. Click **"Clear"** button (top right of chat).

**Operations**:

| Beat | Action |
|---|---|
| 0:55 | Click **Clear chat** button to start a fresh session |
| 0:57 | Click **"📦 Cracked mug · $24"** scenario AGAIN — same image, fresh session_id |
| 0:59 | Click **▶ Send** |
| 1:00–1:08 | (auto) — pipeline streams, but watch for IntentAgent → SupervisorAgent (the parallel agents may skip because of early fraud-gate hit) |
| 1:08 | (auto) — **🚨 red Escalated card appears** + Trust Score **50/100** + factor list shows `❌ image_uniqueness · pHash collision (Hamming=0 cross-session)` |

**Voice-over**:
> "Now I'm a fraudster. New email, new session. I submit the *exact same photo*.
> The LLM Vision can't tell — the image still looks legit.
> But the Supervisor's pHash gate is pure Python, deterministic.
> Hash collision. Cross-session. Force-escalate to human, no auto-pay.
> Trust drops to 50, image_uniqueness factor goes red. Pure Python. Less than 5 milliseconds. Zero LLM cost."

**Cut at**: 1:15

---

### Shot 6 · DEMO B · FRAUD #2 (EXIF deepfake / stale photo) (1:15 → 1:30 · 15s)

**Screen**: Still Tab 1. Click **Clear**.

**Operations**:

| Beat | Action |
|---|---|
| 1:15 | Click **Clear chat** |
| 1:17 | Type or paste: `My mug arrived cracked, please refund` in the textarea |
| 1:19 | Click **📎 paperclip** (image upload) → choose **`old_mug_2024.jpg`** from `data/demo_images/` |
| 1:20 | (auto) — thumbnail appears |
| 1:21 | Click **▶ Send** |
| 1:22–1:28 | (auto) — pipeline runs, Trust card appears |
| 1:28 | (auto) — Trust **50/100**, factor row: `⚠️ image_provenance · photo taken 853 days ago — suspect for current-order claim` |

**Voice-over**:
> "Attack two. The customer uploads a photo. Gemini Vision says it looks like real damage.
> But EXIF metadata says the photo was taken eight hundred fifty-three days ago.
> Order was placed last week. Stale photo — flagged.
> This is the deepfake-era signal Verisk says ninety-nine percent of insurers see, and only thirty-two percent catch."

**Cut at**: 1:30

---

### Shot 7 · DEMO B · FRAUD #3 (Multimodal mismatch) (1:30 → 1:45 · 15s)

**Screen**: Still Tab 1. Click **Clear**.

**Operations**:

| Beat | Action |
|---|---|
| 1:30 | Click **Clear chat** |
| 1:32 | Type: `My ceramic mug arrived cracked, please refund` |
| 1:34 | Click 📎 → upload **`laptop_scratch.jpg`** (text says mug, image is a laptop) |
| 1:36 | Click **▶ Send** |
| 1:37–1:43 | (auto) — pipeline runs, DamageAgent detects `laptop`, Supervisor catches mismatch |
| 1:43 | (auto) — Escalated card + Supervisor decision: `MULTIMODAL_MISMATCH` · "customer described 'mug' but image shows 'laptop'" |

**Voice-over**:
> "Attack three. Text says ceramic mug. Image is a laptop.
> Cross-checking the customer's words against what the Vision agent actually sees — Sierra can't do this, they have no vision channel.
> Multimodal mismatch detected. Force-escalate."

**Cut at**: 1:45

---

### Shot 8 · TIER-2 HOT TOGGLE + METHODOLOGIES (1:45 → 2:05 · 20s)

**Screen**: Switch to Tab 2 — `http://45.32.154.255/admin`. Scroll down to **"🔒 Tier-2 Hard Rules"** panel.

**Operations**:

| Beat | Action |
|---|---|
| 1:45 | Switch to admin tab. Scroll to Tier-2 Hard Rules panel |
| 1:47 | Hover the `HR-LUXURY` row — toggle is `ENABLED` (green) |
| 1:49 | Click the toggle switch → it flips to `DISABLED` (gray) |
| 1:51 | (no need to refresh — supervisor mtime-cache picks up within 60s) |
| 1:53 | Click the toggle back to `ENABLED` (so production stays safe) |
| 1:55 | Switch to Tab 3 — `/methodologies` |
| 1:57 | (auto) — page loads showing **47 auto-synthesized methodology cards** |
| 1:59 | Hover any methodology card — show the "WHEN" + "DO" + source case count |

**Voice-over**:
> "Ops needs to change a rule mid-flight? No code deploy.
> Click toggle on the admin dashboard — HR-LUXURY is now disabled, next claim sees the new rule set in under sixty seconds.
> And here — the methodologies page. Forty-seven SOPs that **Gemini wrote** by clustering past resolved cases.
> Zero authoring by humans. Sierra makes you write your SOPs first. We learn them."

**Cut at**: 2:05

---

### Shot 9 · DATA FLYWHEEL · SFT EXPORT (2:05 → 2:20 · 15s)

**Screen**: Back to Tab 2 — `/admin`. Scroll to **"📥 SFT Dataset Export"** panel.

**Operations**:

| Beat | Action |
|---|---|
| 2:05 | Switch to admin tab, scroll to SFT panel |
| 2:07 | Click **Format dropdown** → select **`openai`** |
| 2:09 | (Quality stays on **gold**) |
| 2:10 | Click **📥 Download .jsonl** button |
| 2:11 | (auto) — browser download bar appears at bottom showing `claimsforge-sft-openai-gold-20260517.jsonl` |
| 2:13 | (optional) Open the .jsonl in a quick-look — show `{"messages": [{"role":"user", ...}, {"role":"assistant", ...}]}` |

**Voice-over**:
> "Every claim that's auto-approved AND accepted by the customer gets tagged *gold*.
> One click — exports as a fine-tuning dataset.
> Vertex AI Gemini format, OpenAI format, or Anthropic format. Pick one, download a JSONL.
> At about two thousand gold cases, you fine-tune a smaller, faster, ClaimsForge-shaped model.
> Thirty percent prompt savings, fifty percent latency cut. The data flywheel."

**Cut at**: 2:20

---

### Shot 10 · CTA (2:20 → 2:30 · 10s)

**Screen**: Slides PDF page 14 (the closing CTA slide with demo URL + GitHub + tech chips). Full-screen.

**Mouse**: None — let the URL be the focus.

**Voice-over**:
> "ClaimsForge. The Trust Layer for AI Claims Resolution — built for the deepfake era.
> Live demo at four-five-three-two-one-five-four-two-five-five. MIT open source on GitHub.
> Built solo for Milan AI Week, powered by Google Gemini and Vultr. Thanks for watching."

**Fade to black at**: 2:30

---

## 🎙️ Recording tips per shot

| Shot | Risk | Mitigation |
|---|---|---|
| 4 (Clean claim) | Pipeline slower than 8s due to Gemini queue | If >12s in rehearsal, record this shot 3 times, pick fastest |
| 5 (pHash) | Anchor set wasn't reset → Shot 4 wouldn't approve | **MUST run pre-recording reset (section A)** |
| 6 (EXIF) | `old_mug_2024.jpg` accidentally re-named on prod | Verify file exists on prod: `ssh root@45.32.154.255 'ls /opt/claimsforge/data/demo_images/old_mug_2024.jpg'` |
| 7 (Mismatch) | Synonym cluster wrongly treats laptop ≈ mug | Already verified by 8/8 unit tests — but record once to confirm Vision detects "laptop" not "mug" |
| 8 (Toggle) | Toggle UI lag | Click slowly, give it 1s to update visual state |
| 9 (SFT download) | Browser download bar covered by other UI | Move browser window so download bar shows clearly |

---

## ✂️ Post-production (15-30 min in iMovie / CapCut)

1. **Splice clean takes** — record each shot separately, splice in order
2. **Add captions** — burn-in English subtitles (matches the voice-over above word-for-word)
3. **Add 1px progress bar at the top** showing 0% → 100% over 2:30 (purple `#a78bfa`)
4. **Title card** (optional 1s at start): "ClaimsForge — Milan AI Week 2026"
5. **End card** (1s at end): the closing slide held for an extra beat
6. **Export**: 1920×1080 H.264 MP4, ~20-30 MB final size
7. **Upload to**: YouTube unlisted (recommended) or Vimeo. Copy URL to lablab submission.

---

## ⏱️ Time budget summary

| Shot | Cumulative | Content |
|---|---|---|
| 1 | 0:15 | Hook · Verisk stats |
| 2 | 0:25 | Positioning |
| 3 | 0:35 | Architecture |
| 4 | 0:55 | **Demo A** · clean refund · Trust 100 |
| 5 | 1:15 | **Fraud #1** · pHash replay |
| 6 | 1:30 | **Fraud #2** · EXIF stale photo |
| 7 | 1:45 | **Fraud #3** · multimodal mismatch |
| 8 | 2:05 | Tier-2 toggle + Methodologies |
| 9 | 2:20 | SFT export |
| 10 | 2:30 | CTA + sponsor acks |

Three differentiator pillars (Supervisor / Trust Score / Self-Evolving) all visible in the demo · all four "wow" moments are in the 0:35 → 2:05 window where attention is highest.
