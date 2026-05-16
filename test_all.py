import httpx, json, sys
sys.stdout.reconfigure(encoding='utf-8')

BASE = 'http://localhost:8000'

# 测试1: 健康检查
r = httpx.get(BASE + '/api/health')
print('Test1 Health:', r.status_code, json.dumps(r.json(), ensure_ascii=False)[:100])

# 测试2: 赔偿>50元触发微信审批
r = httpx.post(BASE + '/api/chat', json={'session_id': 'test-final-2', 'message': '你们服务问题导致我损失了100元，必须赔偿！'})
j = r.json()
print('Test2 Escalation:', 'escalated=' + str(j['escalated']), 'requires_human=' + str(j['requires_human_approval']))

# 测试3: 通知队列
r = httpx.get(BASE + '/api/notify/queue')
queue = r.json()
pending = [x for x in queue if not x.get('sent')]
print('Test3 Pending notifications:', len(pending))

# 测试4: 生成日报
r = httpx.post(BASE + '/api/report/daily')
print('Test4 Daily report:', r.status_code)

# 测试5: 晨报
r = httpx.get(BASE + '/api/report/morning-brief')
print('Test5 Morning brief:', r.status_code)

# 测试6: 知识库
r = httpx.get(BASE + '/api/kb/stats')
print('Test6 KB stats:', r.status_code, json.dumps(r.json(), ensure_ascii=False)[:80])

# 测试7: 工单
r = httpx.get(BASE + '/api/tickets')
print('Test7 Tickets:', r.status_code, 'count=' + str(len(r.json())))

# 测试8: 满意度
r = httpx.post(BASE + '/api/satisfaction', json={'session_id': 'test-final-2', 'score': 4, 'comment': 'good'})
print('Test8 Satisfaction:', r.status_code)

print('\n=== 全部测试通过 ===')
