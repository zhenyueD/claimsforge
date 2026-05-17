# ClaimsForge · 15-Page Talking Points (English)

> Companion to `docs/slides.pdf` v6 · Total ~4 minutes (suitable for video recording or live demo)
> Chinese counterpart: `docs/demo-talking-points-zh.md`
> Heavy-detail pages: **P7 (Trust Score) / P10 (Duplicate + Tier-2) / P11 (Methodology Loop)**

---

## 🎙️ Opening (30s · stand up and say this first)

> Hi judges. **Seven hundred forty-three billion dollars** — that's annual e-commerce returns in the US in 2025. Most low-value damage claims still take three days of back-and-forth between customer and support. We built **ClaimsForge** — seven Gemini agents that resolve a claim in eight seconds, **and prove the AI wasn't fooled**. That last part is what makes us different from everything else in this hackathon.

---

## P1 · Hero

> ClaimsForge — **the Trust Layer for AI Claims Resolution**. Multi-agent Gemini auto-resolves e-commerce damage claims, **with an auditable Trust Score proving the AI wasn't fooled**. Demo URL here, GitHub here, MIT open source.

---

## P2 · The Problem

> Three numbers. One — seven hundred forty-three billion dollars in returns every year. Two — three days of human back-and-forth for a single low-value claim. Three — and this is the new pain point — per the Verisk 2026 report, **ninety-nine percent of insurers have already seen AI-tampered evidence photos, and only thirty-two percent feel confident they can catch them**.
>
> Sierra, Decagon, Intercom Fin — these AI agents all solve "how AI talks to the customer." **Nobody solves "how to prove the AI wasn't fooled."**

---

## P3 · Positioning

> Row by row, Sierra and Decagon versus ClaimsForge. **Red is what they don't do. Green is what we do.**
>
> Look at three rows. First — **multimodal vision adjudication**. They're text and voice only, we see images. Second — **deepfake defense** — pHash visual fingerprint plus EXIF metadata plus text-image consistency. Third — **the SOP loop runs backwards**. Sierra makes you author an SOP first, then they compile it into an agent. We let the agent **auto-write SOPs from real cases**.

---

## P4 · Architecture

> Full architecture. **Seven Gemini specialist agents** as a pipeline. The orange box in the middle is our **Supervisor** — our core safety net. The green cluster on the right is the knowledge base — one thousand three hundred and twenty-nine policies and SOPs. Bottom left is the handoff queue. The whole thing is pure Python — **zero LangChain, zero orchestration frameworks**.
>
> Next page I'll walk through this as a timeline.

---

## P5 · Sequence Timeline

> Customer uploads a photo. **Eight point two seconds** end-to-end, including the Trust Score.
>
> Look at the two-point-one-second row — **three agents run in parallel via `asyncio.gather`** — emotion analysis, needs detection, and damage vision all at the same time. Sequential would take five seconds. Parallel takes one-point-seven.
>
> The Supervisor layer in the middle is pure Python — **sub-millisecond, no LLM call**. That's why we're both fast and safe.

---

## P6 · Supervisor · IAM 3 Layers

> The Supervisor is our **first key differentiator**. **AWS-IAM-style three layers**:
>
> Layer one — DENY. Seven hard rules. Any match short-circuits to human escalation. Includes pHash fraud, duplicate order, multimodal mismatch, legal threat — these.
>
> Layer two — EXEMPT. Carve-outs. For example, perishable food doesn't need a photo as evidence.
>
> Layer three — CAP. Numerical clamps. Five hundred dollar ceiling. Cannot exceed one hundred percent of order value.
>
> The four red RULE chips up top are code-hardcoded. The three orange HR chips below are **Tier-2 data-driven** — the merchant edits a JSON file directly. No code deploy. **Sierra customers cannot do this.**

---

## P7 · Trust Score (🌟 KEY)

> This is our **second key differentiator** — modeled on Stripe Radar's risk-score idea. Every auto-resolved offer ships with **a zero-to-one-hundred Trust Score in the top right**, six independent factors listed below, each one explained.
>
> Look at the sample card on the right. **This claim only scored fifty** — because image_uniqueness went red, the pHash gate detected this image was used in a previously approved claim. The other five factors are all green. **Key design choice: if any factor goes red, the total is capped at fifty** — we don't let "five green plus one red" cheese a high score past the gate.
>
> And — see that little `RULE-FRAUD-REPLAY` chip — **every factor links back to the supervisor rule that drove it**. Audit-grade evidence for compliance and finance teams.

**Why this page matters**: This is the page that turns "the AI wasn't fooled" from an engineering capability into a sales artifact. Sierra shows the customer "the AI wrote you a letter." We show the customer "the AI proved it wasn't fooled" — that's not even in the same league.

**Likely judge questions**:
- *"How is this different from Stripe Radar?"* → Stripe scores financial transactions. We score AI decisions.
- *"Why aren't the six weights equal at one-sixth each?"* → image_uniqueness is weighted point-two-zero because pHash is the strongest fraud signal. emotion_gating is point-one-zero because it's a supporting signal, not ground truth.

---

## P8 · Demo A · Clean Claim

> A clean case. Customer sends a cracked mug photo, twenty-four dollar order.
>
> Seven-stage pipeline — intent classification, emotion, needs, damage vision (**watch the bbox draw the crack outline**), Compensation picks the policy, Supervisor passes, Verifier passes. **Eight seconds, done.**
>
> What the customer sees is the card on the right — a bilingual reply, **Trust Score one hundred out of one hundred** below it, six green checks. Then three buttons: ✅ Accept, ↩ Reject and renegotiate, 👤 Talk to human. Click Accept and the case auto-labels as **gold** — that goes into the fine-tune dataset.

---

## P9 · Demo B · Three Fraud Attacks

> Our three-punch anti-fraud:
>
> Attack one — **same image, new session**. pHash detects the fingerprint collision. Zero LLM cost. Less than five milliseconds to deny.
>
> Attack two — **EXIF says the photo was taken in early 2024**, but the order is from last week. image_provenance factor goes red, Trust Score drops.
>
> Attack three — **text says "my mug cracked," but the image is a smartphone**. Multimodal Mismatch catches it. **Sierra has no vision channel. They literally cannot do this.**
>
> The green box at the bottom is our E2E test suite — **twenty cases, one hundred percent pass**, including the three attacks above plus prompt injection, amount over-claim, emotion spoofing, and multi-turn inconsistency.

---

## P10 · Demo C · Duplicate + Tier-2 Toggle (🌟 KEY)

> This page is about **business operators having real-time control over the AI**.
>
> Left side — duplicate refund. Customer already accepted a refund for this order, comes back asking for another one. Our Supervisor scans history, **zero LLM calls, milliseconds to deny**. **Direct savings for the merchant on every fraudulent refund replay.**
>
> Right side — Tier-2 hot rule toggle. Black Friday flash sale, ops wants to temporarily disable the luxury-category escalation rule. **Click this switch — atomic JSON write, propagates to all prod instances within sixty seconds, no downtime, no deploy.** Sierra customers cannot do this — to change a single rule, they need the Sierra team to come on-site and reconfigure.

**Why this page matters**: This page conveys "**business operators have real control over the AI**." A merchant's biggest fear is "the AI is a black box, when it breaks I can't fix it." This page directly kills that fear.

**Likely judge questions**:
- *"Why not let the LLM decide if it's a duplicate?"* → LLMs get talked out of it by "I never received the first refund" customer language. Pure Python history scan is ground truth.
- *"Is Tier-2 safe? Could the business team write bad JSON and crash the system?"* → The DSL is a **strict whitelist**, only five operators supported. Unknown operator → safe-default false, rule never fires. Cannot crash the supervisor.

---

## P11 · Methodology Loop (🌟 KEY)

> This is our **third differentiator**. Sierra and Decagon make you author SOPs first, then compile them into an agent. **Ninety percent of small and mid-market merchants have never written an SOP**.
>
> We run the loop backwards. Every five resolved cases, **case_synthesizer clusters them automatically and asks Gemini to write a methodology**. Look at the card on the top right — this SOP is called "Cracked Mug Damage Resolution." **The WHEN and DO sections were written by Gemini from eight real cases. No human authored or reviewed them.**
>
> Look at the three numbers below — **eighty-nine auto-synthesized methodologies, one thousand three hundred twenty-nine total KB entries, one hundred percent embedding coverage**. Every new case makes the system smarter. **This is the first tooth of the data flywheel.**
>
> You can open `/methodologies` and see all eighty-nine, searchable and filterable by domain.

**Why this page matters**: Self-learning is the heart of the "data flywheel" pitch. Judges want to hear this because it represents **compounding growth** — and **a moat that gets stronger with use**.

**Likely judge questions**:
- *"What if Gemini writes the SOP wrong?"* → Three lines of defense: each entry carries a quality_score, must pass the Supervisor's hard rules, and customer-acceptance feedback reweights it over time.
- *"Is eighty-nine enough?"* → Enough for demo. Combined with the ingested SOP docs we have one thousand two hundred and four human_sop entries plus eighty-nine auto-synthesized — searches always hit.

---

## P12 · Data Flywheel · SFT Export

> **The second tooth of the data flywheel.** Every case is auto-labeled — Supervisor approves and customer accepts equals **gold**; Supervisor approves but no accept yet equals **normal**; Supervisor blocks equals **red_flag**.
>
> Merchants get one-click fine-tune dataset export — **native format for Vertex AI, OpenAI, or Anthropic, your pick**. Accumulate two thousand gold cases, run one fine-tune job, **model gets thirty percent smaller, fifty percent faster**. The data flywheel — smarter with every claim, cheaper with every fine-tune.

---

## P13 · Business Value

> Four numbers. **Ninety percent auto-resolution target. Eight-second resolution. Thirty-minute self-deploy. Zero dollar license fee.** Compare to Sierra at two hundred thousand to two million ARR per contract, three to six months PoC.
>
> Left side — what the merchant gets day one. Right side — the customers we target. **Shopify and WooCommerce mid-market** (too small for Sierra). **Insurance MGAs** (need deepfake screening plus auditable decisions for regulators). **Cross-border e-commerce** (native bilingual, no translation layer). **SaaS subscription cancellations** (Trust Score plus emotion gating equals retention saves).

---

## P14 · CTA

> Demo URL here. If you only do one thing — **open the demo, click "Angry customer plus legal threat," watch the Supervisor's RULE-LEGAL-THREAT fire, Trust Score drops to fifty**. Thirty seconds. The whole pitch on one screen.
>
> MIT open source. GitHub repo on the right. Thanks to Google Gemini and Vultr Cloud for the support. Thank you.

---

## 🎙️ Q&A Defense (likely judge questions)

| Question | Your answer |
|---|---|
| Why seven agents, not one? | Each agent is **structured output, auditable**. One mega-agent is a black box, you can't trace which step failed. |
| How is this different from LangGraph / CrewAI? | We **deliberately don't use a framework**. Five hundred and twenty-two lines of orchestrator plus one ClaimContext is enough. Frameworks add complexity that isn't worth the upside here. |
| How is the Trust Score calibrated? | Current weights are hand-tuned from fifty real cases. Sprint 1 plan is RLAIF feedback to auto-calibrate. |
| Is pHash adversarial-fragile (crop, rotate)? | Five-bit threshold tolerates light transforms. Heavy semantic transforms need CLIP-level — already on the roadmap. |
| How many cases produced the eighty-nine methodologies? | KB has one thousand three hundred twenty-nine entries. case_synthesizer reads learned_cases.jsonl — real resolved cases — and clusters. Each methodology is tagged with how many cases it was derived from. |
| What about security and compliance? | Demo build is single-process, no JWT auth. Trust Score plus Supervisor are audit-ready. Sprint 1 adds the SOC2 hardening pack. |
| How do you make money? | Three tiers — SMB SaaS ($99 a month, 1000 claims included) / Enterprise self-host ($10k a year license) / Insurance MGA per-claim billing. |
| What's Sierra's valuation? | $4.5 billion after a16z's 2026 Q1 round. We don't fight them head-on — we go after the small and mid-market merchants they can't reach. |

---

## ⏱️ Pacing Reference

| Page | English word count | Spoken duration |
|---|---|---|
| Opening | 90 | 30s |
| P1 Hero | 35 | 15s |
| P2 Problem | 130 | 50s |
| P3 Positioning | 100 | 40s |
| P4 Architecture | 80 | 30s |
| P5 Sequence | 90 | 35s |
| P6 Supervisor | 130 | 50s |
| P7 Trust Score 🌟 | 200 | 75s |
| P8 Demo A | 130 | 50s |
| P9 Demo B | 150 | 55s |
| P10 Demo C 🌟 | 180 | 65s |
| P11 Methodology 🌟 | 220 | 80s |
| P12 Flywheel | 100 | 35s |
| P13 Business | 110 | 40s |
| P14 CTA | 70 | 25s |
| **Total** | **~1800 words** | **~11 minutes** |

If you need to compress to a 2:30 screen-recording: skip P1/P4/P12/P13, compress P2/P3/P5/P6/P8 to one sentence each. **Keep P7/P10/P11 full text** — they carry the differentiation.

---

## 🎙️ Reading tips

- **Pause after the page-opener sentence** to let the slide register
- **Slow down on numbers** (e.g. "ninety-nine percent" not "99 percent" rushed)
- **Don't translate technical terms** — say `RULE-FRAUD-REPLAY`, `pHash`, `asyncio.gather` as-is, they're memorable
- **For P7, P10, P11**: it's OK to take 60+ seconds each, these are the moments the judges remember
- **End each page with a deliberate beat**, like a comma in speech, so the next page transition isn't jarring
