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
from schemas import ClaimContext, Emotion, AgentTrace
import gemini_client
from knowledge import get_learning_stats

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

    ctx = ClaimContext(
        session_id=session_id,
        user_message=cleaned,
        image_id=req.image_id,
        image_bytes=image_bytes,
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

    return {
        "session_id": session_id,
        "intent": result.intent.model_dump() if result.intent else None,
        "damage": result.damage.model_dump() if result.damage else None,
        "offer": result.offer.model_dump() if result.offer else None,
        "verification": result.verification.model_dump() if result.verification else None,
        "final_offer": result.final_offer.model_dump() if result.final_offer else None,
        "final_reply": result.final_reply,
        "escalated": result.escalated_to_human,
        "traces": [t.model_dump() for t in result.traces],
        "image_id": req.image_id,
    }


@app.get("/api/claimsforge/learning")
async def claimsforge_learning():
    """Live Learning Queue — recent claims + outcome distribution."""
    return get_learning_stats()


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


# ── 首页重定向 ───────────────────────────────────────────────

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
