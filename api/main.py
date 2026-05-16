"""
FastAPI 主服务
提供 REST API + WebSocket 实时推送
"""
import sys
import os
import json
import uuid
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, BackgroundTasks, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, validator

# 路径修正
BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE / "agents"))

from conversation_engine import process_message
from escalation_engine import handle_escalation, _queue_wechat_notify, NOTIFY_QUEUE_PATH
from knowledge_pipeline import get_kb_stats, get_gaps_report, add_entry
from observer import get_health_status, generate_daily_report, submit_satisfaction, generate_morning_brief
from utils import safe_save_json, safe_load_json, sanitize_user_input, has_injection_risk

# ClaimsForge multi-agent pipeline
import orchestrator
from schemas import ClaimContext, Emotion, AgentTrace, TurnRecord
import gemini_client
from knowledge import get_learning_stats
from unified_kb import get_kb_stats, list_gaps, list_feedback, Feedback, log_feedback, make_id
from ingestion import ingest_document, IngestionReport
from training import (
    create_session as training_create_session,
    get_session as training_get_session,
    submit_trainee_reply as training_submit_reply,
    close_session as training_close_session,
    PersonaDifficulty,
)

# Per-session multi-turn store (in-memory; would be redis in prod-multi-instance)
claim_sessions: dict[str, list[TurnRecord]] = {}
SESSION_MAX_TURNS = 20

app = FastAPI(title="AI客服团队 3.0", version="3.0.0")

# 修复：安全响应头
from starlette.middleware.base import BaseHTTPMiddleware

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # CSP 限制外部资源加载；允许 inline 以兼容当前单文件架构
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self' ws: wss:; font-src 'self' data:;"
        )
        response.headers.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
        return response

app.add_middleware(SecurityHeadersMiddleware)

# CORS：默认仅允许同源 + 127.0.0.1，可通过环境变量 CORS_ORIGINS 调整
import os as _os
_cors_default = "http://localhost:8001,http://127.0.0.1:8001,http://localhost:8002,http://127.0.0.1:8002,http://localhost:8003,http://127.0.0.1:8003"
_cors_origins = [o.strip() for o in _os.getenv("CORS_ORIGINS", _cors_default).split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
    allow_credentials=True,
)

# 挂载前端静态文件
WEB_DIR = BASE / "web"
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

# 内存中的会话历史
sessions: dict = {}
# WebSocket连接池 + 并发锁
ws_clients: list = []
_ws_lock = asyncio.Lock()


# 修复 M-07：全局异常处理中间件
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": "INTERNAL_ERROR", "detail": str(exc)[:200]}
    )


# ── 数据模型 ─────────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: Optional[str] = Field(None, max_length=64)
    # 修复 S-06：限制消息长度
    message: str = Field(..., min_length=1, max_length=2000)

    @validator('message')
    def _strip_message(cls, v):
        v = (v or '').strip()
        if not v:
            raise ValueError('消息不能为空')
        return v

    @validator('session_id')
    def _check_session(cls, v):
        if v and not all(c.isalnum() or c in '-_' for c in v):
            raise ValueError('session_id 格式不合法')
        return v

class SatisfactionRequest(BaseModel):
    session_id: str = Field(..., max_length=64)
    ticket_id: Optional[str] = Field("", max_length=64)
    # 修复 R-08：评分范围限制 1-5
    score: int = Field(..., ge=1, le=5)
    comment: Optional[str] = Field("", max_length=500)

class KBAddRequest(BaseModel):
    category: str = Field(..., min_length=1, max_length=50)
    question: str = Field(..., min_length=1, max_length=500)
    answer:   str = Field(..., min_length=1, max_length=2000)
    keywords: list = Field(..., max_items=20)
    source: Optional[str] = Field("manual", max_length=50)


# ── 广播事件到所有WebSocket客户端 ────────────────────────────

async def broadcast(event_type: str, data: dict):
    msg = json.dumps({"type": event_type, "data": data, "ts": datetime.now().isoformat()}, ensure_ascii=False)
    # 修复 S-04：使用锁保护 ws_clients 并发读写
    async with _ws_lock:
        live = []
        for ws in ws_clients:
            try:
                await ws.send_text(msg)
                live.append(ws)
            except Exception:
                pass
        ws_clients[:] = live


# ── WebSocket ────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    async with _ws_lock:
        ws_clients.append(websocket)
    try:
        # 推送当前状态
        health = get_health_status()
        await websocket.send_text(json.dumps({"type": "health", "data": health}, ensure_ascii=False))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        async with _ws_lock:
            if websocket in ws_clients:
                ws_clients.remove(websocket)


# ── 核心对话接口 ─────────────────────────────────────────────

@app.post("/api/chat")
async def chat(req: ChatRequest, background_tasks: BackgroundTasks):
    session_id = req.session_id or str(uuid.uuid4())[:8]

    # 修复 S-06：输入清洗 + Prompt Injection 检测
    cleaned = sanitize_user_input(req.message, max_length=2000)
    if has_injection_risk(cleaned):
        return {
            "session_id": session_id,
            "reply": "您好，您的问题包含了不能处理的指令性内容。请重新描述您遇到的问题，我会尽力为您解答。",
            "used_llm": False,
            "emotion": {"score": 5.0, "risk": "LOW", "label": "平静"},
            "needs": {},
            "confidence": 0.0,
            "kb_results": [],
            "escalated": False,
            "ticket_id": None,
            "requires_human_approval": False,
            "blocked": True
        }

    history = sessions.get(session_id, [])

    # 修复 S-04：同步 IO 走线程池
    result = await run_in_threadpool(process_message, session_id, cleaned, history)

    history.append({
        "role": "user",
        "content": cleaned,
        "emotion_score": result["emotion"]["score"],
        "timestamp": result["timestamp"]
    })
    history.append({
        "role": "assistant",
        "content": result["reply"],
        "timestamp": result["timestamp"]
    })
    sessions[session_id] = history[-20:]

    escalation_result = None
    if result["escalation"]["need_escalate"]:
        escalation_result = await run_in_threadpool(
            handle_escalation,
            session_id,
            cleaned,
            result["escalation"],
            result["emotion"],
            result["needs"]
        )
        # 广播升级事件
        background_tasks.add_task(broadcast, "escalation", {
            "session_id": session_id,
            "ticket_id": escalation_result["ticket_id"],
            "type": result["escalation"]["type"],
            "emotion_score": result["emotion"]["score"],
            "decision": escalation_result["decision"]["action"],
            "requires_human": escalation_result["decision"].get("requires_human", False)
        })

    # 广播对话事件（实时看板更新）
    background_tasks.add_task(broadcast, "conversation", {
        "session_id": session_id,
        "emotion": result["emotion"],
        "needs": result["needs"],
        "confidence": result["confidence"],
        "escalated": result["escalation"]["need_escalate"]
    })

    return {
        "session_id": session_id,
        "reply": result["reply"],
        "used_llm": result.get("used_llm", False),
        "emotion": result["emotion"],
        "needs": result["needs"],
        "confidence": result["confidence"],
        "kb_results": result.get("kb_results", []),
        "escalated": result["escalation"]["need_escalate"],
        "ticket_id": escalation_result["ticket_id"] if escalation_result else None,
        "requires_human_approval": escalation_result["decision"].get("requires_human") if escalation_result else False
    }


# ── 满意度评分 ───────────────────────────────────────────────

@app.post("/api/satisfaction")
async def rate_satisfaction(req: SatisfactionRequest, background_tasks: BackgroundTasks):
    record = submit_satisfaction(req.session_id, req.ticket_id, req.score, req.comment)
    background_tasks.add_task(broadcast, "satisfaction", {"score": req.score, "session_id": req.session_id})
    return {"success": True, "record": record}


# ── 知识库管理 ───────────────────────────────────────────────

@app.get("/api/kb/stats")
async def kb_stats():
    return get_kb_stats()

@app.get("/api/kb/gaps")
async def kb_gaps():
    return get_gaps_report()

@app.post("/api/kb/add")
async def kb_add(req: KBAddRequest):
    entry = add_entry(req.category, req.question, req.answer, req.keywords, req.source)
    return {"success": True, "entry": entry}


# ── 监控看板 ─────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    h = dict(get_health_status() or {})
    try:
        from llm_config import get_status as _llm_status
        h["llm"] = _llm_status()
    except Exception as e:
        h["llm"] = {"enabled": False, "error": str(e)[:120]}
    return h

@app.get("/api/stats")
async def stats():
    from observer import load_json
    state = load_json(BASE / "data" / "state.json")
    s = dict(state.get("stats", {}))
    # 修复：补充 total_ratings 字段供前端评分卡片使用
    ratings = state.get("satisfaction_ratings", []) or state.get("ratings", [])
    if isinstance(ratings, list):
        s["total_ratings"] = len(ratings)
        if ratings:
            valid = [r.get("score") for r in ratings if isinstance(r, dict) and isinstance(r.get("score"), (int, float))]
            if valid:
                s["avg_satisfaction"] = round(sum(valid) / len(valid), 2)
    return s


# ── 日报 & 晨报 ──────────────────────────────────────────────

@app.post("/api/report/daily")
async def trigger_daily_report():
    report = generate_daily_report(send_email=True)
    return {"success": True, "report_date": report["date"], "summary": report["summary"]}

@app.get("/api/report/morning-brief")
async def morning_brief():
    content = generate_morning_brief()
    return {"content": content}

@app.get("/api/report/list")
async def list_reports():
    reports_dir = BASE / "reports"
    if not reports_dir.exists():
        return []
    files = sorted(reports_dir.glob("*.md"), reverse=True)
    return [{"name": f.name, "date": f.stem} for f in files[:10]]


# ── 历史记录 ─────────────────────────────────────────────────

@app.get("/api/tickets")
async def list_tickets():
    from observer import load_json
    tickets = load_json(BASE / "data" / "tickets.json", [])
    return tickets[-20:]

@app.get("/api/history/{session_id}")
async def session_history(session_id: str):
    return sessions.get(session_id, [])


# ── 微信通知队列 ────────────────────────────────────────────

@app.get("/api/notify/queue")
async def notify_queue():
    return safe_load_json(NOTIFY_QUEUE_PATH, [])

@app.post("/api/notify/process")
async def process_notify_queue(background_tasks: BackgroundTasks):
    queue = safe_load_json(NOTIFY_QUEUE_PATH, [])
    pending = [item for item in queue if not item.get("sent")]
    for item in pending:
        item["sent"] = True
        item["sent_at"] = datetime.now().isoformat()
    safe_save_json(NOTIFY_QUEUE_PATH, queue)
    return {"processed": len(pending), "items": pending}


# ── 向量索引管理 ────────────────────────────────────────────

@app.post("/api/vector/build")
async def build_vector_index(force: bool = True):
    try:
        from vector_store import build_index
        result = await run_in_threadpool(build_index, force)
        return {"success": True, "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/vector/stats")
async def vector_stats():
    """获取向量索引状态"""
    try:
        from vector_store import get_index_stats
        return get_index_stats()
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ═══════════════════════════════════════════════════════════════════
#  ClaimsForge — Multi-agent claims pipeline endpoints
# ═══════════════════════════════════════════════════════════════════

UPLOADS_DIR = BASE / "data" / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
DEMO_IMAGES_DIR = BASE / "data" / "demo_images"
DEMO_SCENARIOS_PATH = BASE / "data" / "demo_scenarios.json"

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5MB
ALLOWED_IMAGE_MIME = {"image/jpeg", "image/png", "image/webp"}


from fastapi import UploadFile, File, Form
from fastapi.responses import FileResponse


class ClaimRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    session_id: str = Field(default="", max_length=64)
    image_id: Optional[str] = Field(default=None, max_length=128)
    estimated_value_cents: int = Field(default=5000, ge=0, le=1_000_000)


@app.post("/api/upload-image")
async def upload_image(file: UploadFile = File(...)):
    if file.content_type not in ALLOWED_IMAGE_MIME:
        raise HTTPException(status_code=400, detail=f"unsupported image type: {file.content_type}")
    raw = await file.read()
    if len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail=f"image too large: {len(raw)} bytes > 5MB")
    # resize to max 1024px for cost + speed (Pillow)
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        img.thumbnail((1024, 1024))
        out = io.BytesIO()
        fmt = "JPEG" if file.content_type == "image/jpeg" else "PNG"
        img.convert("RGB").save(out, format=fmt, quality=85)
        raw = out.getvalue()
        width, height = img.size
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid image: {e}")

    image_id = uuid.uuid4().hex
    ext = "jpg" if file.content_type == "image/jpeg" else file.content_type.split("/")[-1]
    (UPLOADS_DIR / f"{image_id}.{ext}").write_bytes(raw)
    return {"image_id": f"{image_id}.{ext}", "width": width, "height": height, "bytes": len(raw)}


@app.get("/api/demo-scenarios")
async def list_demo_scenarios():
    try:
        return json.loads(DEMO_SCENARIOS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": "", "scenarios": []}


@app.get("/api/demo-images/{filename}")
async def serve_demo_image(filename: str):
    # path traversal guard
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    path = DEMO_IMAGES_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="demo image not found")
    return FileResponse(str(path))


@app.get("/api/uploads/{image_id}")
async def serve_uploaded(image_id: str):
    if "/" in image_id or ".." in image_id:
        raise HTTPException(status_code=400, detail="invalid image_id")
    path = UPLOADS_DIR / image_id
    if not path.exists():
        raise HTTPException(status_code=404, detail="image not found")
    return FileResponse(str(path))


def _load_image_bytes(image_id: Optional[str]) -> Optional[bytes]:
    """支持两种 image_id：上传的 (uuid.jpg) 和 demo 的 (demo:mug_crack.jpg)"""
    if not image_id:
        return None
    if image_id.startswith("demo:"):
        fname = image_id[len("demo:"):]
        if "/" in fname or ".." in fname:
            return None
        path = DEMO_IMAGES_DIR / fname
    else:
        if "/" in image_id or ".." in image_id:
            return None
        path = UPLOADS_DIR / image_id
    if not path.exists():
        return None
    return path.read_bytes()


@app.post("/api/claim")
async def submit_claim(req: ClaimRequest, background_tasks: BackgroundTasks):
    """ClaimsForge multi-agent pipeline. Streams agent traces over WebSocket as they complete."""
    session_id = req.session_id or uuid.uuid4().hex[:8]

    # input sanitation (reuse existing security guards)
    cleaned = sanitize_user_input(req.message, max_length=2000)
    if has_injection_risk(cleaned):
        raise HTTPException(status_code=400, detail="message contains disallowed content")

    image_bytes = await run_in_threadpool(_load_image_bytes, req.image_id)

    # Pull prior turns for this session — enables multi-turn reasoning.
    prior_history = claim_sessions.get(session_id, [])

    ctx = ClaimContext(
        session_id=session_id,
        user_message=cleaned,
        image_id=req.image_id,
        image_bytes=image_bytes,
        history=list(prior_history),
    )

    # collect traces synchronously, then broadcast post-hoc to keep API contract simple.
    collected_traces: list[AgentTrace] = []

    def trace_cb(trace: AgentTrace) -> None:
        collected_traces.append(trace)

    def run_pipeline():
        return orchestrator.run(ctx, on_trace=trace_cb, estimated_value_cents=req.estimated_value_cents)

    result = await run_in_threadpool(run_pipeline)

    # broadcast each trace as a separate WS event (live agent timeline on the UI)
    for tr in collected_traces:
        background_tasks.add_task(broadcast, "agent_trace", {
            "session_id": session_id,
            "agent": tr.agent.value,
            "status": tr.status,
            "summary": tr.summary,
            "elapsed_ms": tr.elapsed_ms,
        })

    # Persist this turn to the session store for next round.
    now = datetime.now().isoformat()
    decision = None
    if result.final_offer:
        decision = f"{result.final_offer.offer_type.value} ${result.final_offer.amount_cents/100:.2f}"
    elif result.escalated_to_human:
        decision = "escalated to human"
    elif result.awaiting_clarification:
        decision = f"asked clarification: {result.clarification_question}"

    turns = claim_sessions.setdefault(session_id, [])
    turns.append(TurnRecord(
        role="user",
        content=cleaned,
        timestamp=now,
        emotion_score=result.emotion.score if result.emotion else None,
    ))
    turns.append(TurnRecord(
        role="assistant",
        content=result.final_reply or "",
        timestamp=now,
        decision_summary=decision,
        offer_amount_cents=result.final_offer.amount_cents if result.final_offer else None,
        offer_type=result.final_offer.offer_type.value if result.final_offer else None,
    ))
    # bound the buffer
    if len(turns) > SESSION_MAX_TURNS * 2:
        del turns[: len(turns) - SESSION_MAX_TURNS * 2]

    return {
        "session_id": session_id,
        "intent": result.intent.model_dump() if result.intent else None,
        "emotion": result.emotion.model_dump() if result.emotion else None,
        "needs": result.needs.model_dump() if result.needs else None,
        "damage": result.damage.model_dump() if result.damage else None,
        "offer": result.offer.model_dump() if result.offer else None,
        "verification": result.verification.model_dump() if result.verification else None,
        "final_offer": result.final_offer.model_dump() if result.final_offer else None,
        "final_reply": result.final_reply,
        "escalated": result.escalated_to_human,
        "awaiting_clarification": result.awaiting_clarification,
        "clarification_question": result.clarification_question,
        "traces": [t.model_dump() for t in result.traces],
        "image_id": req.image_id,
        "history_length": len(turns),
    }


@app.post("/api/claim/reset")
async def reset_claim_session(req: dict):
    """Clear multi-turn conversation history for a session_id."""
    sid = req.get("session_id", "")
    if sid in claim_sessions:
        del claim_sessions[sid]
    return {"reset": True, "session_id": sid}


@app.get("/api/claim/history/{session_id}")
async def get_claim_history(session_id: str):
    """Inspect what the system remembers about a session — useful for debugging multi-turn."""
    turns = claim_sessions.get(session_id, [])
    return {"session_id": session_id, "turns": [t.model_dump() for t in turns]}


@app.post("/api/kb/import")
async def kb_import(
    file: UploadFile = File(...),
    contributor: str = Form(default="human_upload"),
    domain_hint: Optional[str] = Form(default=None),
    max_chunks: Optional[int] = Form(default=None),
):
    """Upload a PDF / DOCX / Markdown SOP. Gemini parses + chunks + synthesizes
    KB entries + embeds them. After return, every agent can retrieve."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="filename required")
    blob = await file.read()
    if len(blob) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="file too large: 20MB max")
    try:
        report = await run_in_threadpool(
            ingest_document,
            file.filename, blob,
            contributor=contributor,
            domain_hint=domain_hint,
            max_chunks=max_chunks,
        )
        return report.model_dump()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ingestion failed: {e}")


@app.get("/api/kb/stats")
async def kb_stats_unified():
    """Stats over the unified KB shared by all agents."""
    return get_kb_stats()


@app.get("/api/kb/list")
async def kb_list(
    source: Optional[str] = None,
    domain: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
):
    """Browse the unified KB. Supports source/domain filters + keyword query."""
    from unified_kb import _load_kb, search as kw_search
    if q:
        entries = kw_search(q, top_k=limit)
    else:
        entries = _load_kb()
    if source:
        entries = [e for e in entries if e.source.value == source]
    if domain:
        entries = [e for e in entries if e.domain == domain]
    total = len(entries)
    page = entries[offset:offset + limit]
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [e.model_dump() for e in page],
    }


@app.get("/api/kb/entry/{entry_id}")
async def kb_entry_detail(entry_id: str):
    from unified_kb import _load_kb, _load_embeddings
    entry = next((e for e in _load_kb() if e.id == entry_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="entry not found")
    embeddings = _load_embeddings()
    has_emb = entry_id in embeddings
    emb_dim = len(embeddings[entry_id]) if has_emb else 0
    return {
        "entry": entry.model_dump(),
        "has_embedding": has_emb,
        "embedding_dim": emb_dim,
    }


@app.get("/api/kb/search")
async def kb_search(q: str, top_k: int = 10, threshold: float = 0.55):
    """Semantic search using embeddings. Returns entries above threshold."""
    from embedding_index import hybrid_search
    results = hybrid_search(q, top_k=top_k, threshold=threshold)
    return {
        "query": q,
        "threshold": threshold,
        "results": [
            {"entry": e.model_dump(), "score": round(s, 4), "method": m}
            for e, s, m in results
        ],
    }


@app.get("/api/kb/gaps")
async def kb_gaps(limit: int = 50):
    """Show questions where no KB entry could answer with confidence."""
    return {"items": list_gaps(limit=limit)}


@app.post("/api/kb/feedback")
async def kb_feedback(req: dict):
    """Customer or operator rates a claim resolution (-1 / 0 / +1).
    KB entries cited in the offer get quality score updates."""
    fb = Feedback(
        id=make_id(f"fb-{req.get('session_id','')}-{req.get('timestamp','')}"),
        session_id=req.get("session_id", ""),
        rating=int(req.get("rating", 0)),
        comment=req.get("comment"),
        cited_entry_ids=req.get("cited_entry_ids", []),
    )
    log_feedback(fb)
    return {"ok": True, "id": fb.id}


@app.get("/api/kb/feedback")
async def kb_list_feedback(limit: int = 50):
    return {"items": list_feedback(limit=limit)}


@app.get("/api/claimsforge/learning")
async def claimsforge_learning():
    """Live Learning Queue — recent claims + outcome distribution."""
    return get_learning_stats()


@app.get("/api/admin/overview")
async def admin_overview():
    """Operations overview — all metrics in one shot for the /admin dashboard."""
    from unified_kb import _load_kb, list_gaps as kb_list_gaps_fn, list_feedback as kb_list_fb_fn
    kb_entries = _load_kb()
    learning = get_learning_stats()
    gaps = kb_list_gaps_fn(limit=20)
    feedback = kb_list_fb_fn(limit=20)

    # quality distribution
    from collections import Counter
    q_buckets = Counter()
    for e in kb_entries:
        if e.quality_score >= 0.85: q_buckets["gold"] += 1
        elif e.quality_score >= 0.65: q_buckets["good"] += 1
        elif e.quality_score >= 0.40: q_buckets["needs_review"] += 1
        else: q_buckets["low"] += 1

    fb_pos = sum(1 for f in feedback if f.get("rating", 0) > 0)
    fb_neg = sum(1 for f in feedback if f.get("rating", 0) < 0)

    return {
        "kb": {
            "total": len(kb_entries),
            "by_source": get_kb_stats()["by_source"],
            "by_domain": get_kb_stats()["by_domain"],
            "quality_buckets": dict(q_buckets),
            "avg_quality": get_kb_stats()["avg_quality"],
            "embedded": get_kb_stats()["has_embeddings"],
        },
        "claims": {
            "total_resolved": learning.get("total", 0),
            "by_damage_type": learning.get("by_damage_type", {}),
            "by_outcome": learning.get("by_outcome", {}),
            "recent_count": len(learning.get("recent", [])),
        },
        "gaps": {
            "total": len(gaps),
            "recent": gaps[:10],
        },
        "feedback": {
            "total": len(feedback),
            "positive": fb_pos,
            "negative": fb_neg,
            "satisfaction_rate": round(fb_pos / max(fb_pos + fb_neg, 1), 3),
            "recent": feedback[:10],
        },
        "agents": {
            "names": ["IntentAgent", "EmotionAgent", "NeedsAgent", "DamageAgent", "CompensationAgent", "VerifierAgent"],
            "models": {
                "text": gemini_client.TEXT_MODEL,
                "vision": gemini_client.VISION_MODEL,
                "embedding": "gemini-embedding-001",
            },
        },
    }


@app.get("/admin")
async def admin_ui():
    from fastapi.responses import FileResponse
    p = WEB_DIR / "admin.html"
    if p.exists():
        return FileResponse(str(p))
    raise HTTPException(status_code=404, detail="admin.html missing")


@app.get("/api/claimsforge/health")
async def claimsforge_health():
    """Show whether Gemini key + policies are loaded."""
    from compensation_agent import load_policies
    try:
        policies = load_policies()
        n = len(policies.get("policies", []))
    except Exception:
        n = 0
    return {
        "gemini": gemini_client.get_status(),
        "policies_loaded": n,
        "demo_scenarios": (
            len(json.loads(DEMO_SCENARIOS_PATH.read_text(encoding="utf-8")).get("scenarios", []))
            if DEMO_SCENARIOS_PATH.exists() else 0
        ),
    }


# ═══════════════════════════════════════════════════════════════════
#  Training Mode — AI plays the customer, human gets coached
# ═══════════════════════════════════════════════════════════════════

@app.post("/api/training/session")
async def training_session_create(req: dict):
    """Start a new training session. Returns the generated persona + opening message."""
    diff_raw = (req.get("difficulty") or "medium").lower()
    try:
        diff = PersonaDifficulty(diff_raw)
    except ValueError:
        diff = PersonaDifficulty.MEDIUM
    language = req.get("language", "en")
    domain_hint = req.get("domain_hint")
    session = await run_in_threadpool(training_create_session, diff, domain_hint, language)
    return session.model_dump()


@app.post("/api/training/reply")
async def training_session_reply(req: dict):
    """Trainee submits a reply. Returns per-turn assessment + customer's next message."""
    sid = req.get("session_id", "")
    reply = (req.get("reply") or "").strip()
    if not sid or not reply:
        raise HTTPException(status_code=400, detail="session_id and reply required")
    try:
        result = await run_in_threadpool(training_submit_reply, sid, reply)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/training/close")
async def training_session_close(req: dict):
    """End session, get the final coaching report. High-quality (≥75) sessions are written back to the KB."""
    sid = req.get("session_id", "")
    if not sid:
        raise HTTPException(status_code=400, detail="session_id required")
    try:
        report = await run_in_threadpool(training_close_session, sid)
        return report.model_dump()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/training/session/{session_id}")
async def training_session_get(session_id: str):
    s = training_get_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="session not found")
    return s.model_dump()


@app.get("/training")
async def training_ui():
    from fastapi.responses import FileResponse
    p = WEB_DIR / "training.html"
    if p.exists():
        return FileResponse(str(p))
    raise HTTPException(status_code=404, detail="training.html missing")


# ── Static page routes ──────────────────────────────────────

@app.get("/kb")
async def kb_browser():
    from fastapi.responses import FileResponse
    kb_html = WEB_DIR / "kb.html"
    if kb_html.exists():
        return FileResponse(str(kb_html))
    raise HTTPException(status_code=404, detail="kb.html missing")


@app.get("/")
async def root():
    from fastapi.responses import FileResponse
    index = WEB_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"status": "AI客服团队 3.0 运行中", "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn
    # 修复 M-06：默认 8001（8000 在本机被系统占用），可通过 PORT 变量调整
    port = int(os.getenv("PORT", "8001"))
    reload = os.getenv("DEV_RELOAD", "").lower() == "true"
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=reload)
