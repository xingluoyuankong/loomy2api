"""跳转登录流程（面板「添加账号」的 state 会话）。

对照 workbuddy2api-panel 的 `login/start` + `login/poll` 设备授权模式：
上游是 OAuth 设备流（服务端签发 state），Loomy 没有 OAuth，所以这里由**面板自己**
签发 state，并把「授权链接」指向面板托管的登录页 `/panel/login?state=…`。

流程
----
1. 面板点「添加账号」→ `POST /api/panel/login/start {phone?}`
   → 生成 state，返回 `{state, url}`；面板显示链接 + 开始轮询
2. 用户在新标签打开链接 → 在登录页填手机号 + 验证码（登录即注册）
3. 登录页 `POST /api/panel/login/submit {state, code}`
   → 校验 → 拿到 session → 落盘 + **热加载进池** → 标记 done
4. 原面板轮询 `GET /api/panel/login/poll?state=…` 到 done
   → 提示成功、刷新账号池（无需重启、无需在面板里再操作）

state 只存在进程内（不落 /tmp——Windows 上那条路不可用，参考项目踩过）。
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Dict, List, Optional

__all__ = ["LoginFlow", "FlowError"]

#: state 有效期（秒）。超过就作废，避免长期悬挂的半截会话。
DEFAULT_TTL = 600

#: 状态机
PENDING = "pending"      # 已生成链接，等用户在登录页操作
SENT = "sent"            # 验证码已下发
VERIFYING = "verifying"  # 正在校验验证码
DONE = "done"            # 已登录并入库
ERROR = "error"          # 失败（可重试）


class FlowError(RuntimeError):
    pass


class LoginFlow:
    """进程内的跳转登录会话表。"""

    def __init__(self, ttl: int = DEFAULT_TTL, max_entries: int = 200):
        self.ttl = max(30, int(ttl))
        self.max_entries = max(1, int(max_entries))
        self._flows: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 生命周期
    def start(self, phone: str = "", name: str = "") -> Dict[str, Any]:
        state = secrets.token_urlsafe(18)
        now = time.time()
        item = {
            "state": state,
            "phone": str(phone or ""),
            "name": str(name or ""),
            "msgid": "",
            "status": PENDING,
            "account": "",
            "userid": "",
            "error": "",
            "created": now,
            "updated": now,
        }
        with self._lock:
            self._prune_locked(now)
            if len(self._flows) >= self.max_entries:
                oldest = min(self._flows.items(),
                             key=lambda kv: kv[1]["updated"], default=None)
                if oldest:
                    self._flows.pop(oldest[0], None)
            self._flows[state] = item
        return dict(item)

    def get(self, state: str) -> Optional[Dict[str, Any]]:
        if not state:
            return None
        now = time.time()
        with self._lock:
            item = self._flows.get(state)
            if not item:
                return None
            if now - item["updated"] > self.ttl:
                self._flows.pop(state, None)
                return None
            return dict(item)

    def update(self, state: str, **fields) -> Dict[str, Any]:
        with self._lock:
            item = self._flows.get(state)
            if not item:
                raise FlowError("登录会话不存在或已过期，请回面板重新生成链接")
            item.update(fields)
            item["updated"] = time.time()
            return dict(item)

    def fail(self, state: str, error: str) -> Dict[str, Any]:
        """记一次失败。status 回 ERROR，但会话保留（用户可以在登录页重试）。"""
        return self.update(state, status=ERROR, error=str(error)[:300])

    def finish(self, state: str, *, account: str, userid: str = "",
               phone: str = "") -> Dict[str, Any]:
        return self.update(state, status=DONE, error="", account=account,
                           userid=userid, phone=phone or "")

    # -------------------------------------------------------------- 视图
    def poll_view(self, state: str) -> Dict[str, Any]:
        """给面板轮询用的精简视图（对齐参考项目的 done/错误语义）。"""
        item = self.get(state)
        if item is None:
            return {"done": False, "status": "expired",
                    "error": "登录会话已过期，请重新生成链接"}
        return {
            "done": item["status"] == DONE,
            "status": item["status"],
            "account": item["account"],
            "userid": item["userid"],
            "phone": item["phone"],
            "error": item["error"],
            "seconds_left": max(0, int(self.ttl - (time.time() - item["updated"]))),
        }

    def _prune_locked(self, now: float) -> None:
        for key in [k for k, v in self._flows.items()
                    if now - v["updated"] > self.ttl]:
            self._flows.pop(key, None)

    def prune(self) -> int:
        now = time.time()
        with self._lock:
            before = len(self._flows)
            self._prune_locked(now)
            return before - len(self._flows)

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._flows.values()]
