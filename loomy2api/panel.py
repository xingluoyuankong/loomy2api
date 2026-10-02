"""Web control panel: HTML page + JSON API.

The page is a single self-contained HTML file (no CDN, no build step) served at
``/panel``; the JSON API lives under ``/api/panel/*`` and reuses the gateway's
API-key gate when ``api_keys`` is configured.

Endpoints
---------
``GET  /panel``                       the page
``GET  /api/panel/state``             accounts + quota + totals (``?refresh=1``
                                      forces a quota refresh)
``POST /api/panel/refresh``           refresh every account's quota
``POST /api/panel/accounts``          add an account (optionally log in now)
``POST /api/panel/accounts/update``   patch loginid / password / enabled
``POST /api/panel/accounts/remove``   delete an account
``POST /api/panel/accounts/renew``    force re-login + quota refresh
``POST /api/panel/accounts/identity`` inspect / regenerate the account identity
``GET  /api/panel/logs``              tail of the gateway log
``GET  /api/panel/usage``             请求级用量（聚合 + 最近流水）
``POST /api/panel/usage/clear``       清空用量台账
``GET  /api/panel/points``            积分构成（逐账号余额/每日/可用 + 流水）
``GET  /api/panel/models``            模型与档位
``GET  /api/panel/jobs``              后台作业状态
``POST /api/panel/jobs/run``          手动触发一个后台作业
``GET  /api/panel/proxies``           代理出口（全局 + 每账号）
``POST /api/panel/accounts/proxy``    设置账号出口代理
``GET/POST /api/panel/config``        读取 / 热更新配置
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import browser
from . import constants as C
from . import wechat
from .account import Account, AccountError
from .login_flow import LoginFlow
from .pool import PoolError

__all__ = ["Panel"]

C_SESSION_EXPIRE_SECONDS = C.SESSION_EXPIRE_SECONDS

WEB_DIR = Path(__file__).resolve().parent / "web"
PANEL_HTML = WEB_DIR / "index.html"
PANEL_JS = WEB_DIR / "app.js"

#: Hidden phone number for the panel: 138****0000
def mask_phone(value: str) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) < 7:
        return str(value or "")
    return f"{digits[:3]}****{digits[-4:]}"


class Panel:
    """Stateful facade over the account pool for the web UI."""

    def __init__(self, gateway):
        self.gw = gateway
        self.log = gateway.log
        #: 跳转登录会话（「添加账号」的 state 表）
        self.flows = LoginFlow(
            ttl=int(gateway.cfg.get("login_flow_ttl_seconds") or 600))
        #: 受控授权窗口（state → AuthBrowser），全自动截获回调用
        self._browsers: Dict[str, browser.AuthBrowser] = {}
        self._browsers_lock = threading.Lock()
        #: 手机号 → 最近一次下发的短信凭据。前端啥都没带时的最后兜底。
        #: **落盘**：网关重启很频繁，内存版会让用户刚收到的验证码直接作废。
        self._last_sms_path = (Path(__file__).resolve().parent.parent
                               / "state" / "last_sms.json")
        self._last_sms: Dict[str, Dict[str, Any]] = self._load_last_sms()

    # --------------------------------------------------- 短信下发记录（落盘）

    def _load_last_sms(self) -> Dict[str, Dict[str, Any]]:
        try:
            raw = self._last_sms_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        now = time.time()
        # 只留 30 分钟内下发的（验证码本身寿命更短）
        return {k: v for k, v in data.items()
                if isinstance(v, dict) and now - float(v.get("at") or 0) < 1800}

    def _save_last_sms(self) -> None:
        try:
            self._last_sms_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._last_sms_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._last_sms, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self._last_sms_path)
        except OSError as exc:                           # pragma: no cover
            self.log(f"[panel] [warn] 短信记录落盘失败：{exc}")

    # ------------------------------------------------------------ helpers

    @property
    def pool(self):
        return self.gw.pool

    def html(self) -> bytes:
        """页面骨架 + **内联 JS**。

        为什么内联：外链再怎么加指纹，只要 HTML 本身被缓存过，浏览器就会去
        请求那个旧指纹 URL —— 用户永远停在旧代码上，而且没有任何提示。
        内联成单文件后，HTML 的 no-store 就是唯一入口，刷新必是最新。
        """
        try:
            body = PANEL_HTML.read_text(encoding="utf-8")
        except OSError:                                  # pragma: no cover
            return b"<h1>loomy2api</h1><p>panel asset missing</p>"
        try:
            js = PANEL_JS.read_text(encoding="utf-8")
        except OSError:                                  # pragma: no cover
            js = "/* panel js missing */"
        ver = self.js_version()
        js = js.replace("</script", "<\\/script")      # 防提前闭合
        inline = ('<script>\n/* loomy2api panel v' + ver + ' */\n'
                  + js + '\n</script>')
        new_body, n = re.subn(r'<script src="/panel/app\.js[^"]*"></script>',
                              lambda _: inline, body, count=1)
        if n != 1:                                       # pragma: no cover
            new_body = body + inline
        footer = (f'\n<!-- panel build {ver} -->').encode("utf-8")
        return new_body.encode("utf-8") + footer

    _js_version_cache: Optional[str] = None

    def js_version(self) -> str:
        """app.js 内容指纹（sha256 前 8 字节）—— 参考项目踩过的坑：index.html
        更新了而浏览器还拿着旧 app.js，就会出现「界面是旧的、接口对不上」。
        """
        if Panel._js_version_cache is None:
            try:
                import hashlib
                Panel._js_version_cache = hashlib.sha256(
                    PANEL_JS.read_bytes()).hexdigest()[:8]
            except OSError:                              # pragma: no cover
                Panel._js_version_cache = "dev"
        return Panel._js_version_cache

    def js(self) -> bytes:
        """面板脚本（独立文件，便于与 index.html 分别缓存/更新）。"""
        try:
            return PANEL_JS.read_bytes()
        except OSError:                                  # pragma: no cover
            return b"/* panel asset missing */"

    def _account_view(self, acc: Account) -> Dict[str, Any]:
        view = acc.public_dict()
        view["loginid_masked"] = mask_phone(acc.loginid)
        view["points_used"] = acc.points_used
        view["mode"] = "password" if (acc.loginid and acc.password) else (
            "session" if acc.session else "empty")
        return view

    def _quota_stale(self, acc: Account) -> bool:
        minutes = float(self.gw.cfg.get("quota_refresh_minutes") or 30)
        return (time.time() - (acc.quota_updated_at or 0)) > minutes * 60

    def _refresh_all(self, only_stale: bool = True) -> None:
        for acc in self.pool.accounts:
            if not acc.session_valid:
                continue
            if only_stale and not self._quota_stale(acc):
                continue
            try:
                self.pool.refresh_quota(acc)
            except Exception as exc:                     # noqa: BLE001
                self.log(f"[panel] 刷新 {acc.name} 额度失败：{exc}")
        self.pool.save()

    # --------------------------------------------------------------- state

    def state(self, *, refresh: bool = False) -> Dict[str, Any]:
        self._refresh_all(only_stale=not refresh)
        accounts = [self._account_view(a) for a in self.pool.accounts]

        # one account can hold two sessions (ours + the client's) — count once
        seen: Dict[str, int] = {}
        for acc in self.pool.accounts:
            if isinstance(acc.available, int):
                key = acc.userid or acc.name
                seen[key] = max(seen.get(key, 0), acc.available)

        return {
            "ok": True,
            "now": int(time.time()),
            "totals": {
                "accounts": len(accounts),
                "usable": len(self.pool.usable()),
                "available": sum(seen.values()),
                "unique_accounts": len(seen),
                "requests": sum(a["requests"] for a in accounts),
                "points_used": sum(a["points_used"] for a in accounts),
            },
            "pool": {
                "sticky": self.pool.sticky.snapshot(),
                "inflight": self.pool.inflight.snapshot(),
                "pick_top_n": self.gw.cfg.get("pick_top_n"),
                "soft_rate_base_seconds": self.gw.cfg.get("soft_rate_base_seconds"),
                "soft_rate_max_seconds": self.gw.cfg.get("soft_rate_max_seconds"),
                "breaker_threshold": self.gw.cfg.get("breaker_threshold"),
                "max_inflight_per_account": self.gw.cfg.get("max_inflight_per_account"),
            },
            "usage": self.gw.usage.summary(window_hours=24)["totals"],
            "config": {
                "upstream": self.gw.cfg["upstream"],
                "strategy": self.gw.cfg.get("strategy"),
                "default_model": self.gw.cfg.get("default_model"),
                "identity_mode": self.gw.cfg.get("identity_mode"),
                "models": len(self.gw.catalogue()),
                "auth_required": bool(self.gw.cfg.api_keys),
                "quota_refresh_minutes": self.gw.cfg.get("quota_refresh_minutes"),
            },
            "accounts": accounts,
        }

    # --------------------------------------------------------------- writes

    def refresh(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._refresh_all(only_stale=False)
        return self.state()

    def add_account(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        loginid = str(payload.get("loginid") or "").strip()
        password = str(payload.get("password") or "")
        session = str(payload.get("session") or "").strip()
        if not name:
            raise PoolError("账号名不能为空 / name is required")
        if not loginid and not session:
            raise PoolError("需要手机号和密码，或直接提供一个 session / "
                            "provide phone+password, or a session")
        if loginid and not password and not session:
            raise PoolError("只给手机号时还需要密码 / password required with phone")

        acc = self.pool.add_account(name, loginid=loginid, password=password,
                                    session=session,
                                    proxy=str(payload.get("proxy") or ""))
        result: Dict[str, Any] = {"ok": True, "name": acc.name,
                                  "identity": acc.identity_view()}
        if loginid and password and payload.get("login", True):
            try:
                self.pool.ensure_session(acc, force=True)
                self.pool.refresh_quota(acc)
                result["logged_in"] = True
                result["userid"] = acc.userid
            except (AccountError, PoolError) as exc:
                result["logged_in"] = False
                result["error"] = str(exc)
            self.pool.save(force=True)
        self.pool.save(force=True)
        result["state"] = self.state()
        return result

    def update_account(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        fields: Dict[str, Any] = {}
        if "loginid" in payload:
            fields["loginid"] = payload["loginid"]
        if payload.get("password"):
            fields["password"] = payload["password"]
        if "enabled" in payload:
            fields["enabled"] = bool(payload["enabled"])
        acc = self.pool.update_account(name, **fields)
        if payload.get("login") and acc.loginid and acc.password:
            try:
                self.pool.ensure_session(acc, force=True)
                self.pool.refresh_quota(acc)
            except (AccountError, PoolError) as exc:
                self.log(f"[panel] {name} 重新登录失败：{exc}")
            self.pool.save(force=True)
        return {"ok": True, "name": acc.name, "state": self.state()}

    def remove_account(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        removed = self.pool.remove_account(name)
        if not removed:
            raise PoolError(f"没有名为 {name} 的账号 / no such account: {name}")
        self.log(f"[panel] 已删除账号 {name}")
        return {"ok": True, "removed": name, "state": self.state()}

    def renew_account(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        acc = self.pool.get(name)
        if acc is None:
            raise PoolError(f"没有名为 {name} 的账号 / no such account: {name}")
        result: Dict[str, Any] = {"ok": True, "name": name}
        try:
            self.pool.ensure_session(acc, force=True)
            self.pool.refresh_quota(acc)
            # 手动续期 = 用户认为这个号没问题：冷却/熔断/软退避一并清零
            acc.cooldown_until = 0.0
            acc.cool_kind = ""
            acc.soft_streak = 0
            acc.breaker_until = 0.0
            acc.breaker_fails = 0
            acc.breaker_retries = 0
            acc.model_cooldowns.clear()
            result["userid"] = acc.userid
            result["session_days_left"] = acc.days_left
        except (AccountError, PoolError) as exc:
            result["ok"] = False
            result["error"] = str(exc)
        self.pool.save(force=True)
        result["state"] = self.state()
        return result

    def identity(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Inspect (``regenerate: false``) or rebind (``regenerate: true``)."""
        name = str(payload.get("name") or "").strip()
        acc = self.pool.get(name)
        if acc is None:
            raise PoolError(f"没有名为 {name} 的账号 / no such account: {name}")
        if payload.get("regenerate"):
            self.pool.rebind_identity(name)
            self.log(f"[panel] {name} 已重新绑定设备标识 "
                     f"devid={acc.identity.get('devid')}")
        elif not acc.identity:
            self.pool.client.ensure_identity(acc)
            self.pool.save(force=True)
        return {"ok": True, "name": name, "identity": acc.identity_view(),
                "state": self.state()}

    def logs(self, lines: int = 200) -> Dict[str, Any]:
        path = self.gw.log.path
        lines = max(10, min(int(lines or 200), 2000))
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            content = []
        return {"ok": True, "path": str(path), "lines": content[-lines:]}

    # ------------------------------------------------- 用量 / 积分 / 模型

    def usage(self, limit: int = 100, window_hours: int = 24,
              upstream: bool = False) -> Dict[str, Any]:
        """面板「用量」页：聚合 + 最近流水（可选附上游真实扣费对照）。"""
        out = {
            "ok": True,
            "summary": self.gw.usage.summary(window_hours=window_hours),
            "recent": self.gw.usage.recent(limit=limit),
        }
        if upstream:
            calls = points = 0
            by_model: Dict[str, Dict[str, Any]] = {}
            errors: List[str] = []
            for acc in self.pool.accounts:
                if not acc.enabled or not acc.session:
                    continue
                detail = self.pool.points_breakdown(acc)
                if detail.get("error"):
                    errors.append(f"{acc.name}: {detail['error'][:120]}")
                    continue
                for m, stat in (detail.get("model_pricing") or {}).items():
                    agg = by_model.setdefault(
                        m, {"calls": 0, "points": 0, "min": 999, "max": 0})
                    agg["calls"] += stat["calls"]
                    agg["points"] += round(stat["avg"] * stat["calls"])
                    agg["min"] = min(agg["min"], stat["min"])
                    agg["max"] = max(agg["max"], stat["max"])
                    calls += stat["calls"]
                    points += round(stat["avg"] * stat["calls"])
            out["upstream"] = {"calls": calls, "points": points,
                               "by_model": by_model,
                               "errors": errors}
        return out

    def clear_usage(self) -> Dict[str, Any]:
        removed = self.gw.usage.clear()
        return {"ok": True, "cleared": removed}

    def points(self, *, refresh: bool = False) -> Dict[str, Any]:
        """面板「积分构成」页：逐账号的余额/每日/可用 + 流水。"""
        if refresh:
            self._refresh_all(only_stale=False)
        rows: List[Dict[str, Any]] = []
        totals = {"balance": 0, "daily_balance": 0, "available": 0}
        for acc in self.pool.accounts:
            detail = self.pool.points_breakdown(acc)
            row = {
                "name": acc.name,
                "loginid_masked": mask_phone(acc.loginid),
                "enabled": acc.enabled,
                "balance": acc.balance,
                "daily_balance": acc.daily_balance,
                "available": acc.available,
                "points_used": acc.points_used,
                "daily_consumed": acc.daily_consumed,
                "requests": acc.requests,
                "quota_updated_at": acc.quota_updated_at,
                "error": detail.get("error", ""),
                "records": detail.get("records") or [],
                "expiring": detail.get("expiring"),
                "total": detail.get("total"),
                "model_pricing": detail.get("model_pricing") or {},
                "daily_quota": detail.get("daily_quota"),
                "daily_consumed": detail.get("daily_consumed"),
            }
            for key in totals:
                value = row.get(key)
                if isinstance(value, int):
                    totals[key] += value
            rows.append(row)
        return {"ok": True, "totals": totals, "accounts": rows}

    def models_view(self) -> Dict[str, Any]:
        """面板「模型与档位」页：按倍率分档的模型目录。"""
        catalogue = self.gw.catalogue()
        rows = []
        for item in catalogue:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            mult = item.get("multiplier")
            if mult is None and "x" in name:
                try:
                    mult = float(name[name.rfind("x") + 1:].rstrip(")"))
                except Exception:                       # noqa: BLE001
                    mult = None
            caps = item.get("capabilities") or {}
            modalities = item.get("modalities") or caps.get("input_modalities") or []
            rows.append({
                "id": item.get("id"),
                "name": name,
                "multiplier": mult,
                "context_length": item.get("context_length") or caps.get("context_length"),
                "modalities": modalities,
                "reasoning": bool(caps.get("reasoning")),
                "tools": bool(caps.get("function_calling")),
                "vision": bool(caps.get("vision")),
                "streaming": bool(caps.get("streaming")),
                "type": item.get("type") or "chat",
                "reasoning_efforts": item.get("reasoning_efforts") or [],
                "default_reasoning_effort": item.get("default_reasoning_effort") or "",
                "is_default": item.get("id") == self.gw.cfg.get("default_model"),
            })
        rows.sort(key=lambda r: (r["multiplier"] if isinstance(r["multiplier"], (int, float))
                                 else 999))
        return {"ok": True, "default_model": self.gw.cfg.get("default_model"),
                "models": rows}

    # --------------------------------------------------------- 代理出口

    def proxies(self) -> Dict[str, Any]:
        """面板「代理出口」页：全局代理 + 每账号代理 + 生效值。"""
        global_proxy = str(self.gw.cfg.get("proxy") or "")
        accounts = []
        for acc in self.pool.accounts:
            accounts.append({
                "name": acc.name,
                "loginid_masked": mask_phone(acc.loginid),
                "proxy": acc.proxy or "",
                "effective": acc.proxy or global_proxy or "(直连)",
                "source": "账号级" if acc.proxy else ("全局" if global_proxy else "直连"),
                "enabled": acc.enabled,
            })
        return {"ok": True, "global_proxy": global_proxy or "",
                "global_effective": global_proxy or "(直连)", "accounts": accounts}

    def set_proxy(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        proxy = str(payload.get("proxy") or "").strip()
        acc = self.pool.set_proxy(name, proxy)
        self.log(f"[panel] {name} 出口代理 → {proxy or '(回落到全局/直连)'}")
        return {"ok": True, "name": name, "proxy": acc.proxy,
                "proxies": self.proxies()}

    # --------------------------------------------------------- 任务中心

    def run_job(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        key = str(payload.get("key") or "").strip()
        job = self.pool.run_job(key)
        self.log(f"[panel] 手动触发作业 {key}")
        return {"ok": True, "job": job, "jobs": self.pool.jobs_snapshot(),
                "state": self.state()}

    # ------------------------------------------------- 跳转登录（添加账号）

    def _unique_name(self, wanted: str, phone: str) -> str:
        """决定这次跳转登录写进哪个账号。

        规则：同名账号**且手机号一致** → 视为「重新登录这个账号」（覆盖 session、
        清冷却）；否则找一个不冲突的名字（`base-2`、`base-3`…）。
        同名不同号时绝不复用——那会把另一个账号的登录态冲掉。
        """
        base = wanted.strip() or f"loomy-{phone[-4:] if len(phone) >= 4 else phone}"
        existing = self.pool.get(base)
        if existing is None or (phone and existing.loginid == phone):
            return base
        for i in range(2, 100):
            candidate = f"{base}-{i}"
            other = self.pool.get(candidate)
            if other is None or (phone and other.loginid == phone):
                return candidate
        return f"{base}-{int(time.time()) % 10000}"

    def login_start(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """创建一次短信登录会话（面板内直接输验证码，不跳转）。"""
        flow = self.flows.start(phone=str(payload.get("phone") or "").strip(),
                                name=str(payload.get("name") or "").strip())
        self.flows.update(flow["state"], mode="sms")
        return {"ok": True, "state": flow["state"],
                "expires_in": self.flows.ttl}

    def login_send(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """在登录页发验证码（也可由面板预发）。

        会话丢了（服务重启 / TTL 到点 / 前端没带上 state）就**就地补一个**，
        不再把用户挡在「登录会话不存在或已过期」这句话上——发短信这件事
        本身只依赖手机号，会话只是用来记 msgid 的簿记。
        """
        state = str(payload.get("state") or "").strip()
        phone = str(payload.get("phone") or "").strip()
        flow = self.flows.get(state) if state else None
        if flow is None:
            flow = self.flows.start(phone=phone,
                                    name=str(payload.get("name") or "").strip())
            state = flow["state"]
            self.log(f"[panel] 短信登录：会话不存在，已就地重建"
                     f"（state={state[:8]}…）")
        else:
            phone = phone or str(flow.get("phone") or "").strip()
        if not phone:
            raise PoolError("请先填写手机号")
        try:
            resp = self.pool.client.send_sms_code(phone)
        except AccountError as exc:
            self.flows.fail(state, str(exc))
            raise PoolError(f"验证码发送失败：{exc}") from exc
        msgid = str((resp.get("data") or {}).get("msgid")
                    or resp.get("msgid") or "")
        self.flows.update(state, phone=phone, msgid=msgid,
                          status="sent", error="")
        masked = f"{phone[:3]}****{phone[-4:]}" if len(phone) >= 7 else phone
        # 上游原文留痕：短信发不出去时能一眼看出是参数错、限流还是风控
        self.log(f"[panel] 验证码已下发 {masked} msgid={msgid[:12] or '(空)'}… "
                 f"上游={str(resp)[:200]}")
        if not msgid:
            self.log(f"[panel] [warn] 上游没返回 msgid，验证码可能没发出去")
        else:
            self._last_sms[phone] = {"msgid": msgid, "state": state,
                                     "phone": phone, "at": time.time()}
            if len(self._last_sms) > 50:               # 只留最近几十个
                oldest = min(self._last_sms.items(), key=lambda kv: kv[1]["at"])
                self._last_sms.pop(oldest[0], None)
            self._save_last_sms()
        # 把（可能被重建过的）state 回给前端，保证下一步 submit 用得上
        return {"ok": True, "msgid": msgid, "phone": phone, "state": state}

    def login_submit(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """登录页提交验证码 → 换 session → 落盘 + 热加载进池。

        phone / msgid 以**前端带来的为准**：会话只是簿记，服务一重启内存就空了，
        不该让用户因为簿记丢失而把收到的验证码作废。只要 phone+msgid+code
        齐全就能登录完。
        """
        state = str(payload.get("state") or "").strip()
        code = str(payload.get("code") or "").strip()
        flow = self.flows.get(state) if state else None
        if flow is None and state:
            self.log(f"[panel] 短信登录：提交时会话已不在（state={state[:8]}…），"
                     f"改用前端带来的 phone/msgid")
        msgid = str((flow or {}).get("msgid") or "").strip()
        phone = str((flow or {}).get("phone") or "").strip()
        msgid = str(payload.get("msgid") or msgid).strip()
        phone = str(payload.get("phone") or phone).strip()
        if not phone or not msgid:
            # 前端没带全（旧缓存 JS / 刷新丢了变量）就用服务端最近一次下发的记录补。
            # 用户刚收到短信，那条记录就是他的 —— 没必要因为前端丢变量把人挡回去。
            rec = self._last_sms.get(phone) if phone else None
            if rec is None and self._last_sms:
                rec = max(self._last_sms.values(), key=lambda r: r["at"])
            if rec:
                phone = phone or str(rec.get("phone") or "")
                msgid = msgid or str(rec.get("msgid") or "")
                self.log(f"[panel] 短信登录：前端没带全，用最近一次下发的记录补上"
                         f"（{phone[:3]}****{phone[-4:] if len(phone) >= 4 else ''}）")
        # 观测：这条日志就是为了「提交报错时能一眼看出请求里到底带了什么」
        self.log(f"[panel] 提交验证码：state={'有' if state else '空'} "
                 f"phone={mask_phone(phone) if phone else '空'} "
                 f"msgid={'有(' + str(len(msgid)) + '位)' if msgid else '空'} "
                 f"code={len(code)}位 "
                 f"payload键={sorted(k for k in payload.keys())}")
        if not phone:
            raise PoolError("请先填手机号")
        if not code:
            raise PoolError("请输入验证码")
        # 不校验 msgid：用户都已经收到短信了，没有理由替上游把这一步拦下来。
        # 拿不到就传空，让上游自己去判断 —— 它要是不认，会给出明确错误。
        if flow is None:
            flow = self.flows.start(phone=phone, name="")
            state = flow["state"]
        name_hint = str(flow.get("name") or payload.get("name") or "")
        self.flows.update(state, phone=phone, msgid=msgid, status="verifying",
                          error="")
        # 登录是风控重点行为：从「收到短信」到「提交」加一点人类延迟，
        # 秒回验证码（<1s）是机器特征
        time.sleep(random.uniform(1.0, 3.5))
        try:
            got = self.pool.client.login_by_sms(phone, code, msgid)
        except AccountError as exc:
            self.flows.fail(state, str(exc))
            raise PoolError(f"登录失败：{exc}") from exc

        session = got.get("session") or ""
        userid = str(got.get("userid") or "")
        name = self._unique_name(name_hint, phone)
        acc = self.pool.get(name)
        if acc is None:
            acc = self.pool.add_account(name, loginid=phone, session=session,
                                        userid=userid)
        else:
            acc.loginid, acc.session, acc.userid = phone, session, userid
            acc.obtained_at = int(time.time())
            acc.expire_at = acc.obtained_at + C_SESSION_EXPIRE_SECONDS
        # 新登录 = 这个号是好的：清掉旧冷却
        self._clear_cooldown(acc)
        self.pool.refresh_quota(acc)          # 失败也不影响登录结果
        self.pool.save(force=True)
        self.flows.finish(state, account=acc.name, userid=acc.userid, phone=phone)
        self.log(f"[panel] 跳转登录完成：{acc.name} userid={userid} "
                 f"可用 {acc.available} —— 已热加载进池")
        return {"ok": True, "account": acc.name, "userid": userid,
                "available": acc.available, "balance": acc.balance,
                "daily_balance": acc.daily_balance,
                "state": self.state()}

    def checkin(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """每日签到（上游 ``POST /points/first-login``，幂等：重复调用返回已处理）。"""
        results = []
        first = True
        for acc in self.pool.accounts:
            if not acc.enabled or not acc.session:
                continue
            if not first:
                self._jitter()
            first = False
            ident = acc.identity or {}
            entry: Dict[str, Any] = {"name": acc.name}
            try:
                payload = self.gw.pool.gateway.points_first_login(
                    acc.session, proxy=acc.proxy or None,
                    device_id=str(ident.get("campus_device_id") or ""))
                data = payload.get("data") or {}
                entry.update({
                    "ok": payload.get("code") == "000000",
                    "already": bool(data.get("alreadyProcessed")),
                    "balance": data.get("currentBalance"),
                    "permanent": data.get("permanentBalance"),
                    "daily": data.get("dailyBalance"),
                    "daily_quota": data.get("dailyQuota"),
                    "cycle": data.get("dailyCycleDate"),
                    "desc": payload.get("desc") or "",
                })
                self.log(f"[panel] 签到 {acc.name}: "
                         f"{'今日已领' if entry['already'] else '已签到'} "
                         f"余额 {entry.get('balance')}")
            except Exception as exc:                     # noqa: BLE001
                entry.update({"ok": False, "error": str(exc)[:200]})
                self.log(f"[panel] 签到 {acc.name} 失败：{exc}")
            results.append(entry)
        for acc in self.pool.accounts:
            if acc.enabled and acc.session:
                try:
                    self.pool.refresh_quota(acc)
                except Exception:                        # noqa: BLE001
                    pass
        return {"ok": True, "results": results}

    # ------------------------------------------ 平台任务（客户端任务系统）

    @staticmethod
    def _jitter(low: float = 0.6, high: float = 2.4) -> None:
        """多账号批量操作间的随机抖动：把「机器节奏」打散成「人的节奏」。

        上游做时序统计时，等间隔请求是最容易聚类的特征。
        只用于签到/兑换/绑定这类低频批量操作，**绝不**进 chat 热路径。
        """
        time.sleep(random.uniform(low, high))

    def _gw(self) -> Any:
        """ModelGateway 实例（Panel.gw 是 server.Gateway，真正的上游客户端在 pool）。"""
        return self.gw.pool.gateway

    def platform_tasks(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """平台客户端任务聚合：签到 + 激活状态 + 本账号邀请码。"""
        acc = next((a for a in self.pool.accounts
                    if a.enabled and a.session), None)
        if acc is None:
            raise PoolError("没有可用账号")
        gw = self._gw()
        out: Dict[str, Any] = {"account": acc.name}
        try:
            payload_ = gw.points_first_login(
                acc.session, proxy=acc.proxy or None,
                device_id=str((acc.identity or {}).get("campus_device_id") or ""))
            data = payload_.get("data") or {}
            out["checkin"] = {
                "ok": payload_.get("code") == "000000",
                "already": bool(data.get("alreadyProcessed")),
                "balance": data.get("currentBalance"),
                "permanent": data.get("permanentBalance"),
                "daily": data.get("dailyBalance"),
                "daily_quota": data.get("dailyQuota"),
                "daily_consumed": data.get("dailyConsumed"),
                "cycle": data.get("dailyCycleDate"),
                "invite_codes": data.get("invitationCodes") or [],
            }
        except Exception as exc:                         # noqa: BLE001
            out["checkin"] = {"ok": False, "error": str(exc)[:200]}
        try:
            act = gw.query_activation(acc.session, proxy=acc.proxy or None)
            out["activation"] = act.get("data") if isinstance(act.get("data"), dict) \
                else {"raw": act.get("desc") or str(act)[:160]}
        except Exception as exc:                         # noqa: BLE001
            out["activation"] = {"error": str(exc)[:200]}
        return {"ok": True, **out}

    def redeem(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """兑换积分码（上游 redemption-codes/redeem）。"""
        code = str((payload or {}).get("code") or "").strip()
        if not code:
            raise PoolError("请输入兑换码")
        acc = next((a for a in self.pool.accounts
                    if a.enabled and a.session), None)
        if acc is None:
            raise PoolError("没有可用账号")
        result = self._gw().redeem_code(acc.session, code,
                                        proxy=acc.proxy or None)
        ok = result.get("code") == "000000"
        try:
            self.pool.refresh_quota(acc)
        except Exception:                                # noqa: BLE001
            pass
        self.log(f"[panel] 兑换码 {acc.name}: {'成功' if ok else '失败'} "
                 f"{str(result.get('desc'))[:60]}")
        return {"ok": ok, "desc": result.get("desc") or "",
                "data": result.get("data") or {}, "account": acc.name}

    def bind_invite(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """绑定邀请码（上游 points/activation）。"""
        invite = str((payload or {}).get("invite_code")
                     or (payload or {}).get("inviteCode") or "").strip()
        if not invite:
            raise PoolError("请输入邀请码")
        acc = next((a for a in self.pool.accounts
                    if a.enabled and a.session), None)
        if acc is None:
            raise PoolError("没有可用账号")
        result = self._gw().bind_invite_code(
            acc.session, invite,
            device_id=str((acc.identity or {}).get("campus_device_id") or ""),
            proxy=acc.proxy or None)
        ok = result.get("code") == "000000"
        self.log(f"[panel] 绑定邀请码 {acc.name}: {'成功' if ok else '失败'} "
                 f"{str(result.get('desc'))[:60]}")
        return {"ok": ok, "desc": result.get("desc") or "",
                "data": result.get("data") or {}, "account": acc.name}

    def login_poll(self, state: str) -> Dict[str, Any]:
        view = self.flows.poll_view(state)
        view["browser"] = self.browser_snapshot(state)
        return {"ok": True, **view}

    # ----------------------------------------- 微信扫码（服务端取码，全自动）

    def login_wechat_qr_start(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """生成二维码（服务端拉授权页取 uuid），返回可直接内嵌的图片地址。

        这是主链路：面板显示二维码 → 轮询本接口 → 服务端长轮询微信取 code →
        换 session。**用户不需要跳浏览器、也不需要复制任何东西。**
        """
        flow = self.flows.start(name=str(payload.get("name") or "").strip())
        state = flow["state"]
        try:
            qr = wechat.fetch_login_qr(state=state)
        except Exception as exc:                        # noqa: BLE001
            self._flow_fail(state, f"获取微信二维码失败：{exc}")
            raise PoolError(f"获取微信二维码失败：{exc}") from exc
        self.flows.update(state, mode="wechat-qr", uuid=qr["uuid"], status="pending")
        self.log(f"[panel] 微信二维码已生成（state={state[:8]}… uuid={qr['uuid']}）")
        return {"ok": True, "state": state, "qr_url": qr["qr_url"],
                "link": self._panel_base() + "/panel/login-link?state=" + state,
                "expires_in": 240}

    def _panel_base(self) -> str:
        host = str(self.gw.cfg.get("host") or "127.0.0.1")
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"     # 给用户的链接必须能直接打开
        return f"http://{host}:{int(self.gw.cfg.get('port') or 17890)}"

    def login_link_page(self) -> bytes:
        """「登录链接」的落地页：微信扫码 / 手机号+密码，完成后自动回调面板。"""
        try:
            return (WEB_DIR / "login-link.html").read_bytes()
        except OSError:                                  # pragma: no cover
            return b"<h1>loomy2api</h1><p>login link page missing</p>"

    def login_wechat_qr_poll(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """轮询扫码状态；确认后服务端直接取 code 并完成登录。"""
        state = str(payload.get("state") or "").strip()
        flow = self.flows.get(state)
        if flow is None:
            raise PoolError("登录会话不存在或已过期，请重新生成二维码")
        uuid = flow.get("uuid") or ""
        if not uuid:
            raise PoolError("这次会话没有二维码，请重新生成")

        last = str(payload.get("last") or "")
        try:
            status = wechat.poll_login(uuid, last=last)
        except Exception as exc:                        # noqa: BLE001
            # 长轮询本身超时/断连不算失败，让前端下一轮继续
            return {"ok": True, "errcode": wechat.ERR_WAITING, "status": "waiting",
                    "text": f"轮询异常（继续重试）：{str(exc)[:80]}"}

        errcode = status["errcode"]
        if errcode == wechat.ERR_EXPIRED:
            self._flow_fail(state, "二维码已过期")
            return {"ok": True, "errcode": errcode, "status": "expired",
                    "text": "二维码已过期，请重新生成"}
        if errcode == wechat.ERR_CANCELLED:
            self._flow_fail(state, "用户在手机上取消了授权")
            return {"ok": True, "errcode": errcode, "status": "cancelled",
                    "text": "已取消授权，可重新扫码"}

        if errcode != wechat.ERR_CONFIRMED:
            # 408 等待扫码 / 404 已扫待确认
            return {"ok": True, "errcode": errcode,
                    "status": "scanned" if errcode == wechat.ERR_SCANNED else "waiting",
                    "text": status["text"]}

        code = status["code"]
        if not code:
            self._flow_fail(state, f"微信已确认但没给 code：{status['raw']}")
            raise PoolError("微信已确认但没返回 code，请重新扫码")

        self.log(f"[panel] 微信已确认（state={state[:8]}…），服务端取到 code，开始换 session")
        return self._wechat_exchange(state, code)

    def _wechat_exchange(self, state: str, code: str) -> Dict[str, Any]:
        """code → bindAuth → bindSkip(已绑) / needs_phone(未绑)。"""
        flow = self.flows.get(state) or {}
        self.flows.update(state, status="verifying", error="")
        try:
            data = self.pool.client.bind_auth_third_account(code)
        except AccountError as exc:
            self._flow_fail(state, f"微信鉴权失败：{exc}")
            raise PoolError(f"微信鉴权失败：{exc}") from exc
        self.log(f"[panel] 微信鉴权返回：{str(data)[:200]}")

        rcode = str(data.get("rcode") or "")
        bind = str(data.get("bind") or data.get("bindStatus") or "")
        if not rcode:
            msg = f"微信鉴权未返回 rcode：{str(data)[:160]}"
            self._flow_fail(state, msg)
            raise PoolError(msg)
        self.flows.update(state, rcode=rcode)

        if bind in ("1", "true", "True"):
            self.log("[panel] 微信已绑手机号（bind=1），直接换 session")
            try:
                got = self.pool.client.bind_skip(rcode)
            except AccountError as exc:
                self._flow_fail(state, f"换 session 失败：{exc}")
                raise PoolError(f"换 session 失败：{exc}") from exc
            return self._finish_wechat_login(state, got)

        self.log(f"[panel] 该微信未绑手机号（bind={bind or '空'}），进入绑手机步骤")
        self.flows.update(state, status="needs_phone", error="")
        return {"ok": True, "needs_phone": True, "rcode": rcode,
                "status": "needs_phone",
                "message": "该微信还没绑手机号，绑定后即可完成注册/登录"}

    # ----------------------------------------- 微信扫码（浏览器兜底）

    def login_wechat_start(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """生成微信扫码授权链接（客户端同款 qrconnect 流程）。"""
        flow = self.flows.start(name=str(payload.get("name") or "").strip())
        url = wechat.build_auth_url(flow["state"])
        self.flows.update(flow["state"], mode="wechat")
        self.log(f"[panel] 已生成微信扫码授权链接（state={flow['state'][:8]}…）")
        return {"ok": True, "state": flow["state"], "url": url,
                "hint": wechat.CALLBACK_HINT, "expires_in": self.flows.ttl}

    def _flow_fail(self, state: str, msg: str) -> None:
        """统一记录跳转登录失败 —— 失败必须留痕，否则用户卡在哪完全不可见。

        state 可能压根不存在（过期/伪造），这时 flows.fail 会抛 FlowError；
        记账失败不能盖过真正的错误，所以吞掉它。
        """
        try:
            self.flows.fail(state, msg)
        except Exception:                                # noqa: BLE001
            pass
        self.log(f"[panel] 跳转登录失败（state={str(state)[:8]}…）：{msg}")

    def login_wechat_complete(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """用户把回调链接粘回来 → 解出 code → 换 session 或进入绑手机步骤。"""
        state = str(payload.get("state") or "").strip()
        raw = str(payload.get("callback") or "")
        flow = self.flows.get(state)
        if flow is None:
            self._flow_fail(state, "登录会话不存在或已过期")
            raise PoolError("登录会话不存在或已过期，请回面板重新生成链接")
        self.log(f"[panel] 微信回调已收到（state={state[:8]}…，{len(raw)} 字符），开始解析")
        return self._wechat_from_callback(state, raw)

    # ------------------------------------- 受控浏览器（全自动，无需复制粘贴）

    def login_wechat_browser(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """开一个受控 Chromium 打开授权页，后台轮询直到截获回调 —— 用户只需扫码。

        这是官方客户端做法的等价实现：客户端用 Electron BrowserWindow 的
        ``will-redirect`` 截跳转，这里用 CDP 的 ``/json`` 读窗口当前地址。
        """
        exe = browser.find_chromium()
        if not exe:
            raise PoolError(
                "没找到 Chrome / Edge，无法自动截获回调。"
                "请改用「短信验证码」方式，或按提示在 404 页面复制地址栏后粘贴。")
        flow = self.flows.start(name=str(payload.get("name") or "").strip())
        state = flow["state"]
        self.flows.update(state, mode="browser")
        url = wechat.build_auth_url(state)
        br = browser.AuthBrowser(
            url, exe=exe,
            timeout=float(self.gw.cfg.get("wechat_browser_timeout") or 300),
            on_result=lambda outcome: self._wechat_browser_done(state, outcome),
            logger=self.log)
        with self._browsers_lock:
            self._browsers[state] = br
        try:
            br.start()
        except browser.BrowserError as exc:
            with self._browsers_lock:
                self._browsers.pop(state, None)
            self._flow_fail(state, str(exc))
            raise PoolError(str(exc)) from exc
        self.log(f"[panel] 已打开受控授权窗口（state={state[:8]}…，{exe}）")
        return {"ok": True, "state": state, "mode": "browser",
                "url": url, "browser": br.snapshot()}

    def _wechat_browser_done(self, state: str, outcome: Dict[str, Any]) -> None:
        """受控浏览器线程回调：拿到回调就完成登录，否则把原因写进 flow。"""
        with self._browsers_lock:
            self._browsers.pop(state, None)
        status = outcome.get("status")
        if status == "done":
            url = (outcome.get("result") or {}).get("url") or ""
            self.log(f"[panel] 受控窗口截获回调（state={state[:8]}…），开始换 session")
            try:
                self._wechat_from_callback(state, url)
            except Exception as exc:                     # noqa: BLE001
                self.log(f"[panel] 受控窗口换 session 失败：{exc}")
            return
        reason = {"cancelled": "授权窗口被关闭（已取消）",
                  "timeout": "等待扫码超时，请重试",
                  "error": outcome.get("error") or "授权失败"}.get(status, "授权未完成")
        self.log(f"[panel] 受控窗口结束（state={state[:8]}…）：{reason}")
        self._flow_fail(state, reason)

    def login_wechat_cancel(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """关掉受控授权窗口（用户点了取消 / 关闭弹窗）。"""
        state = str(payload.get("state") or "").strip()
        with self._browsers_lock:
            br = self._browsers.pop(state, None)
        if br:
            br.stop()
            self.log(f"[panel] 已关闭受控授权窗口（state={state[:8]}…）")
        return {"ok": True, "state": state}

    def browser_snapshot(self, state: str) -> Dict[str, Any]:
        with self._browsers_lock:
            br = self._browsers.get(state)
        return br.snapshot() if br else {}

    # ------------------------------------------------------ 微信回调核心

    def _wechat_from_callback(self, state: str, raw: str) -> Dict[str, Any]:
        """把一条回调 URL 换成登录态（受控浏览器与手动粘贴共用这一段）。"""
        try:
            parsed = wechat.parse_callback(raw)
        except ValueError as exc:
            self._flow_fail(state, str(exc))
            raise PoolError(str(exc)) from exc
        # state 是我们自己签发的：对不上说明粘错了/被串改
        if parsed.get("state") and parsed["state"] != state:
            msg = (f"state 不匹配：回调里是 {parsed['state'][:12]}…，"
                   f"本次会话是 {state[:12]}…（多半是上一次的链接）")
            self._flow_fail(state, msg)
            raise PoolError(msg)

        self.flows.update(state, status="verifying", error="")
        try:
            data = self.pool.client.bind_auth_third_account(parsed["code"])
        except AccountError as exc:
            self._flow_fail(state, f"微信鉴权失败：{exc}")
            raise PoolError(f"微信鉴权失败：{exc}") from exc
        self.log(f"[panel] 微信鉴权返回：{str(data)[:200]}")

        rcode = str(data.get("rcode") or "")
        bind = str(data.get("bind") or data.get("bindStatus") or "")
        self.flows.update(state, rcode=rcode)
        if not rcode:
            msg = f"微信鉴权未返回 rcode：{str(data)[:160]}"
            self._flow_fail(state, msg)
            raise PoolError(msg)

        if bind in ("1", "true", "True"):
            # 该微信已绑过手机号 → 直接换 session
            self.log("[panel] 微信已绑手机号（bind=1），尝试直接换 session")
            try:
                got = self.pool.client.bind_skip(rcode)
            except AccountError as exc:
                self._flow_fail(state, f"换 session 失败：{exc}")
                raise PoolError(f"换 session 失败：{exc}") from exc
            return self._finish_wechat_login(state, got)

        # bind=0：新微信/未绑手机 → 需要绑手机号（这一步同时也是「注册」）
        self.log(f"[panel] 该微信未绑手机号（bind={bind or '空'}），进入绑手机步骤")
        self.flows.update(state, status="needs_phone", error="")
        return {"ok": True, "needs_phone": True, "rcode": rcode,
                "message": "该微信还没绑手机号，绑定后即可完成注册/登录"}

    def login_wechat_bind_send(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """绑手机流程第 2 步：发验证码。"""
        state = str(payload.get("state") or "").strip()
        flow = self.flows.get(state)
        if flow is None:
            raise PoolError("登录会话不存在或已过期，请回面板重新生成链接")
        rcode = flow.get("rcode") or ""
        if not rcode:
            raise PoolError("请先扫码完成微信授权")
        phone = str(payload.get("phone") or "").strip()
        if not phone:
            raise PoolError("请填写手机号")
        try:
            data = self.pool.client.bind_send_msg(rcode, phone)
        except AccountError as exc:
            self.flows.fail(state, str(exc))
            raise PoolError(f"验证码发送失败：{exc}") from exc
        msgid = str(data.get("msgid") or "")
        self.flows.update(state, msgid=msgid, phone=phone, error="")
        self.log(f"[panel] 微信绑手机：验证码已下发 {phone[:3]}****{phone[-4:]} "
                 f"msgid={msgid[:12]}…")
        return {"ok": True, "msgid": msgid, "phone": phone}

    def login_wechat_bind_submit(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """绑手机流程第 3 步：验码 → 完成绑定 + 登录。"""
        state = str(payload.get("state") or "").strip()
        code = str(payload.get("code") or "").strip()
        flow = self.flows.get(state)
        if flow is None:
            raise PoolError("登录会话不存在或已过期，请回面板重新生成链接")
        if not flow.get("rcode"):
            raise PoolError("请先扫码完成微信授权")
        if not flow.get("msgid"):
            raise PoolError("还没有发送验证码，请先点「发送验证码」")
        if not code:
            raise PoolError("请输入验证码")
        self.flows.update(state, status="verifying", error="")
        try:
            got = self.pool.client.bind_check_code(flow["rcode"], code,
                                                   flow["msgid"])
        except AccountError as exc:
            self.flows.fail(state, str(exc))
            raise PoolError(f"绑定失败：{exc}") from exc
        return self._finish_wechat_login(state, got)

    def _finish_wechat_login(self, state: str, got: Dict[str, Any]) -> Dict[str, Any]:
        """微信链路拿到 session 之后的收尾（与短信链路共用）。"""
        flow = self.flows.get(state) or {}
        session = got.get("session") or ""
        if not session:
            msg = f"登录未返回 session：{str(got)[:160]}"
            self.flows.fail(state, msg)
            raise PoolError(msg)
        userid = str(got.get("userid") or "")
        phone = str(got.get("phone") or flow.get("phone") or "")
        name = self._unique_name(flow.get("name") or "", phone or "wx")
        acc = self.pool.get(name)
        if acc is None:
            acc = self.pool.add_account(name, loginid=phone, session=session,
                                        userid=userid)
        else:
            if phone:
                acc.loginid = phone
            acc.session, acc.userid = session, userid
            acc.obtained_at = int(time.time())
            acc.expire_at = acc.obtained_at + C_SESSION_EXPIRE_SECONDS
        self._clear_cooldown(acc)
        self.pool.refresh_quota(acc)
        self.pool.save(force=True)
        self.flows.finish(state, account=acc.name, userid=userid, phone=phone)
        self.log(f"[panel] 微信登录完成：{acc.name} userid={userid} "
                 f"可用 {acc.available} —— 已热加载进池")
        return {"ok": True, "account": acc.name, "userid": userid,
                "available": acc.available, "balance": acc.balance,
                "daily_balance": acc.daily_balance, "state": self.state()}

    @staticmethod
    def _clear_cooldown(acc: Account) -> None:
        """新登录成功 = 这个号是好的：清冷却/熔断/软退避/模型级负缓存。"""
        acc.cooldown_until = 0.0
        acc.cool_kind = ""
        acc.soft_streak = 0
        acc.breaker_until = 0.0
        acc.breaker_fails = 0
        acc.breaker_retries = 0
        acc.model_cooldowns.clear()
