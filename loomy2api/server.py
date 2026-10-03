"""HTTP gateway: OpenAI-compatible + Anthropic-compatible endpoints."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import os
import socket
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import constants as C
from . import anthropic as anth
from .panel import Panel
from .pool import AccountPool, PoolError
from .routing import session_key
from .upstream import ModelGateway, sse_usage, human_usage
from .usage import UsageLedger

__all__ = ["Gateway", "serve"]


def _secret_equal(a: str, b: str) -> bool:
    """常量时间比较两个密钥。

    先 SHA-256 摘要再比：长度差异被吸收进摘要，比较耗时不随前缀匹配长度变化，
    避免计时侧信道（借鉴 workbuddy2api-panel 的 internal/httpauth）。
    """
    da = hashlib.sha256((a or "").encode("utf-8")).digest()
    db = hashlib.sha256((b or "").encode("utf-8")).digest()
    return hmac.compare_digest(da, db)


def _rate(usage: Dict[str, Any], elapsed: float) -> str:
    """把 usage 折算成 token/秒，拼进日志尾巴。"""
    if not elapsed or elapsed <= 0:
        return ""
    total = 0
    for key in ("completion_tokens", "total_tokens"):
        value = (usage or {}).get(key)
        if isinstance(value, (int, float)) and value:
            total = int(value)
            break
    return f" {total / elapsed:.1f}tok/s" if total else ""


def _security_headers(cfg) -> Dict[str, str]:
    """面板/接口的安全响应头（可关）。CSP 只放行内联脚本与同源资源。

    面板是单文件内联 HTML（go:embed 风格），所以 ``unsafe-inline`` 必须留着，
    但 ``connect-src 'self'`` 仍能把数据外发限制在同源。
    """
    if not cfg.get("security_headers", True):
        return {}
    return {
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
    }


class Logger:
    """Timestamped file+console logger (never raises).

    三件事（都是排障时最缺的）:

    * **级别** —— ``[INFO]`` / ``[WARN]`` / ``[ERROR]``，面板可按级别过滤。
      兼容历史写在消息里的 ``[warn]`` / ``[error]`` 前缀：自动升级为级别
      并剥掉前缀，不重复显示。
    * **请求 ID** —— thread-local 上下文，一次请求的所有日志串在同一个
      ``[req:xxxxxxxx]`` 下；错误响应里回传同一个 ID，日志能对上号。
    * **按大小轮转** —— 默认 20MB，留 3 份历史，避免日志文件把盘撑满。
    """

    def __init__(self, log_dir: Path, name: str = "gateway.log",
                 console: bool = True, max_bytes: int = 20 * 1024 * 1024,
                 backups: int = 3):
        self.path = Path(log_dir) / name
        self.console = console
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()
        self._ctx = threading.local()

    # -- 请求上下文（thread-local）------------------------------------
    def bind(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self._ctx, k, v)

    def unbind(self, *names) -> None:
        for n in names:
            try:
                delattr(self._ctx, n)
            except AttributeError:
                pass

    # -- 级别便捷方法 -------------------------------------------------
    def info(self, message: str) -> None:
        self(message, level="INFO")

    def warn(self, message: str) -> None:
        self(message, level="WARN")

    def error(self, message: str) -> None:
        self(message, level="ERROR")

    def __call__(self, message: str, *, level: str = "INFO",
                 console: Optional[bool] = None) -> None:
        # 历史消息里的级别前缀 → 升级为真正的级别
        for prefix, lv in (("[warn]", "WARN"), ("[error]", "ERROR"),
                           ("[debug]", "DEBUG")):
            if message.startswith(prefix):
                level = lv
                message = message[len(prefix):].lstrip()
                break
        rid = getattr(self._ctx, "request_id", None)
        head = "[%s]" % time.strftime("%Y-%m-%d %H:%M:%S")
        if rid:
            head += " [req:%s]" % rid
        line = "%s [%s] %s" % (head, level, message)
        if self.console if console is None else console:
            print(line, flush=True)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                self._rotate_if_needed()
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:                               # noqa: BLE001
            pass

    def _rotate_if_needed(self) -> None:
        """超 max_bytes 就轮转：当前 → .1，.1 → .2 …… 超出 backups 的丢弃。"""
        try:
            if not self.path.exists():
                return
            if self.path.stat().st_size < self.max_bytes:
                return
            for i in range(self.backups, 0, -1):
                src = self.path.with_name(self.path.name + ".%d" % i)
                if i >= self.backups:
                    try:
                        src.unlink()
                    except OSError:
                        pass
                    continue
                dst = self.path.with_name(self.path.name + ".%d" % (i + 1))
                if src.exists():
                    src.rename(dst)
            self.path.rename(self.path.with_name(self.path.name + ".1"))
        except Exception:                               # noqa: BLE001
            pass


class Gateway:
    """Holds the configuration, the account pool and the model catalogue."""

    def __init__(self, cfg, log: Optional[Logger] = None):
        self.cfg = cfg
        self.log = log or Logger(cfg.path("log_dir", "logs"),
                                 console=bool(cfg.get("log_console", True)))
        self.pool = AccountPool(cfg, logger=self.log)
        self.models_client = ModelGateway(cfg)
        self.panel = Panel(self)
        self.models: list = []
        self._models_lock = threading.Lock()
        #: 请求级用量台账（面板「用量」页）
        self.usage = UsageLedger(
            int(cfg.get("usage_max_entries") or 500),
            persist_path=Path(__file__).resolve().parent.parent / "state"
            / "usage.jsonl")

    # -- model catalogue -------------------------------------------------

    def refresh_models(self) -> list:
        try:
            acc = self.pool.acquire()
            payload = self.models_client.models(acc.session)
            data = payload.get("data") or []
            for m in data:
                if isinstance(m, dict) and (m.get("multiplier") in (None, "", 0)):
                    mm = re.search(r"(?:x|×)([0-9]+(?:\.[0-9]+)?)",
                                   str(m.get("name") or ""), re.I)
                    if mm:
                        m["multiplier"] = float(mm.group(1))
            with self._models_lock:
                self.models = data
            self.log(f"上游模型列表已刷新：{len(data)} 个")
        except Exception as exc:                        # noqa: BLE001
            with self._models_lock:
                have = bool(self.models)
            if not have:
                self.models = [dict(m, object="model", owned_by="loomy")
                               for m in C.FALLBACK_MODELS]
                self.log(f"[warn] 拉取模型列表失败，使用内置清单：{exc}")
        return self.models

    def catalogue(self) -> list:
        with self._models_lock:
            if not self.models:
                self.models = [dict(m, object="model", owned_by="loomy")
                               for m in C.FALLBACK_MODELS]
            return list(self.models)

    def resolve_model(self, requested: str) -> str:
        """Model ids are used **verbatim** — there is no alias table.

        Only the provider prefix is stripped (``imodel/deepseek-…`` →
        ``deepseek-…``), because several clients insist on writing it. Note that
        the upstream itself falls back to its default model when it does not
        recognise an id (measured: ``gpt-4o-mini`` → ``deepseek-v4-flash-0731``,
        HTTP 200), so a typo degrades instead of erroring — always check the
        ``model`` field of the response.
        """
        name = str(requested or "").strip()
        if self.cfg.get("strip_model_prefix", True) and "/" in name:
            name = name.split("/", 1)[1].strip()
        return name or str(self.cfg.get("default_model") or C.DEFAULT_MODEL)

    # -- background ------------------------------------------------------

    def start(self) -> None:
        self.pool.bootstrap()
        self.pool.start()
        self.refresh_models()


# ------------------------------------------------------------------ handler


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "loomy2api"
    gateway: Gateway = None                        # injected by serve()

    #: Statuses that implicate the *account* (drop its session / cool it down).
    ACCOUNT_FAULT = (401, 403, 402, 429)
    #: Statuses where retrying is pointless (the request itself is wrong).
    FATAL_REQUEST = (400, 404, 422)

    def setup(self):                               # noqa: D102
        super().setup()
        self._body_read = False

    # ------------------------------------------------------------- basics

    def log_message(self, fmt, *args):             # silence default access log
        return

    def _json(self, status: int, obj: Any, extra: Optional[dict] = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for key, value in (extra or {}).items():
            self.send_header(key, str(value))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, message: str, kind: str = "invalid_request_error") -> None:
        # Drain any unread request body and close the connection: on Windows an
        # abort (RST) rather than a clean error surfaces otherwise when we
        # reject before reading the payload (e.g. a 401 from the API-key gate).
        self._drain()
        self.close_connection = True
        payload: Dict[str, Any] = {"message": message, "type": kind, "code": status}
        rid = getattr(self, "_req_id", None)
        if rid:
            payload["request_id"] = rid
        # 被拒的请求也要留痕：之前只有响应里有，日志里查不到，
        # 排障时看不到「谁在打、为什么被拒」。带 req id，能对上号。
        try:
            self.gateway.log(
                f"{self.command} {self._path()} → {status} {message}",
                level=("ERROR" if status >= 500 else "WARN"))
        except Exception:                               # noqa: BLE001
            pass
        self._json(status, {"error": payload})

    # -- chunked 保活：防 Cloudflare 60~100s 504 -------------------------

    def _chunked_open(self) -> None:
        """开一个 chunked 响应（不带 Content-Length，可边算边发）。

        body 仍是合法 JSON（保活写的是 RFC 8259 允许的前导空白）。
        实测：走 Cloudflare 代理时非流式响应有 ~60s 硬限制（三次实测
        均在 62s 断连），换成 SSE Content-Type 也一样 —— 那是 CF 的
        限制，保活解决不了；但在**不走 CF** 的入口（IP 直连）上，保活
        能正常撑住长任务。
        """
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

    def _chunk_write(self, data: bytes) -> bool:
        """写一个 chunk；客户端已断开时返回 False。"""
        try:
            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
            self.wfile.flush()
            return True
        except Exception:                               # noqa: BLE001
            return False

    def _chunk_close(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:                               # noqa: BLE001
            pass

    def _run_with_keepalive(self, fn, interval: float = 15.0):
        """阻塞调用上游期间，定期写 JSON 合法的前导空白保活。

        Cloudflare 免费版对「无字节流动」的请求约 60~100s 就返回 504 ——
        非流式长任务（写长文 / 长代码）必然被掐（实测 62s 即 504）。
        chunked + 空白前缀让连接持续有字节流动；RFC 8259 允许 JSON 值
        前后有空白，客户端 ``json.loads()`` 直接忽略，解析不受影响。
        """
        box: Dict[str, Any] = {}
        done = threading.Event()

        def worker() -> None:
            try:
                box["value"] = fn()
            except Exception as exc:                    # noqa: BLE001
                box["error"] = exc
            finally:
                done.set()

        threading.Thread(target=worker, daemon=True).start()
        last = time.time()
        while not done.wait(0.4):
            if time.time() - last >= interval:
                if not self._chunk_write(b" "):         # 前导空白，JSON 合法
                    return None                         # 客户端已断开
                last = time.time()
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def _drain(self) -> None:
        if self._body_read:
            return
        self._body_read = True
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            self._read_chunked()                    # 读掉并丢弃
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length:
            try:
                self.rfile.read(length)
            except Exception:                       # noqa: BLE001
                pass

    def _read_chunked(self) -> bytes:
        """读 chunked 请求体（``Transfer-Encoding: chunked``）。

        之前只认 Content-Length：大 prompt 的客户端一旦用 chunked 传输，
        请求体被整段丢弃 → 上游收到空 messages → 400，还会误冷却账号。
        """
        data = bytearray()
        try:
            while True:
                line = self.rfile.readline(65536).strip()
                if b";" in line:
                    line = line.split(b";", 1)[0]
                try:
                    size = int(line, 16)
                except ValueError:
                    break
                if size == 0:                       # 最后一块 → 读 trailer
                    while True:
                        trailer = self.rfile.readline(65536)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                data += self.rfile.read(size)
                self.rfile.read(2)                  # 块尾 CRLF
                if len(data) > 64 * 1024 * 1024:    # 64MB 防护
                    break
        except Exception:                           # noqa: BLE001
            pass
        return bytes(data)

    def _query_int(self, name: str, default: int) -> int:
        from urllib.parse import parse_qs, urlparse as _urlparse
        values = parse_qs(_urlparse(self.path).query).get(name) or []
        try:
            return int(values[0])
        except (IndexError, ValueError):
            return default

    def _read_body(self) -> bytes:
        if self._body_read:
            return b""
        self._body_read = True
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            return self._read_chunked()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(length) if length else b""

    def _read_json(self) -> Dict[str, Any]:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:                        # noqa: BLE001
            raise ValueError(f"请求体不是合法 JSON / body is not valid JSON: {exc}") from exc

    def _path(self) -> str:
        """Normalise the path, keeping the non-``/v1`` surfaces intact.

        Clients are sloppy about the prefix, so ``/chat/completions`` is
        rewritten to ``/v1/chat/completions``; the panel, its API and the
        health/admin endpoints are exempt.
        """
        path = self.path.split("?", 1)[0]
        while "/v1/v1" in path:
            path = path.replace("/v1/v1", "/v1")
        if path in ("", "/", "/index.html"):
            return "/panel"                       # the panel is the landing page
        exempt = ("/panel", "/api/", "/health", "/admin", "/favicon")
        if not path.startswith(exempt):
            if path != "/v1" and not path.startswith("/v1/"):
                path = "/v1" + path
        return path.rstrip("/") or "/"

    # --------------------------------------------------------------- auth

    def _presented_key(self) -> Tuple[str, str]:
        """Pull the caller's key out of whatever shape they sent it in.

        Real clients are all over the place: ``Authorization: Bearer k``,
        bare ``Authorization: k``, ``x-api-key``, or a query parameter — and
        some paste a key with stray whitespace/quotes. Accept all of them so a
        client is never rejected for cosmetics.
        """
        from urllib.parse import parse_qs, urlparse as _urlparse

        raw = (self.headers.get("Authorization") or "").strip()
        if raw.lower().startswith("basic "):
            # 反代层（nginx auth_basic）的凭证，不是本网关的 key；
            # 不跳过它会把 Basic 串当 key 去比，导致 x-api-key 永远轮不到
            raw = ""
        if raw:
            if raw.lower().startswith("bearer "):
                return raw[7:].strip(), "authorization:bearer"
            if raw.lower().startswith("token "):
                return raw[6:].strip(), "authorization:token"
            return raw, "authorization:raw"
        for header in ("x-api-key", "api-key", "apikey", "x-goog-api-key"):
            value = (self.headers.get(header) or "").strip()
            if value:
                return value, f"header:{header}"
        query = parse_qs(_urlparse(self.path).query)
        for name in ("api_key", "apikey", "key", "access_token"):
            if query.get(name):
                return query[name][0].strip(), f"query:{name}"
        return "", "none"

    @staticmethod
    def _mask(value: str) -> str:
        if not value:
            return "(empty)"
        if len(value) <= 12:
            return value[:4] + "…"
        return f"{value[:8]}…{value[-4:]}(len={len(value)})"

    def _authorized(self) -> bool:
        path = self.path.split("?", 1)[0]
        keys = self.gateway.cfg.api_keys
        panel_pw = str(self.gateway.cfg.get("panel_password") or "")
        # 登录端点自身豁免（它就是拿凭证的地方）
        if path == "/api/panel/login":
            return True
        if not keys and not panel_pw:
            return True
        token, source = self._presented_key()
        token = token.strip().strip('"').strip("'")
        if token and any(_secret_equal(token, k) for k in keys):
            return True
        # 面板路径：面板密码（登录成功后前端持有的管理令牌）也可作为凭证
        if path.startswith("/api/panel/") and panel_pw:
            if _secret_equal(token, panel_pw):
                return True
        if path.startswith("/api/panel/"):
            self.gateway.log(f"[auth] 面板拒绝 {self.command} {self.path}")
            self._error(401, "请先登录面板 / panel login required",
                        "panel_auth_required")
            return False
        # never log the secret itself — just enough to see what arrived
        self.gateway.log(f"[auth] 拒绝 {self.command} {self.path} ← {source} "
                         f"token={self._mask(token)}")
        self._error(401, "无效的 API Key / invalid API key", "authentication_error")
        return False

    # --------------------------------------------- 面板登录（密码 → api key）

    _login_fails: Dict[str, List[float]] = {}

    def _panel_login(self, payload: Dict[str, Any]) -> None:
        """面板密码登录：成功返回 api_key（即面板管理令牌）。带简单防爆破。

        ``payload`` 由 ``_panel_api`` 解析后传入——这里**不能**再调
        ``_read_json()``：``_body_read`` 标志会让第二次读拿到空 body，
        密码恒为空、永远「密码错误」（实测踩过）。
        """
        import time as _t
        pw = str((payload or {}).get("password") or "")
        panel_pw = str(self.gateway.cfg.get("panel_password") or "")
        ip = self.client_address[0]
        now = _t.time()
        # 防爆破：同 IP 10 分钟内最多 8 次失败
        fails = [t for t in self._login_fails.get(ip, []) if now - t < 600]
        if len(fails) >= 8:
            self._error(429, "尝试过多，请 10 分钟后再试", "rate_limited")
            return
        if not panel_pw:
            self._error(404, "未启用面板登录（panel_password 未设置）",
                        "no_panel_auth")
            return
        if not pw or not _secret_equal(pw, panel_pw):
            fails.append(now)
            self._login_fails[ip] = fails
            self.gateway.log(f"[auth] 面板登录失败 {ip}（{len(fails)}/8）")
            self._error(401, "密码错误", "authentication_error")
            return
        self._login_fails.pop(ip, None)
        keys = self.gateway.cfg.api_keys
        self._json(200, {"ok": True, "api_key": (keys[0] if keys else ""),
                         "auth_required": bool(keys)})

    # ------------------------------------------------------------ routing

    def do_OPTIONS(self):                             # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):                                # noqa: N802
        self.do_GET()

    def handle_one_request(self):
        """每个请求进来都绑一个 8 位 request_id。

        日志里会出现 ``[req:xxxxxxxx]``，错误响应里回传同一个 ID ——
        客户端贴过来的报错能直接对上网关日志里那几行。
        """
        rid = uuid.uuid4().hex[:8]
        self._req_id = rid
        try:
            self.gateway.log.bind(request_id=rid)
        except Exception:                               # noqa: BLE001
            pass
        try:
            super().handle_one_request()
        finally:
            try:
                self.gateway.log.unbind("request_id")
            except Exception:                           # noqa: BLE001
                pass

    def do_GET(self):                                 # noqa: N802
        path = self._path()
        if path in ("/v1/health", "/health", "/healthz", "/v1/healthz"):
            return self._health()
        if path == "/panel/app.js":
            return self._panel_js()
        if path == "/panel/login-link":
            return self._login_link_page()
        if path in ("/panel", "/", "/index.html"):
            return self._panel_page()
        if not self._authorized():
            return
        try:
            if path == "/v1/models":
                return self._models()
            if path == "/v1/points":
                return self._points()
            if path in ("/v1/admin/accounts", "/admin/accounts"):
                return self._accounts()
            if path == "/api/panel/state":
                refresh = "refresh=1" in (self.path or "")
                return self._json(200, self.gateway.panel.state(refresh=refresh))
            if path == "/api/panel/logs":
                lines = self._query_int("lines", 200)
                return self._json(200, self.gateway.panel.logs(lines))
            if path == "/api/panel/usage":
                limit = self._query_int("limit", 100)
                window = self._query_int("hours", 24)
                from urllib.parse import parse_qs as _pqs, urlparse as _pu
                upstream = (_pqs(_pu(self.path).query).get("upstream")
                            or ["0"])[0] in ("1", "true")
                return self._json(200, self.gateway.panel.usage(
                    limit=limit, window_hours=window, upstream=upstream))
            if path == "/api/panel/apikey":
                # 只给已通过 nginx Basic Auth 的管理员看（反代注入 X-Panel-Auth）
                if not (self.headers.get("X-Panel-Auth") or "").strip():
                    return self._error(401, "仅限面板认证来源 / panel auth required",
                                       "authentication_error")
                keys = self.gateway.cfg.api_keys
                return self._json(200, {"ok": True,
                                        "api_key": (keys[0] if keys else "")})
            if path == "/api/panel/points":
                from urllib.parse import parse_qs as _pqs, urlparse as _pu
                refresh = (_pqs(_pu(self.path).query).get("refresh")
                           or ["0"])[0] in ("1", "true")
                return self._json(200, self.gateway.panel.points(refresh=refresh))
            if path == "/api/panel/models":
                return self._json(200, self.gateway.panel.models_view())
            if path == "/api/panel/tasks":
                return self._json(200, self.gateway.panel.platform_tasks())
            if path == "/api/panel/jobs":
                return self._json(200, {"ok": True,
                                        "jobs": self.gateway.pool.jobs_snapshot()})
            if path == "/api/panel/login/poll":
                state = ""
                from urllib.parse import parse_qs, urlparse as _u
                state = (parse_qs(_u(self.path).query).get("state") or [""])[0]
                return self._json(200, self.gateway.panel.login_poll(state))
            if path == "/api/panel/proxies":
                return self._json(200, self.gateway.panel.proxies())
            if path == "/api/panel/config":
                return self._json(200, {"ok": True,
                                        "config": self.gateway.cfg.public_view(),
                                        "restart_keys": list(
                                            self.gateway.cfg.RESTART_KEYS)})
            if path == "/favicon.ico":
                return self._error(404, "no favicon")
            return self._error(404, f"未知路径 / unknown path: {self.path}")
        except PoolError as exc:
            return self._error(503, str(exc), "account_pool_error")
        except Exception as exc:                        # noqa: BLE001
            self.gateway.log(f"[error] GET {self.path}: {exc}\n{traceback.format_exc()}")
            return self._error(502, f"上游调用失败 / upstream failure: {exc}", "upstream_error")

    def do_POST(self):                                # noqa: N802
        path = self._path()
        if not self._authorized():
            return
        try:
            if path == "/v1/chat/completions":
                return self._chat()
            if path == "/v1/messages":
                return self._messages()
            if path == "/v1/embeddings":
                return self._passthrough("embeddings")
            if path == "/v1/images/generations":
                return self._passthrough("images/generations")
            if path in ("/v1/admin/accounts/reload", "/admin/accounts/reload"):
                self.gateway.pool.load()
                self.gateway.refresh_models()
                return self._json(200, self.gateway.pool.snapshot())
            if path == "/api/panel/config":
                return self._config_patch()
            if path.startswith("/api/panel/"):
                return self._panel_api(path)
            return self._error(404, f"未知路径 / unknown path: {self.path}")
        except ValueError as exc:
            return self._error(400, str(exc))
        except PoolError as exc:
            return self._error(400, str(exc), "account_pool_error")
        except Exception as exc:                        # noqa: BLE001
            self.gateway.log(f"[error] POST {self.path}: {exc}\n{traceback.format_exc()}")
            try:
                return self._error(502, f"上游调用失败 / upstream failure: {exc}",
                                   "upstream_error")
            except Exception:                           # noqa: BLE001
                return

    # ------------------------------------------------------------- panel

    def _panel_page(self) -> None:
        body = self.gateway.panel.html()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in _security_headers(self.gateway.cfg).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _login_link_page(self) -> None:
        """登录链接的落地页（微信扫码 / 手机号+密码，完成后自动回调面板）。"""
        body = self.gateway.panel.login_link_page()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in _security_headers(self.gateway.cfg).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _panel_js(self) -> None:
        body = self.gateway.panel.js()
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in _security_headers(self.gateway.cfg).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _panel_api(self, path: str) -> None:
        payload = self._read_json()
        panel = self.gateway.panel
        handlers = {
            "/api/panel/accounts": panel.add_account,
            "/api/panel/accounts/update": panel.update_account,
            "/api/panel/accounts/remove": panel.remove_account,
            "/api/panel/accounts/renew": panel.renew_account,
            "/api/panel/accounts/identity": panel.identity,
            "/api/panel/accounts/proxy": panel.set_proxy,
            "/api/panel/refresh": lambda _p: panel.refresh(_p),
            "/api/panel/jobs/run": lambda p: panel.run_job(p),
            "/api/panel/usage/clear": lambda _p: panel.clear_usage(),
            "/api/panel/login/start": panel.login_start,
            "/api/panel/login/send": panel.login_send,
            "/api/panel/login/submit": panel.login_submit,
            "/api/panel/apikey": panel.apikey_view,
            "/api/panel/checkin": panel.checkin,
            "/api/panel/tasks": panel.platform_tasks,
            "/api/panel/redeem": panel.redeem,
            "/api/panel/invite": panel.bind_invite,
            "/api/panel/login/wechat/start": panel.login_wechat_start,
            "/api/panel/login/wechat/qr/start": panel.login_wechat_qr_start,
            "/api/panel/login/wechat/qr/poll": panel.login_wechat_qr_poll,
            "/api/panel/login/wechat/browser": panel.login_wechat_browser,
            "/api/panel/login/wechat/cancel": panel.login_wechat_cancel,
            "/api/panel/login/wechat/complete": panel.login_wechat_complete,
            "/api/panel/login/wechat/bind/send": panel.login_wechat_bind_send,
            "/api/panel/login/wechat/bind/submit": panel.login_wechat_bind_submit,
        }
        if path == "/api/panel/login":
            return self._panel_login(payload)
        handler = handlers.get(path)
        if handler is None:
            return self._error(404, f"未知面板接口 / unknown panel endpoint: {path}")
        result = handler(payload)
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        return self._json(200, result)

    # ------------------------------------------------------ simple routes

    def _health(self) -> None:
        gw = self.gateway
        accounts = gw.pool.accounts
        return self._json(200, {
            "status": "ok" if gw.pool.usable() else "degraded",
            "healthy": len(gw.pool.usable()),
            "total": len(accounts),
            "service": "loomy2api",
            "name": "loomy2api",
            "upstream": gw.cfg["upstream"],
            "accounts": len(accounts),
            "usable_accounts": len(gw.pool.usable()),
            "models": len(gw.catalogue()),
            "auth_required": bool(gw.cfg.api_keys),
            "strategy": gw.cfg.get("strategy"),
            "sticky": gw.pool.sticky.snapshot(),
            "inflight": gw.pool.inflight.snapshot(),
            "panel": f"http://{gw.cfg['host']}:{gw.cfg['port']}/panel",
        })

    def _config_patch(self) -> None:
        """在线改配置（深合并 + 原子写 + 热生效）。"""
        gw = self.gateway
        payload = self._read_json()
        updates = payload.get("config") if isinstance(payload, dict) else None
        if not isinstance(updates, dict):
            return self._error(400, "缺少 config 对象 / missing config object")
        try:
            restart = gw.cfg.patch(updates)
        except ValueError as exc:
            return self._error(400, str(exc))
        # 池参数改了要重建选号/粘性组件
        gw.pool.apply_config()
        gw.log(f"[panel] 配置已更新（{', '.join(updates) or '空补丁'}）"
               + (f"；需重启：{', '.join(restart)}" if restart else "；全部热生效"))
        return self._json(200, {"ok": True, "restart_required": restart,
                                "config": gw.cfg.public_view()})

    def _models(self) -> None:
        gw = self.gateway
        catalogue = gw.catalogue()
        if not catalogue:
            gw.refresh_models()
            catalogue = gw.catalogue()
        return self._json(200, {"object": "list", "data": list(catalogue)})

    def _points(self) -> None:
        gw = self.gateway
        out = []
        for acc in gw.pool.accounts:
            gw.pool.refresh_quota(acc)
            out.append({
                "name": acc.name,
                "userid": acc.userid,
                "available": acc.available,
                "balance": acc.balance,
                "daily_balance": acc.daily_balance,
                "requests": acc.requests,
                "points_used": acc.points_used,
            })
        gw.pool.save()
        # the same account can appear twice (own session + imported client
        # session) — count its quota only once
        seen: Dict[str, int] = {}
        for item in out:
            key = item["userid"] or item["name"]
            if isinstance(item["available"], int):
                seen[key] = max(seen.get(key, 0), item["available"])
        return self._json(200, {
            "total_available": sum(seen.values()),
            "unique_accounts": len(seen),
            "accounts": out,
        })

    def _accounts(self) -> None:
        return self._json(200, self.gateway.pool.snapshot())

    # ------------------------------------------------------- passthroughs

    def _passthrough(self, suffix: str) -> None:
        gw = self.gateway
        raw = self._read_body()
        acc = gw.pool.acquire()
        t0 = time.time()
        status, headers, data = gw.models_client.call(
            acc.session, suffix, json.loads(raw.decode("utf-8")) if raw else {})
        if status >= 400:
            gw.pool.report_failure(acc, status, data[:200].decode("utf-8", "replace"))
        else:
            gw.pool.report_success(acc)
        # 记账：生图一次扣上百积分，之前完全不进台账，「用量」页自然是错的。
        # points 在响应**顶层**（images/generations 无 usage 对象，实测）。
        try:
            body = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            body = {}
        points = body.get("points_consumed") if isinstance(body, dict) else None
        if points is None and isinstance(body, dict):
            points = (body.get("usage") or {}).get("points_consumed")
        gw.usage.record(account=acc.name, model=str(
            (json.loads(raw.decode("utf-8")) if raw else {}).get("model") or suffix),
            status=status, usage={} if points is None
            else {"points_consumed": int(points or 0)},
            latency=time.time() - t0, kind="image" if "images" in suffix else "passthrough",
            proxy=acc.proxy or "")
        self.send_response(status)
        self.send_header("Content-Type",
                         headers.get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------------------------------------------------------- chat (core)

    def _chat_stream_to_json(self, req: Dict[str, Any], extra: Dict[str, str],
                             model: str, skey: Optional[str] = None) -> Tuple[int, bytes]:
        """非流式长任务 → **用流式调上游** → 聚合回 OpenAI 非流式 JSON。

        实测：上游对「长时间无响应」的非流式请求会在 ~60s 返回 504
        （8000 tok 任务 62s 即 http=504，日志实锤），但同一个模型的流式
        调用能跑几分钟（32000 tok / 212s 成功）。所以把非流式长任务转成
        流式取数，边收 SSE 边聚合，最后拼回非流式格式返回 —— 客户端
        无感，长任务不再被上游 504 掐断。
        """
        gw = self.gateway
        sreq = dict(req)
        sreq["stream"] = True
        tried: list = []
        last: Tuple[int, bytes] = (0, b"")
        for _ in range(int(gw.cfg.get("max_retries") or 0) + 1):
            try:
                acc = gw.pool.acquire(exclude=tried, model=model,
                                      session_key_value=skey)
            except PoolError as exc:
                gw.log(f"[warn] {exc}")
                break
            tried.append(acc.name)
            started = time.time()
            conn = resp = None
            try:
                conn, resp = gw.models_client.stream(
                    acc.session, "chat/completions", sreq, extra,
                    proxy=acc.proxy or None)
            except Exception as exc:                    # noqa: BLE001
                gw.pool.release(acc)
                gw.log(f"[call] {acc.name} {model} 长任务流式连接异常：{exc}")
                last = (502, str(exc).encode("utf-8"))
                continue
            if resp.status != 200:
                data = resp.read()
                conn.close()
                gw.pool.release(acc)
                last = (resp.status, data)
                reason = data[:200].decode("utf-8", "replace")
                gw.usage.record(account=acc.name, model=model, status=resp.status,
                                latency=time.time() - started, kind="chat",
                                error=reason, proxy=acc.proxy)
                gw.pool.report_failure(acc, resp.status, reason, model=model)
                continue

            content_parts: list = []
            reason_parts: list = []
            tools: Dict[int, Dict[str, str]] = {}
            finish = ""
            usage: Dict[str, Any] = {}
            rid = ""
            model_out = ""
            created = int(time.time())
            buffer = b""
            try:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        line = line.strip()
                        if not line.startswith(b"data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == b"[DONE]":
                            continue
                        try:
                            j = json.loads(payload.decode("utf-8"))
                        except Exception:               # noqa: BLE001
                            continue
                        if j.get("id"):
                            rid = j["id"]
                        if j.get("created"):
                            created = j["created"]
                        if j.get("model"):
                            # 上游对不认识的模型会静默回落 —— 以回包为准，
                            # 否则客户端/面板会误以为用的是请求里那个模型
                            model_out = j["model"]
                        if j.get("usage"):
                            usage = j["usage"]
                        ch = (j.get("choices") or [{}])[0]
                        if ch.get("finish_reason"):
                            finish = ch["finish_reason"]
                        d = ch.get("delta") or {}
                        if d.get("content"):
                            content_parts.append(d["content"])
                        if d.get("reasoning_content"):
                            reason_parts.append(d["reasoning_content"])
                        for tc in (d.get("tool_calls") or []):
                            idx = tc.get("index", 0)
                            slot = tools.setdefault(idx, {"name": "", "args": ""})
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["args"] += fn["arguments"]
            finally:
                gw.pool.release(acc)
                try:
                    conn.close()
                except Exception:                       # noqa: BLE001
                    pass

            elapsed = time.time() - started
            content = "".join(content_parts)
            reasoning = "".join(reason_parts)
            msg: Dict[str, Any] = {"role": "assistant", "content": content}
            if reasoning:
                msg["reasoning_content"] = reasoning
            if tools:
                msg["tool_calls"] = [
                    {"id": "call_%d" % i, "type": "function",
                     "function": {"name": t["name"], "arguments": t["args"]}}
                    for i, t in sorted(tools.items())]
                if not content:
                    msg["content"] = None
            obj = {"id": rid or ("chatcmpl-" + uuid.uuid4().hex[:12]),
                   "object": "chat.completion", "created": created,
                   "model": model_out or model,
                   "choices": [{"index": 0, "message": msg, "logprobs": None,
                                "finish_reason": finish or "stop"}],
                   "usage": usage}
            gw.pool.report_success(acc, usage, model=model)
            gw.usage.record(account=acc.name, model=model, status=200, usage=usage,
                            latency=elapsed, kind="chat", proxy=acc.proxy)
            gw.log(f"[call] {acc.name} {model} {elapsed:.1f}s http=200 流式聚合 "
                   f"{human_usage(usage)}{_rate(usage, elapsed)}")
            return (200, json.dumps(obj, ensure_ascii=False).encode("utf-8"))
        return last

    def _chat(self) -> None:
        gw = self.gateway
        req = self._read_json()
        model = gw.resolve_model(req.get("model"))
        stream = bool(req.get("stream"))
        skey = session_key(req)                 # 会话粘性键（可能为 None）

        # 空 messages 直接 400 快速失败：不打上游、不占账号、不冷却。
        # 之前这种请求会打到上游吃 400，再被 report_failure 冷却唯一账号
        # 300s，整个池子瘫痪（01:38~02:25 事故实锤）。
        if not (req.get("messages") or []):
            return self._error(400, "messages 不能为空（请求体解析失败或客户端未传）",
                               "invalid_request_error")

        extra: Dict[str, str] = {}
        for src, dst in (("chat_id", "ChatId"), ("msg_id", "MsgId"),
                         ("turn_id", "TurnId")):
            value = req.pop(src, None)
            if value:
                extra[dst] = value
        purpose = req.pop("request_purpose", None)
        if purpose:
            extra["X-Loomy-Request-Purpose"] = purpose
        req["model"] = model

        if stream:
            return self._chat_stream(req, extra, model, skey)

        def core() -> Tuple[int, bytes]:
            """调上游（含重试）→ ``(status, body)``，不负责写响应。"""
            tried: list = []
            last: Tuple[int, bytes] = (0, b"")
            for _ in range(int(gw.cfg.get("max_retries") or 0) + 1):
                try:
                    acc = gw.pool.acquire(exclude=tried, model=model,
                                          session_key_value=skey)
                except PoolError as exc:
                    gw.log(f"[warn] {exc}")
                    break
                tried.append(acc.name)
                started = time.time()
                try:
                    status, _hdrs, data = gw.models_client.call(
                        acc.session, "chat/completions", req, extra,
                        proxy=acc.proxy or None)
                finally:
                    gw.pool.release(acc)
                if status == 200:
                    try:
                        obj = json.loads(data.decode("utf-8"))
                    except Exception:                   # noqa: BLE001
                        obj = None
                    usage = (obj or {}).get("usage") or {}
                    elapsed = time.time() - started
                    gw.pool.report_success(acc, usage, model=model)
                    gw.usage.record(account=acc.name, model=model, status=200,
                                    usage=usage, latency=elapsed, kind="chat",
                                    proxy=acc.proxy)
                    gw.log(f"[call] {acc.name} {model} {elapsed:.1f}s http=200 "
                           f"{human_usage(usage)}{_rate(usage, elapsed)}")
                    return (200, data)
                last = (status, data)
                reason = data[:200].decode("utf-8", "replace")
                gw.usage.record(account=acc.name, model=model, status=status,
                                latency=time.time() - started, kind="chat",
                                error=reason, proxy=acc.proxy)
                if status in self.ACCOUNT_FAULT:
                    gw.log(f"[call] {acc.name} {model} http={status} → 账号问题，换账号重试")
                    gw.pool.report_failure(acc, status, reason, model=model)
                    if skey:
                        gw.pool.sticky.unbind(skey, acc.name)   # 失败解绑
                else:
                    # upstream hiccup (502/504/500) — do NOT punish the account
                    gw.pool.report_failure(acc, status, reason, model=model)
                    gw.log(f"[call] {acc.name} {model} http={status} → 上游异常，换账号重试"
                           f"（不冷却该账号）")
                if status in self.FATAL_REQUEST:        # not an account problem
                    break
            return last

        # 长任务（输出上限大）→ chunked + 保活，绕开 Cloudflare 60~100s 504
        min_tok = int(gw.cfg.get("keepalive_min_tokens") or 1500)
        if int(req.get("max_tokens") or 0) >= min_tok:
            self._chunked_open()
            try:
                # 长任务：内部转流式取数（上游非流式 ~60s 就 504），
                # 聚合后仍以非流式 JSON 返回，客户端无感。
                res = self._run_with_keepalive(
                    lambda: self._chat_stream_to_json(req, extra, model, skey))
            except Exception as exc:                    # noqa: BLE001
                res = (502, json.dumps({"error": {
                    "message": str(exc), "type": "upstream_error", "code": 502}
                }).encode("utf-8"))
            if not res:
                return                                  # 客户端已断开
            status, data = res
            if not data:
                data = json.dumps({"error": {
                    "message": "所有账号都失败了 / all accounts failed",
                    "type": "upstream_error", "code": 502}}).encode("utf-8")
            self._chunk_write(data)
            self._chunk_close()
            return

        # 短请求：走原路径，保留精确 HTTP 状态码
        status, data = core()
        try:
            return self._json(status or 502, json.loads(data.decode("utf-8")))
        except Exception:                               # noqa: BLE001
            return self._error(status or 502, data[:500].decode("utf-8", "replace"),
                               "upstream_error")

    def _chat_stream(self, req: Dict[str, Any], extra: Dict[str, str],
                     model: str, skey: Optional[str] = None) -> None:
        gw = self.gateway
        tried: list = []
        conn = resp = None
        acc = None

        for _ in range(int(gw.cfg.get("max_retries") or 0) + 1):
            try:
                acc = gw.pool.acquire(exclude=tried, model=model,
                                      session_key_value=skey)
            except PoolError as exc:
                gw.log(f"[warn] {exc}")
                break
            tried.append(acc.name)
            try:
                conn, resp = gw.models_client.stream(
                    acc.session, "chat/completions", req, extra,
                    proxy=acc.proxy or None)
            except Exception as exc:                    # noqa: BLE001
                gw.pool.release(acc)
                gw.log(f"[call] {acc.name} {model} stream 连接异常：{exc}")
                gw.usage.record(account=acc.name, model=model, status=0, kind="chat",
                                error=str(exc), stream=True, proxy=acc.proxy)
                conn = resp = None
                continue
            if resp.status == 200:
                break
            data = resp.read()
            conn.close()
            reason = data[:200].decode("utf-8", "replace")
            gw.pool.release(acc)
            if resp.status in self.ACCOUNT_FAULT:
                gw.log(f"[call] {acc.name} {model} stream http={resp.status}"
                       f" → 账号问题，换账号重试")
                gw.pool.report_failure(acc, resp.status, reason, model=model)
                if skey:
                    gw.pool.sticky.unbind(skey, acc.name)
            else:
                gw.pool.report_failure(acc, resp.status, reason, model=model)
                gw.log(f"[call] {acc.name} {model} stream http={resp.status}"
                       f" → 上游异常，换账号重试（不冷却该账号）")
            conn = resp = None

        if resp is None:
            return self._error(502, "所有账号都失败了 / all accounts failed",
                               "upstream_error")
        if resp.status != 200:
            data = resp.read()
            conn.close()
            gw.pool.release(acc)
            try:
                return self._json(resp.status, json.loads(data.decode("utf-8")))
            except Exception:                           # noqa: BLE001
                return self._error(resp.status, data[:500].decode("utf-8", "replace"),
                                   "upstream_error")

        started = time.time()
        first_chunk_at: Optional[float] = None
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.close_connection = True

        usage: Dict[str, Any] = {}
        buffer = b""
        try:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                if first_chunk_at is None:
                    first_chunk_at = time.time()
                self.wfile.write(chunk)
                self.wfile.flush()
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    sse_usage(line.strip(), usage)
            if buffer:
                sse_usage(buffer.strip(), usage)
            elapsed = time.time() - started
            ttfb = (first_chunk_at - started) if first_chunk_at else elapsed
            gw.pool.report_success(acc, usage, model=model)
            gw.usage.record(account=acc.name, model=model, status=200, usage=usage,
                            latency=elapsed, ttfb=ttfb, stream=True, kind="chat",
                            proxy=acc.proxy)
            gw.log(f"[call] {acc.name} {model} {elapsed:.1f}s http=200 "
                   f"{human_usage(usage)}{_rate(usage, elapsed)} "
                   f"ttfb={ttfb:.2f}s stream")
        except (BrokenPipeError, ConnectionResetError):
            gw.log(f"[call] {acc.name} {model} client disconnected")
        except Exception as exc:                        # noqa: BLE001
            # 上游断流 / 读超时等：也要记账，否则失败请求在用量台账里
            # 凭空消失，排障时看不到这段失败
            gw.log(f"[call] {acc.name} {model} stream 异常：{exc}")
            gw.usage.record(account=acc.name, model=model, status=0,
                            latency=time.time() - started, kind="chat",
                            error=str(exc), stream=True, proxy=acc.proxy)
        finally:
            gw.pool.release(acc)
            try:
                conn.close()
            except Exception:                           # noqa: BLE001
                pass

    # -------------------------------------------------- anthropic messages

    def _messages(self) -> None:
        gw = self.gateway
        req = self._read_json()
        if not (req.get("messages") or req.get("prompt")):
            return self._error(400, "messages 不能为空（请求体解析失败或客户端未传）",
                               "invalid_request_error")
        payload = anth.anthropic_to_openai(req)
        payload["model"] = gw.resolve_model(payload.get("model"))
        model = payload["model"]
        skey = session_key(payload)
        if req.get("stream"):
            return self._messages_stream(payload, model, skey)

        # 长任务：内部转流式取数再聚合（上游非流式 ~60s 就 504），
        # 拿到 OpenAI JSON 后同样走 anthropic 转换，客户端无感。
        min_tok = int(gw.cfg.get("keepalive_min_tokens") or 1500)
        if int(payload.get("max_tokens") or 0) >= min_tok:
            self._chunked_open()
            try:
                res = self._run_with_keepalive(
                    lambda: self._chat_stream_to_json(payload, {}, model, skey))
            except Exception as exc:                    # noqa: BLE001
                res = (502, json.dumps({"error": {
                    "message": str(exc), "type": "upstream_error", "code": 502}
                }).encode("utf-8"))
            if not res:
                return                                  # 客户端已断开
            status, data = res
            if not data:
                data = json.dumps({"error": {
                    "message": "所有账号都失败了 / all accounts failed",
                    "type": "upstream_error", "code": 502}}).encode("utf-8")
            try:
                obj = json.loads(data.decode("utf-8"))
            except Exception:                           # noqa: BLE001
                obj = None
            if status == 200 and isinstance(obj, dict) and obj.get("choices"):
                out = json.dumps(anth.openai_to_anthropic(obj, model),
                                 ensure_ascii=False).encode("utf-8")
            else:
                out = data                              # 上游错误体原样透传
            self._chunk_write(out)
            self._chunk_close()
            return

        tried: list = []
        last: Tuple[int, bytes] = (0, b"")
        for _ in range(int(gw.cfg.get("max_retries") or 0) + 1):
            try:
                acc = gw.pool.acquire(exclude=tried, model=model,
                                      session_key_value=skey)
            except PoolError as exc:
                gw.log(f"[warn] {exc}")
                break
            tried.append(acc.name)
            started = time.time()
            try:
                status, _h, data = gw.models_client.call(
                    acc.session, "chat/completions", payload, proxy=acc.proxy or None)
            finally:
                gw.pool.release(acc)
            if status == 200:
                obj = json.loads(data.decode("utf-8"))
                usage = obj.get("usage") or {}
                elapsed = time.time() - started
                gw.pool.report_success(acc, usage, model=model)
                gw.usage.record(account=acc.name, model=model, status=200, usage=usage,
                                latency=elapsed, kind="messages", proxy=acc.proxy)
                gw.log(f"[messages] {acc.name} {model} {elapsed:.1f}s "
                       f"http=200 {human_usage(usage)}{_rate(usage, elapsed)}")
                return self._json(200, anth.openai_to_anthropic(obj, model))
            last = (status, data)
            reason = data[:200].decode("utf-8", "replace")
            gw.usage.record(account=acc.name, model=model, status=status,
                            latency=time.time() - started, kind="messages",
                            error=reason, proxy=acc.proxy)
            if status in self.ACCOUNT_FAULT:
                gw.pool.report_failure(acc, status, reason, model=model)
                if skey:
                    gw.pool.sticky.unbind(skey, acc.name)
                gw.log(f"[messages] {acc.name} {model} http={status} → 账号问题，换账号重试")
            else:
                gw.pool.report_failure(acc, status, reason, model=model)
                gw.log(f"[messages] {acc.name} {model} http={status}"
                       f" → 上游异常，换账号重试（不冷却该账号）")
            if status in self.FATAL_REQUEST:
                break
        status, data = last
        return self._json(status or 502, {
            "type": "error",
            "error": {"type": "api_error",
                      "message": data[:500].decode("utf-8", "replace")}})

    def _messages_stream(self, payload: Dict[str, Any], model: str,
                         skey: Optional[str] = None) -> None:
        gw = self.gateway
        tried: list = []
        conn = resp = None
        acc = None

        for _ in range(int(gw.cfg.get("max_retries") or 0) + 1):
            try:
                acc = gw.pool.acquire(exclude=tried, model=model,
                                      session_key_value=skey)
            except PoolError as exc:
                gw.log(f"[warn] {exc}")
                break
            tried.append(acc.name)
            try:
                conn, resp = gw.models_client.stream(
                    acc.session, "chat/completions", payload,
                    proxy=acc.proxy or None)
            except Exception as exc:                    # noqa: BLE001
                gw.pool.release(acc)
                gw.log(f"[messages] {acc.name} {model} stream 连接异常：{exc}")
                gw.usage.record(account=acc.name, model=model, status=0,
                                kind="messages", error=str(exc), stream=True,
                                proxy=acc.proxy)
                conn = resp = None
                continue
            if resp.status == 200:
                break
            data = resp.read()
            conn.close()
            reason = data[:200].decode("utf-8", "replace")
            gw.pool.release(acc)
            if resp.status in self.ACCOUNT_FAULT:
                gw.pool.report_failure(acc, resp.status, reason, model=model)
                if skey:
                    gw.pool.sticky.unbind(skey, acc.name)
                gw.log(f"[messages] {acc.name} {model} stream http={resp.status}"
                       f" → 账号问题，换账号重试")
            else:
                gw.pool.report_failure(acc, resp.status, reason, model=model)
                gw.log(f"[messages] {acc.name} {model} stream http={resp.status}"
                       f" → 上游异常，换账号重试（不冷却该账号）")
            conn = resp = None

        if resp is None:
            return self._json(502, {"type": "error", "error": {
                "type": "api_error", "message": "all accounts failed"}})
        if resp.status != 200:
            data = resp.read()
            conn.close()
            gw.pool.release(acc)
            return self._json(resp.status, {"type": "error", "error": {
                "type": "api_error", "message": data[:500].decode("utf-8", "replace")}})

        translator = anth.StreamTranslator(model)
        started = time.time()
        first_chunk_at: Optional[float] = None
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.close_connection = True

        def emit(event: str, data: Dict[str, Any]) -> None:
            self.wfile.write(
                f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                .encode("utf-8"))
            self.wfile.flush()

        buffer = b""
        try:
            for event, data in translator.start():
                emit(event, data)
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                if first_chunk_at is None:
                    first_chunk_at = time.time()
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    body = line[5:].strip()
                    if not body or body == b"[DONE]":
                        continue
                    try:
                        obj = json.loads(body)
                    except Exception:                   # noqa: BLE001
                        continue
                    for event, data in translator.feed(obj):
                        emit(event, data)
            for event, data in translator.finish():
                emit(event, data)
            elapsed = time.time() - started
            ttfb = (first_chunk_at - started) if first_chunk_at else elapsed
            gw.pool.report_success(acc, translator.usage, model=model)
            gw.usage.record(account=acc.name, model=model, status=200,
                            usage=translator.usage, latency=elapsed, ttfb=ttfb,
                            stream=True, kind="messages", proxy=acc.proxy)
            gw.log(f"[messages] {acc.name} {model} {elapsed:.1f}s "
                   f"http=200 {human_usage(translator.usage)}"
                   f"{_rate(translator.usage, elapsed)} ttfb={ttfb:.2f}s stream")
        except (BrokenPipeError, ConnectionResetError):
            gw.log(f"[messages] {acc.name} {model} client disconnected")
        except Exception as exc:                        # noqa: BLE001
            gw.log(f"[messages] {acc.name} {model} stream 异常：{exc}")
            gw.usage.record(account=acc.name, model=model, status=0,
                            latency=time.time() - started, kind="messages",
                            error=str(exc), stream=True, proxy=acc.proxy)
        finally:
            gw.pool.release(acc)
            try:
                conn.close()
            except Exception:                           # noqa: BLE001
                pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows lets two processes bind the same port with SO_REUSEADDR, which
    # silently sends requests to an old instance.  Only enable it elsewhere.
    allow_reuse_address = (os.name != "nt")

    def handle_error(self, request, client_address):
        """吞掉「客户端断开」类噪音，只把真错误打出来。

        浏览器刷新 / 关标签 / 预连接取消都会在 readline 阶段抛
        ConnectionResetError/ConnectionAbortedError，socketserver 默认会把整段
        堆栈打到 stderr —— 实测 9 小时日志被这种噪音淹没，真正的故障反而看不见。
        """
        import sys as _sys
        exc = _sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def serve(cfg, *, gateway: Optional[Gateway] = None) -> Gateway:
    gw = gateway or Gateway(cfg)
    gw.start()

    handler = type("BoundHandler", (Handler,), {"gateway": gw})
    httpd = Server((str(cfg["host"]), int(cfg["port"])), handler)
    gw.log(f"loomy2api 已启动 / listening → http://{cfg['host']}:{cfg['port']}"
           f"  (OpenAI: /v1/chat/completions · Anthropic: /v1/messages)"
           f"  账号 {len(gw.pool.accounts)} 个，可用 {len(gw.pool.usable())} 个")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        gw.log("收到中断，退出 / interrupted, shutting down")
    finally:
        gw.pool.stop()
        gw.pool.flush()
        httpd.server_close()
    return gw
