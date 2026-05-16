# DamageAgent Eval · 2026-05-16 14:33

## Summary
- Items evaluated: **30**
- Joint accuracy (type + severity): **96.7%**
- Type accuracy: **96.7%**
- Severity ±1 accuracy: **96.7%**
- p50 latency: **2919ms**
- Errors: 0

## Confusion matrix (true → predicted, top 10)

| true | predicted | count |
|------|-----------|-------|
| crack | crack ✅ | 9 |
| scratch | scratch ✅ | 9 |
| missing_part | missing_part ✅ | 3 |
| stain | stain ✅ | 3 |
| water_damage | water_damage ✅ | 3 |
| dent | dent ✅ | 2 |
| dent | unclear ❌ | 1 |

## Failed cases (1)
- **phone_dent_4_028.jpg** · true=(dent, [3, 5]) → pred=(unclear, sev=0, conf=0.10)
  - reasoning: 图片显示的是一个手机的示意图，无法判断是否存在凹痕。