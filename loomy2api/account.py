"""Account model + iFlytek account-service client.

One :class:`Account` is one Loomy/iFlytek account: phone number, password,
and the 14-day session that the account service hands out.  The session is
exactly what the model gateway wants as a Bearer token, so "logging in
server-side" is all that stands between us and a usable API key.

Login paths (both fully server-side, no desktop client involved):

* password — ``/login/account/getPuKey`` returns a 1024-bit RSA public key
  **and** an ``rcode``; the password is RSA/PKCS#1 v1.5 encrypted and posted to
  ``/login/account/byPwd``.  No captcha is involved: ``rcode`` is a server
  nonce, so this is fully automatable.
* SMS — ``/login/phone/sendMsgCode`` → ``msgid``, then
  ``/login/phone/checkCode``.  Needs a human to read the code once.

Each account carries a per-account **identity** (see :func:`new_identity`): the
only device-ish fields the protocol actually carries are ``devid``, ``ua``,
``modelid``/``version`` in the request envelope plus a per-request random
``traceid``.  Binding a distinct identity per account keeps accounts visually
separate instead of all announcing ``devid=web``; it does **not** change the
network origin, which is what most risk control looks at.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from . import constants as C
from .crypto import rsa_encrypt
from .httpc import request as http_request
from .signer import build_headers

__all__ = ["Account", "AccountClient", "AccountError", "new_identity",
           "IDENTITY_FIELDS"]


class AccountError(RuntimeError):
    """Raised for account-service business errors."""

    def __init__(self, message: str, code: str = "", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


# --------------------------------------------------------------------- identity

#: Fields that make up an account identity, i.e. the values a request envelope
#: announces about "which device is this".
IDENTITY_FIELDS = ("devid", "ua", "modelid", "version", "campus_device_id")


def new_identity(mode: str = "per_account", *, rng=None) -> Dict[str, Any]:
    """Build a device identity for one account.

    风控基线（逐字段对照客户端 ``electron/xfyun/account-service.js``）::

        modelid: 'Web'                          常量
        version: '1.0.0'                        常量（与客户端一致，勿改成 app 版本）
        devid:   'web'                          **固定常量**，所有官方客户端都发 'web'
        ua:      'Loomy|Desktop|Electron|macOS'  常量（Windows 版也发这个）
        traceid: 每请求新生成（32hex，不属于 identity）

    所以 devid **不能**自创格式（web-<hex> 之类在官方全流量里不存在，
    是最显眼的异常信号）；设备区分由 campus_device_id（promotions 侧，
    客户端每台机器一个，keyring 存储）承担，保持 per-account。
    """
    ident: Dict[str, Any] = {
        "devid": "web",
        "ua": C.CLIENT_UA,
        "modelid": C.WEB_MODEL_ID,
        "version": C.CLIENT_VERSION,
        "campus_device_id": f"{C.CAMPUS_DEVICE_ID_PREFIX}{uuid.uuid4()}",
        "created_at": int(time.time()),
    }
    return ident


@dataclass
class Account:
    """A single account plus its cached session/ quota state."""

    name: str
    loginid: str = ""            # phone number used to log in
    password: str = ""
    enabled: bool = True
    persist: bool = True         # False for accounts derived from the client
    #: 账号级出口代理（http://host:port 或 socks5://host:port）；空 = 用全局 proxy
    proxy: str = ""

    session: str = ""
    userid: str = ""
    expire_at: int = 0           # unix seconds
    obtained_at: int = 0

    #: per-account device identity (devid / ua / campus device id / …)
    identity: Dict[str, Any] = field(default_factory=dict)

    # quota cache (filled from /points/records)
    balance: Optional[int] = None
    daily_balance: Optional[int] = None
    available: Optional[int] = None
    multiplier: float = 0.0      # not used for routing, informational
    quota_updated_at: int = 0

    # runtime state (never persisted)
    cooldown_until: float = 0.0
    #: "" | "soft"（限流类，有界退避）| "hard"（额度耗尽，等到恢复墙钟）
    cool_kind: str = ""
    #: 连续软冷却次数（只有"进入一次新冷却"才 +1，冷却中重试不叠加）
    soft_streak: int = 0
    #: 熔断截止时间（连续 5xx 类失败逐次加倍封禁，与 cooldown_until 正交）
    breaker_until: float = 0.0
    breaker_fails: int = 0
    breaker_retries: int = 0
    #: 模型级冷却表：model -> {"until": float, "reason": str, "hits": int}
    model_cooldowns: Dict[str, Dict] = field(default_factory=dict)
    last_used: float = 0.0
    requests: int = 0
    points_used: int = 0
    daily_consumed: int = 0      # 上游 dailyConsumed：当日已扣（用户对账口径）
    failures: int = 0
    last_error: str = ""
    source: str = "config"       # config | client

    # ------------------------------------------------------------------
    @property
    def session_valid(self) -> bool:
        return bool(self.session) and (self.expire_at == 0 or self.expire_at > time.time())

    @property
    def days_left(self) -> Optional[float]:
        if not self.expire_at:
            return None
        return (self.expire_at - time.time()) / 86400

    @property
    def in_breaker(self) -> bool:
        return self.breaker_until > time.time()

    @property
    def in_cooldown(self) -> bool:
        """冷却中（含熔断）。调用方大多只关心"现在能不能用"。"""
        return self.cooldown_until > time.time() or self.in_breaker

    def healthy(self, now: Optional[float] = None) -> bool:
        """能否参与选号（不看额度，额度由 pool.usable 单独判）。"""
        now = time.time() if now is None else now
        return (self.enabled and self.session_valid
                and self.cooldown_until <= now and self.breaker_until <= now)

    def model_cooled(self, model: str, now: Optional[float] = None) -> bool:
        if not model:
            return False
        now = time.time() if now is None else now
        item = self.model_cooldowns.get(model)
        return bool(item and float(item.get("until") or 0) > now)

    def healthy_for_model(self, model: str, now: Optional[float] = None) -> bool:
        """模型感知的健康判定：被该模型限流的账号换模型仍可用。"""
        return self.healthy(now) and not self.model_cooled(model, now)

    def prune_model_cooldowns(self, now: Optional[float] = None) -> None:
        """惰性清理过期模型级冷却，防止 map 无限膨胀。"""
        if not self.model_cooldowns:
            return
        now = time.time() if now is None else now
        for key in [k for k, v in self.model_cooldowns.items()
                    if float(v.get("until") or 0) <= now]:
            self.model_cooldowns.pop(key, None)

    # -- 冷却入口 --------------------------------------------------------
    def cool(self, seconds: float, kind: str = "soft", reason: str = "") -> None:
        """冷却到 now+seconds（固定时长，不叠加）。"""
        self.cooldown_until = time.time() + max(1.0, float(seconds))
        self.cool_kind = kind
        if reason:
            self.last_error = reason

    def cool_until(self, until_ts: float, kind: str = "soft", reason: str = "") -> None:
        """冷却到指定墙钟（上游给了权威重置时间时用它）。"""
        self.cooldown_until = max(time.time() + 0.001, float(until_ts))
        self.cool_kind = kind
        if reason:
            self.last_error = reason

    def cool_soft(self, base_seconds: float, max_seconds: float,
                  reason: str = "", reset_at: Optional[float] = None) -> float:
        """429/限流类软冷却。返回本次冷却截止时间。

        · 上游给了重置墙钟 → 对齐到该墙钟（截断到 max），**不做指数放大**；
        · 没给 → 有界指数退避 base × 2^(streak-1)，封顶 max；
        · 已在软冷却中再次命中 → 不推进 streak、不延长（防止"越重试越冷"）。
        """
        now = time.time()
        if reset_at:
            until = min(float(reset_at), now + max(1.0, float(max_seconds)))
            if until <= now:
                until = now + 0.001
            self.cool_until(until, "soft", reason)
            return until
        if self.cool_kind == "soft" and now < self.cooldown_until:
            return self.cooldown_until           # 冷却中的兜底探测：保持原截止
        self.soft_streak += 1
        shift = min(self.soft_streak - 1, 6)     # 移位上限，防溢出
        duration = float(base_seconds) * (2 ** shift)
        duration = min(max(1.0, duration), max(1.0, float(max_seconds)))
        self.cool(duration, "soft", reason)
        return self.cooldown_until

    def cool_hard(self, until_ts: float, reason: str = "") -> None:
        """硬冷却（额度耗尽）：等到恢复墙钟。"""
        self.cool_until(until_ts, "hard", reason)

    def note_error(self, threshold: int = 5, base: float = 60.0,
                   cap: float = 1800.0) -> bool:
        """记一次"账号自身"的失败，达到阈值则熔断（逐次加倍，封顶 cap）。"""
        self.breaker_fails += 1
        if self.breaker_fails < max(1, int(threshold)):
            return False
        duration = float(base) * (2 ** min(self.breaker_retries, 6))
        duration = min(duration, max(1.0, float(cap)))
        self.breaker_fails = 0
        self.breaker_retries += 1
        self.breaker_until = time.time() + duration
        return True

    def note_success(self) -> None:
        """成功一次：清熔断 + 清软冷却退避（账号确实恢复了）。

        与 workbuddy2api-panel 的 NoteSuccess 同口径：熔断的解除有两条路——
        到期自然过期，或者这个账号真的又成功了一次。
        """
        self.breaker_fails = 0
        self.breaker_retries = 0
        self.breaker_until = 0.0
        self.soft_streak = 0
        if self.cool_kind == "soft":
            self.cooldown_until = 0.0
            self.cool_kind = ""

    #: 兼容旧调用（原 `cooldown(seconds, reason)`）
    def cooldown(self, seconds: float, reason: str = "") -> None:
        self.cool(seconds, "soft", reason)

    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, raw: Dict[str, Any], index: int = 0) -> "Account":
        name = str(raw.get("name") or raw.get("loginid") or f"account{index + 1}")
        identity = raw.get("identity")
        acc = cls(
            name=name,
            loginid=str(raw.get("loginid") or raw.get("phone") or ""),
            password=str(raw.get("password") or ""),
            enabled=raw.get("enabled", True) is not False,
            proxy=str(raw.get("proxy") or ""),
            session=str(raw.get("session") or ""),
            userid=str(raw.get("userid") or ""),
            expire_at=int(raw.get("expireAt") or raw.get("expire_at") or 0),
            obtained_at=int(raw.get("obtainedAt") or raw.get("obtained_at") or 0),
            identity=dict(identity) if isinstance(identity, dict) else {},
        )
        # 运行计数恢复（老文件没有这些键 -> 默认 0）
        acc.requests = int(raw.get("requests") or 0)
        acc.points_used = int(raw.get("pointsUsed") or 0)
        acc.failures = int(raw.get("failures") or 0)
        acc.daily_consumed = int(raw.get("dailyConsumed") or 0)
        return acc

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "enabled": self.enabled,
        }
        if self.loginid:
            out["loginid"] = self.loginid
        if self.password:
            out["password"] = self.password
        if self.proxy:
            out["proxy"] = self.proxy
        if self.session:
            out["session"] = self.session
        if self.userid:
            out["userid"] = self.userid
        if self.expire_at:
            out["expireAt"] = self.expire_at
        if self.obtained_at:
            out["obtainedAt"] = self.obtained_at
        # 运行计数也落盘：重启不该把「已扣积分/请求数」清零（用户对账要用）
        out["requests"] = int(self.requests)
        out["pointsUsed"] = int(self.points_used)
        out["failures"] = int(self.failures)
        out["dailyConsumed"] = int(self.daily_consumed)
        if self.identity:
            out["identity"] = self.identity
        return out

    def identity_view(self) -> Dict[str, Any]:
        """Identity as shown in the panel / admin API (nothing secret in it)."""
        ident = self.identity or {}
        return {
            "devid": ident.get("devid", ""),
            "ua": ident.get("ua", ""),
            "modelid": ident.get("modelid", ""),
            "version": ident.get("version", ""),
            "campus_device_id": ((ident.get("campus_device_id") or "")[:32] + "…"
                                 if len(ident.get("campus_device_id") or "") > 32
                                 else ident.get("campus_device_id", "")),
            "created_at": ident.get("created_at", 0),
            "bound": bool(ident),
        }

    def public_dict(self) -> Dict[str, Any]:
        """Status view without secrets (used by /admin/accounts)."""
        out = {
            "name": self.name,
            "loginid": self.loginid,
            "userid": self.userid,
            "enabled": self.enabled,
            "has_password": bool(self.password),
            "proxy": self.proxy or "",
            "session": (self.session[:8] + "…") if self.session else "",
            "session_days_left": (round(self.days_left, 2) if self.days_left is not None else None),
            "expire_at": self.expire_at,
            "balance": self.balance,
            "daily_balance": self.daily_balance,
            "available": self.available,
            "quota_updated_at": self.quota_updated_at,
            "requests": self.requests,
            "points_used": self.points_used,
            "daily_consumed": self.daily_consumed,
            "failures": self.failures,
            "in_cooldown": self.in_cooldown,
            "cooldown_seconds_left": (round(self.cooldown_until - time.time(), 1)
                                      if self.in_cooldown else 0),
            "cooldown_kind": self.cool_kind,
            "soft_streak": self.soft_streak,
            "in_breaker": self.in_breaker,
            "breaker_seconds_left": (round(self.breaker_until - time.time(), 1)
                                     if self.in_breaker else 0),
            "breaker_fails": self.breaker_fails,
            "model_cooldowns": {
                k: {"seconds_left": round(float(v.get("until") or 0) - time.time(), 1),
                    "reason": v.get("reason", "")}
                for k, v in self.model_cooldowns.items()
                if float(v.get("until") or 0) > time.time()
            },
            "source": self.source,
            "last_error": self.last_error,
            "identity": self.identity_view(),
        }
        return out


class AccountClient:
    """Talks to ``account_base`` (login + userinfo) on behalf of an account."""

    def __init__(self, cfg):
        self.cfg = cfg

    # -- plumbing -------------------------------------------------------

    def identity_mode(self) -> str:
        return str(self.cfg.get("identity_mode") or "per_account")

    def _base(self, identity: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        ident = dict(identity or {}) or new_identity(self.identity_mode())
        return {
            "appid": self.cfg.get("account_appid") or C.DEFAULT_ACCOUNT_APPID,
            "modelid": str(ident.get("modelid") or C.WEB_MODEL_ID),
            "version": str(ident.get("version") or C.CLIENT_VERSION),
            # 风控：客户端（全部平台）的 devid 是固定常量 'web'
            # （electron/xfyun/account-service.js:22 WEB_DEVICE_ID='web'）。
            # 任何自创格式（如 web-<hex>）在全流量里都是异类。
            "devid": "web",
            "ua": str(ident.get("ua") or C.CLIENT_UA),
            # the real client regenerates traceid on every single request
            "traceid": uuid.uuid4().hex,
        }

    def call(self, path: str, body: Optional[dict] = None,
             *, identity: Optional[Dict[str, Any]] = None,
             timeout: float = 30, proxy: Optional[str] = None) -> Dict[str, Any]:
        body_str = json.dumps(body, ensure_ascii=False) if body else ""
        headers = build_headers(
            self.cfg.ak_id, self.cfg.ak_secret,
            method="POST", path=path, body=body_str,
        )
        url = f"{str(self.cfg['account_base']).rstrip('/')}{path}"
        try:
            status, _hdrs, data = http_request(
                url, method="POST", headers=headers,
                body=body_str.encode("utf-8") if body_str else None,
                timeout=timeout,
                proxy=str(proxy if proxy is not None else (self.cfg.get("proxy") or "")),
            )
        except Exception as exc:                        # noqa: BLE001
            raise AccountError(f"account service unreachable: {exc}",
                               code="NETWORK", retryable=True) from exc

        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception:                               # noqa: BLE001
            raise AccountError(
                f"account service returned HTTP {status}: "
                f"{data[:200].decode('utf-8', 'replace')}", code=f"HTTP_{status}",
                retryable=status >= 500)

        code = str(payload.get("code") or payload.get("errorCode") or "")
        if code and code != "000000":
            msg = payload.get("desc") or payload.get("message") or code
            raise AccountError(f"{path} failed: {msg}", code=code,
                               retryable=code in ("100001", "100002"))
        if status >= 400 and "message" in payload:
            raise AccountError(f"{path} HTTP {status}: {payload['message']}",
                               code=f"HTTP_{status}", retryable=status >= 500)
        return payload

    def ensure_identity(self, account: "Account") -> Dict[str, Any]:
        """Give the account an identity if it does not have one yet."""
        if not account.identity:
            account.identity = new_identity(self.identity_mode())
        return account.identity

    def rebind_identity(self, account: "Account") -> Dict[str, Any]:
        """Generate a fresh identity for the account (new "device")."""
        account.identity = new_identity(self.identity_mode())
        return account.identity

    # -- login ----------------------------------------------------------

    def get_public_key(self, identity: Optional[Dict[str, Any]] = None,
                       proxy: Optional[str] = None) -> Tuple[str, str]:
        """→ ``(pukey_b64, rcode)``.  Zero side effects: a good first probe."""
        payload = self.call("/login/account/getPuKey", {"base": self._base(identity)},
                            proxy=proxy)
        data = payload.get("data") or {}
        pukey, rcode = data.get("pukey") or "", data.get("rcode") or ""
        if not pukey:
            raise AccountError("getPuKey returned no public key", code="NO_PUKEY")
        return pukey, rcode

    def login_by_password(self, loginid: str, password: str,
                          identity: Optional[Dict[str, Any]] = None,
                          proxy: Optional[str] = None) -> Dict[str, str]:
        """Full password login → ``{session, userid, phone}``."""
        pukey, rcode = self.get_public_key(identity, proxy=proxy)
        encrypted = rsa_encrypt(pukey, password)
        payload = self.call("/login/account/byPwd", {
            "base": self._base(identity),
            "param": {
                "loginid": loginid,
                "password": encrypted,
                "rcode": rcode,
                "type": 1,
                "expire": C.SESSION_EXPIRE_SECONDS,
            },
        }, proxy=proxy)
        return _extract_session(payload)

    def send_sms_code(self, phone: str,
                      identity: Optional[Dict[str, Any]] = None,
                      proxy: Optional[str] = None) -> Dict[str, Any]:
        return self.call("/login/phone/sendMsgCode", {
            "base": self._base(identity),
            "param": {"ccode": "86", "phone": phone, "expire": 300},
        }, proxy=proxy)

    def login_by_sms(self, phone: str, code: str, msgid: str,
                     identity: Optional[Dict[str, Any]] = None,
                     proxy: Optional[str] = None) -> Dict[str, str]:
        payload = self.call("/login/phone/checkCode", {
            "base": self._base(identity),
            "param": {"ccode": "86", "phone": phone, "mcode": code, "msgid": msgid,
                      "expire": C.SESSION_EXPIRE_SECONDS},
        }, proxy=proxy)
        return _extract_session(payload)

    def query_base_info(self, session: str,
                        identity: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.call("/userinfo/query/baseInfo",
                         {"base": self._base(identity), "param": {"session": session}})

    # -- 微信第三方登录（客户端 electron/xfyun/account-service.js §3.2.7）-------
    #
    # 四步强制绑手机号流程：
    #   1. bind_auth_third_account(code) → { bind: 0|1, rcode }
    #        bind=1 该微信已绑过手机号 → 直接 bind_skip 拿 session
    #        bind=0 新微信/未绑       → bind_send_msg + bind_check_code 绑手机
    #   2. bind_send_msg({rcode, phone})          → { msgid }
    #   3. bind_check_code({rcode, mcode, msgid}) → session + userid + phone
    #   4. bind_skip({rcode})                     → session + userid
    #
    # 注意：微信 code 只在第 1 步用一次，后续接口只用 rcode。

    def bind_auth_third_account(self, code: str, third_type: str = "wx",
                                identity: Optional[Dict[str, Any]] = None,
                                proxy: Optional[str] = None) -> Dict[str, Any]:
        if not code:
            raise AccountError("缺少微信授权 code", code="MISSING_CODE")
        param: Dict[str, Any] = {"tcode": {"code": code}, "type": third_type}
        authconf = str(self.cfg.get("wechat_authconf") or "")
        if authconf:
            param["authconf"] = authconf
        payload = self.call("/login/thirdAccount/bind/auth",
                            {"base": self._base(identity), "param": param},
                            proxy=proxy)
        return payload.get("data") if isinstance(payload.get("data"), dict) else payload

    def bind_send_msg(self, rcode: str, phone: str, ccode: str = "86",
                      expire: int = 300,
                      identity: Optional[Dict[str, Any]] = None,
                      proxy: Optional[str] = None) -> Dict[str, Any]:
        if not rcode:
            raise AccountError("缺少 rcode", code="MISSING_RCODE")
        payload = self.call("/login/thirdAccount/bind/sendMsg", {
            "base": self._base(identity),
            "param": {"rcode": rcode, "phone": phone, "ccode": ccode,
                      "expire": expire},
        }, proxy=proxy)
        return payload.get("data") if isinstance(payload.get("data"), dict) else payload

    def bind_check_code(self, rcode: str, mcode: str, msgid: str,
                        expire: int = C.SESSION_EXPIRE_SECONDS,
                        identity: Optional[Dict[str, Any]] = None,
                        proxy: Optional[str] = None) -> Dict[str, str]:
        if not (rcode and mcode and msgid):
            raise AccountError("缺少 rcode/mcode/msgid", code="MISSING_PARAM")
        payload = self.call("/login/thirdAccount/bind/checkCode", {
            "base": self._base(identity),
            "param": {"rcode": rcode, "mcode": mcode, "msgid": msgid,
                      "expire": expire},
        }, proxy=proxy)
        return _extract_session(payload)

    def bind_skip(self, rcode: str, expire: int = C.SESSION_EXPIRE_SECONDS,
                  identity: Optional[Dict[str, Any]] = None,
                  proxy: Optional[str] = None) -> Dict[str, str]:
        if not rcode:
            raise AccountError("缺少 rcode", code="MISSING_RCODE")
        payload = self.call("/login/thirdAccount/bind/skip", {
            "base": self._base(identity),
            "param": {"rcode": rcode, "expire": expire},
        }, proxy=proxy)
        return _extract_session(payload)

    def logout(self, session: str,
               identity: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.call("/login/account/logout",
                         {"base": self._base(identity), "param": {"session": session}})

    # -- convenience ----------------------------------------------------

    def fill(self, account: Account) -> Account:
        """Log the account in (password path) and refresh its session fields.

        Also binds the account's own identity — created on first login, then
        reused for every later request so the account keeps looking like one
        consistent device.
        """
        if not account.loginid or not account.password:
            raise AccountError(
                f"account {account.name}: loginid/password missing",
                code="NO_CREDENTIALS")
        identity = self.ensure_identity(account)
        got = self.login_by_password(account.loginid, account.password, identity,
                                     proxy=account.proxy or None)
        account.session = got["session"]
        account.userid = got.get("userid", "")
        account.obtained_at = int(time.time())
        account.expire_at = account.obtained_at + C.SESSION_EXPIRE_SECONDS
        return account


def _extract_session(payload: Dict[str, Any]) -> Dict[str, str]:
    """Pull session/userid/phone out of whatever shape the server returns."""
    node = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(node, dict):
        raise AccountError(f"unexpected login payload: {payload}")
    session = str(node.get("session") or "")
    if not session:
        raise AccountError(f"login succeeded but returned no session: {payload}")
    userid = str(node.get("userid") or node.get("userId") or "")
    phone = str(node.get("phone") or node.get("loginid") or "")
    return {"session": session, "userid": userid, "phone": phone}
