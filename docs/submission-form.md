# lablab.ai Submission Form — Copy-Paste Ready

提交时去 https://lablab.ai/ai-hackathons/milan-ai-week-hackathon 你的 dashboard，每个字段直接复制下面对应内容。

---

## 📋 Basic Information

### Project Title
```
ClaimsForge
```

### Short Description (≤ 200 chars)
```
Multi-agent AI for automated e-commerce damage claims. Gemini Vision reads the photo, 4 agents propose & verify compensation, end-to-end in 8 seconds.
```

### Long Description
```
ClaimsForge automates the $743B/year e-commerce returns pain. Customers upload a photo of a damaged product and describe the issue. Four specialist Gemini agents collaborate end-to-end in ~8 seconds:

🎯 IntentAgent — classifies the request and extracts the order ID (Gemini 2.5 Flash + function calling).
📸 DamageAgent — Gemini 2.5 Flash Vision evaluates the photo and outputs structured JSON: damage type, severity 0-10, affected parts, confidence, reasoning.
💰 CompensationAgent — RAG-retrieves the matching policy from a 10-rule policy library, then proposes a tone-appropriate offer (full refund / partial / replacement / store credit) with policy citations.
✅ VerifierAgent — hard-caps the amount against policy limits, reviews the tone, supports a single revise loop, and escalates edge cases (low-confidence evidence, water damage on electronics, legal threats) to humans.

The agents share state via a typed Pydantic ClaimContext. The orchestrator is plain Python (~150 LOC, no LangChain). Each agent's progress is streamed to the browser over WebSocket as an "agent_trace" event, so users see the pipeline thinking in real time.

The project is built on top of an existing intelligent customer service backbone (easyclaw-demo) and adds the multi-agent claims pipeline, multimodal evidence assessment, and demo UI. Total new code: ~600 LOC across 8 files.

Deployed on Vultr Cloud Compute. Eligible for both Best Use of Gemini and Vultr Award.
```

### Technology & Category Tags
```
Gemini, Multi-Agent, Computer Vision, FastAPI, Python, Multimodal, Function Calling, Agentic Workflow, E-commerce, Vultr
```

### Tracks (multi-select)
- ✅ Agentic Workflows
- ✅ Enterprise Utility
- ✅ Multimodal Intelligence
- ✅ Collaborative Systems

---

## 📸 Cover Image and Presentation

### Cover Image
Upload: `docs/architecture.png`

### Video Presentation
After recording per `docs/demo-video-script.md`, upload to YouTube unlisted, paste link here.

Suggested title: `ClaimsForge — Multi-agent claims resolution (Milan AI Week 2026)`

### Slide Presentation
Upload: `docs/slides.pdf` (8 pages, 873 KB)
Or: upload to Google Slides, make link-shareable, paste link.

---

## 💻 App Hosting & Code Repository

### Public GitHub Repository
```
https://github.com/zhenyueD/claimsforge
```
✅ MIT licensed
✅ README has architecture, quickstart, Docker, Vultr deploy guide
✅ All 4 agents + orchestrator + UI present

### Demo Application Platform
```
Vultr Cloud Compute (Frankfurt) — Ubuntu 24.04 + nginx + systemd
```

### Application URL
```
http://45.32.154.255
```
✅ 已部署到 Vultr Cloud Compute Frankfurt，5/5 smoke 测试通过。

---

## 🏆 Eligible Awards

- ⭐ **Best Use of Gemini** — Native features: `response_schema` + Pydantic, `thinking_budget` per agent, multimodal in one call, function calling. Single API powers all 4 agents.
- ⭐ **Best Use of Vultr** — Deployed on Vultr Cloud Compute as the central system of record (planning, logs, demo URL all on Vultr).

---

## ✅ Pre-submit checklist

- [ ] Repo is public on GitHub
- [ ] LICENSE file present (MIT)
- [ ] README has all sections (problem, arch, quickstart, deploy, tracks)
- [ ] Demo URL reachable from outside the dev machine
- [ ] WebSocket works on the deployed URL (open browser dev tools, watch /ws)
- [ ] At least 1 demo scenario runs end-to-end on prod (cracked mug recommended)
- [ ] Video uploaded to YouTube unlisted
- [ ] Slides accessible (PDF link or Google Slides public)
- [ ] All 4 deliverables links filled in the submission form

## 🚨 Submission timing

- **Hard deadline:** 2026-05-19 22:00 CST
- **Recommended submit:** 2026-05-19 18:00 CST (4h buffer for last-minute fixes)
- **Do NOT** wait until the last hour — lablab.ai submission portal can be flaky under load
