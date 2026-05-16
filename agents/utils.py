"""
通用工具：原子文件写入、跨进程文件锁、金额提取
所有 JSON 持久化操作必须经过 safe_save_json，读-改-写必须用 update_json 上下文
"""
import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

# 进程内锁
_file_locks: dict = {}
_locks_master = threading.Lock()

_IS_WINDOWS = sys.platform.startswith('win')
if _IS_WINDOWS:
    import msvcrt
else:
    import fcntl


def _get_lock(path: Path) -> threading.Lock:
    """每个文件路径一个锁，避免不同文件相互阻塞"""
    key = str(path.resolve())
    with _locks_master:
        if key not in _file_locks:
            _file_locks[key] = threading.Lock()
        return _file_locks[key]


def _acquire_cross_process_lock(lock_fd, timeout_sec: float = 10.0):
    """跨进程独占锁，超时抛 TimeoutError"""
    deadline = time.time() + timeout_sec
    while True:
        try:
            if _IS_WINDOWS:
                msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.time() > deadline:
                raise TimeoutError("cross-process lock timeout")
            time.sleep(0.02)


def _release_cross_process_lock(lock_fd):
    try:
        if _IS_WINDOWS:
            try:
                msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass


def safe_save_json(path, data) -> None:
    """
    原子写入 JSON 文件：先写 .tmp 再 os.replace
    使用进程内锁 + 跨进程文件锁双重保护
    """
    p = Path(path)
    lock = _get_lock(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    lockfile = p.with_suffix(p.suffix + '.lock')
    with lock:
        with open(lockfile, 'a+b') as lf:
            try:
                _acquire_cross_process_lock(lf.fileno())
                tmp = p.with_suffix(p.suffix + ".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, p)
            finally:
                _release_cross_process_lock(lf.fileno())


def safe_load_json(path, default=None):
    """加锁读取 JSON，文件不存在或损坏返回 default"""
    p = Path(path)
    if not p.exists():
        return default
    lock = _get_lock(p)
    with lock:
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            time.sleep(0.05)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return default


@contextmanager
def update_json(path, default=None):
    """
    原子读-改-写上下文管理器，避免 RMW 竞争。
    用法：
        with update_json(STATE_PATH, default={"stats": {}}) as state:
            state["stats"]["total_conversations"] += 1
    锁的生命周期覆盖 读→改→写，跨线程跨进程安全。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lock = _get_lock(p)
    lockfile = p.with_suffix(p.suffix + '.lock')
    with lock:
        with open(lockfile, 'a+b') as lf:
            try:
                _acquire_cross_process_lock(lf.fileno())
                # 读
                if p.exists():
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            data = json.load(f)
                    except json.JSONDecodeError:
                        data = default if default is not None else {}
                else:
                    data = default if default is not None else {}
                yield data
                # 写
                tmp = p.with_suffix(p.suffix + ".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, p)
            finally:
                _release_cross_process_lock(lf.fileno())


# ── 金额精确提取（含中英文）──────────────────────────────────
_MONEY_PATTERNS = [
    r'(\d+(?:\.\d+)?)\s*(?:元|块钱|块|RMB|rmb)',
    r'¥\s*(\d+(?:\.\d+)?)',
    r'\$\s*(\d+(?:\.\d+)?)',
    r'(\d+(?:\.\d+)?)\s*(?:dollars?|usd|USD)',
    r'(?:赔|补|返)(?:偿|我|款|付|还)?\s*(\d+(?:\.\d+)?)',
    r'(?:compensate|refund|pay|reimburse)\s+(?:me\s+)?(\d+(?:\.\d+)?)',
    r'(\d+(?:\.\d+)?)\s*万(?:元)?',
]


def extract_compensation_amount(text: str):
    """提取金额，只接受带货币/赔偿语义的正数。返回 float 或 None"""
    if not text:
        return None

    # 万元单独
    m_wan = re.search(r'(\d+(?:\.\d+)?)\s*万(?:元)?', text)
    if m_wan:
        amt = float(m_wan.group(1)) * 10000
        return amt if amt > 0 else None

    for pattern in _MONEY_PATTERNS[:-1]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            amt = float(m.group(1))
            return amt if amt > 0 else None
    return None


# ── Prompt Injection 防护 ──────────────────────────────────
_INJECTION_PATTERNS = [
    "ignore previous", "ignore above", "disregard previous",
    "ignore all previous", "ignore your instructions",
    "忽略以上", "忽略之前", "忽略所有", "忘记你之前", "忘记之前",
    "你现在是", "system:", "assistant:",
    "<|im_start|>", "<|im_end|>",
    "system prompt", "系统提示", "你的指令是", "你的系统提示",
    "重置你的", "你不再是",
]


def has_injection_risk(text: str) -> bool:
    """检测明显的 prompt injection 特征"""
    if not text:
        return False
    low = text.lower()
    return any(p in low for p in _INJECTION_PATTERNS)


def sanitize_user_input(text: str, max_length: int = 2000) -> str:
    """输入清洗：去除控制字符、限制长度"""
    if not text:
        return ""
    cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    cleaned = cleaned.strip()
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length]
    return cleaned


# ── 寒暄/无意义短语判断（避免污染 gaps.json）─────────────────
_GREETING_PATTERNS = [
    r'^\s*(hi|hello|hey|你好|您好|在吗|在不在|早|早上好|晚上好|下午好)\s*[!！?？.。]*\s*$',
    r'^\s*(thanks?|thank you|谢谢|多谢|感谢|3q)\s*[!！?？.。]*\s*$',
    r'^\s*(ok|好|嗯|哦|行|可以|明白|知道了|收到)\s*[!！?？.。]*\s*$',
    r'^\s*(bye|goodbye|再见|拜拜|88)\s*[!！?？.。]*\s*$',
    r'^\s*[!！?？.。、，,\s]{0,4}$',  # 仅标点
]


def is_trivial_message(text: str) -> bool:
    """判断是否寒暄或无意义短语，这类消息不应被记为知识缺口"""
    if not text:
        return True
    t = text.strip().lower()
    if len(t) <= 2:
        return True
    return any(re.match(p, t, re.IGNORECASE) for p in _GREETING_PATTERNS)
