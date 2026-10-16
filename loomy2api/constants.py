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
DEFAULT_LOOMY_VERSION = "0.9.38"

#: Passphrase used by the client to obfuscate ``resources/.env.prod``
#: (hard-coded in ``electron/utils/env-file-crypto.js`` of the client).
ENV_CRYPT_MAGIC = "LOOMYENC1"
ENV_CRYPT_PASSPHRASE = "loomy::env::3f9c1d2a7b6e4f08::local-obfuscation::v1"

#: Where the client keeps its data on Windows (``<root>/<sha256(user)[:12]>``).
CLIENT_PUBLIC_ROOT = r"C:\Users\Public\Loomy"

# ---------------------------------------------------------------- account

#: The client always asks for a 14-day session; there is no refresh token.
SESSION_EXPIRE_SECONDS = 14 * 24 * 3600

#: ``SESSION_EXPIRE_SECONDS`` 过期后的宽限期（秒）——**只做本地兜底，不替代上游鉴权**。
#:
#: 背景（2026-10-16 实测，根因）：登录响应里没有可解析的过期字段，
#: ``account._extract_session`` 只取 ``session/userid/phone``。于是
#: ``expire_at`` 是本地按 ``SESSION_EXPIRE_SECONDS`` 编出来的，不是上游给的。
#: 而客户端声称的 14 天**严重偏短**：当时 loomy-3147 本地「已过期 0.28 天」，
#: 上游 ``GET /points/records`` 仍回 HTTP 200（available=3811）。
#:
#: 后果：``Account.session_valid`` 到期即 False → ``pool.usable()`` 把它滤掉 →
#: 流量全挤到唯一剩下的号 → 那个号一遇上游 502/504 抖动就直接 502 甩给客户端
#: （日志里 `账号池里没有可用账号（session 均不可用）` 的来源），
#: 并且每个请求都刷两行「请尽快短信重登」，把真正的故障日志淹没。
#:
#: 修正：本地 TTL 过期不再判死，宽限期内继续参与选号；
#: 凭证真伪交给上游 ``401/403`` 终裁（``pool.report_failure`` /
#: ``pool.refresh_quota_light`` 已经实现了清 session + 冷却）。
#: 可用 ``session_grace_days`` 配置覆盖。
SESSION_GRACE_SECONDS = 30 * 24 * 3600

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
FALLBACK_MODELS = (
    {"id": "spark-x", "name": "Spark X2.5 (x0.1)", "multiplier": 0.1,
     "context_length": 1048576, "modalities": ["text"]},
    {"id": "GLM-5.3-Flash", "name": "GLM 5.3 Flash (x0.8)", "multiplier": 0.8,
     "context_length": 1048576, "modalities": ["text", "image", "video"]},
    {"id": "qwen3.8-flash", "name": "qwen 3.8 flash (x0.8)", "multiplier": 0.8,
     "context_length": 1000000, "modalities": ["text", "image", "video"]},
    {"id": "deepseek-v4-flash-0731", "name": "DeepSeek V4 Flash 0731 (x3.0)",
     "multiplier": 3.0, "context_length": 1048576, "modalities": ["text"]},
    {"id": "mimo-v2.5", "name": "MiMo V2.5 (x3.3)", "multiplier": 3.3,
     "context_length": 1048576, "modalities": ["text", "image", "audio", "video"]},
    {"id": "MiniMax-M3", "name": "MiniMax M3 (x4.0)", "multiplier": 4.0,
     "context_length": 1048576, "modalities": ["text", "image", "video"]},
    {"id": "Kimi-k2.6", "name": "Kimi k2.6 (x6.5)", "multiplier": 6.5,
     "context_length": 262144, "modalities": ["text", "image", "video"]},
    {"id": "qwen-3.8-max", "name": "Qwen 3.8 Max (x12.0)", "multiplier": 12.0,
     "context_length": 1000000, "modalities": ["text"]},
    {"id": "Hy-Image-3.5-preview", "name": "Hy image 3.5 preview",
     "multiplier": 0.0, "context_length": 100000, "modalities": ["text", "image"]},
    {"id": "doubao-seedream-5-lite", "name": "Doubao seedream 5 lite",
     "multiplier": 0.0, "context_length": 128000, "modalities": ["text", "image"]},
    {"id": "qwen-image-3.0-pro", "name": "qwen image 3.0 pro",
     "multiplier": 0.0, "context_length": 0, "modalities": ["text", "image"]},
)

DEFAULT_MODEL = "deepseek-v4-flash-0731"

#: Minimum AES-GCM tag / iv / salt sizes from the client's crypto module.
SALT_LEN, IV_LEN, TAG_LEN, KEY_LEN = 16, 12, 16, 32
