import urllib.request, json

API = 'http://localhost:8001'

cases = [
    ('退款查询', '我想申请退款，请问怎么操作？'),
    ('技术故障', '登录一直失败，提示网络错误怎么办'),
    ('升级触发', '你们系统有问题！我要赔偿！12315投诉你们！'),
    ('知识缺口', '你们支持区块链支付吗'),
]

for name, msg in cases:
    data = json.dumps({'session_id': f'test-{name}', 'message': msg}).encode()
    req = urllib.request.Request(f'{API}/api/chat', data=data, headers={'Content-Type':'application/json'})
    d = json.loads(urllib.request.urlopen(req, timeout=15).read())
    reply = d['reply']
    print(f'[{name}]')
    print(f'  回复: {reply[:70]}')
    print(f'  LLM: {d.get("used_llm")} | 置信度: {d.get("confidence")} | 升级: {d.get("escalated")} | ticket: {d.get("ticket_id")}')
    kb = d.get('kb_results', [])
    if kb:
        src = [(r['id'], r['category']) for r in kb[:2]]
        print(f'  来源: {src}')
    print()
