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
5-agent Gemini pipeline for e-commerce damage claims. Vision + emotion + policy DSL + verifier. 96.7% accuracy, 8 second resolution, bilingual reply, live learning loop.
```

### Long Description
```
ClaimsForge automates the $743B/year e-commerce returns pain. Customers upload a photo of a damaged product and describe the issue. Five specialist Gemini agents collaborate end-to-end in ~8 seconds with 96.7% damage-classification accuracy (measured on a 30-image labeled eval set).

🎯 IntentAgent — classifies the request and extracts the order ID. Recognizes legal-threat-only messages as claims (Gemini 2.5 Flash + function calling).

💗 EmotionAgent — grades customer affect on a calibrated 0–10 scale and returns structured signals: triggers, escalation markers (lawyer / regulator / media), and a suggested reply tone. Bilingual (English + Chinese, including 12315/消协 patterns). CRITICAL risk is auto-promoted whenever escalation signals are present, even if the customer writes politely.

📸 DamageAgent — Gemini 2.5 Flash Vision evaluates the photo and outputs structured JSON: damage type, severity 0-10, affected parts, confidence, reasoning. The reasoning is quoted back to the customer in the final reply. 96.7% type-and-severity accuracy on the eval harness.

💰 CompensationAgent — RAG-retrieves matching policies from a 26-rule policy DSL covering apparel, electronics, perishables, luxury, cross-border, seasonal, and emotion-aware uplifts. Also retrieves 93 entries of curated merchant wisdom (Amazon Seller / eBay Seller Center / Shopify Help / Reddit) and recent live precedent from the learning loop. Proposes a tone-appropriate offer (full refund / partial / replacement / store credit) with explicit policy citations.

✅ VerifierAgent — hard-caps amounts against policy limits, reviews the tone (banned phrases include "we apologize for any inconvenience"), supports a single revise loop, and escalates edge cases to humans.

📚 Continuous learning loop — every resolved claim is appended to data/learned_cases.jsonl. The next CompensationAgent call retrieves recent precedent for the same damage type. The system genuinely improves with usage; the live UI shows the Learning Queue updating.

Agents share state via a typed Pydantic ClaimContext. The orchestrator is plain Python (~200 LOC, no LangChain). Each agent's progress streams to the browser over WebSocket as an "agent_trace" event, so users see the pipeline thinking in real time.

Bilingual replies — the customer gets a response in the language they wrote in, in the voice of a senior CS specialist (5+ years on the floor at Amazon/Shopify Plus), not a template generator.

The eval harness (eval/run_damage_eval.py) ships with the repo and reruns in 30 seconds, producing a confusion matrix and failure cases. Prompt changes are validated against the labeled set before deploy.

Deployed on Vultr Cloud Compute (Frankfurt). Live demo: http://45.32.154.255. Eligible for both Best Use of Gemini and Vultr Award.
```

### Technology & Category Tags
```
Gemini, Multi-Agent, Computer Vision, FastAPI, Python, Multimodal, Function Calling, Agentic Workflow, E-commerce, Vultr, RAG, Structured Output, Emotion AI, Continuous Learning
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
