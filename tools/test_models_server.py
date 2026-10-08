# -*- coding: utf-8 -*-
"""服务器全模型实测：11 个模型逐一调用（真实扣分），输出可用性 + 单价。"""
import json, sys, time, urllib.request
sys.path.insert(0, "/opt/loomy2api")
KEY = [l.split("=",1)[1] for l in open("/root/.loomy2api_key") if l.startswith("KEY=")][0].strip()
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
B = "http://127.0.0.1:17890"

CHAT = ["deepseek-v4-flash-0731", "MiniMax-M3", "Kimi-k2.6", "qwen-3.8-max",
        "GLM-5.3-Flash", "qwen3.8-flash", "spark-x", "mimo-v2.5"]
IMAGES = ["Hy-Image-3.5-preview", "doubao-seedream-5-lite", "qwen-image-3.0-pro"]
BUDGET = {"qwen-3.8-max": 40}

def chat(model, max_tokens, prompt):
    body = json.dumps({"model": model, "stream": False, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    r = urllib.request.Request(B + "/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY},
        method="POST")
    t0 = time.time()
    j = json.loads(urllib.request.urlopen(r, timeout=200).read())
    u = j.get("usage") or {}
    ch = (j.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    return {"ok": True, "latency": round(time.time() - t0, 1),
            "tok": f"{u.get(chr(112)+chr(114)+chr(111)+chr(109)+chr(112)+chr(116)+chr(95)+chr(116)+chr(111)+chr(107)+chr(101)+chr(110)+chr(115))}/{u.get(chr(99)+chr(111)+chr(109)+chr(112)+chr(108)+chr(101)+chr(116)+chr(105)+chr(111)+chr(110)+chr(95)+chr(116)+chr(111)+chr(107)+chr(101)+chr(110)+chr(115))}",
            "points": u.get("points_consumed"),
            "served_model": j.get("model"),
            "content_head": (msg.get("content") or "")[:40]}

def gen_image(model):
    body = json.dumps({"model": model, "prompt": "a cute cat, flat illustration",
                       "n": 1, "size": "1024x1024", "response_format": "url"}).encode()
    r = urllib.request.Request(B + "/v1/images/generations", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY},
        method="POST")
    t0 = time.time()
    j = json.loads(urllib.request.urlopen(r, timeout=280).read())
    return {"ok": True, "latency": round(time.time() - t0, 1),
            "points": j.get("points_consumed"),
            "url": bool((j.get("data") or [{}])[0].get("url"))}

results = {}
print("==== chat 模型 ====")
for m in CHAT:
    try:
        r = chat(m, BUDGET.get(m, 120), "1+1等于几？直接回答数字")
        results[m] = r
        print(f"  {m:<28} ok 延迟{r[chr(108)+chr(97)+chr(116)+chr(101)+chr(110)+chr(99)+chr(121)]}s tok={r[chr(116)+chr(111)+chr(107)]} 扣{r[chr(112)+chr(111)+chr(105)+chr(110)+chr(116)+chr(115)]}分 回复={r[chr(99)+chr(111)+chr(110)+chr(116)+chr(101)+chr(110)+chr(116)+chr(95)+chr(104)+chr(101)+chr(97)+chr(100)]!r}")
    except urllib.error.HTTPError as e:
        results[m] = {"ok": False, "err": e.read().decode("utf-8","replace")[:100]}
        print(f"  {m:<28} FAIL {results[m][chr(101)+chr(114)+chr(114)][:80]}")
    except Exception as e:
        results[m] = {"ok": False, "err": f"{type(e).__name__}: {str(e)[:80]}"}
        print(f"  {m:<28} FAIL {results[m][chr(101)+chr(114)+chr(114)]}")

print("==== 生图模型 ====")
for m in IMAGES:
    try:
        r = gen_image(m)
        results[m] = r
        print(f"  {m:<28} ok 出图={r[chr(117)+chr(114)+chr(108)]} 延迟{r[chr(108)+chr(97)+chr(116)+chr(101)+chr(110)+chr(99)+chr(121)]}s 扣{r[chr(112)+chr(111)+chr(105)+chr(110)+chr(116)+chr(115)]}分")
    except urllib.error.HTTPError as e:
        results[m] = {"ok": False, "err": e.read().decode("utf-8","replace")[:100]}
        print(f"  {m:<28} FAIL {results[m][chr(101)+chr(114)+chr(114)][:80]}")
    except Exception as e:
        results[m] = {"ok": False, "err": f"{type(e).__name__}: {str(e)[:80]}"}
        print(f"  {m:<28} FAIL {results[m][chr(101)+chr(114)+chr(114)]}")

json.dump(results, open("/opt/loomy2api/state/model_test.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print("结果已写 state/model_test.json")
