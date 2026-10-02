# loomy2api

把 **Loomy**（科大讯飞桌面 AI 助理）账号的模型额度，变成一个自托管的
**OpenAI / Anthropic 双协议兼容 API**，并且**支持多账号池**——轮换、故障转移、登录态自动续期。

* **零依赖** —— 纯 Python 标准库（3.9+），不需要装任何第三方包。
* **不需要装桌面客户端** —— 网关自己用「手机号 + 密码」在服务端登录，并自动续期 14 天的登录态。
* **多账号** —— 想加几个账号就加几个；按余额 / 轮询 / 最近最少使用三种策略路由，
  登录态失效自动冷却并换下一个账号重试。
* **两种协议都支持** —— OpenAI 客户端走 `/v1/chat/completions`，Claude Code 等走 `/v1/messages`
  （流式、工具调用、思考链都已翻译）。

[English README](README.md) · [逆向协议文档](docs/PROTOCOL.md)

---

## 功能一览

| | |
|---|---|
| OpenAI 协议 | `POST /v1/chat/completions`（流式/非流式）、`GET /v1/models`、`POST /v1/embeddings`、`POST /v1/images/generations` |
| Anthropic 协议 | `POST /v1/messages`（流式/非流式），支持 thinking 块、tool_use / tool_result |
| 网页面板 | `http://127.0.0.1:17890/panel` —— 看各账号积分、添加/删除/禁用、强制续期、重绑设备标识、实时日志 |
| 多账号 | `balance`（默认，按可用积分）· `round_robin`（轮询）· `lru`（最久未用）三种策略；账号级冷却；失败自动换号重试；额度跟踪 |
| 设备标识 | 每个账号在首次登录时随机生成一套独立设备标识并绑定，之后每次续期都复用它 |
| 登录态 | 密码登录、短信登录、导入桌面客户端登录态、到期前自动重登 |
| 运维 | `/health`、`/v1/points`、`/v1/admin/accounts`，请求日志记录模型 / tokens / 扣分 / 耗时 |
| 安全 | 可选给网关自己加 API Key；密码等敏感文件默认不进 git |

## 相对上游的增量（借鉴 workbuddy2api-panel 全面优化）

本分支在 [上游](https://github.com/Patrick130306/loomy2api) 基础上做了一轮加固，语义对齐
[workbuddy2api-panel](https://github.com/xingluoyuankong/workbuddy2api-panel) 的成熟实现：

| 能力 | 说明 |
|---|---|
| **三因子加权选号** | 新增 `weighted` 策略（现为默认）：余额占比 ×10 + 闲置补偿 ×1 + 成功率 ×3，取 Top-N 短名单后加权随机抽签。等权重候选先洗牌，避免字典序靠后的账号被饿死。旧的 `balance`（严格最大积分）保留为确定性选项 |
| **分级冷却** | 不再所有错误一律 `cooldown_seconds`：<br>· `429` → 软冷却，有上游重置墙钟则对齐，否则**有界指数退避**（`soft_rate_base_seconds` 起，封顶 `soft_rate_max_seconds`）；**冷却中重试不再叠加**（这是"越重试越冷、全池推到 2h 封顶"的根因）<br>· `402` → 硬冷却到**次日 04:00**（对齐每日额度重置），额度恢复自动解冻<br>· `404` → 固定短冷却（`not_found_cooldown_seconds`）<br>· `401/403` → 清 session 等重登<br>· `5xx` → **不罚账号**，只累计熔断器 |
| **模型级冷却** | `429` 带模型时只冷这个模型（`model_cooldowns`），换模型请求照样路由到该账号；该模型再次成功即自动清除负缓存 |
| **熔断** | 连续 `breaker_threshold` 次上游失败 → 指数加倍封禁（`breaker_cooldown_seconds` → `breaker_cooldown_max_seconds`）；账号下次成功自动复活 |
| **会话粘性** | `conversation_id → 账号` 绑定，TTL 滚动续期（`sticky_ttl_seconds`），请求失败自动解绑重分配。**客户端不发 `conversation_id` 时**（通用 OpenAI 客户端都不发），用 `system + 首条 user` 的哈希派生稳定键（`d-` 前缀），照样保住多轮上下文 |
| **在途租约** | 单账号并发占满则跳过（`max_inflight_per_account`，0=不限），避免把单号打爆 |
| **在线配置热生效** | 面板新增「在线配置」：深合并写回 `config.json`（保留未知键 + 原子替换），**保存即生效**；`host/port/upstream/account_base/log_dir/accounts_file` 会提示需重启 |
| **首启自举** | 无 `config.json` 时自动生成一份，含 `crypto/rand` 随机 API Key（**只打印一次**，不写日志、不进 git） |
| **微信扫码登录** | 逆向客户端 `wechat-oauth.js` + `account-service.js` + 解密 `.env.prod`，把客户端唯一的「跳转授权」链路（微信开放平台网站应用扫码 → `loomy://oauth/wechat` 回调）完整实现进面板：真二维码页 + code 换 session + 新微信绑手机即注册 |
| **安全加固** | API Key 比较改 **常量时间**（SHA-256 + `hmac.compare_digest`，消除计时侧信道）；面板响应加 CSP / `X-Content-Type-Options` / `X-Frame-Options` / `Referrer-Policy`（可关） |
| **可观测** | 每请求一行日志，新增 **TTFB** 与 **token/秒**；`/healthz` 带 `service` 标识与 `healthy/total`，可直接接负载均衡探活 |
| **面板增强** | 账号状态显示冷却类型（软/硬/熔断）与「N 个模型限流」；卡片显示会话粘性条目数 / 在途数；新增在线配置编辑器 |

> 测试从 115 个增加到 **186 个**（新增 `test_routing.py` / `test_cooldown.py` / `test_config_hot.py` / `test_hardening.py`），
> 全部离线跑在本地假上游上，不碰真实账号、不消耗积分。

## 环境要求

* Python **3.9+**（已在 Windows / Linux 的 3.9、3.11、3.13 上测过）
* 一个 Loomy 账号（手机号 + 在讯飞账号中心设过的密码）
* 能访问 `account.xfinfr.com` 与 `loomyad.xunfei.cn`

> **密码怎么设**：客户端里没有设密码的入口，需要去**讯飞账号中心**（网页或手机端）设置一次，
> 之后本项目的密码登录就能长期自动续期。

## 快速开始

```bash
git clone https://github.com/Patrick130306/loomy2api.git
cd loomy2api

cp config.example.json config.json        # 可选，默认值即可用
cp accounts.example.json accounts.json    # 把你的账号写进去

# 添加账号并登录（登录态会写回 accounts.json）
python -m loomy2api add main --phone 13800000000 --password '你的密码'
python -m loomy2api accounts              # 看登录态剩余天数和额度

python -m loomy2api serve                 # http://127.0.0.1:17890
```

接任意 OpenAI 兼容客户端：

```bash
curl http://127.0.0.1:17890/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "deepseek-v4-flash-0731",
       "messages": [{"role": "user", "content": "你好"}]}'
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:17890/v1", api_key="随便填")
print(client.chat.completions.create(
    model="deepseek-v4-flash-0731",            # 见 GET /v1/models
    messages=[{"role": "user", "content": "你好"}],
).choices[0].message.content)
```

Claude Code / 任意 Anthropic 客户端：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:17890
export ANTHROPIC_API_KEY=随便填
```

## 多账号池

`accounts.json`（已在 .gitignore 里）长这样：

```json
{
  "accounts": [
    { "name": "主号",   "loginid": "13800000000", "password": "…", "enabled": true },
    { "name": "备用",   "loginid": "13900000000", "password": "…", "enabled": true },
    { "name": "朋友共享", "session": "<32 位 session>", "userid": "…", "expireAt": 0 }
  ]
}
```

* **有密码的账号**会自己续期：后台线程按 `quota_refresh_minutes` 巡检，
  剩余天数低于 `session_renew_before_days`（默认 3 天）就自动重登——整池可以无人值守。
* **只有 session 的账号**也能用（适合别人给你的号），但 14 天到期后要重新
  `loomy2api login` 或短信登录。
* **客户端导入**：本机装有 Loomy 桌面端且已登录时，它的登录态会被自动当作一个额外账号
  （`sessions_from_client: true`）。

路由策略：

| 策略 | 行为 |
|---|---|
| `weighted`（默认） | 三因子加权随机（余额 ×10 + 闲置 ×1 + 成功率 ×3），Top-N 短名单内抽签，防惊群 |
| `balance` | 用可用积分最多的账号（确定性，单号/测试场景） |
| `round_robin` | 按顺序轮换 |
| `lru` | 优先用最久没用的 |

遇到 `401/403` 会丢弃该账号的登录态；遇到 `402` / 额度耗尽会把账号冷却
`cooldown_seconds` 秒，然后**自动换下一个账号重试**（`max_retries`）。
当前每个账号的状态在 `GET /v1/admin/accounts` 里一目了然。

## 网页面板

浏览器打开 <http://127.0.0.1:17890/panel>（直接开根路径也是这个页面）。

**左侧标签导航**（明暗双主题，右上角切换；浅色为纯白底）：

| 标签 | 内容 |
|---|---|
| **账号池** | 6 张状态卡（总数/可用/冷却/禁用/可用积分/累计请求）+ 账号表（状态徽标含「软冷却 / 硬冷却 / 熔断 / N 个模型限流」、出口代理、设备标识）+ **添加账号向导**（跳转登录 / 手动填写） |
| **用量** | 请求级台账：总量卡（请求/成功/失败/token 入出/已扣积分/平均耗时与 TTFB）+ 按模型 + 按账号 + 最近 120 条流水（可清空） |
| **积分构成** | 逐账号 长期余额 / 每日额度 / 可用合计 / 本次已扣 + 上游最近流水 |
| **任务中心** | 后台作业状态（登录态守护 / 额度刷新 / 粘性表清理：周期、上次运行、下次、运行次数与失败数、结果）+ 手动「立即执行」+ 账号池参数速查 |
| **模型与档位** | 模型目录按倍率排序（x0.1 最省 → x12 最贵），标出上下文长度、能力（推理/工具/视觉/音频）、默认模型 |
| **代理出口** | 全局代理 + 逐账号代理（可填 `http://` / `socks5://`），显示生效出口与来源；全局代理改完即热生效 |
| **配置** | 在线编辑 `config.json`，深合并 + 原子写 + 热生效，装配期字段会提示需重启 |
| **运行日志** | `logs/gateway.log` 尾巴，可自动刷新 |

> 面板是 `web/index.html` + `web/app.js` + `web/login.html` 三个静态资源，零 CDN、零构建。
> `tools/smoke_panel.cjs` 是前端渲染冒烟测试（假 DOM + 真实后端数据，逐个跑 8 个视图 + 添加向导）。

### 添加账号：三种方式

#### 1. 微信扫码（真·跳转微信官方授权页）

Loomy 客户端唯一的外部授权链路就是**微信开放平台「网站应用」扫码登录**。本分支把它
完整实现进了面板（逆向自客户端 `electron/xfyun/wechat-oauth.js` +
`electron/xfyun/account-service.js`，AppID 来自加密的 `resources/.env.prod`）：

```
点「添加账号」→ 微信扫码 →「打开微信授权页」
   → 新标签打开 open.weixin.qq.com/connect/qrconnect?appid=wx18d60be432287cf8
        &redirect_uri=https://loomy.xunfei.cn/oauth/wechat/callback
        &scope=snsapi_login&state=<我们签发的 state>
   → 用户微信扫码授权
   → 微信 302 到 loomy.xunfei.cn/oauth/wechat/callback?code=…&state=…
   → 把地址栏那条链接粘回面板
   → POST /api/panel/login/wechat/complete
        → POST /login/thirdAccount/bind/auth {tcode:{code}, type:"wx"} → {bind, rcode}
             bind=1（该微信已绑手机）→ POST /login/thirdAccount/bind/skip → session
             bind=0（新微信）        → bind/sendMsg + bind/checkCode 绑手机 → session
   → 落盘 + 热加载进池
```

**为什么必须手动粘一次链接**（这不是偷懒，是微信的限制）：

* 微信开放平台只校验 redirect_uri 的**域名**（登记的是 `loomy.xunfei.cn`），
  **不校验路径** —— 实测 `https://loomy.xunfei.cn/<任意路径>` 都能正常出二维码；
* 但 `https://loomy.xunfei.cn@127.0.0.1:17890/…` 这类 userinfo 绕过变体**一律被拒**
  （已实测），所以回调落不到本机；
* `https://loomy.xunfei.cn/oauth/wechat/callback` 线上是 **404**（官方客户端把这一页塞进
  Electron `BrowserWindow`，在 `will-redirect` 里拦截拿 code、并 `preventDefault()`
  不真的加载），所以 code 只出现在浏览器地址栏 → 只能由用户粘回来。

官方客户端能做到全自动，是因为它**自己就是个浏览器**；网关进程做不到，除非你在
`loomy.xunfei.cn` 域下有自己的页面。粘一次链接是这个约束下的最优解。

#### 2. 短信验证码（面板内直接做）

手机号 → 发验证码 → 输 6 位码 → 完成（登录即注册）。不跳转。

#### 3. 手动填写

手机号+密码（需先在讯飞账号中心设一次）/ 直接贴 session / 指定出口代理。

对应的接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/panel/login/wechat/start` | 生成微信扫码授权链接（返回 state + url + hint） |
| POST | `/api/panel/login/wechat/complete` | 粘回回调链接 → 换 session，或返回 `needs_phone` |
| POST | `/api/panel/login/wechat/bind/send` | 新微信绑手机：发验证码 |
| POST | `/api/panel/login/wechat/bind/submit` | 新微信绑手机：验码 → 完成注册 + 登录 |
| POST | `/api/panel/login/start` `/send` `/submit` | 短信链路（面板内） |
| GET | `/api/panel/login/poll?state=` | 会话状态：`{done,status,account,error}` |

如果配置了 `api_keys`，面板的接口就需要这个 Key（页面本身保持公开，方便你填 Key），
Key 存在浏览器 localStorage 里。

## 账号设备标识（指纹）

每个账号在**首次登录时**随机生成一套独立设备标识，并绑定写进 `accounts.json`：

```json
"identity": {
  "devid": "web-0ca44246df704952",
  "ua": "Loomy|Desktop|Electron|macOS",
  "modelid": "Web", "version": "1.0.0",
  "campus_device_id": "loomy-campus-0cbce23d-6ec2-4ee5-9591-f43408d23896",
  "created_at": 1790471588
}
```

之后每次续期、每次请求都用同一套，所以一个账号始终表现为同一台设备，
而不是所有账号都对外宣布 `devid: web`。

**说清楚它是什么、不是什么**：协议里真正涉及"设备"的字段只有四个
（`devid`、`ua`、`modelid`/`version`，加一个每请求随机的 `traceid`），
而校园推广用的设备号只出现在 `/points/activation` 和 `/points/first-login` 的请求体里，
**不会随对话请求发出**。绑独立标识能让账号在这些字段上互不雷同，
但它**不改变出口 IP**——而 IP 才是大多数风控真正看的东西。
所以请把它当"账号隔离"，不要当"防封保证"。

`config.json` 里的 `identity_mode`：

| 值 | 行为 |
|---|---|
| `per_account`（默认） | 每个账号独立 `devid`（`web-<16 位 hex>`）和校园设备号；`ua`/`modelid`/`version` 仍用真实客户端的值 |
| `client` | 完全照抄官方客户端（`devid: web`） |

重绑方式：面板上的"换标识"，或
`POST /api/panel/accounts/identity {"name": "...", "regenerate": true}` ——
它会生成一套新标识**并**用新标识重新登录一次。

## 模型清单

上游 `/models` 返回什么就暴露什么——模型名**原样使用**，没有别名表（只剥掉 `provider/` 前缀）。
一个实测出来的行为要注意：**上游自己对不认识的模型名会静默回落到 `deepseek-v4-flash-0731`**
（传 `gpt-4o-mini` 会返回 HTTP 200，但回包 `model` 是 `deepseek-v4-flash-0731`），
所以写错名字不会报错，要看回包里的 `model` 字段确认实际用了哪个。
典型清单（倍率就是扣分系数，`spark-x` 的 x0.1 最省）：

| id | 倍率 | 说明 |
|---|---|---|
| `spark-x` | x0.1 | Spark X2.5，文本 + 推理 |
| `GLM-5.3-Flash` | x0.8 | 视觉 / 视频 / 工具 |
| `qwen3.8-flash` | x0.8 | 视觉 / 视频 / 工具 |
| `deepseek-v4-flash-0731` | x3.0 | 1M 上下文，支持工具 |
| `mimo-v2.5` | x3.3 | 音频 / 图像 / 视频 |
| `MiniMax-M3` | x4.0 | 视觉 / 视频 |
| `Kimi-k2.6` | x6.5 | 视觉 / 视频 |
| `qwen-3.8-max` | x12.0 | 最强也最贵 |
| `Hy-Image-3.5-preview`、`doubao-seedream-5-lite`、`qwen-image-3.0-pro` | — | 生图 |

## 配置

`config.json` 所有键都可省略（默认值见 `loomy2api/config.py`，带注释的样例见
`config.example.json`）。环境变量优先级更高：

| 环境变量 | 作用 |
|---|---|
| `LOOMY_HOST` / `LOOMY_PORT` | 监听地址 |
| `LOOMY_UPSTREAM` | 模型网关地址 |
| `LOOMY_ACCOUNT_BASE` | 账号服务地址 |
| `LOOMY_AK_ID` / `LOOMY_AK_SECRET` | 覆盖内置的客户端签名密钥 |
| `LOOMY_API_KEYS` | 网关自己的 Key，逗号分隔（`[]` = 不鉴权） |
| `LOOMY_DEFAULT_MODEL` | 兜底模型 |
| `LOOMY_ACCOUNTS_FILE` / `LOOMY_LOG_DIR` | 状态文件位置 |
| `LOOMY_PROXY` | 如 `http://127.0.0.1:7877`（默认直连） |
| `LOOMY_STRATEGY` | `weighted` / `balance` / `round_robin` / `lru` |
| `LOOMY_PICK_TOP_N` | 加权选号短名单大小（默认 5） |
| `LOOMY_STICKY_TTL` | 会话粘性 TTL 秒（0 = 关闭） |
| `LOOMY_MAX_INFLIGHT` | 单账号在途上限（0 = 不限） |
| `LOOMY_SECURITY_HEADERS` | `0` 关闭面板 CSP 等安全响应头 |

### 给网关加把锁

```json
{ "api_keys": ["sk-local-whatever"] }
```

客户端带 `Authorization: Bearer sk-local-whatever` 或 `x-api-key: sk-local-whatever`。
`/health` 始终公开，方便探活。

## Docker

```bash
docker build -t loomy2api .
docker run -d --name loomy2api -p 17890:17890 -v $PWD/data:/data loomy2api
# 把 accounts.json 放进 ./data（容器内即 /data/accounts.json）
```

或者用 Compose：

```bash
mkdir -p data && cp accounts.example.json data/accounts.json
# 编辑 data/accounts.json 后：
docker compose up -d --build
docker compose logs -f
```

## 部署教程

### 0. 先给账号设个密码

桌面客户端里没有设密码的入口，要去**讯飞账号中心**（网页或手机端）设置一次，
之后本项目就能长期无人值守地自动续期。没有密码也可以走短信路线：

```bash
python -m loomy2api sms 13800000000           # 发送验证码
python -m loomy2api verify main 13800000000 <验证码> <msgid>
```

### 1. 安装（Windows / Linux / macOS 通用）

```bash
git clone https://github.com/Patrick130306/loomy2api.git
cd loomy2api

cp config.example.json config.json      # 可选，默认值就能跑
cp accounts.example.json accounts.json  # 你的账号写这里
chmod 600 accounts.json config.json     # 里面有明文密码，权限收紧

python -m loomy2api add main --phone 13800000000 --password '你的密码'
python -m loomy2api accounts            # 复核：额度 + 登录态剩余天数

python -m loomy2api serve               # http://127.0.0.1:17890
```

* Python **3.9+**，**零依赖**（纯标准库）。
* `--port 9000` 换端口；`-c /path/config.json` 指定别的配置文件。
* 面板：<http://127.0.0.1:17890/panel>

### 2. 常驻运行：Linux（systemd）

```ini
# /etc/systemd/system/loomy2api.service
[Unit]
Description=loomy2api gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=loomy
WorkingDirectory=/opt/loomy2api
ExecStart=/usr/bin/python3 -m loomy2api serve
Restart=always
RestartSec=5
UMask=0077                     # accounts.json 里有明文密码
Environment=LOOMY_HOST=127.0.0.1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now loomy2api
journalctl -u loomy2api -f          # 跟日志
```

### 3. 常驻运行：Windows

任务计划程序（系统自带，无需额外软件）：

```powershell
# start.cmd
@echo off
cd /d D:\loomy2api
python -m loomy2api serve >> logs\console.log 2>&1

schtasks /create /tn loomy2api /sc onstart /rl highest /tr "D:\loomy2api\start.cmd" /f
schtasks /run /tn loomy2api
```

或者用 NSSM 注册成真正的 Windows 服务：

```powershell
nssm install loomy2api "C:\Python312\python.exe" "-m loomy2api serve"
nssm set loomy2api AppDirectory D:\loomy2api
nssm set loomy2api AppStdout D:\loomy2api\logs\service.log
nssm start loomy2api
```

### 4. Docker / Compose

```bash
mkdir -p data && cp accounts.example.json data/accounts.json   # 然后编辑它
docker compose up -d --build
docker compose logs -f
```

镜像就是 `python:3.12-slim` + 源码，没有别的要装。状态（`accounts.json`、`logs/`）
都在 `./data`，升级就是 `git pull && docker compose up -d --build`。

### 5. 对外提供服务（可选，务必读完）

默认只监听 `127.0.0.1`，只有本机能访问。要在局域网/公网共享：

1. **先设 API Key**。不设的话，谁能连上这个端口就能花你账号的积分：

   ```json
   { "api_keys": ["sk-换成一串又长又随机的字符串"] }
   ```
2. 再设 `LOOMY_HOST=0.0.0.0`（或 config.json 里 `"host": "0.0.0.0"`）。
3. 公网的话，前面挂一个带 TLS 的反代。Caddy 示例：

   ```
   api.example.com {
       reverse_proxy 127.0.0.1:17890
   }
   ```

之后客户端就用 `https://api.example.com/v1` 作为 base URL。

> 更推荐用 VPN / Tailscale / WireGuard，而不是直接暴露公网。这个端点自身没有限流，
> 而且上游账号是你自己的。

### 6. 把客户端接上来

| 客户端 | 填法 |
|---|---|
| OpenAI SDK / LangChain | `base_url="http://127.0.0.1:17890/v1"`，`api_key` 随便填（或填你设的 Key） |
| Cherry Studio / LobeChat / NextChat / Open WebUI | 选「OpenAI 兼容」，base URL 填上面那个 |
| 沉浸式翻译 / 双语阅读类 | 自定义 OpenAI 接口，同一 base URL |
| Claude Code | `ANTHROPIC_BASE_URL=http://127.0.0.1:17890`、`ANTHROPIC_API_KEY=<随便>` |
| curl | 见上面「快速开始」 |

模型名直接用上游 id（见 `/v1/models`，比如 `deepseek-v4-flash-0731`、`spark-x`、`Kimi-k2.6`）。
网关**不做任何映射**，名字原样转发（只剥掉 `provider/` 前缀）；注意上游自己对不认识的模型名会
静默回落到默认模型，所以怀疑没生效时看回包的 `model` 字段。

### 7. 升级与备份

```bash
git pull && sudo systemctl restart loomy2api     # 或 docker compose up -d --build
```

要备份 `accounts.json`——账号、登录态、（如果你填了）密码、以及每个账号绑定的设备标识
都在里面。`config.json` 是你的配置。两个文件都已在 .gitignore 里。

### 8. 健康检查与日志

```bash
curl -s http://127.0.0.1:17890/health | python -m json.tool   # 状态 + 面板地址
python -m loomy2api accounts                                  # 各账号额度
tail -f logs/gateway.log                                      # 每次调用的模型/tokens/扣分
```

面板（<http://127.0.0.1:17890/panel>）能实时看到同样的信息，还带续期 / 换标识 / 禁用 / 删除按钮。

## 命令行

```
loomy2api serve                 启动网关
loomy2api accounts              账号池状态：额度、剩余天数、冷却
loomy2api add <名字> --phone … --password …
loomy2api remove <名字>
loomy2api login [名字…]          登录 / 强制续期
loomy2api sms <手机号>           发短信验证码（短信登录第一步）
loomy2api verify <名字> <手机号> <验证码> <msgid>
loomy2api identity <名字> [--rebind]   查看 / 重绑设备标识
loomy2api models                列出上游模型
loomy2api quota                 各账号积分
loomy2api chat "问题"            走账号池发一次请求自检
```

## 原理

Loomy 客户端是 Electron 应用，它的**主进程源码是明文**的
（`resources/app.asar.unpacked/electron/`，773 个 JS 文件），而 provider 配置里写了
`useSessionAuth: true` —— 也就是不存 apiKey，直接把登录态当 Bearer 用。
讯飞账号服务是一套标准 HTTP + HMAC-SHA1 签名的接口，而且密码登录天生可脚本化：
`getPuKey` 随 RSA 公钥一起下发的 `rcode` 是服务端 nonce，**不是人机验证码**。

所以本项目做的就是：在账号服务上登录 → 持有 14 天登录态 → 用它把 OpenAI / Anthropic
请求转发到模型网关。

完整逆向记录（端点、签名串、错误指纹、积分账本、客户端配置加密）见
**[docs/PROTOCOL.md](docs/PROTOCOL.md)**。

## 测试

```bash
python -m unittest discover -s tests -t . -v
```

全部离线：本地假上游同时扮演账号服务和模型网关，跑测试不会碰真实账号、不消耗积分。
CI 在 Linux 和 Windows 上跑 Python 3.9 / 3.11 / 3.13。

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `账号池里没有可用账号` | `accounts.json` 为空、账号被禁用，或全部处于冷却 |
| `... session 不可用且没有账号密码` | 补 `loginid` + `password`，或走 `loomy2api sms` + `loomy2api verify` |
| `getPuKey ... HMAC signature does not match` | `access_key_secret` 抄错/被截断——留空即可用内置的客户端常量 |
| 上游一直挂到超时 | 有人把 `traceparent` 头去掉了 |
| `402` / 积分耗尽 | 充值，或往池子里再加一个账号 |
| Windows 上两个实例抢同一端口 | Windows 允许重复 `SO_REUSEADDR` 绑定——`netstat -ano \| findstr 17890` 找出残留 PID 杀掉 |

## 注意事项

* 每次调用都扣账号积分（长期余额 + 每日免费额度），和官方客户端完全一样，
  用 `GET /v1/points` 盯着。
* 上游账号体系是真的：别猛怼登录接口，也不要做激进的重试脚本。
* 请用你自己的账号。把同一个号共享给很多人用，会明显提高被限流甚至封号的概率。

## 免责声明

本项目与科大讯飞无任何从属关系，仅为了与自己账号的互操作性、供个人使用。
`loomy2api/constants.py` 里的签名常量是官方客户端每个安装包里都会带的客户端常量，
只用于和账号服务通信。请勿用它滥用、转售或压垮上游服务。

## 协议

[MIT](LICENSE)
