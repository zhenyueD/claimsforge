# AI 数字客服团队 3.1

> 基于 RAG + LLM 的智能客服系统，集成情绪分析、需求挖掘、自动升级决策、向量检索。
>
> **3.1 重要修复**：并发安全、赔偿金额提取、Prompt Injection 防护、WS 重连、输入校验、XSS 防护 · 共 16 项高优先级修复。

---

## 🏗️ 系统架构

```
┌─────────────────────────────────────────────────────┐
│                    前端 (web/index.html)              │
│  聊天窗口 · 情绪看板 · 工单列表 · 实时 WebSocket 事件流 │
└────────────────────┬────────────────────────────────┘
                     │ REST + WebSocket
┌────────────────────▼────────────────────────────────┐
│               FastAPI 服务 (api/main.py)              │
│  /api/chat  /api/kb  /api/report  /api/vector        │
└──────┬───────────┬────────────┬──────────────────────┘
       │           │            │
┌──────▼──┐  ┌─────▼────┐  ┌───▼──────────┐
│Conversa-│  │Escalation│  │  Knowledge   │
│tion     │  │ Engine   │  │  Pipeline    │
│ Engine  │  │(升级决策) │  │ (知识库管理) │
│         │  │          │  │              │
│ ┌─────┐ │  │ 50元阈值 │  │ ┌──────────┐│
│ │ RAG │ │  │ 工单系统 │  │ │TF-IDF    ││
│ │检索 │ │  │ 微信通知 │  │ │向量索引  ││
│ └──┬──┘ │  └──────────┘  │ └──────────┘│
│    │LLM │                └─────────────┘
│    │API │
│    ▼    │
│ 模板降级│
└─────────┘
         │
┌────────▼──────────────────┐
│  knowledge_base.json (30条)│
│  tfidf_index.pkl (向量索引)│
│  tickets.json (工单历史)   │
└───────────────────────────┘
```

---

## ⚡ 快速启动

### 1. 安装依赖
```bash
pip install fastapi uvicorn openai chromadb sentence-transformers
```

### 2. 配置 LLM（可选，不配置自动走模板回复）
```bash
# Windows PowerShell
$env:LLM_API_KEY = "sk-xxxxx"
$env:LLM_BASE_URL = "https://api.openai.com/v1"   # 或任意 OpenAI 兼容地址
$env:LLM_MODEL = "gpt-3.5-turbo"                   # 可选：gpt-4, deepseek-chat 等
$env:LLM_TEMPERATURE = "0.7"
$env:LLM_TIMEOUT = "8"
$env:PORT = "8002"   # 默认 8001（8000/8001 可能被占用可改 8002）
```

### 3. 导入知识库（首次运行）
```bash
python data/import_kb.py --reset
```

### 4. 启动服务
```bash
# 默认 8001，被占可通过环境变量调整
PORT=8002 python run.py
# 或直接
uvicorn api.main:app --port 8002
```

### 5. 访问前端
```
http://localhost:8002/
```

---

## 🌟 核心功能

| 功能 | 说明 |
|------|------|
| **RAG 向量检索** | TF-IDF 离线检索 + 关键词混合排序，置信度评分 |
| **LLM 智能回复** | OpenAI 兼容接口，失败自动降级模板，不崩溃 |
| **情绪分析** | 0-10 评分，HIGH/MEDIUM/LOW 风险分级 |
| **需求挖掘** | 表层/潜在/情感需求三层分析 + 留存评分 |
| **升级决策** | ¥50 阈值自主决策，超额推微信审批队列 |
| **工单系统** | 自动创建 TKT-YYYYMMDDHHMMSS 工单，状态追踪 |
| **实时看板** | WebSocket 推送，情绪/工单/健康状态实时更新 |
| **知识库管理** | CSV 批量导入，自动触发索引重建，缺口记录 |

---

## 📁 目录结构

```
customer-service-team/
├── agents/
│   ├── conversation_engine.py   # 核心对话引擎（RAG+LLM+情绪+需求）
│   ├── escalation_engine.py     # 升级决策（50元阈值+工单+微信通知）
│   ├── knowledge_pipeline.py    # 知识库 CRUD
│   ├── llm_config.py            # LLM 配置（环境变量驱动）
│   ├── observer.py              # 健康监控 + 日报生成
│   └── vector_store.py          # TF-IDF 向量检索（离线，零依赖）
├── api/
│   └── main.py                  # FastAPI 服务 + WebSocket
├── data/
│   ├── knowledge_base.json      # 知识库（30 条，14 分类）
│   ├── knowledge_base_real.csv  # 原始 CSV 数据
│   ├── tfidf_index.pkl          # 向量索引缓存（自动生成）
│   ├── tickets.json             # 工单历史
│   ├── state.json               # 系统统计状态
│   └── import_kb.py             # 知识库导入工具
├── web/
│   └── index.html               # 前端单页应用（深色主题）
├── reports/                     # 日报输出目录
├── run.py                       # 一键启动脚本
├── test_acceptance.py           # 验收测试
└── README.md                    # 本文件
```

---

## 🎭 演示剧本（比赛用）

按顺序输入以下消息，覆盖所有功能点：

```
1. 【普通查询·高置信度】
   "我想申请退款，请问有什么条件？"
   → 预期：命中 KB001，置信度 80%+，显示来源引用

2. 【技术支持·中置信度】
   "登录一直失败，提示密码错误但密码是对的"
   → 预期：命中 KB010，提供排查步骤

3. 【情绪危机·自动升级】
   "你们系统已经坏了两天了，耽误我工作！垃圾服务！"
   → 预期：情绪 HIGH，自动创建工单，升级到组长

4. 【赔偿申请·LLM决策】
   "服务中断导致我损失了30元，要求赔偿！"
   → 预期：30元<50元阈值，自动批准赔偿

5. 【高额赔偿·人工审批】
   "你们故障导致我损失了200元，必须赔偿！"
   → 预期：200元>50元，推入微信审批队列

6. 【知识缺口·记录机制】
   "你们支持NFT数字藏品购买吗？"
   → 预期：低置信度，记录缺口，告知1工作日内回复

7. 【多轮对话·上下文】
   先发："我忘记密码了" → 再发："验证邮件发到哪个邮箱？"
   → 预期：两次都能命中相关 KB，保持上下文

8. 【发票咨询·精准命中】
   "我需要开增值税专用发票，怎么申请？"
   → 预期：命中 KB019，完整答复开票流程
```

---

## ⚙️ 环境变量参考

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_ENABLED` | `true` | 是否启用 LLM（false 则纯模板） |
| `LLM_API_KEY` | `""` | API Key，留空自动降级 |
| `LLM_BASE_URL` | OpenAI 官方 | 支持任意 OpenAI 兼容接口 |
| `LLM_MODEL` | `gpt-3.5-turbo` | 模型名称 |
| `LLM_TEMPERATURE` | `0.7` | 生成温度（0-1） |
| `LLM_MAX_TOKENS` | `512` | 最大输出 token |
| `LLM_TIMEOUT` | `8.0` | LLM 调用超时（秒） |

---

## 🧪 验收测试

```bash
python test_acceptance.py
```

预期输出：4 个场景全部通过，包括退款查询（置信度 80%+）、技术故障（70%+）、升级触发（工单创建）、知识缺口（低置信度+缺口记录）。

---

## ⚠️ 已知限制

1. **端口**：8000 被系统占用，默认使用 **8001**，被占时可设 `PORT=8002` 调整
2. **向量引擎**：使用纯 Python TF-IDF（离线、零依赖，去 1-gram 噪声后中文效果接近 jieba+BM25）
3. **LLM**：未配置 API Key 时自动走模板回复，功能完整不崩溃
4. **WebSocket**：本地运行正常，反向代理需配置 Upgrade 头

## 🔒 3.1 安全升级说明

- 输入限制：消息最长 2000 字，评分 1-5，KB 添加字段都有 max_length
- Prompt Injection 防护：检测“忽略以上指令”类词句自动拦截
- XSS 防护：所有动态拼接 innerHTML 都走 escHtml（含引号转义）
- 并发安全：所有 JSON 文件写入使用原子替换 + 文件锁
- 赔偿金额：正则改为必须带货币语义，“等了3天赔100元” 不会误取 3
- 频率限制：同 session 24h 内仅能自动审批一次赔偿
- WebSocket：指数退避重连，最多 10 次，JSON 解析安全
- API 超时：所有前端 fetch 均带 AbortController，默认 12s，聊天 20s
- 错误传递：HTTP 4xx/5xx 会报错而不是静默崩溃
- 页面可见性：后台标签页不轮询，节省带宽
