"""Multi-account pool: session lifecycle, quota tracking, rotation.

Design notes
------------
* One account = one Loomy/iFlytek account = one 14-day session.  Sessions are
  refreshed automatically (password login) before they expire, so a pool keeps
  working unattended.
* Routing strategies:

  ``balance``      pick the account with the most available points (default)
  ``round_robin``  cycle in order
  ``lru``          least recently used

* An account that answers ``401/403`` or reports an exhausted quota is put in
  a cooldown and skipped until it expires; the gateway then retries the request
  on the next account.
* The pool state (sessions, quota) is written back to ``accounts.json``
  atomically.  Passwords live in the same file — keep it out of git
  (``.gitignore`` already covers it).
"""

from __future__ import annotations

import glob
import json
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import constants as C
from .account import Account, AccountClient, AccountError
from .routing import InflightTracker, StickyRouter, session_key, weight_of, weighted_pick
from .upstream import ModelGateway, UpstreamError

__all__ = ["AccountPool", "PoolError", "next_day_4am"]


def next_day_4am(now: Optional[float] = None) -> float:
    """下一个 04:00 的墙钟时间戳（本地时区）。

    额度耗尽的账号等到下一个 04:00 再试——对齐每日免费额度的重置窗口。
    now 落在 00:00~04:00 之间时返回**当天** 04:00（那时额度已刷新），
    否则返回次日 04:00。
    """
    import datetime as _dt
    now = time.time() if now is None else now
    current = _dt.datetime.fromtimestamp(now)
    if current.hour < 4:
        target = current.replace(hour=4, minute=0, second=0, microsecond=0)
    else:
        target = (current + _dt.timedelta(days=1)).replace(
            hour=4, minute=0, second=0, microsecond=0)
    return target.timestamp()


class PoolError(RuntimeError):
    pass


class AccountPool:
    def __init__(self, cfg, logger=None):
        self.cfg = cfg
        self.log = logger or (lambda msg: None)
        self.client = AccountClient(cfg)
        self.gateway = ModelGateway(cfg)
        self._lock = threading.RLock()
        self._accounts: List[Account] = []
        self._rr_index = 0
        self._rr_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_save = 0.0
        self._dirty = False
        self._file = cfg.path("accounts_file", "accounts.json")
        # 会话粘性 + 在途租约（借鉴 workbuddy2api-panel）
        self.sticky = StickyRouter(
            ttl=float(cfg.get("sticky_ttl_seconds") or 1800),
            max_entries=int(cfg.get("sticky_max_entries") or 2000))
        self.inflight = InflightTracker(
            limit=int(cfg.get("max_inflight_per_account") or 0))
        self._rng = __import__("random").Random()
        # 后台作业台账（面板「任务中心」页的数据源）
        self._jobs: Dict[str, Dict] = {}
        self._init_jobs()
        self.load()

    # ------------------------------------------------------------ jobs

    def _init_jobs(self) -> None:
        interval_min = max(1, int(float(self.cfg.get("quota_refresh_minutes") or 30)))
        self._jobs = {
            "session_keeper": {
                "name": "登录态守护",
                "desc": "巡检 session，剩余天数低于阈值自动重登",
                "interval": f"{interval_min} 分钟",
                "interval_seconds": interval_min * 60,
                "last_run": 0, "last_result": "", "last_error": "",
                "runs": 0, "failures": 0, "enabled": True, "manual": False,
            },
            "quota_refresh": {
                "name": "额度刷新",
                "desc": "拉取各账号积分，额度恢复自动解冻硬冷却",
                "interval": f"{interval_min} 分钟",
                "interval_seconds": interval_min * 60,
                "last_run": 0, "last_result": "", "last_error": "",
                "runs": 0, "failures": 0, "enabled": True, "manual": False,
            },
            "sticky_prune": {
                "name": "粘性表清理",
                "desc": "清理过期的会话绑定，防止 map 膨胀",
                "interval": f"{interval_min} 分钟",
                "interval_seconds": interval_min * 60,
                "last_run": 0, "last_result": "", "last_error": "",
                "runs": 0, "failures": 0, "enabled": True, "manual": False,
            },
        }

    def _job_done(self, key: str, result: str = "", error: str = "") -> None:
        job = self._jobs.get(key)
        if not job:
            return
        job["last_run"] = int(time.time())
        job["runs"] += 1
        if error:
            job["failures"] += 1
            job["last_error"] = error[:200]
        else:
            job["last_error"] = ""
        if result:
            job["last_result"] = result[:200]

    def jobs_snapshot(self) -> List[Dict]:
        now = time.time()
        out = []
        for key, job in self._jobs.items():
            item = dict(job)
            item["key"] = key
            if job["last_run"] and job["enabled"]:
                item["next_run_in"] = max(
                    0, int(job["last_run"] + job["interval_seconds"] - now))
            else:
                item["next_run_in"] = 0 if job["enabled"] else -1
            out.append(item)
        return out

    def run_job(self, key: str) -> Dict:
        """手动触发一个后台作业（面板按钮）。"""
        if key not in self._jobs:
            raise PoolError(f"未知作业 / unknown job: {key}")
        started = time.time()
        if key == "session_keeper":
            self.tick()
            self._job_done(key, f"巡检 {len(self.accounts)} 个账号，耗时 "
                                f"{time.time() - started:.1f}s")
        elif key == "quota_refresh":
            for acc in self.accounts:
                self.refresh_quota(acc)
            self.save(force=True)
            self._job_done(key, f"刷新 {len(self.accounts)} 个账号额度")
        elif key == "sticky_prune":
            removed = self.sticky.prune()
            self._job_done(key, f"清理 {removed} 条过期绑定")
        return next((j for j in self.jobs_snapshot() if j["key"] == key),
                    {"key": key, **self._jobs[key]})

    # ------------------------------------------------------------ storage

    def load(self) -> None:
        raw: object = {}
        if self._file.exists():
            try:
                raw = json.loads(self._file.read_text(encoding="utf-8"))
            except Exception as exc:                    # noqa: BLE001
                raise PoolError(f"{self._file} 解析失败 / parse error: {exc}") from exc

        items = raw.get("accounts") if isinstance(raw, dict) else raw
        accounts: List[Account] = []
        for idx, item in enumerate(items or []):
            if isinstance(item, dict):
                accounts.append(Account.from_dict(item, idx))

        if self.cfg.get("sessions_from_client", True):
            accounts.extend(self._client_accounts(existing=accounts))

        with self._lock:
            # keep runtime stats across reloads for same-named accounts
            previous = {a.name: a for a in self._accounts}
            for acc in accounts:
                old = previous.get(acc.name)
                if old is not None:
                    acc.requests, acc.points_used, acc.failures = (
                        old.requests, old.points_used, old.failures)
                    acc.last_used, acc.last_error = old.last_used, old.last_error
                    if acc.expire_at == 0 and old.expire_at:
                        acc.session, acc.expire_at, acc.userid = (
                            old.session, old.expire_at, old.userid)
            self._accounts = accounts

        self.log(f"账号池载入 {len(accounts)} 个账号"
                 + (f"（{', '.join(a.name for a in accounts)}）" if accounts else ""))

    def _client_accounts(self, existing: Sequence[Account]) -> List[Account]:
        """Import sessions from any installed desktop client (read-only).

        These are *derived* accounts: they are never written back to
        accounts.json (the client owns that file), and an import that collides
        with a configured account by session or by name is skipped.
        """
        pattern = os.path.join(str(self.cfg.get("client_root") or C.CLIENT_PUBLIC_ROOT),
                               "*", "userData", "auth-session.json")
        found: List[Account] = []
        known_sessions = {a.session for a in existing if a.session}
        known_names = {a.name for a in existing}
        # Same *account* reached through two different sessions (the client's
        # session vs. ours) must not become two pool entries: match on userid.
        known_users = {a.userid for a in existing if a.userid}
        for path in glob.glob(pattern):
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
            except Exception:                           # noqa: BLE001
                continue
            session = str(data.get("session") or "")
            userid = str(data.get("userid") or "")
            if not session or session in known_sessions:
                continue
            if userid and userid in known_users:
                continue
            name = f"desktop-{str(data.get('phone') or 'unknown')[-4:]}"
            if name in known_names:
                continue
            created = int(data.get("updatedAt") or 0)
            # the client stores milliseconds since epoch; sessions last 14 days
            expire = (created // 1000 + C.SESSION_EXPIRE_SECONDS) if created else 0
            found.append(Account(
                name=name,
                loginid=str(data.get("phone") or ""),
                session=session, userid=userid,
                expire_at=expire, obtained_at=created // 1000, source="client",
                persist=False,
            ))
            known_sessions.add(session)
            known_names.add(name)
            if userid:
                known_users.add(userid)
            self.log(f"发现桌面客户端登录态：{path}")
        return found

    def save(self, *, force: bool = False) -> None:
        now = time.time()
        with self._lock:
            if not force and (now - self._last_save) < 5:
                self._dirty = True
                return
            self._dirty = False
            self._last_save = now
            payload = {"accounts": [a.to_dict() for a in self._accounts if a.persist]}
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(self._file.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self._file)

    def flush(self) -> None:
        """Force a write if a throttled save is pending."""
        if self._dirty:
            self.save(force=True)

    # ------------------------------------------------------------ access

    @property
    def accounts(self) -> List[Account]:
        with self._lock:
            return list(self._accounts)

    def get(self, name: str) -> Optional[Account]:
        for acc in self.accounts:
            if acc.name == name:
                return acc
        return None

    def add_account(self, name: str, loginid: str = "", password: str = "",
                    session: str = "", userid: str = "",
                    identity: Optional[Dict] = None,
                    proxy: str = "") -> Account:
        if self.get(name):
            raise PoolError(f"账号 {name} 已存在 / account already exists")
        acc = Account(name=name, loginid=loginid, password=password,
                      session=session, userid=userid, proxy=str(proxy or "").strip())
        # bind a device identity at creation time, so the account always looks
        # like one consistent device from its very first request
        acc.identity = dict(identity) if identity else self.client.ensure_identity(acc)
        if session and not acc.expire_at:
            acc.obtained_at = int(time.time())
            acc.expire_at = acc.obtained_at + C.SESSION_EXPIRE_SECONDS
        with self._lock:
            self._accounts.append(acc)
        self.save(force=True)
        return acc

    def update_account(self, name: str, **fields) -> Account:
        """Patch an existing account (loginid / password / enabled / proxy)."""
        acc = self.get(name)
        if acc is None:
            raise PoolError(f"没有名为 {name} 的账号 / no such account: {name}")
        for key in ("loginid", "password"):
            if key in fields and fields[key] is not None:
                setattr(acc, key, str(fields[key]))
                if key == "password":
                    # a new credential means the old session belongs to the old
                    # login: force a fresh login on next use
                    acc.session, acc.expire_at = "", 0
        if "proxy" in fields and fields["proxy"] is not None:
            acc.proxy = str(fields["proxy"]).strip()
        if "enabled" in fields and fields["enabled"] is not None:
            acc.enabled = bool(fields["enabled"])
        self.save(force=True)
        return acc

    def set_proxy(self, name: str, proxy: str) -> Account:
        """单独设置账号出口代理（面板「代理出口」页用）。"""
        return self.update_account(name, proxy=proxy)

    def points_breakdown(self, acc: Account) -> Dict:
        """一个账号的积分构成（额度分桶 + 最近流水）。"""
        if not acc.session:
            return {"error": "无登录态"}
        try:
            return self.gateway.points_breakdown(acc.session, proxy=acc.proxy or None)
        except UpstreamError as exc:
            return {"error": str(exc)}
        except Exception as exc:                        # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

    def rebind_identity(self, name: str) -> Account:
        """Give the account a brand-new device identity (and log it in again)."""
        acc = self.get(name)
        if acc is None:
            raise PoolError(f"没有名为 {name} 的账号 / no such account: {name}")
        self.client.rebind_identity(acc)
        if acc.loginid and acc.password:
            self.ensure_session(acc, force=True)
        self.save(force=True)
        return acc

    def remove_account(self, name: str) -> bool:
        with self._lock:
            before = len(self._accounts)
            self._accounts = [a for a in self._accounts if a.name != name]
            removed = len(self._accounts) != before
        if removed:
            self.save(force=True)
        return removed

    # ------------------------------------------------------ session keep

    def ensure_session(self, acc: Account, *, force: bool = False) -> Account:
        """Log in / refresh the account session when needed."""
        renew_days = float(self.cfg.get("session_renew_before_days") or 3)
        needs = force or not acc.session_valid
        if not needs and acc.days_left is not None and acc.days_left < renew_days:
            needs = True
            self.log(f"账号 {acc.name} session 剩余 {acc.days_left:.1f} 天，提前续期")
        if not needs:
            return acc
        if not (acc.loginid and acc.password):
            raise PoolError(
                f"账号 {acc.name} 的 session 不可用且没有账号密码，无法自动重登"
                f"（请在 accounts.json 补 loginid/password，或用 `loomy2api login` 短信登录）")
        self.client.fill(acc)
        self.save()
        self.log(f"账号 {acc.name} 登录成功 userid={acc.userid} "
                 f"session={acc.session[:8]}… 剩余 {(acc.days_left or 0):.1f} 天")
        return acc

    def refresh_quota(self, acc: Account) -> Account:
        """Refresh one account's points; never raises.

        This runs from the panel, the CLI and the background keeper — any of
        which can be offline, behind a dead proxy, or talking to an upstream
        that is simply down. A failed refresh must degrade to "quota unknown",
        not crash the caller (a socket/DNS error is *not* an UpstreamError).
        """
        if not acc.session:
            return acc
        try:
            totals = self.gateway.points_totals(acc.session,
                                                proxy=acc.proxy or None)
            if totals.get("points_used"):
                # 上游流水求和是**权威**累计：本地计数丢过（重启）也能自愈
                acc.points_used = int(totals["points_used"])
        except Exception as exc:                        # noqa: BLE001
            self.log(f"账号 {acc.name} 累计消耗汇总失败：{exc}")
        daily_consumed = None
        try:
            fl = self.gateway.points_first_login(acc.session,
                                                 proxy=acc.proxy or None)
            data = fl.get("data") or {}
            daily_consumed = data.get("dailyConsumed")
            if daily_consumed is None:
                # 频繁调用时上游可能拒绝：留痕，避免静默 None 难排查
                self.log(f"账号 {acc.name} dailyConsumed 未取到："
                         f"code={fl.get('code')} desc={str(fl.get('desc'))[:60]}")
        except Exception as exc:                        # noqa: BLE001
            self.log(f"账号 {acc.name} 当日扣分查询失败：{exc}")
        if daily_consumed is not None:
            acc.daily_consumed = int(daily_consumed)
        try:
            quota = self.gateway.quota(acc.session, proxy=acc.proxy or None)
        except UpstreamError as exc:
            if exc.status in (401, 403):
                acc.session, acc.expire_at = "", 0
                self.log(f"账号 {acc.name} session 被上游拒绝（HTTP {exc.status}）")
            else:
                acc.last_error = f"quota: {exc}"
                self.log(f"账号 {acc.name} 额度查询失败：{exc}")
            return acc
        except Exception as exc:                        # noqa: BLE001
            acc.last_error = f"quota: {exc}"
            self.log(f"账号 {acc.name} 额度查询异常（网络？）：{exc}")
            return acc
        acc.balance = quota.get("balance")
        acc.daily_balance = quota.get("daily_balance")
        acc.available = quota.get("available")
        acc.quota_updated_at = int(time.time())
        # 额度恢复 → 解冻硬冷却（额度耗尽型），但不动熔断器
        if isinstance(acc.available, int) and acc.available > 0 \
                and acc.cool_kind == "hard":
            acc.cooldown_until = 0.0
            acc.cool_kind = ""
            self.log(f"账号 {acc.name} 额度恢复（可用 {acc.available}）→ 解冻")
        return acc

    # ------------------------------------------------------------ routing

    def usable(self, exclude: Sequence[str] = (), model: str = "") -> List[Account]:
        """可用候选：启用 + 无冷却/熔断 + session 有效 + 有额度 + 未占满在途。

        传 model 时叠加模型级冷却判定（该模型被限流的账号换模型仍可用）。
        """
        now = time.time()
        out: List[Account] = []
        for acc in self.accounts:
            if not acc.enabled or acc.name in exclude:
                continue
            if not acc.healthy(now) or not acc.session_valid:
                continue
            if acc.available == 0:                 # 额度确认为 0 → 不参与
                continue
            if model and acc.model_cooled(model, now):
                continue
            if self.inflight.full(acc.name):
                continue
            out.append(acc)
        return out

    def acquire(self, exclude: Sequence[str] = (), model: str = "",
                session_key_value: Optional[str] = None) -> Account:
        """选一个账号（session 保证可用），并占用一个在途租约。

        顺序：会话粘性命中 → 三因子加权 Top-N 抽签 → 全冷却兜底。
        调用方用完必须 `release(acc)`（流式在 finally 里，非流式在 finally 里）。
        """
        now = time.time()

        # 1) 会话粘性：同一个会话尽量落在同一个账号，保住多轮上下文
        if session_key_value:
            bound = self.sticky.get(session_key_value)
            if bound and bound not in exclude:
                acc = self.get(bound)
                if acc is not None and acc.healthy(now) and acc.session_valid \
                        and not self.inflight.full(acc.name):
                    self.ensure_session(acc)
                    acc.last_used = now
                    self.inflight.acquire(acc.name)
                    return acc
                self.sticky.unbind(session_key_value, bound)

        # 2) 常规选号
        candidates = self.usable(exclude, model=model)
        if not candidates:
            # 没有健康账号：给单账号/全冷却场景一次自愈机会
            all_enabled = [a for a in self.accounts
                           if a.enabled and a.name not in exclude]
            if not all_enabled:
                raise PoolError("账号池里没有可用账号（accounts.json 为空或全部禁用）")
            if model:
                # 模型级冷却导致全员不可用 → 退一步忽略模型冷却（换模型才有意义，
                # 但至少不要直接 503）
                relaxed = [a for a in all_enabled
                           if a.healthy(now) and a.session_valid
                           and not self.inflight.full(a.name)]
                candidates = relaxed or []
            if not candidates:
                candidates = [a for a in all_enabled if not self.inflight.full(a.name)]
            self.log("没有健康账号，尝试对现有账号续期/重登")

        acc = self._pick(candidates)
        self.ensure_session(acc)
        acc.last_used = now
        self.inflight.acquire(acc.name)
        if session_key_value:
            self.sticky.bind(session_key_value, acc.name)
        return acc

    def _pick(self, candidates: Sequence[Account]) -> Account:
        """按策略选号。

        ``weighted``（默认）——三因子加权 Top-N 抽签，防惊群；
        ``balance`` ——严格取最大可用积分（确定性，测试/单号场景用）；
        ``round_robin`` / ``lru`` ——顺序轮换 / 最久未用。
        """
        strategy = str(self.cfg.get("strategy") or "weighted").lower()
        if strategy == "round_robin":
            with self._rr_lock:
                acc = candidates[self._rr_index % len(candidates)]
                self._rr_index = (self._rr_index + 1) % max(1, len(candidates))
            return acc
        if strategy == "lru":
            return min(candidates, key=lambda a: a.last_used or 0)
        if strategy == "balance":
            # 严格最大可用积分（确定性；旧默认语义，保留给需要可预测的场景）
            return max(candidates,
                       key=lambda a: (a.available if a.available is not None else 10**9,
                                      -a.last_used))
        # 默认 weighted：三因子加权随机（余额占比 ×10 + 闲置补偿 ×1 + 成功率 ×3）
        now = time.time()
        max_available = max(
            (a.available for a in candidates if isinstance(a.available, int)),
            default=0)
        top_n = max(1, int(self.cfg.get("pick_top_n") or 5))
        picked = weighted_pick(
            candidates,
            lambda a: weight_of(a, max_available, now),
            top_n=top_n, rng=self._rng)
        return picked or candidates[0]

    def apply_config(self) -> None:
        """配置热更新后重建受配置驱动的组件（选号 Top-N / 粘性 TTL / 在途上限）。"""
        self.sticky.ttl = max(0.0, float(self.cfg.get("sticky_ttl_seconds") or 0))
        self.sticky.max_entries = max(0, int(self.cfg.get("sticky_max_entries") or 0))
        self.inflight.limit = max(0, int(self.cfg.get("max_inflight_per_account") or 0))
        self.log(f"池参数已热更新（Top-{self.cfg.get('pick_top_n')} · "
                 f"粘性 TTL {self.sticky.ttl:.0f}s · 在途上限 "
                 f"{self.inflight.limit or '不限'}）")

    def release(self, acc: Optional[Account]) -> None:
        """归还在途租约（幂等）。"""
        if acc is not None:
            self.inflight.release(acc.name)

    def report_success(self, acc: Account, usage: Optional[Dict] = None,
                       model: str = "") -> None:
        acc.requests += 1
        points = (usage or {}).get("points_consumed")
        if isinstance(points, (int, float)):
            acc.points_used += int(points)
            if acc.available is not None:
                acc.available = max(0, int(acc.available) - int(points))
        acc.last_error = ""
        acc.note_success()
        if model and acc.model_cooled(model):
            # 该模型实测又通了 → 清掉它的模型级负缓存
            acc.model_cooldowns.pop(model, None)
        self.save()

    #: 需要"账号级"处置的上游状态码
    ACCOUNT_FAULT = (401, 403, 402, 429)

    def report_failure(self, acc: Account, status: int, reason: str = "",
                       model: str = "", reset_at: Optional[float] = None) -> None:
        """按错误类别分级处置账号（借鉴 workbuddy2api-panel 的错误分类）。

        | 状态 | 处置 |
        |---|---|
        | 401/403 | 登录态失效 → 清 session，等下次重登 |
        | 402 | 额度耗尽 → 硬冷却到次日 04:00 |
        | 429 | 限流 → 软冷却（有重置墙钟则对齐，否则有界指数退避） |
        | 5xx  | 上游自己的问题 → **不罚账号**，只记错误 |
        | 其它 | 固定短冷却 |
        """
        acc.failures += 1
        acc.last_error = reason or f"HTTP {status}"

        if status in (401, 403):
            acc.session, acc.expire_at = "", 0
            acc.cool(float(self.cfg.get("cooldown_seconds") or 60), "soft", acc.last_error)
            self.log(f"账号 {acc.name} 登录态被拒（HTTP {status}）→ 清 session")
        elif status == 402:
            until = next_day_4am()
            acc.cool_hard(until, acc.last_error)
            hours = (until - time.time()) / 3600
            self.log(f"账号 {acc.name} 额度耗尽（402）→ 硬冷却 {hours:.1f}h 至次日 04:00")
        elif status == 429:
            base = float(self.cfg.get("soft_rate_base_seconds") or 600)
            cap = float(self.cfg.get("soft_rate_max_seconds") or 7200)
            until = acc.cool_soft(base, cap, acc.last_error, reset_at=reset_at)
            left = until - time.time()
            if model:
                # 模型级限流：只冷这个模型，换模型还能用
                acc.model_cooldowns[model] = {
                    "until": until, "reason": acc.last_error, "hits": 1}
                self.log(f"账号 {acc.name} 模型 {model} 限流（429）→ 模型级冷却 {left:.0f}s")
            else:
                self.log(f"账号 {acc.name} 限流（429）→ 软冷却 {left:.0f}s"
                         f"（streak={acc.soft_streak}）")
        elif status == 404:
            acc.cool(float(self.cfg.get("not_found_cooldown_seconds") or 60),
                     "soft", acc.last_error)
            self.log(f"账号 {acc.name} HTTP 404 → 短冷却 60s")
        elif status in (400, 422):
            # 请求本身不合法（上游 invalid_request_error），换一个账号结果
            # 一样 —— 之前的实现会冷却账号 300s，客户端一个坏请求就能把
            # 唯一账号打入冷宫、整个池子瘫痪（01:38~02:25 事故实锤）。
            self.log(f"账号 {acc.name} HTTP {status} → 请求不合法，不冷却账号"
                     f"（{acc.last_error[:120]}）")
        elif status >= 500:
            # 上游挂了，不是账号的错 —— 只累计熔断器，不做冷却
            threshold = int(self.cfg.get("breaker_threshold") or 5)
            if acc.note_error(threshold,
                              float(self.cfg.get("breaker_cooldown_seconds") or 60),
                              float(self.cfg.get("breaker_cooldown_max_seconds") or 1800)):
                self.log(f"账号 {acc.name} 连续 {threshold} 次上游失败 → 熔断 "
                         f"{acc.breaker_until - time.time():.0f}s")
            else:
                self.log(f"账号 {acc.name} HTTP {status} → 上游异常，不冷却"
                         f"（熔断计数 {acc.breaker_fails}/{threshold}）")
        else:
            acc.cool(float(self.cfg.get("cooldown_seconds") or 300), "soft", acc.last_error)
            self.log(f"账号 {acc.name} 失败 HTTP {status} → 冷却"
                     f"{float(self.cfg.get('cooldown_seconds') or 300):.0f}s（{acc.last_error}）")
        self.save()

    def snapshot(self) -> Dict:
        return {
            "strategy": self.cfg.get("strategy"),
            "count": len(self.accounts),
            "usable": len(self.usable()),
            "sticky": self.sticky.snapshot(),
            "inflight": self.inflight.snapshot(),
            "accounts": [a.public_dict() for a in self.accounts],
        }

    # --------------------------------------------------------- background

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="account-keeper",
                                        daemon=True)
        self._thread.start()
        self.log("账号守护线程已启动（续期 + 额度刷新）")

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        interval = max(60, int(float(self.cfg.get("quota_refresh_minutes") or 30) * 60))
        while not self._stop.is_set():
            if self._stop.wait(interval):
                break
            try:
                self.tick()
            except Exception as exc:                    # noqa: BLE001
                self.log(f"账号守护线程异常：{exc}")

    def tick(self) -> None:
        """One maintenance pass: renew sessions, refresh quota, prune caches."""
        started = time.time()
        removed = self.sticky.prune()
        self._job_done("sticky_prune", f"清理 {removed} 条过期绑定")
        renewed = 0
        refreshed = 0
        errors: List[str] = []
        for acc in self.accounts:
            acc.prune_model_cooldowns()
            if not acc.enabled:
                continue
            try:
                before = acc.session
                self.ensure_session(acc)
                if acc.session != before:
                    renewed += 1
                self.refresh_quota(acc)
                refreshed += 1
            except (AccountError, PoolError) as exc:
                errors.append(f"{acc.name}: {exc}")
                self.log(f"账号 {acc.name} 维护失败：{exc}")
            except Exception as exc:                    # noqa: BLE001
                errors.append(f"{acc.name}: {exc}")
                self.log(f"账号 {acc.name} 维护异常：{exc}")
        self._job_done("session_keeper",
                       f"巡检 {len(self.accounts)} 个账号，续期 {renewed} 个",
                       error="；".join(errors[:3]))
        self._job_done("quota_refresh", f"刷新 {refreshed} 个账号额度")
        self.log(f"后台巡检完成：{len(self.accounts)} 账号 / 续期 {renewed} / "
                 f"额度 {refreshed} / 粘性清理 {removed} / 耗时 {time.time() - started:.1f}s")
        self.save()

    def bootstrap(self) -> None:
        """Login/Fill any account that has no usable session."""
        for acc in self.accounts:
            if acc.enabled and not acc.session_valid:
                try:
                    self.ensure_session(acc)
                except Exception as exc:                # noqa: BLE001
                    self.log(f"账号 {acc.name} 启动登录失败：{exc}")
        self.save()
