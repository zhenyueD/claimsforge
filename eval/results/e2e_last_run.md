# E2E Eval · 2026-05-16 22:01

## Summary
- Cases: **13**
- Joint accuracy: **84.6%** (11/13)
- p50 latency: **10008ms**
- Elapsed: 59.7s

## By category
- language: 2/2
- emotion: 2/2
- intent_routing: 2/2
- needs_inference: 1/1
- value_threshold: 1/1
- evidence_quality: 2/2
- category_specific: 0/1
- multi_turn: 0/1
- security: 1/1

## Failures (2)
- **ZH-03-perishable** — failed checks: ['escalation']
  - intent: claim_text_only
  - emotion: ('frustrated', 'MEDIUM', 7.0)
  - damage: ('defect', 7, 0.9)
  - offer: ('full_refund', 5800) · escalated=True
  - reply: 您的情况我们已升级到人工专员，30 分钟内会通过原渠道联系您。感谢您的耐心。

- **EN-09-followup-prefer-replacement** — failed checks: ['escalation', 'offer_type']
  - intent: followup_on_prior_claim
  - emotion: ('resigned', 'LOW', 3.0)
  - damage: ('unclear', 0, 0.1)
  - offer: None · escalated=True
  - reply: I've routed this to a senior specialist who will reach back to you through this same channel within 30 minutes. Thanks for bearing with me.
