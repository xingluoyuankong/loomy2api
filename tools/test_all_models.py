# -*- coding: utf-8 -*-
"""全模型实测矩阵：非流式/流式/工具调用/思考等级/生图。经网关，扣真实积分。"""
import json
import sys
import urllib.request

sys.path.insert(0, '.')
from loomy2api.upstream import ModelGateway

op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
cfg = json.load(open('config.json', encoding='utf-8'))
gw = ModelGateway(cfg)
d = json.load(open('accounts.json', encoding='utf-8'))
accs = d['accounts'] if isinstance(d, dict) and 'accounts' in d else d
sess, proxy = accs[0]['session'], (accs[0].get('proxy') or None)

CHAT = ['deepseek-v4-flash-0731', 'MiniMax-M3', 'Kimi-k2.6', 'qwen-3.8-max',
        'GLM-5.3-Flash', 'qwen3.8-flash', 'spark-x', 'mimo-v2.5']
IMAGES = ['Hy-Image-3.5-preview', 'doubao-seedream-5-lite', 'qwen-image-3.0-pro']
# qwen-3.8-max 倍率 x12，限量省积分
BUDGET = {'qwen-3.8-max': 40}

TOOL = [{
    'type': 'function',
    'function': {'name': 'get_weather',
                 'description': '查询城市天气',
                 'parameters': {'type': 'object',
                                'properties': {'city': {'type': 'string'}},
                                'required': ['city']}},
}]


def chat(payload, timeout=180):
    body = json.dumps(payload).encode()
    r = urllib.request.Request('http://127.0.0.1:17890/v1/chat/completions',
                               data=body, headers={'Content-Type': 'application/json'},
                               method='POST')
    try:
        j = json.loads(op.open(r, timeout=timeout).read())
        ch = (j.get('choices') or [{}])[0]
        msg = ch.get('message') or {}
        return {
            'ok': True,
            'content': (msg.get('content') or '')[:60],
            'reasoning': (msg.get('reasoning_content')
                          or msg.get('reasoning') or '')[:40],
            'tool_calls': [tc.get('function', {}).get('name')
                           for tc in (msg.get('tool_calls') or [])],
            'finish': ch.get('finish_reason'),
            'usage': j.get('usage') or {},
        }
    except urllib.error.HTTPError as e:
        return {'ok': False, 'err': e.read().decode('utf-8', 'replace')[:160]}
    except Exception as e:                              # noqa: BLE001
        return {'ok': False, 'err': f'{type(e).__name__}: {str(e)[:120]}'}


def stream_ok(model):
    body = json.dumps({'model': model, 'stream': True, 'max_tokens': 24,
                       'messages': [{'role': 'user', 'content': '数到3'}]}).encode()
    r = urllib.request.Request('http://127.0.0.1:17890/v1/chat/completions',
                               data=body, headers={'Content-Type': 'application/json'},
                               method='POST')
    try:
        resp = op.open(r, timeout=180)
        chunks, has_reason, has_tool, done = 0, False, False, False
        for line in resp:
            line = line.decode('utf-8', 'replace').strip()
            if line.startswith('data:'):
                chunks += 1
                payload = line[5:].strip()
                if payload == '[DONE]':
                    done = True
                    continue
                try:
                    delta = (((json.loads(payload).get('choices') or [{}])[0])
                             .get('delta') or {})
                except ValueError:
                    continue
                if delta.get('reasoning_content') or delta.get('reasoning'):
                    has_reason = True
                if delta.get('tool_calls'):
                    has_tool = True
        return f'{chunks}块 DONE={done} 含思考={has_reason}'
    except Exception as e:                              # noqa: BLE001
        return f'ERR {str(e)[:80]}'


print('==== A. 非流式 + 思考(xhigh) + 工具调用 ====')
for m in CHAT:
    mt = BUDGET.get(m, 160)
    r1 = chat({'model': m, 'max_tokens': mt, 'stream': False,
               'messages': [{'role': 'user', 'content': '1+1等于几？直接回答'}]})
    r2 = chat({'model': m, 'max_tokens': min(mt, 300), 'stream': False,
               'reasoning_effort': 'xhigh',
               'messages': [{'role': 'user', 'content': '9.11 和 9.9 哪个大？'}]})
    r3 = chat({'model': m, 'max_tokens': min(mt, 160), 'stream': False,
               'tools': TOOL, 'tool_choice': 'auto',
               'messages': [{'role': 'user', 'content': '北京今天天气怎么样？'}]})
    u = r1.get('usage') or {}
    print(f"{m}")
    print(f"   基本: ok={r1['ok']} finish={r1.get('finish')} "
          f"content={r1.get('content','')!r} 扣分={u.get('points_consumed')}"
          + ('' if r1['ok'] else f" err={r1.get('err')}"))
    print(f"   思考xhigh: ok={r2['ok']} reasoning={r2.get('reasoning','')!r} "
          f"content={r2.get('content','')!r}"
          + ('' if r2['ok'] else f" err={r2.get('err')}"))
    print(f"   工具: ok={r3['ok']} tool_calls={r3.get('tool_calls')}"
          + ('' if r3['ok'] else f" err={r3.get('err')}"))

print()
print('==== B. 流式（每个模型）====')
for m in CHAT:
    print(f'{m:<28} {stream_ok(m)}')

print()
print('==== C. 生图（上游 /images/generations 直连）====')
for m in IMAGES:
    body = json.dumps({'model': m, 'prompt': '一只戴墨镜的橘猫，扁平插画风',
                       'n': 1, 'size': '1024x1024',
                       'response_format': 'url'}).encode()
    code, _h, raw = gw.call(sess, 'images/generations', json.loads(body),
                            method='POST', proxy=proxy)
    txt = raw.decode('utf-8', 'replace')
    try:
        j = json.loads(txt)
        data = j.get('data') or []
        first = (data[0] if data else {})
        url = first.get('url') or ''
        b64 = first.get('b64_json') or ''
        print(f'{m}: HTTP {code} code={j.get("code")} n={len(data)} '
              f'url={url[:80]!r} b64={len(b64)}字节')
        if j.get('code') and j.get('code') != '000000':
            print('   desc:', j.get('desc'), str(j.get('data'))[:120])
    except ValueError:
        print(f'{m}: HTTP {code} 非JSON {txt[:150]}')
