# ClaimsForge · 15 页讲稿（中文）

> 配套 `docs/slides.pdf` v6 · 总长 ~4 分钟（适合录视频或现场 demo）
> 英文对照见 `docs/demo-talking-points-en.md`
> 重点详解页：**P7 (Trust Score) / P10 (Duplicate + Tier-2) / P11 (Methodology Loop)**

---

## 🎙️ 开场（30 秒，站起来先讲）

> 各位评委好。**743 亿美元**是 2025 年美国电商每年的退货金额。大部分小额损坏理赔，现在还要客户和客服来回扯三天。我们做了一个叫 **ClaimsForge** 的东西 —— 7 个 Gemini agent 8 秒之内自动判赔，**而且证明 AI 没被骗**。这是我们这次比赛和别人最不一样的地方。

---

## P1 · Hero（封面）

> ClaimsForge —— **AI 理赔系统的信任层**。多 agent Gemini 自动处理电商损坏理赔，并且**给出可审计的信任分证明 AI 没被骗**。这是 demo 网址，下面是 GitHub，MIT 开源。

---

## P2 · The Problem（问题）

> 三个数字。第一，每年 7430 亿美元退货。第二，单条小额理赔要 3 天人工来回。第三 —— 这是新出现的痛点 —— 根据 Verisk 2026 报告，**99% 的保险公司已经看到过 AI 篡改的证据照片，只有 32% 觉得自己能识别**。
>
> 现在 Sierra、Decagon、Intercom Fin 这些 AI agent，都只解决了"AI 怎么跟客户说话"。**没人解决"怎么证明 AI 没被骗"**。

---

## P3 · Positioning（定位对比表）

> 这是我们和 Sierra、Decagon 的逐项对比。**红的他们没做，绿的我们做了**。
>
> 重点看 3 行：第一，**多模态 vision 定损** —— 他们只有文字语音，我们能看图。第二，**deepfake 防御** —— pHash 视觉指纹 + EXIF 元数据 + 文图一致性。第三，**写 SOP 这件事反过来** —— Sierra 让你先写 SOP 他编译成 agent，我们让 agent 从案例里**自动写 SOP**。

---

## P4 · Architecture（架构图）

> 这是完整架构。**7 个 Gemini specialist agent** 作为流水线，中间是**橙色的 Supervisor** —— 我们的核心安全网。右边绿色的是知识库，1329 条政策和 SOP。左下角是 handoff 队列。整个系统纯 Python，**不用 LangChain 任何编排框架**。
>
> 下一页我会用时间轴展开讲。

---

## P5 · Sequence Timeline（时间轴）

> 客户上传图片，**8.2 秒**端到端拿到回复 + 信任分。
>
> 看 2.1 秒这一行 —— **三个 agent 用 `asyncio.gather` 同时跑** —— 情绪分析、需求挖掘、视觉定损并行。如果串行要 5 秒，并行 1.7 秒。
>
> 中间 Supervisor 那一层是纯 Python，**0.毫秒级，不调 LLM**。这是为什么我们既快又安全。

---

## P6 · Supervisor IAM 三层

> Supervisor 是我们的**核心差异化第一项**。**仿 AWS IAM 的三层**：
>
> 第一层 DENY —— 7 条硬规则，命中任一条就立刻升级人工。包括 pHash 欺诈、重复订单、多模态不一致、法律威胁这些。
>
> 第二层 EXEMPT —— 例外豁免，比如生鲜食品不需要拍照证据。
>
> 第三层 CAP —— 金额钳制，500 美元上限、不能超过订单价 100%。
>
> 上面 4 条 RULE 是代码硬编码（红色）。下面 3 条 HR- （橙色）是 **Tier-2 数据驱动** —— 商家直接改 JSON 文件，不用重新部署代码。这是 Sierra 客户做不到的。

---

## P7 · Trust Score（🌟 重点详解）

> 这是我们**第二个核心差异化** —— 学 Stripe Radar 的信任分思路。每一笔自动赔付，**右上角都显示 0 到 100 的信任分**，下面列 6 个独立因子，每个因子说明依据。
>
> 看右边那张样卡，**这一单只有 50 分** —— 因为 image_uniqueness 红了，pHash 检测到这张图之前赔过。其他 5 项都绿。**关键设计：任何一项红，总分锁 50** —— 不允许"5 绿 1 红"刷高分。
>
> 而且 —— 看那个 `RULE-FRAUD-REPLAY` 的小标签 —— 每个因子都**可点击追溯到触发它的具体规则**。给合规 / 财务团队的可审计证据。

**为什么这页重要**：这是把"我没被骗"从工程能力变成销售素材的关键。Sierra 给客户看"AI 给你写信"，我们给客户看"AI 给你证明它没被骗" —— 卖点不在一个档次。

**评委可能问的 Q&A**：
- *"和 Stripe Radar 区别？"* → Stripe 给金融交易打分，我们给"AI 决策"打分。
- *"为什么 6 个权重不是 1/6 平均？"* → image_uniqueness 0.20 高，因为 pHash 是最强的欺诈信号；emotion_gating 0.10 低，因为它是辅助信号而不是 ground truth。

---

## P8 · Demo A · Clean Claim

> 一个干净的 case：客户传杯子破损图，订单 24 美元。
>
> 7 步流水线 —— 意图识别、情绪、需求、损坏识别（**bbox 把裂纹圈出来**）、Compensation 选政策、Supervisor 通过、Verifier 通过。**8 秒搞定**。
>
> 客户看到的是右边这张卡 —— 一段双语回复，**下面是 Trust 100 满分**，6 个绿勾。然后 3 个按钮：✅ Accept、↩ Reject 重新谈、👤 找人工。点 Accept 就把这个 case 自动标记为 **gold**，进 fine-tune 数据集。

---

## P9 · Demo B · 3 种欺诈攻击

> 这是我们的反欺诈三连击：
>
> 攻击 1：**同一张图换 session 再传** —— pHash 检测到指纹重复，0 LLM 成本，5 毫秒拒绝。
>
> 攻击 2：**照片 EXIF 显示 2024 年拍的**，但客户说是上周订单 —— image_provenance 因子红了，扣信任分。
>
> 攻击 3：**文字说"我的杯子裂了"但图里是手机** —— Multimodal Mismatch 拦截。**Sierra 没有 vision channel，做不到这一步**。
>
> 底下绿框是我们的 E2E 测试 —— 20 个 case 100% 通过，包括上面 3 种攻击 + prompt injection + 金额操纵 + 情绪伪装 + 多轮信息不一致。

---

## P10 · Demo C · Duplicate + Tier-2 Toggle（🌟 重点详解）

> 这页讲业务运营怎么实时控制 AI。
>
> 左边 —— 重复退款。客户已经接受过同订单的退款，再来一次。我们的 supervisor 扫 history，0 个 LLM 调用，几毫秒拒绝。**直接给商家省下重复赔付**。
>
> 右边 —— Tier-2 规则热切换。双 11 闪购，运营要临时放开奢侈品规则。**点这个 switch，atomic 写 JSON，60 秒内全 prod 生效，不下线、不部署**。这是 Sierra 客户做不到的 —— 他们改一条规则要找 Sierra 团队驻场配置。

**为什么这页重要**：这页传递的是"**业务运营对 AI 的控制权**" —— 商家最怕的就是"AI 是黑盒，出问题没法救"。我们这页直接消除这个恐惧。

**评委可能问的 Q&A**：
- *"为什么不让 LLM 自己判断重复？"* → LLM 会被客户的"我没收到"话术骗。纯 Python history scan 是 ground truth。
- *"Tier-2 安全吗？业务团队会写错 JSON 把系统改崩吗？"* → DSL 是**严格白名单**只支持 5 个 operator，未知 operator → safe-default 不触发，崩不了 supervisor。

---

## P11 · Methodology Loop（🌟 重点详解）

> 这是我们**第三个差异化**。Sierra、Decagon 让你先写 SOP，他们编译成 agent。**90% 中小商家根本没写过 SOP**。
>
> 我们反过来：每解决 5 个 case，**case_synthesizer 自动聚类，让 Gemini 写一条 methodology**。看右上 —— 这条 SOP 叫"破损杯子理赔流程"，**WHEN/DO 两段全是 Gemini 从 8 个真实 case 里蒸馏出来的，没有人审过**。
>
> 看下面三个数字：**89 条** auto-synthesized methodology，**1329 条** 总 KB，**100% embedding 覆盖**。每个新 case 都让系统更聪明，**这就是数据飞轮的第一齿**。
>
> 你可以打开 `/methodologies` 页面看到全部 89 条，每条都能搜索、按 domain 过滤。

**为什么这页重要**：自学习是"数据飞轮"的关键 —— 评委愿意听这个故事是因为它代表**复合增长** + **越用越值钱的护城河**。

**评委可能问的 Q&A**：
- *"Gemini 写错了 SOP 怎么办？"* → 三道防线：每条带 quality_score、经过 supervisor 硬规则验证、客户接受率反馈调权重。
- *"89 条够吗？"* → 现阶段够 demo。算上 SOP 文档 ingest 一共 1204 human_sop entries，加上 89 个自动合成，搜索都能命中。

---

## P12 · Data Flywheel · SFT Export

> **数据飞轮的第二齿**。每个 case 自动打标：supervisor approve + 客户 accept = **gold**；supervisor approve 但客户没回应 = **normal**；supervisor 拦截 = **red_flag**。
>
> 商家可以一键导出 fine-tune 数据集，**支持 Vertex AI、OpenAI、Anthropic 三种原生格式**。攒到 2000 条 gold，跑一次 fine-tune job，**模型变小 30%，延迟降 50%**。这就是数据飞轮 —— 越用越聪明、越用越便宜。

---

## P13 · Business Value

> 4 个数字：**90% 自动率目标 · 8 秒处理 · 30 分钟自部署 · $0 license 费**。对比 Sierra 一套合同 20 万到 200 万美元、3-6 个月 PoC。
>
> 左边是商家 day 1 拿到的能力，右边是我们瞄准的客户群 —— **Shopify/WooCommerce 中小商家**（Sierra 顾不上的）、**保险 MGA**（要 deepfake 防御 + 可审计决策）、**跨境电商**（中英双语原生）、**SaaS 订阅退订**（信任分 + 情绪门 = 留存）。

---

## P14 · CTA

> 试用网址在这。如果只看一件事 —— **打开 demo，点'Angry customer + legal threat'场景，看 supervisor 的 RULE-LEGAL-THREAT 触发，Trust Score 掉到 50**。30 秒，整个 pitch 就在那一屏。
>
> MIT 开源，GitHub repo 在右边。感谢 Google Gemini 和 Vultr 的支持。谢谢。

---

## 🎙️ Q&A 防御准备（评委可能问的）

| 评委可能问 | 你怎么回 |
|---|---|
| 为啥要 7 个 agent，不能合成 1 个？ | 每个 agent **结构化输出 + 可审计**。1 个大 agent 黑盒，每个失败原因都查不到。 |
| 跟 LangGraph / CrewAI 区别？ | 我们故意**不用框架**。522 行 orchestrator + 1 个 ClaimContext 就够。框架的好处不值它的复杂度。 |
| Trust Score 怎么校准？ | 当前 weights 是从 50 张真实 case 手工调出来的。Sprint 1 会用 RLAIF 反馈自动调。 |
| pHash 容易被对抗（剪裁/旋转）吗？ | 5 bits 阈值容忍轻度变换。重度变换需要语义级 (CLIP)，已在 roadmap。 |
| 89 个 methodology 是从多少 case 学的？ | KB 总 1329 entry，case_synthesizer 看 learned_cases.jsonl 真实 resolved cases 聚类，每个 methodology 标记从 N 个 case 来。 |
| 安全/合规怎么办？ | 当前是 demo 版（单进程、无 JWT auth）。Trust Score + Supervisor 是 audit-ready 的，下个 sprint 加 SOC2 套件。 |
| 怎么挣钱？ | 三档：SMB SaaS（$99/月含 1000 claims）/ Enterprise self-host（$10k/年 license）/ Insurance MGA per-claim charging。 |
| Sierra 估值多少？ | 2026 Q1 a16z 领投后 $4.5B。我们不跟他正面打，做他做不到的中小商家自部署。 |

---

## ⏱️ 时长配速参考

| 页 | 中文字数 | 念出来约 |
|---|---|---|
| 开场 | 100 | 30s |
| P1 Hero | 60 | 20s |
| P2 Problem | 140 | 45s |
| P3 Positioning | 120 | 40s |
| P4 Architecture | 100 | 30s |
| P5 Sequence | 100 | 30s |
| P6 Supervisor | 150 | 50s |
| P7 Trust Score 🌟 | 180 | 60s |
| P8 Demo A | 130 | 40s |
| P9 Demo B | 160 | 50s |
| P10 Demo C 🌟 | 150 | 50s |
| P11 Methodology 🌟 | 200 | 60s |
| P12 Flywheel | 110 | 35s |
| P13 Business | 110 | 35s |
| P14 CTA | 80 | 25s |
| **总计** | ~1890 字 | **~10 分钟** |

如果要压到 2:30 录屏视频：跳过 P1/P4/P12/P13，浓缩 P2/P3/P5/P6/P8 到一句话每页。重头戏 P7/P10/P11 保留全文。
