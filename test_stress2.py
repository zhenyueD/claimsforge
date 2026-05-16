"""
端到端压测脚本 - 简化版，避免编码问题
"""
import threading
import requests
import json
import time
import sys
import os
import io
from pathlib import Path
from datetime import datetime

# 强制 UTF-8 输出
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

BASE_URL = "http://localhost:8002"
BASE_URL_ALT = "http://localhost:8001"
DATA_DIR = Path(r"C:\Users\admin\.easyclaw\workspace\customer-service-team\data")
STATE_PATH = DATA_DIR / "state.json"
TICKETS_PATH = DATA_DIR / "tickets.json"
GAPS_PATH = DATA_DIR / "gaps.json"

results = {}

def log(tag, msg):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}][{tag}] {msg}", flush=True)

def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except:
        return None

# ============================================================
# TEST 1: 真实并发 50 个请求
# ============================================================
def test_concurrent_chat():
    log("T1", "=== CONCURRENT 50 REQUESTS ===")
    
    state_before = read_json(STATE_PATH)
    tc_before = state_before["stats"]["total_conversations"] if state_before else 0
    log("T1", f"Initial total_conversations = {tc_before}")
    
    N = 50
    ok = 0
    errors = []
    lock = threading.Lock()
    
    def shoot(i):
        nonlocal ok
        try:
            r = requests.post(f"{BASE_URL}/api/chat", json={
                "session_id": f"stress-t1-{i:03d}",
                "message": f"query order {i:03d}"
            }, timeout=15)
            if r.status_code == 200:
                with lock: ok += 1
            else:
                with lock: errors.append((i, r.status_code, r.text[:80]))
        except Exception as e:
            with lock: errors.append((i, "EXC", str(e)[:80]))
    
    threads = [threading.Thread(target=shoot, args=(i,)) for i in range(N)]
    t0 = time.time()
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed = time.time() - t0
    
    state_after = read_json(STATE_PATH)
    tc_after = state_after["stats"]["total_conversations"] if state_after else 0
    actual_increment = tc_after - tc_before
    
    log("T1", f"Sent={N}, OK={ok}, Errors={len(errors)}, Time={elapsed:.2f}s")
    log("T1", f"tc increment: {actual_increment} (expected={ok})")
    if errors:
        log("T1", f"First error: {errors[0]}")
    
    lost = ok - actual_increment
    results["T1_concurrent"] = {
        "sent": N, "ok": ok, "errors": len(errors),
        "tc_increment": actual_increment,
        "lost_count": lost,
        "passed": lost == 0
    }
    
    if lost > 0:
        log("T1", f"[FAILED] Lost {lost} counts (concurrent write conflict!)")
    else:
        log("T1", f"[PASSED] No lost counts")
    
    return ok, tc_before


# ============================================================
# TEST 2: Cross-process concurrent (8001 vs 8002)
# ============================================================
def test_cross_process_concurrent():
    log("T2", "=== CROSS-PROCESS CONCURRENT (8001 vs 8002) ===")
    
    state_before = read_json(STATE_PATH)
    tc_before = state_before["stats"]["total_conversations"] if state_before else 0
    
    N_each = 20
    ok_8001 = 0
    ok_8002 = 0
    lock = threading.Lock()
    
    def shoot_8001(i):
        nonlocal ok_8001
        try:
            r = requests.post(f"{BASE_URL_ALT}/api/chat", json={
                "session_id": f"stress-t2-8001-{i:03d}",
                "message": f"test 8001 port {i}"
            }, timeout=15)
            if r.status_code == 200:
                with lock: ok_8001 += 1
        except: pass
    
    def shoot_8002(i):
        nonlocal ok_8002
        try:
            r = requests.post(f"{BASE_URL}/api/chat", json={
                "session_id": f"stress-t2-8002-{i:03d}",
                "message": f"test 8002 port {i}"
            }, timeout=15)
            if r.status_code == 200:
                with lock: ok_8002 += 1
        except: pass
    
    threads = (
        [threading.Thread(target=shoot_8001, args=(i,)) for i in range(N_each)] +
        [threading.Thread(target=shoot_8002, args=(i,)) for i in range(N_each)]
    )
    t0 = time.time()
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed = time.time() - t0
    
    state_after = read_json(STATE_PATH)
    tc_after = state_after["stats"]["total_conversations"] if state_after else 0
    actual_increment = tc_after - tc_before
    expected = ok_8001 + ok_8002
    lost = expected - actual_increment
    
    log("T2", f"8001 OK={ok_8001}, 8002 OK={ok_8002}, Time={elapsed:.2f}s")
    log("T2", f"tc increment: {actual_increment} (expected={expected}), lost={lost}")
    
    results["T2_cross_process"] = {
        "ok_8001": ok_8001, "ok_8002": ok_8002,
        "expected": expected, "actual_increment": actual_increment,
        "lost_count": lost,
        "passed": abs(lost) <= 2
    }
    
    if abs(lost) > 2:
        log("T2", f"[FAILED] Cross-process write conflict! Lost {lost}")
    else:
        log("T2", f"[PASSED] Cross-process OK (lost={lost})")


# ============================================================
# TEST 3: TF-IDF corner cases
# ============================================================
def test_tfidf_corner_cases():
    log("T3", "=== TF-IDF CORNER CASES ===")
    
    queries = [
        "refund request",
        "refund refund refund refund refund",
        "hello",
        "hi there",
        "aaaaaaaaa",
        "refund",
        "I want refund",
        "\u6211\u8981\u9000\u6b3e",
        "\u9000\u9000\u9000\u9000\u9000\u6b3e",
        "\u4f60\u597d",
        "\u5728\u5417",
    ]
    
    tfidf_results = {}
    for q in queries:
        try:
            r = requests.post(f"{BASE_URL}/api/chat", json={
                "session_id": "stress-t3-tfidf",
                "message": q
            }, timeout=10)
            if r.status_code == 200:
                data = r.json()
                conf = data.get("confidence", 0)
                kb = data.get("kb_results", [])
                tfidf_results[q] = {
                    "confidence": conf,
                    "top": kb[0]["question"][:30] if kb else None
                }
                log("T3", f"  '{q[:25]}' conf={conf:.3f} top={kb[0]['question'][:25] if kb else 'NONE'}")
        except Exception as e:
            log("T3", f"  '{q}' ERROR: {e}")
    
    # Check: repeat chars vs normal
    normal_conf = tfidf_results.get("\u6211\u8981\u9000\u6b3e", {}).get("confidence", 0)
    repeat_conf = tfidf_results.get("\u9000\u9000\u9000\u9000\u9000\u6b3e", {}).get("confidence", 0)
    chat_conf   = tfidf_results.get("\u4f60\u597d", {}).get("confidence", 0)
    
    results["T3_tfidf"] = {
        "normal_refund_conf": normal_conf,
        "repeat_char_conf": repeat_conf,
        "greeting_conf": chat_conf,
        "repeat_inflated": repeat_conf > normal_conf * 1.5,
        "greeting_causes_gap": chat_conf < 0.3
    }
    
    if repeat_conf > normal_conf * 1.5:
        log("T3", f"[ISSUE] Repeat chars inflate conf: normal={normal_conf:.3f} repeat={repeat_conf:.3f}")
    if chat_conf < 0.3:
        log("T3", f"[ISSUE] Greeting conf={chat_conf:.3f} < 0.3 => triggers gap pollution")


# ============================================================
# TEST 4: Emotion trend underflow
# ============================================================
def test_emotion_trend():
    log("T4", "=== EMOTION TREND UNDERFLOW ===")
    session_id = "stress-t4-emotion"
    
    angry_msgs = [
        "\u5783\u573e\u670d\u52a1\uff01\uff01\uff01",
        "\u9a97\u4eba\u7684\uff01\uff01\u5e9f\u7269\uff01\uff01",
        "\u70c2\u900f\u4e86\uff0c\u65e0\u80fd\uff01\uff01\uff01",
        "\u8fd8\u5728\u9a97\u4eba\uff0c\u6eda\uff01\uff01",
        "\u5e9f\u7269\u5783\u573e\u9a97\u5b50\uff01\uff01\uff1f\uff1f",
        "\u592a\u70c2\u4e86\u592a\u70c2\u4e86\uff01\uff01\uff01\uff01\uff01\uff01\uff01\uff01",
        "\u5f7b\u5e95\u5931\u671b\uff0c\u65e0\u80fd\u5e9f\u7269\uff01\uff01\uff01\uff01",
    ]
    
    scores = []
    for i, msg in enumerate(angry_msgs):
        try:
            r = requests.post(f"{BASE_URL}/api/chat", json={
                "session_id": session_id, "message": msg
            }, timeout=10)
            if r.status_code == 200:
                data = r.json()
                score = data.get("emotion", {}).get("score", 5)
                scores.append(score)
                log("T4", f"  Round {i+1}: score={score}")
        except Exception as e:
            log("T4", f"  Round {i+1} ERROR: {e}")
    
    results["T4_emotion"] = {
        "scores": scores,
        "min_score": min(scores) if scores else None,
        "below_zero": any(s < 0 for s in scores),
        "all_in_range": all(0 <= s <= 10 for s in scores)
    }
    
    if any(s < 0 for s in scores):
        log("T4", f"[FAILED] Negative emotion score! {[s for s in scores if s < 0]}")
    else:
        log("T4", f"[PASSED] All scores in [0,10]: {scores}")


# ============================================================
# TEST 5: Ticket accumulation / memory leak
# ============================================================
def test_ticket_accumulation():
    log("T5", "=== TICKET ACCUMULATION ===")
    
    tickets_before = read_json(TICKETS_PATH)
    n_before = len(tickets_before) if isinstance(tickets_before, list) else 0
    
    N = 20
    created = 0
    for i in range(N):
        try:
            r = requests.post(f"{BASE_URL}/api/chat", json={
                "session_id": f"stress-t5-{i:03d}",
                "message": f"\u4f60\u4eec\u9a97\u4eba\uff01\u6211\u8981\u8d54\u507f{10+i}\u5143\uff01"
            }, timeout=10)
            if r.status_code == 200 and r.json().get("escalated"):
                created += 1
        except: pass
    
    tickets_after = read_json(TICKETS_PATH)
    n_after = len(tickets_after) if isinstance(tickets_after, list) else 0
    
    r = requests.get(f"{BASE_URL}/api/tickets", timeout=5)
    api_count = len(r.json()) if r.status_code == 200 else -1
    
    file_size = TICKETS_PATH.stat().st_size if TICKETS_PATH.exists() else 0
    
    log("T5", f"Tickets: {n_before} -> {n_after} (+{n_after-n_before})")
    log("T5", f"API /api/tickets returns: {api_count} items (capped at 20)")
    log("T5", f"tickets.json size: {file_size/1024:.1f} KB")
    
    results["T5_tickets"] = {
        "before": n_before, "after": n_after, "created": created,
        "api_returns": api_count, "file_size_kb": file_size/1024,
        "file_grows_unbounded": True  # no cap on file
    }
    
    log("T5", f"[NOTE] tickets.json grows unbounded: {n_after} total, no archive/truncate")


# ============================================================
# TEST 6: KB add oversized fields
# ============================================================
def test_kb_add_oversized():
    log("T6", "=== KB ADD OVERSIZED FIELDS ===")
    
    kb_before = read_json(DATA_DIR / "knowledge_base.json")
    n_before = len(kb_before.get("entries", [])) if kb_before else 0
    
    test_cases = [
        ("normal", "Test", "Normal Q?", "Normal A.", ["kw1"]),
        ("long_question_600", "Test", "Q"*600, "Normal A.", ["kw1"]),
        ("long_answer_3000", "Test", "Normal Q?", "A"*3000, ["kw1"]),
        ("long_category_100", "C"*100, "Normal Q?", "Normal A.", ["kw1"]),
        ("too_many_keywords_50", "Test", "Normal Q?", "Normal A.", [f"kw{i}" for i in range(50)]),
        ("very_long_keyword", "Test", "Normal Q?", "Normal A.", ["K"*500]),
    ]
    
    kb_results = {}
    for name, cat, q, a, kws in test_cases:
        try:
            r = requests.post(f"{BASE_URL}/api/kb/add", json={
                "category": cat, "question": q, "answer": a, "keywords": kws
            }, timeout=10)
            kb_results[name] = r.status_code
            if r.status_code == 200:
                log("T6", f"  '{name}': 200 OK [NOT BLOCKED]")
            elif r.status_code == 422:
                log("T6", f"  '{name}': 422 Rejected [BLOCKED]")
            else:
                log("T6", f"  '{name}': {r.status_code}")
        except Exception as e:
            kb_results[name] = "EXC"
            log("T6", f"  '{name}': EXCEPTION {e}")
    
    kb_after = read_json(DATA_DIR / "knowledge_base.json")
    n_after = len(kb_after.get("entries", [])) if kb_after else 0
    
    unblocked_oversized = [n for n in kb_results if n != "normal" and kb_results[n] == 200]
    
    results["T6_kb_add"] = {
        "before": n_before, "after": n_after,
        "added_count": n_after - n_before,
        "unblocked_oversized": unblocked_oversized,
        "cases": kb_results
    }
    
    if unblocked_oversized:
        log("T6", f"[ISSUE] Oversized fields accepted: {unblocked_oversized}")
    else:
        log("T6", f"[PASSED] All oversized fields blocked")


# ============================================================
# TEST 7: Vector rebuild blocking
# ============================================================
def test_vector_rebuild_blocking():
    log("T7", "=== VECTOR REBUILD BLOCKING ===")
    
    rebuild_times = []
    chat_latencies = []
    lock = threading.Lock()
    
    def do_rebuild():
        t0 = time.time()
        try:
            r = requests.post(f"{BASE_URL}/api/vector/build?force=true", timeout=30)
            elapsed = time.time() - t0
            with lock: rebuild_times.append(elapsed)
            log("T7", f"  Rebuild: {elapsed:.3f}s, status={r.status_code}")
        except Exception as e:
            log("T7", f"  Rebuild ERROR: {e}")
    
    def do_chat():
        t0 = time.time()
        try:
            r = requests.post(f"{BASE_URL}/api/chat", json={
                "session_id": "stress-t7-chat",
                "message": "order status query"
            }, timeout=10)
            elapsed = time.time() - t0
            with lock: chat_latencies.append(elapsed)
        except:
            with lock: chat_latencies.append(999)
    
    threads = (
        [threading.Thread(target=do_rebuild) for _ in range(3)] +
        [threading.Thread(target=do_chat) for _ in range(10)]
    )
    for t in threads: t.start()
    for t in threads: t.join()
    
    avg_chat = sum(chat_latencies)/len(chat_latencies) if chat_latencies else 0
    max_chat = max(chat_latencies) if chat_latencies else 0
    
    log("T7", f"  Chat avg={avg_chat*1000:.0f}ms, max={max_chat*1000:.0f}ms during rebuild")
    
    results["T7_rebuild"] = {
        "rebuild_times": [round(t, 3) for t in rebuild_times],
        "chat_avg_ms": round(avg_chat*1000),
        "chat_max_ms": round(max_chat*1000),
        "blocking_detected": max_chat > 2.0
    }
    
    if max_chat > 2.0:
        log("T7", f"[ISSUE] Rebuild blocks chat: max={max_chat:.2f}s")
    else:
        log("T7", f"[PASSED] Rebuild impact acceptable")


# ============================================================
# TEST 8: Gap pollution from greetings
# ============================================================
def test_gap_pollution():
    log("T8", "=== GAP POLLUTION FROM GREETINGS ===")
    
    gaps_before = read_json(GAPS_PATH)
    n_before = len(gaps_before) if isinstance(gaps_before, list) else 0
    
    greetings = ["\u4f60\u597d", "\u5728\u5417", "\u55e8", "hello", "\u8bf7\u95ee",
                 "Hi", "\u6709\u4eba\u5417", "\u5581", "\u60a8\u597d", "\u5728\u7ebf\u5417"]
    gap_triggers = 0
    trigger_list = []
    
    for g in greetings:
        try:
            r = requests.post(f"{BASE_URL}/api/chat", json={
                "session_id": "stress-t8-gap",
                "message": g
            }, timeout=10)
            if r.status_code == 200:
                data = r.json()
                conf = data.get("confidence", 1.0)
                if conf < 0.3:
                    gap_triggers += 1
                    trigger_list.append(g)
                    log("T8", f"  '{g}' conf={conf:.3f} => GAP TRIGGERED")
                else:
                    log("T8", f"  '{g}' conf={conf:.3f} OK")
        except Exception as e:
            log("T8", f"  '{g}' ERROR: {e}")
    
    gaps_after = read_json(GAPS_PATH)
    n_after = len(gaps_after) if isinstance(gaps_after, list) else 0
    
    results["T8_gap"] = {
        "greetings_sent": len(greetings),
        "gap_triggers": gap_triggers,
        "pollution_rate": round(gap_triggers/len(greetings), 2),
        "trigger_list": trigger_list,
        "gaps_before": n_before, "gaps_after": n_after
    }
    
    log("T8", f"Gap pollution: {gap_triggers}/{len(greetings)} = {gap_triggers/len(greetings)*100:.0f}%")
    if gap_triggers > len(greetings) * 0.5:
        log("T8", f"[ISSUE] High gap pollution rate from greetings")


# ============================================================
# TEST 9: File/memory bloat
# ============================================================
def test_state_bloat():
    log("T9", "=== FILE BLOAT ANALYSIS ===")
    
    state = read_json(STATE_PATH)
    state_size = STATE_PATH.stat().st_size if STATE_PATH.exists() else 0
    gaps_size = GAPS_PATH.stat().st_size if GAPS_PATH.exists() else 0
    gaps = read_json(GAPS_PATH)
    gaps_count = len(gaps) if isinstance(gaps, list) else 0
    
    kb = read_json(DATA_DIR / "knowledge_base.json")
    update_log_size = len(kb.get("update_log", [])) if kb else 0
    kb_size = (DATA_DIR / "knowledge_base.json").stat().st_size
    
    tickets = read_json(TICKETS_PATH)
    tickets_count = len(tickets) if isinstance(tickets, list) else 0
    tickets_size = TICKETS_PATH.stat().st_size if TICKETS_PATH.exists() else 0
    
    log("T9", f"  state.json: {state_size} bytes")
    log("T9", f"  gaps.json: {gaps_count} entries, {gaps_size} bytes")
    log("T9", f"  KB update_log: {update_log_size} entries")
    log("T9", f"  tickets.json: {tickets_count} entries, {tickets_size/1024:.1f} KB")
    
    results["T9_bloat"] = {
        "state_bytes": state_size,
        "gaps_count": gaps_count,
        "gaps_bytes": gaps_size,
        "kb_update_log_count": update_log_size,
        "kb_bytes": kb_size,
        "tickets_count": tickets_count,
        "tickets_kb": round(tickets_size/1024, 1),
        "no_gaps_cleanup": True,
        "no_tickets_archive": True,
        "no_update_log_cap": True
    }
    
    log("T9", f"  [NOTE] gaps.json: no auto-cleanup ({gaps_count} entries)")
    log("T9", f"  [NOTE] tickets.json: no archive ({tickets_count} entries)")
    log("T9", f"  [NOTE] KB update_log: no cap ({update_log_size} entries)")


# ============================================================
# TEST 10: Error info exposure
# ============================================================
def test_error_info_exposure():
    log("T10", "=== ERROR INFO EXPOSURE ===")
    
    test_cases = [
        ("GET nonexistent-route", "GET", f"{BASE_URL}/api/xxx-nonexistent"),
        ("GET history no-session", "GET", f"{BASE_URL}/api/history/no-such-session-xyz"),
        ("POST chat empty body", "POST_EMPTY", f"{BASE_URL}/api/chat"),
        ("POST kb/add bad body", "POST_INVALID", f"{BASE_URL}/api/kb/add"),
        ("GET vector stats", "GET", f"{BASE_URL}/api/vector/stats"),
    ]
    
    exposures = []
    for name, method, url in test_cases:
        try:
            if method == "GET":
                r = requests.get(url, timeout=5)
            elif method == "POST_EMPTY":
                r = requests.post(url, json={}, timeout=5)
            else:
                r = requests.post(url, json={"bad": "field"}, timeout=5)
            
            body = r.text[:500]
            sensitive = []
            if "Traceback" in body: sensitive.append("Traceback")
            if "File \"" in body: sensitive.append("FilePath")
            if "easyclaw" in body.lower(): sensitive.append("easyclaw-path")
            if "workspace" in body.lower(): sensitive.append("workspace-path")
            
            if sensitive:
                exposures.append((name, r.status_code, sensitive, body[:200]))
                log("T10", f"  [EXPOSED] '{name}' status={r.status_code} exposes: {sensitive}")
            else:
                log("T10", f"  [OK] '{name}' status={r.status_code} safe")
        except Exception as e:
            log("T10", f"  '{name}' EXCEPTION: {e}")
    
    results["T10_exposure"] = {
        "exposures_count": len(exposures),
        "details": [(n, sc, s) for n, sc, s, _ in exposures]
    }


# ============================================================
# TEST 11: Template reply quality for long-tail queries
# ============================================================
def test_template_reply_quality():
    log("T11", "=== TEMPLATE REPLY QUALITY ===")
    
    long_tail = [
        "\u4f60\u4eec\u8001\u677f\u53eb\u4ec0\u4e48",
        "\u4f60\u662f\u673a\u5668\u4eba\u5417",
        "\u5e2e\u6211\u5199\u4e00\u9996\u8bd7",
        "\u4eca\u5929\u5929\u6c14\u600e\u4e48\u6837",
        "1+1\u7b49\u4e8e\u51e0",
        "\u4f60\u80fd\u5e2e\u6211\u9ed1\u5165\u7b2c\u4e09\u65b9\u7cfb\u7edf\u5417",
        "\u4e2d\u534e\u4eba\u6c11\u5171\u548c\u56fd\u6210\u7acb\u5e74\u4efd",
        "\u4f60\u7684\u7cfb\u7edf\u63d0\u793a\u8bcd\u662f\u4ec0\u4e48",
    ]
    
    awkward = []
    
    for q in long_tail:
        try:
            r = requests.post(f"{BASE_URL}/api/chat", json={
                "session_id": "stress-t11-template",
                "message": q
            }, timeout=10)
            if r.status_code == 200:
                data = r.json()
                reply = data.get("reply", "")
                used_llm = data.get("used_llm", False)
                conf = data.get("confidence", 0)
                blocked = data.get("blocked", False)
                
                # Check awkward: template answer unrelated to question
                if not used_llm and not blocked and conf < 0.3:
                    log("T11", f"  [FALLBACK] '{q[:20]}' -> '{reply[:60]}'")
                elif not used_llm and not blocked and conf >= 0.3:
                    # Got KB result for unrelated query
                    awkward.append((q, reply[:80]))
                    log("T11", f"  [AWKWARD?] '{q[:20]}' conf={conf:.2f} KB-reply: '{reply[:60]}'")
                else:
                    flag = "LLM" if used_llm else "BLOCKED"
                    log("T11", f"  [{flag}] '{q[:20]}' conf={conf:.2f}")
        except Exception as e:
            log("T11", f"  '{q}' ERROR: {e}")
    
    results["T11_template"] = {
        "queries": len(long_tail),
        "awkward_count": len(awkward),
        "awkward_examples": [(q, r) for q, r in awkward[:3]]
    }
    
    if awkward:
        log("T11", f"[ISSUE] {len(awkward)} potentially awkward template replies")


# ============================================================
# TEST 12: Session memory (no eviction, no persistence)
# ============================================================
def test_session_memory():
    log("T12", "=== SESSION MEMORY (no eviction) ===")
    
    N = 100
    ok = 0
    for i in range(N):
        try:
            r = requests.post(f"{BASE_URL}/api/chat", json={
                "session_id": f"stress-t12-unique-{i:04d}",
                "message": "session memory test"
            }, timeout=5)
            if r.status_code == 200: ok += 1
        except: pass
    
    log("T12", f"  Created {ok}/{N} unique sessions (all in-memory, no eviction)")
    log("T12", f"  sessions dict has no TTL/LRU, OOM risk under sustained traffic")
    log("T12", f"  After restart: all session history lost (no persistence)")
    
    results["T12_sessions"] = {
        "created": ok, "in_memory_only": True,
        "no_eviction": True, "no_persistence": True,
        "no_ttl": True
    }


# ============================================================
# TEST 13: KB keywords length validation
# ============================================================
def test_kb_keyword_length():
    log("T13", "=== KB KEYWORD ITEM LENGTH ===")
    
    # Keywords list items can be arbitrarily long (no per-item length check)
    long_kw = "K" * 1000
    try:
        r = requests.post(f"{BASE_URL}/api/kb/add", json={
            "category": "Test",
            "question": "Test question",
            "answer": "Test answer",
            "keywords": [long_kw, "normal"]
        }, timeout=10)
        if r.status_code == 200:
            log("T13", f"  [ISSUE] 1000-char keyword item accepted (status=200)")
            results["T13_keyword_len"] = {"status": 200, "blocked": False}
        else:
            log("T13", f"  [OK] Blocked with status={r.status_code}")
            results["T13_keyword_len"] = {"status": r.status_code, "blocked": True}
    except Exception as e:
        log("T13", f"  ERROR: {e}")
        results["T13_keyword_len"] = {"status": "EXC", "blocked": False}


# ============================================================
# Summary
# ============================================================
def print_summary():
    print("\n" + "="*70)
    print("STRESS TEST SUMMARY")
    print("="*70)
    for k, v in results.items():
        passed = v.get("passed", None)
        if passed is True:
            icon = "[PASS]"
        elif passed is False:
            icon = "[FAIL]"
        else:
            icon = "[INFO]"
        print(f"{icon} {k}: {json.dumps(v, ensure_ascii=False, default=str)}")
    print("="*70)


if __name__ == "__main__":
    print("="*70)
    print("AI Customer Service - Stress Test")
    print(f"Target: {BASE_URL} / {BASE_URL_ALT}")
    print(f"Time: {datetime.now()}")
    print("="*70 + "\n")
    
    test_concurrent_chat()
    time.sleep(1)
    test_cross_process_concurrent()
    time.sleep(1)
    test_tfidf_corner_cases()
    time.sleep(0.5)
    test_emotion_trend()
    time.sleep(0.5)
    test_ticket_accumulation()
    time.sleep(0.5)
    test_kb_add_oversized()
    time.sleep(0.5)
    test_vector_rebuild_blocking()
    time.sleep(0.5)
    test_gap_pollution()
    time.sleep(0.5)
    test_state_bloat()
    time.sleep(0.5)
    test_error_info_exposure()
    time.sleep(0.5)
    test_template_reply_quality()
    time.sleep(0.5)
    test_session_memory()
    time.sleep(0.5)
    test_kb_keyword_length()
    
    print_summary()
