"""Default endpoints and client constants.

Everything here is *configurable* at runtime (config.json / environment
variables); these are only defaults.

The account access key pair below is the one the official Loomy client itself
ships inside ``resources/.env.prod`` (a client-side constant that is present in
every installation, obfuscated with a passphrase that is hard-coded next to it
in ``electron/utils/env-file-crypto.js``).  It is used to sign requests to the
iFlytek *account* service, which is what turns "log in with a phone number"
into a plain HTTP call.  Override it with ``access_key_id`` /
``access_key_secret`` in config.json or with the ``LOOMY_AK_ID`` /
``LOOMY_AK_SECRET`` environment variables if your client ships different ones.
"""

from __future__ import annotations

# ---------------------------------------------------------------- upstreams

#: Model gateway (OpenAI-compatible: /chat/completions, /models, /embeddings, ...)
DEFAULT_MODEL_BASE = "https://loomyad.xunfei.cn/api/v1"

#: iFlytek account service (login / userinfo)
DEFAULT_ACCOUNT_BASE = "https://account.xfinfr.com"

DEFAULT_ACCOUNT_APPID = "GM3LOOMY"

#: Client-shipped signing constants for the account service.
DEFAULT_ACCESS_KEY_ID = "2thryby66wxi53sk"
DEFAULT_ACCESS_KEY_SECRET = "zsak6eadrbawz683wf5r3m2snrwj868r"

#: Sent as the ``loomy-version`` header; the gateway only checks it is present.
#: 2026-11-14 实测：官方 CDN 已有 0.9.39（Loomy-0.9.39-win.zip / Loomy-Setup-0.9.39.exe
#: 均 200），0.9.40 还是 404，最新就是 0.9.39。
DEFAULT_LOOMY_VERSION = "0.9.39"

#: Passphrase used by the client to obfuscate ``resources/.env.prod``
#: (hard-coded in ``electron/utils/env-file-crypto.js`` of the client).
ENV_CRYPT_MAGIC = "LOOMYENC1"
ENV_CRYPT_PASSPHRASE = "loomy::env::3f9c1d2a7b6e4f08::local-obfuscation::v1"

#: Where the client keeps its data on Windows (``<root>/<sha256(user)[:12]>``).
CLIENT_PUBLIC_ROOT = r"C:\Users\Public\Loomy"

# ---------------------------------------------------------------- account

#: The client always asks for a 14-day session; there is no refresh token.
#:
#: 注意（2026-10-16 实测）：这只是客户端**声称**的时长，登录响应里没有可解析
#: 的过期字段，``expire_at`` 全是本地按这个值编的。实测 loomy-3147「过期」
#: 0.28 天后上游仍回 200 —— 所以它只用于展示/维护播报（account.session_stale），
#: **绝不参与路由判定**；凭证死活唯一权威是上游 401/403。
SESSION_EXPIRE_SECONDS = 14 * 24 * 3600

DEVICE_ID = "web"
WEB_MODEL_ID = "Web"
CLIENT_VERSION = "1.0.0"
CLIENT_UA = "Loomy|Desktop|Electron|macOS"

#: Prefix of the promotions device id the client derives from the machine
#: fingerprint (``loomy-campus-fp-<sha256(machineId)>``).  It is sent only with
#: ``/points/activation`` and ``/points/first-login`` bodies — never with chat
#: requests — but it is the one field that is device-scoped by design.
CAMPUS_DEVICE_ID_PREFIX = "loomy-campus-"

# ---------------------------------------------------------------- wechat oauth
#
# 来源：客户端安装包 resources/.env.prod（LOOMYENC1 AES-256-GCM 加密，
# 口令硬编码在 electron/utils/env-file-crypto.js —— 官方注释自己承认这是
# 「混淆而非密钥保密」）。
#
#   LOOMY_WECHAT_APP_ID=wx18d60be432287cf8   （微信开放平台「网站应用」，科迅创想主体）
#
# 回调域在微信开放平台侧登记为 loomy.xunfei.cn，**微信只校验域名、不校验路径**
# （实测 https://loomy.xunfei.cn/<任意路径> 都能正常出二维码；userinfo@ 之类的
# 绕过变体一律被拒）。所以回调只能落在 loomy.xunfei.cn，code 需要从浏览器地址栏取回。
WECHAT_APP_ID = "wx18d60be432287cf8"
WECHAT_REDIRECT_URI = "https://loomy.xunfei.cn/oauth/wechat/callback"
WECHAT_AUTH_BASE = "https://open.weixin.qq.com/connect/qrconnect"
WECHAT_SCOPE = "snsapi_login"
#: 第三方登录类型（讯飞侧）：wx = 微信
WECHAT_THIRD_TYPE = "wx"

#: ``X-Loomy-Request-Purpose`` values understood by the upstream (17 total).
REQUEST_PURPOSES = (
    "chat.message", "chat.title", "pet.comment", "pet.reward",
    "kb.vision", "kb.audio", "kb.summary", "kb.embed",
    "search.web", "image.generate", "assistant.completion", "assistant.asr-correct",
    "memory.extract", "memory.recall", "memory.dream",
    "skill.evolution", "skill.curator",
)

# ---------------------------------------------------------------- models

#: Fallback catalogue, used when the upstream /models call fails.
#: ``multiplier`` is the points-per-request cost factor reported by the client.
#: 2026-11-14 上游实测快照（10 个）：0731 / MiniMax-M3 / Kimi-k2.6 / qwen-3.8-max /
#: spark-x 均已下线；新增 DeepSeek-V4.1-Flash、1017、spark-x2.6、auto。
#: 注意 1017 的 id：上游 API 返回的是省略了 ".1" 的 ``deepseek-v4-flash-1017``，
#: 但 DeepSeek 官方发布的模型就是 DeepSeek-V4.1-Flash-1017（2026-10-17 快照，
#: V4.1 Flash 的小更新，552B，1M 上下文；V4 那条线的快照是 0731，284B）。
#: 上游自己的名字 "DeepSeek V4.1 Flash 1017" 是对的。网关对外用官方 id
#: ``deepseek-v4.1-flash-1017``（见 catalogue() 的翻译），请求时再翻回上游 id。
FALLBACK_MODELS = (
    {"id": "spark-x2.6", "name": "Spark-X2.6 正式版(限时免费)(x0.0)", "multiplier": 0.0,
     "context_length": 1048576, "modalities": ["text"]},
    {"id": "DeepSeek-V4.1-Flash", "name": "DeepSeek V4.1 Flash(x2.0)", "multiplier": 2.0,
     "context_length": 1048576, "modalities": ["text", "image"]},
    {"id": "deepseek-v4.1-flash-1017", "name": "DeepSeek V4.1 Flash 1017(x2.0)",
     "multiplier": 2.0, "context_length": 1048576, "modalities": ["text", "image"]},
    {"id": "GLM-5.3-Flash", "name": "GLM 5.3 Flash(x0.8)", "multiplier": 0.8,
     "context_length": 1048576, "modalities": ["text", "image", "video"]},
    {"id": "qwen3.8-flash", "name": "qwen 3.8 flash（x0.8）", "multiplier": 0.8,
     "context_length": 1000000, "modalities": ["text", "image", "video"]},
    {"id": "Hy-Image-3.5-preview", "name": "Hy image 3.5 preview",
     "multiplier": 0.0, "context_length": 100000, "modalities": ["text", "image"]},
    {"id": "doubao-seedream-5-lite", "name": "Doubao seedream 5 lite",
     "multiplier": 0.0, "context_length": 128000, "modalities": ["text", "image"]},
    {"id": "qwen-image-3.0-pro", "name": "qwen image 3.0 pro",
     "multiplier": 0.0, "context_length": 0, "modalities": ["text", "image"]},
    {"id": "mimo-v2.5", "name": "MiMo V2.5（x3.3）", "multiplier": 3.3,
     "context_length": 1024000, "modalities": ["text", "image", "audio", "video"]},
    {"id": "auto", "name": "自动 · 均衡", "multiplier": None,
     "context_length": 0, "modalities": ["text"]},
)

#: Display corrections applied to the live upstream catalogue in
#: :meth:`Gateway.catalogue` (the single outward path for /v1/models and the
#: panel). Only fields the upstream itself gets wrong:
#:
#: - ``spark-x2.6``: upstream name ends "(x0.0)" (free for now) but starts with
#:   "Spark-X2.6", so a first-match multiplier parse reads 2.6. The multiplier
#:   is pinned to 0.0 here as well as fixed at parse time in refresh_models.
#: （1017 不在这里：它的名字上游写对了，错的是 id 省略了 ".1"——那个翻译在
#: catalogue() 里做，和 DeepSeek-V4.1-Flash 的小写翻译同一处。）
MODEL_FIELD_OVERRIDES = {
    "spark-x2.6": {"multiplier": 0.0},
}

DEFAULT_MODEL = "deepseek-v4.1-flash-1017"

#: Minimum AES-GCM tag / iv / salt sizes from the client's crypto module.
SALT_LEN, IV_LEN, TAG_LEN, KEY_LEN = 16, 12, 16, 32
