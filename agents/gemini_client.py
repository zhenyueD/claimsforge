"""
GeminiClient — 包装 google-genai SDK 给所有 ClaimsForge agents 用。

设计原则：
  - 所有 agent 通过 get_client() 拿同一个 client（共享 HTTP 连接池）
  - chat / vision / structured 三个核心方法，名字直观，少配置
  - 不重新实现 SDK 已有的功能（重试、流式等），只做"业务封装"
  - 失败抛 GeminiError，让调用方决定降级策略
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional, Type

from google import genai
from google.genai import types
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class GeminiError(Exception):
    """所有 Gemini 调用失败统一抛这个。"""


# 启动时加载 .env（最朴素方式，不引入 python-dotenv 依赖）
def _load_env_file() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_env_file()


# ── 配置 ─────────────────────────────────────────────────
TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-2.5-flash")
VISION_MODEL = os.getenv("GEMINI_VISION_MODEL", "gemini-2.5-flash")

_singleton: Optional[genai.Client] = None


def get_client() -> genai.Client:
    global _singleton
    if _singleton is None:
        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise GeminiError(
                "Missing GOOGLE_API_KEY / GEMINI_API_KEY. "
                "Get one free at https://aistudio.google.com/apikey and put it in .env."
            )
        _singleton = genai.Client(api_key=api_key)
    return _singleton


# ── chat：纯文本对话 ─────────────────────────────────────
def chat(
    prompt: str,
    *,
    system: Optional[str] = None,
    temperature: float = 0.4,
    max_tokens: int = 2048,
    model: Optional[str] = None,
    thinking_budget: int = 0,
) -> str:
    """
    单轮文本对话。返回模型的文本输出。

    `thinking_budget=0` 关闭 Gemini 2.5 的内置 thinking（节省 token、降低延迟）。
    需要复杂推理时调大（例如 verifier_agent 可设 256）。
    """
    client = get_client()
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        max_output_tokens=max_tokens,
        thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget),
    )
    try:
        resp = client.models.generate_content(
            model=model or TEXT_MODEL,
            contents=prompt,
            config=cfg,
        )
    except Exception as e:
        raise GeminiError(f"chat failed: {e}") from e
    text = (resp.text or "").strip()
    if not text:
        raise GeminiError("Gemini returned empty text.")
    return text


# ── structured：把模型输出强制成 Pydantic model ──────────
def structured(
    prompt: str,
    schema: Type[BaseModel],
    *,
    system: Optional[str] = None,
    temperature: float = 0.2,
    max_tokens: int = 1024,
    model: Optional[str] = None,
    image_bytes: Optional[bytes] = None,
    image_mime: str = "image/jpeg",
) -> BaseModel:
    """
    要求模型按 schema 输出 JSON，自动 parse 成 Pydantic 实例。

    可选传 image_bytes，模型同时接收图像（同一 Vision 模型）。
    """
    client = get_client()
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        max_output_tokens=max_tokens,
        response_mime_type="application/json",
        response_schema=schema,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    contents: list[Any] = [prompt]
    if image_bytes is not None:
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type=image_mime))

    target_model = model or (VISION_MODEL if image_bytes else TEXT_MODEL)
    try:
        resp = client.models.generate_content(
            model=target_model,
            contents=contents,
            config=cfg,
        )
    except Exception as e:
        raise GeminiError(f"structured call failed: {e}") from e

    # SDK 提供 .parsed 当 response_schema 是 Pydantic
    parsed = getattr(resp, "parsed", None)
    if isinstance(parsed, schema):
        return parsed
    # Fallback：手动 parse 文本 JSON
    text = (resp.text or "").strip()
    if not text:
        raise GeminiError("Gemini returned empty JSON.")
    try:
        return schema.model_validate_json(text)
    except Exception as e:
        raise GeminiError(f"failed to parse Gemini JSON as {schema.__name__}: {e}\nraw={text[:500]}") from e


# ── vision：图像理解（自由文本输出）──────────────────────
def vision(
    image_bytes: bytes,
    prompt: str,
    *,
    image_mime: str = "image/jpeg",
    system: Optional[str] = None,
    temperature: float = 0.4,
    max_tokens: int = 2048,
    model: Optional[str] = None,
    thinking_budget: int = 0,
) -> str:
    """图像 + 文本输入，文本输出。用于 quick description，结构化请用 structured()。"""
    client = get_client()
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        max_output_tokens=max_tokens,
        thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget),
    )
    contents = [
        prompt,
        types.Part.from_bytes(data=image_bytes, mime_type=image_mime),
    ]
    try:
        resp = client.models.generate_content(
            model=model or VISION_MODEL,
            contents=contents,
            config=cfg,
        )
    except Exception as e:
        raise GeminiError(f"vision failed: {e}") from e
    text = (resp.text or "").strip()
    if not text:
        raise GeminiError("Gemini returned empty vision text.")
    return text


# ── 健康检查（暴露给 /api/health）────────────────────────
def get_status() -> dict[str, Any]:
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or ""
    return {
        "enabled": bool(api_key),
        "text_model": TEXT_MODEL,
        "vision_model": VISION_MODEL,
        "key_present": bool(api_key),
        "key_preview": (api_key[:6] + "..." + api_key[-4:]) if len(api_key) >= 12 else "",
    }
