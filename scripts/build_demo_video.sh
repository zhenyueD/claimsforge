#!/bin/bash
# Assemble the final 2:30 demo MP4 from:
#   - 4 static slide images (Hook / Positioning / Architecture / CTA)
#   - 6 Playwright-recorded webm shots (Demo A through SFT)
#   - macOS `say` TTS voice-over per shot
#
# Output: ~/Desktop/claimsforge-v6-demo.mp4 (1920x1080, H.264, AAC)

set -euo pipefail

REPO=~/code/claimsforge
SLIDES="$REPO/docs"
SHOTS=/tmp/cf-demo
VO=/tmp/cf-vo
OUT=~/Desktop/claimsforge-v6-demo.mp4

mkdir -p "$VO"
rm -f "$VO"/*

VOICE="Samantha"

# ─────────────────────────────────────────────────────────────
#  1) Generate voice-over per shot via macOS `say`
# ─────────────────────────────────────────────────────────────
echo "→ generating TTS voice-overs (voice: $VOICE)"

# Shot 1 — Hook (~15s)
say -v "$VOICE" -r 175 -o "$VO/shot1.aiff" \
  "743 billion dollars in returns every year. Most damage claims still take three days of human back-and-forth. And now AI-tampered photos are showing up. 99 percent of insurers have seen them. Only 32 percent feel they can catch them. Existing AI agents — Sierra, Decagon, Fin — they all automate the dialogue. None of them prove the AI didn't get fooled."

# Shot 2 — Positioning (~10s)
say -v "$VOICE" -r 175 -o "$VO/shot2.aiff" \
  "ClaimsForge is the Trust Layer that sits next to the AI agent. Sierra-style hard rules in pure Python, deepfake-aware visual gates, and a customer-facing Trust Score on every offer."

# Shot 3 — Architecture (~10s)
say -v "$VOICE" -r 175 -o "$VO/shot3.aiff" \
  "Seven Gemini specialist agents run as an async pipeline. A pure-Python Supervisor enforces three layers — Deny, Exempt, Cap. The whole thing self-evolves: every resolved case becomes a methodology in the knowledge base."

# Shot 4 — Clean claim (~20s)
say -v "$VOICE" -r 175 -o "$VO/shot4.aiff" \
  "A real claim: cracked mug, 24 dollars. The customer sends a photo. Agent traces stream live — Intent, Emotion, Needs, Damage Vision running in parallel. DamageAgent localizes the crack at severity 8 of 10. CompensationAgent picks policy P-RET-01, full refund. Supervisor passes. Verifier passes. Eight seconds end-to-end. And every offer ships with a Trust Score. 100 out of 100. Six green factors, each linked back to the rule that fired."

# Shot 5 — pHash replay (~20s)
say -v "$VOICE" -r 175 -o "$VO/shot5.aiff" \
  "Now I'm a fraudster. New session, new email. I submit the exact same photo. The LLM Vision can't tell. But the Supervisor's pHash gate is pure Python, deterministic. Hash collision. Cross-session. Force-escalate to human, no auto-pay. Trust drops to 50, image uniqueness factor goes red. Less than 5 milliseconds. Zero LLM cost."

# Shot 6 — EXIF stale photo (~15s)
say -v "$VOICE" -r 175 -o "$VO/shot6.aiff" \
  "Attack two. The customer uploads a photo. Gemini Vision says it looks like real damage. But EXIF metadata says the photo was taken 853 days ago. Order was placed last week. Stale photo. Flagged. The deepfake-era signal 99 percent of insurers see, but only 32 percent catch."

# Shot 7 — Multimodal mismatch (~15s)
say -v "$VOICE" -r 175 -o "$VO/shot7.aiff" \
  "Attack three. Text says ceramic mug. Image is a laptop. Cross-checking what the customer wrote against what Vision actually saw. Sierra cannot do this. They have no vision channel. Multimodal mismatch. Force-escalate."

# Shot 8 — Tier-2 toggle + Methodologies (~20s)
say -v "$VOICE" -r 175 -o "$VO/shot8.aiff" \
  "Ops needs to change a rule mid-flight? No code deploy. Click the toggle on the admin dashboard. HR-LUXURY is now disabled. Next claim sees the new rule set in under 60 seconds. And here — the methodologies page. 89 SOPs that Gemini wrote, by clustering past resolved cases. Zero authoring by humans. Sierra makes you write your SOPs first. We learn them."

# Shot 9 — SFT export (~15s)
say -v "$VOICE" -r 175 -o "$VO/shot9.aiff" \
  "Every claim that's auto-approved AND accepted by the customer gets tagged gold. One click. Exports as a fine-tuning dataset. Vertex AI, OpenAI, or Anthropic format. At about 2000 gold cases, you fine-tune a smaller, faster, ClaimsForge-shaped model. Thirty percent prompt savings. Fifty percent latency cut. The data flywheel."

# Shot 10 — CTA (~10s)
say -v "$VOICE" -r 175 -o "$VO/shot10.aiff" \
  "ClaimsForge. The Trust Layer for AI Claims Resolution. Live demo at 45.32.154.255. MIT open source on GitHub. Built solo for Milan AI Week, powered by Google Gemini and Vultr. Thanks for watching."

echo "  voice-overs done"

# ─────────────────────────────────────────────────────────────
#  2) Build per-shot MP4 (image OR webm + voice-over, padded to voice length)
# ─────────────────────────────────────────────────────────────
echo "→ building per-shot MP4 segments"

build_static() {
  local idx="$1"; local img="$2"; local audio="$3"; local out="$4"
  # Loop the image for the duration of the audio, scale to 1920x1080
  ffmpeg -y -loop 1 -i "$img" -i "$audio" \
    -vf "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,setsar=1" \
    -c:v libx264 -tune stillimage -pix_fmt yuv420p \
    -c:a aac -b:a 192k -shortest -r 30 \
    "$out" 2>&1 | tail -1
}

build_motion() {
  local idx="$1"; local webm="$2"; local audio="$3"; local out="$4"
  # Take the webm video. If webm shorter than audio, loop the last frame.
  # If longer, trim to audio length.
  local v_dur a_dur
  a_dur=$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$audio")
  v_dur=$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$webm")
  echo "    shot $idx: video=${v_dur}s audio=${a_dur}s"
  # Use audio's duration as the final segment length; loop video via filter
  ffmpeg -y -stream_loop -1 -i "$webm" -i "$audio" \
    -vf "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=30" \
    -c:v libx264 -pix_fmt yuv420p \
    -c:a aac -b:a 192k -t "$a_dur" -r 30 \
    "$out" 2>&1 | tail -1
}

# Static slides for shots 1, 2, 3, 10
build_static 1 "$SLIDES/slide_v6-02.png" "$VO/shot1.aiff" /tmp/cf-seg-01.mp4
build_static 2 "$SLIDES/slide_v6-03.png" "$VO/shot2.aiff" /tmp/cf-seg-02.mp4
build_static 3 "$SLIDES/slide_v6-05.png" "$VO/shot3.aiff" /tmp/cf-seg-03.mp4
# Motion segments for 4-9
build_motion 4 "$SHOTS/shot4-clean.webm"        "$VO/shot4.aiff" /tmp/cf-seg-04.mp4
build_motion 5 "$SHOTS/shot5-phash-replay.webm" "$VO/shot5.aiff" /tmp/cf-seg-05.mp4
build_motion 6 "$SHOTS/shot6-exif-stale.webm"   "$VO/shot6.aiff" /tmp/cf-seg-06.mp4
build_motion 7 "$SHOTS/shot7-multimodal.webm"   "$VO/shot7.aiff" /tmp/cf-seg-07.mp4
build_motion 8 "$SHOTS/shot8-tier2-toggle.webm" "$VO/shot8.aiff" /tmp/cf-seg-08.mp4
build_motion 9 "$SHOTS/shot9-sft-download.webm" "$VO/shot9.aiff" /tmp/cf-seg-09.mp4
# Static CTA
build_static 10 "$SLIDES/slide_v6-15.png" "$VO/shot10.aiff" /tmp/cf-seg-10.mp4

# ─────────────────────────────────────────────────────────────
#  3) Concat all segments
# ─────────────────────────────────────────────────────────────
echo "→ concatenating into final MP4"

cat > /tmp/cf-concat.txt <<EOF
file '/tmp/cf-seg-01.mp4'
file '/tmp/cf-seg-02.mp4'
file '/tmp/cf-seg-03.mp4'
file '/tmp/cf-seg-04.mp4'
file '/tmp/cf-seg-05.mp4'
file '/tmp/cf-seg-06.mp4'
file '/tmp/cf-seg-07.mp4'
file '/tmp/cf-seg-08.mp4'
file '/tmp/cf-seg-09.mp4'
file '/tmp/cf-seg-10.mp4'
EOF

ffmpeg -y -f concat -safe 0 -i /tmp/cf-concat.txt -c copy "$OUT" 2>&1 | tail -2

# ─────────────────────────────────────────────────────────────
#  Summary
# ─────────────────────────────────────────────────────────────
DUR=$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$OUT")
SIZE=$(ls -lh "$OUT" | awk '{print $5}')
echo ""
echo "============================================================"
echo "  OUT: $OUT"
echo "  duration: ${DUR}s · size: $SIZE"
echo "============================================================"
