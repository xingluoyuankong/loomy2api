r"""受控浏览器：用本机 Chromium 内核浏览器开授权页，并自动截获回调 URL。

为什么需要它
------------
微信 OAuth 的回调域被白名单锁死在 ``loomy.xunfei.cn``（实测只校验域名、
userinfo@ 之类绕过一律被拒），而该回调路径线上是 **404** ——
官方客户端之所以能全自动，是因为它把授权页塞进 Electron ``BrowserWindow``，
在 ``will-redirect`` 里把跳转截在进程内。

网关进程没有浏览器，所以此前只能让用户自己在 404 页面 Ctrl+C 再粘回来。
实测用户试了十几次都卡在这一步（日志里只有 start 没有 complete）。

这里换个思路：**借用户机器上已有的 Chromium 开一个受控窗口**，
用 CDP 的 HTTP 接口（``GET /json``）读它的当前地址 —— 不需要 WebSocket、
不需要任何第三方依赖。用户只需要扫码，剩下的自动完成。

设计要点
--------
* 独立 ``--user-data-dir``（临时目录），不碰用户自己的浏览器配置和登录态
* 随机高位端口，避免和用户已开的调试端口撞车
* 轮询 ``/json`` 拿 target 的 ``url``；命中 ``code=`` 即视为回调成功
* 用户关掉窗口 → 进程退出 → 判定为取消，不留僵尸进程
* 全流程可超时（默认 5 分钟），超时自动清理
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

__all__ = ["find_chromium", "AuthBrowser", "BrowserError"]

#: Chromium 内核浏览器候选（Windows 优先，其次 macOS/Linux）
CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/microsoft-edge",
)


class BrowserError(RuntimeError):
    pass


def find_chromium() -> Optional[str]:
    """找一个可用的 Chromium 内核浏览器，找不到返回 None。"""
    for path in CANDIDATES:
        if path and os.path.exists(path):
            return path
    # PATH 里碰碰运气
    for name in ("chrome", "chromium", "msedge", "google-chrome", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _targets(port: int, timeout: float = 1.5) -> List[Dict[str, Any]]:
    """读 CDP 的 target 列表（纯 HTTP，无需 WebSocket）。"""
    url = f"http://127.0.0.1:{port}/json"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


class AuthBrowser:
    """开一个受控浏览器窗口，等它跳到回调 URL 并把 code 交出来。"""

    def __init__(self, url: str, *, exe: Optional[str] = None,
                 timeout: float = 300.0, poll_interval: float = 1.0,
                 callback_marker: str = "code=",
                 on_result: Optional[Callable[[Dict[str, Any]], None]] = None,
                 logger: Optional[Callable[[str], None]] = None):
        self.url = url
        #: None = 自己去探测；空字符串 = 明确表示「没有可用浏览器」
        self.exe = find_chromium() if exe is None else exe
        self.timeout = float(timeout)
        self.poll_interval = max(0.3, float(poll_interval))
        self.callback_marker = callback_marker
        self.on_result = on_result
        self.log = logger or (lambda _m: None)
        self.port = _free_port()
        self.profile: Optional[str] = None
        self.proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        #: pending | waiting | done | cancelled | timeout | error
        self.status = "pending"
        self.result: Optional[Dict[str, Any]] = None
        self.error = ""

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if not self.exe:
            raise BrowserError(
                "没找到 Chrome / Edge。请装一个，或改用「短信验证码」方式添加账号。")
        self.profile = tempfile.mkdtemp(prefix="loomy2api-auth-")
        args = [
            self.exe,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,MediaRouter",
            "--window-size=520,760",
            "--new-window",
            self.url,
        ]
        creation = 0
        if os.name == "nt":
            # 新进程组：父进程退出时不把授权窗口一起带走
            creation = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self.proc = subprocess.Popen(  # noqa: S603
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creation)
        self.status = "waiting"
        self.log(f"[browser] 已打开受控授权窗口（pid={self.proc.pid} port={self.port}）")
        self._thread = threading.Thread(target=self._watch, daemon=True,
                                        name="loomy2api-authbrowser")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self.proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:                            # noqa: BLE001
                try:
                    proc.kill()
                except Exception:                        # noqa: BLE001
                    pass
        if self.profile:
            shutil.rmtree(self.profile, ignore_errors=True)
            self.profile = None

    # ---------------------------------------------------------------- 轮询
    def _watch(self) -> None:
        deadline = time.time() + self.timeout
        while not self._stop.is_set():
            if self.proc and self.proc.poll() is not None:
                if self.status == "waiting":
                    self.status = "cancelled"
                    self.error = "授权窗口已被关闭"
                    self.log("[browser] 授权窗口被用户关闭")
                break
            found = self._scan()
            if found:
                self.result = found
                self.status = "done"
                self.log(f"[browser] 已截获回调 URL（{len(found['url'])} 字符）")
                break
            if time.time() > deadline:
                self.status = "timeout"
                self.error = "等待扫码超时"
                self.log("[browser] 等待扫码超时")
                break
            self._stop.wait(self.poll_interval)
        try:
            if self.on_result:
                self.on_result({"status": self.status, "result": self.result,
                                "error": self.error})
        finally:
            self.stop()

    def _scan(self) -> Optional[Dict[str, Any]]:
        """在所有 target 的 url 里找带回调标记的那条。"""
        for t in _targets(self.port):
            url = str(t.get("url") or "")
            if self.callback_marker in url and "loomy" in url:
                return {"url": url, "title": t.get("title") or "",
                        "target_id": t.get("id") or ""}
        return None

    # ---------------------------------------------------------------- 视图
    def snapshot(self) -> Dict[str, Any]:
        return {"status": self.status, "error": self.error,
                "pid": self.proc.pid if self.proc else 0,
                "url_found": (self.result or {}).get("url", "")}
