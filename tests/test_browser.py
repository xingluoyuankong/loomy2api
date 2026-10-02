"""受控授权窗口（browser.py）+ 其面板端点的测试。

不真的开浏览器：把 `_targets`（CDP 的 /json 读取）打桩成可控序列。
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from loomy2api import browser
from tests.test_panel import PanelHarness


class TestFindChromium(unittest.TestCase):
    def test_returns_path_or_none(self):
        exe = browser.find_chromium()
        self.assertTrue(exe is None or isinstance(exe, str))

    def test_respects_candidates_order(self):
        with mock.patch("os.path.exists", side_effect=lambda p: p.endswith("msedge.exe")):
            self.assertTrue(browser.find_chromium().endswith("msedge.exe"))

    def test_none_when_nothing_found(self):
        with mock.patch("os.path.exists", return_value=False), \
             mock.patch("shutil.which", return_value=None):
            self.assertIsNone(browser.find_chromium())


class TestAuthBrowser(unittest.TestCase):
    def _browser(self, results, **kw):
        """results: 每次轮询 _targets 返回的列表（None 表示还没到）。"""
        seq = list(results)

        def fake_targets(_port, timeout=1.5):
            return seq.pop(0) if seq else []

        br = browser.AuthBrowser("https://example.test/auth",
                                 exe="C:/fake/chrome.exe",
                                 poll_interval=0.01, timeout=kw.pop("timeout", 2.0),
                                 **kw)
        return br, fake_targets

    def test_picks_up_callback_url(self):
        cb = "https://loomy.xunfei.cn/oauth/wechat/callback?code=ABC123&state=s1"
        br, fake = self._browser([[], [{"url": "https://open.weixin.qq.com/x"}],
                                  [{"url": cb, "title": "微信登录"}]])
        with mock.patch.object(browser, "_targets", side_effect=fake), \
             mock.patch.object(browser.subprocess, "Popen") as popen, \
             mock.patch.object(browser.AuthBrowser, "stop"), \
             mock.patch("tempfile.mkdtemp", return_value="C:/fake/profile"):
            popen.return_value.pid = 4242
            popen.return_value.poll.return_value = None
            br.start()
            br._thread.join(timeout=3)
        self.assertEqual(br.status, "done")
        self.assertEqual(br.result["url"], cb)

    def test_ignores_urls_without_code(self):
        br, fake = self._browser([[{"url": "https://loomy.xunfei.cn/oauth/wechat/callback"}]])
        with mock.patch.object(browser, "_targets", side_effect=fake):
            self.assertIsNone(br._scan())

    def test_scan_requires_loomy_domain(self):
        br, _ = self._browser([])
        with mock.patch.object(browser, "_targets",
                               return_value=[{"url": "https://evil.test/cb?code=X"}]):
            self.assertIsNone(br._scan())

    def test_timeout_sets_status(self):
        br, fake = self._browser([[]] * 500, timeout=0.05)
        with mock.patch.object(browser, "_targets", side_effect=fake), \
             mock.patch.object(browser.subprocess, "Popen") as popen, \
             mock.patch.object(browser.AuthBrowser, "stop"), \
             mock.patch("tempfile.mkdtemp", return_value="C:/fake/profile"):
            popen.return_value.pid = 4242
            popen.return_value.poll.return_value = None
            br.start()
            br._thread.join(timeout=3)
        self.assertEqual(br.status, "timeout")

    def test_missing_browser_raises(self):
        br = browser.AuthBrowser("https://x.test", exe="")
        with self.assertRaises(browser.BrowserError):
            br.start()

    def test_snapshot_shape(self):
        br = browser.AuthBrowser("https://x.test", exe="C:/fake/chrome.exe")
        snap = br.snapshot()
        self.assertEqual(snap["status"], "pending")
        self.assertIn("pid", snap)
        self.assertIn("url_found", snap)


class TestBrowserEndpoint(PanelHarness, unittest.TestCase):
    def _post(self, path, payload):
        import urllib.request
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        return json.loads(urllib.request.urlopen(req, timeout=30).read())

    def test_no_browser_gives_actionable_error(self):
        import urllib.error
        import urllib.request
        with mock.patch.object(browser, "find_chromium", return_value=None):
            body = json.dumps({}).encode("utf-8")
            req = urllib.request.Request(
                self.base + "/api/panel/login/wechat/browser", data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=30)
            self.assertEqual(ctx.exception.code, 400)
            detail = ctx.exception.read().decode("utf-8")
            self.assertIn("短信", detail)          # 明确给出替代路径

    def test_start_opens_window_and_poll_reports(self):
        started = {}

        class FakeBrowser:
            def __init__(self, url, **kw):
                started["url"] = url
                started["kw"] = kw
                self.status = "waiting"

            def start(self):
                started["started"] = True

            def stop(self):
                started["stopped"] = True

            def snapshot(self):
                return {"status": self.status, "error": "", "pid": 123,
                        "url_found": ""}

        with mock.patch.object(browser, "find_chromium",
                               return_value="C:/fake/chrome.exe"), \
             mock.patch.object(browser, "AuthBrowser", FakeBrowser):
            r = self._post("/api/panel/login/wechat/browser", {})
            self.assertTrue(r["ok"])
            self.assertEqual(r["mode"], "browser")
            self.assertTrue(started.get("started"))
            self.assertIn("open.weixin.qq.com/connect/qrconnect", started["url"])
            self.assertIn(r["state"], started["url"])

            _s, poll = self.get_json("/api/panel/login/poll?state=" + r["state"])
            self.assertEqual(poll["browser"]["status"], "waiting")
            self.assertFalse(poll["done"])

            cancel = self._post("/api/panel/login/wechat/cancel", {"state": r["state"]})
            self.assertTrue(cancel["ok"])
            self.assertTrue(started.get("stopped"))

    def test_callback_completion_marks_flow_done(self):
        """受控窗口拿到回调后，走的是与手动粘贴同一段换 session 逻辑。"""
        self.gateway.pool.client.wechat_bind = "1"

        class FakeBrowser:
            def __init__(self, url, **kw):
                self.on_result = kw.get("on_result")
                self.status = "waiting"

            def start(self):
                pass

            def stop(self):
                pass

            def snapshot(self):
                return {"status": self.status}

        holder = {}

        def make(url, **kw):
            br = FakeBrowser(url, **kw)
            holder["br"] = br
            holder["url"] = url
            return br

        with mock.patch.object(browser, "find_chromium",
                               return_value="C:/fake/chrome.exe"), \
             mock.patch.object(browser, "AuthBrowser", side_effect=make):
            r = self._post("/api/panel/login/wechat/browser", {})
            state = r["state"]
            cb = (f"https://loomy.xunfei.cn/oauth/wechat/callback"
                  f"?code=REALCODE123&state={state}")
            holder["br"].on_result({"status": "done",
                                    "result": {"url": cb}, "error": ""})

        _s, poll = self.get_json("/api/panel/login/poll?state=" + state)
        self.assertTrue(poll["done"])
        acc = self.gateway.pool.get(poll["account"])
        self.assertIsNotNone(acc)
        self.assertEqual(acc.session, "wx-session")

    def test_browser_cancel_marks_flow_failed(self):
        class FakeBrowser:
            def __init__(self, url, **kw):
                self.on_result = kw.get("on_result")

            def start(self):
                pass

            def stop(self):
                pass

            def snapshot(self):
                return {"status": "cancelled"}

        holder = {}

        def make(url, **kw):
            holder["br"] = FakeBrowser(url, **kw)
            return holder["br"]

        with mock.patch.object(browser, "find_chromium",
                               return_value="C:/fake/chrome.exe"), \
             mock.patch.object(browser, "AuthBrowser", side_effect=make):
            r = self._post("/api/panel/login/wechat/browser", {})
            state = r["state"]
            holder["br"].on_result({"status": "cancelled", "result": None,
                                    "error": ""})

        _s, poll = self.get_json("/api/panel/login/poll?state=" + state)
        self.assertFalse(poll["done"])
        self.assertEqual(poll["status"], "error")
        self.assertIn("关闭", poll["error"])


if __name__ == "__main__":
    unittest.main()
