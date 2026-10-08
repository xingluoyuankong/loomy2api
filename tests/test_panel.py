"""Web control panel tests — page, state, and every write endpoint."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from loomy2api.server import Gateway, Handler, Server
from tests.support import (FakeAccountClient, FakeUpstream, make_config,
                           write_accounts)


class PanelHarness:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.upstream = FakeUpstream()
        base = self.upstream.start()
        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "pw",
             "session": "s1", "userid": "u1",
             "expireAt": int(time.time()) + 14 * 86400,
             "identity": {"devid": "web-aaaaaaaaaaaaaaaa", "ua": "UA",
                          "modelid": "Web", "version": "1.0.0",
                          "campus_device_id": "loomy-campus-fixed",
                          "created_at": 1700000000}},
        ])
        self.cfg = make_config(self.dir, base)
        self.gateway = Gateway(self.cfg)
        self.gateway.pool.client = FakeAccountClient()
        self.gateway.pool.bootstrap()
        self.gateway.refresh_models()
        handler = type("Bound", (Handler,), {"gateway": self.gateway})
        self.httpd = Server(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.upstream.stop()
        self.tmp.cleanup()

    # -- helpers --------------------------------------------------------

    def get(self, path, headers=None):
        request = urllib.request.Request(self.base + path, headers=headers or {})
        response = urllib.request.urlopen(request, timeout=30)
        return response.status, response

    def get_json(self, path, headers=None):
        status, response = self.get(path, headers)
        return status, json.loads(response.read())

    def post(self, path, payload, headers=None):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST")
        response = urllib.request.urlopen(request, timeout=60)
        return response.status, json.loads(response.read())


class TestPanelPage(PanelHarness, unittest.TestCase):
    def test_page_is_served(self):
        status, response = self.get("/panel")
        body = response.read().decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("loomy2api", body)
        # 页面骨架：左侧标签导航 + **内联脚本**（单文件，缓存只看 HTML 一个入口）
        self.assertIn('data-view="accounts"', body)
        self.assertIn("/* loomy2api panel v", body)      # 内联标记 + 构建指纹
        self.assertNotIn('<script src="/panel/app.js"', body)
        self.assertIn("text/html", response.headers["Content-Type"])

    def test_panel_js_is_served(self):
        status, response = self.get("/panel/app.js")
        body = response.read().decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("/api/panel/state", body)      # 脚本里引用的接口
        self.assertIn("/api/panel/usage", body)
        self.assertIn("/api/panel/proxies", body)
        self.assertIn("javascript", response.headers["Content-Type"])

    def test_root_serves_the_page(self):
        status, response = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("loomy2api", response.read().decode("utf-8"))

    def test_health_advertises_the_panel(self):
        _s, payload = self.get_json("/health")
        self.assertTrue(payload["panel"].endswith("/panel"))


class TestPanelState(PanelHarness, unittest.TestCase):
    def test_state_lists_accounts_with_quota(self):
        _s, payload = self.get_json("/api/panel/state")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["totals"]["accounts"], 1)
        self.assertEqual(payload["totals"]["available"], 150)
        account = payload["accounts"][0]
        self.assertEqual(account["name"], "a")
        self.assertEqual(account["available"], 150)
        self.assertEqual(account["balance"], 100)
        self.assertEqual(account["daily_balance"], 50)
        self.assertEqual(account["loginid_masked"], "138****0001")
        self.assertEqual(account["mode"], "password")
        self.assertNotIn('"password":', json.dumps(payload))

    def test_state_reports_identity(self):
        _s, payload = self.get_json("/api/panel/state")
        identity = payload["accounts"][0]["identity"]
        self.assertTrue(identity["bound"])
        self.assertEqual(identity["devid"], "web-aaaaaaaaaaaaaaaa")

    def test_state_reports_config(self):
        _s, payload = self.get_json("/api/panel/state")
        cfg = payload["config"]
        # 默认策略改为 weighted（三因子加权随机，防惊群）；balance 仍可用
        self.assertEqual(cfg["strategy"], "weighted")
        self.assertEqual(cfg["identity_mode"], "per_account")
        self.assertFalse(cfg["auth_required"])

    def test_repeated_state_does_not_hit_upstream_again(self):
        self.get_json("/api/panel/state")
        before = len([c for c in self.upstream.calls if "points" in c["path"]])
        self.get_json("/api/panel/state")
        after = len([c for c in self.upstream.calls if "points" in c["path"]])
        self.assertEqual(before, after)          # quota cache is reused

    def test_refresh_flag_forces_upstream_call(self):
        self.get_json("/api/panel/state")
        before = len([c for c in self.upstream.calls if "points" in c["path"]])
        self.get_json("/api/panel/state?refresh=1")
        after = len([c for c in self.upstream.calls if "points" in c["path"]])
        # refresh_quota 现在每账号发 3 个 points 请求：
        # points_totals（累计自愈）+ points/first-login（当日已扣）+ quota
        self.assertEqual(after, before + 3)


class TestPanelWrites(PanelHarness, unittest.TestCase):
    def test_add_account_with_password_logs_in_and_binds_identity(self):
        status, payload = self.post("/api/panel/accounts", {
            "name": "new", "loginid": "13900000002", "password": "pw", "login": True})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["logged_in"])
        # devid 恒为客户端常量 'web'（风控基线，见 account.new_identity）
        self.assertEqual(payload["identity"]["devid"], "web")
        saved = json.loads((self.dir / "accounts.json").read_text(encoding="utf-8"))
        entry = [a for a in saved["accounts"] if a["name"] == "new"][0]
        self.assertEqual(entry["identity"]["devid"], payload["identity"]["devid"])
        self.assertIn("new", self.gateway.pool.client.fills)

    def test_add_account_with_session_only(self):
        _s, payload = self.post("/api/panel/accounts", {
            "name": "shared", "session": "borrowed-session"})
        self.assertTrue(payload["ok"])
        self.assertNotIn("logged_in", payload)
        acc = self.gateway.pool.get("shared")
        self.assertEqual(acc.session, "borrowed-session")
        self.assertTrue(acc.identity)          # identity bound at creation

    def test_add_without_credentials_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/accounts", {"name": "x"})
        self.assertEqual(ctx.exception.code, 400)

    def test_duplicate_name_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/accounts", {"name": "a", "session": "s"})
        self.assertEqual(ctx.exception.code, 400)

    def test_renew_refreshes_session_and_clears_cooldown(self):
        self.gateway.pool.get("a").cooldown(600, "test")
        _s, payload = self.post("/api/panel/accounts/renew", {"name": "a"})
        self.assertTrue(payload["ok"])
        acc = self.gateway.pool.get("a")
        self.assertFalse(acc.in_cooldown)
        self.assertAlmostEqual(acc.days_left, 14, delta=0.1)
        self.assertEqual(self.gateway.pool.client.fills, ["a"])

    def test_renew_unknown_account_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/accounts/renew", {"name": "nope"})
        self.assertEqual(ctx.exception.code, 400)

    def test_rebind_identity_rotates_campus_device_id(self):
        """rebind 轮换的是 campus_device_id（设备区分位）；devid 恒为客户端常量。"""
        acc = self.gateway.pool.get("a")
        before_campus = acc.identity["campus_device_id"]
        _s, payload = self.post("/api/panel/accounts/identity",
                                {"name": "a", "regenerate": True})
        self.assertEqual(payload["identity"]["devid"], "web")
        self.assertNotEqual(payload["identity"]["campus_device_id"], before_campus)
        self.assertEqual(self.gateway.pool.client.rebinds, ["a"])

    def test_identity_inspect_does_not_regenerate(self):
        before = self.gateway.pool.get("a").identity["devid"]
        _s, payload = self.post("/api/panel/accounts/identity", {"name": "a"})
        self.assertEqual(payload["identity"]["devid"], before)

    def test_toggle_enabled(self):
        _s, payload = self.post("/api/panel/accounts/update",
                                {"name": "a", "enabled": False})
        self.assertTrue(payload["ok"])
        self.assertFalse(self.gateway.pool.get("a").enabled)
        self.assertEqual(payload["state"]["totals"]["usable"], 0)

    def test_update_password_forces_new_login(self):
        _s, payload = self.post("/api/panel/accounts/update",
                                {"name": "a", "password": "new-pw", "login": True})
        acc = self.gateway.pool.get("a")
        self.assertEqual(acc.password, "new-pw")
        self.assertTrue(acc.session_valid)
        self.assertEqual(self.gateway.pool.client.fills, ["a"])

    def test_remove_account(self):
        _s, payload = self.post("/api/panel/accounts/remove", {"name": "a"})
        self.assertTrue(payload["ok"])
        self.assertIsNone(self.gateway.pool.get("a"))
        saved = json.loads((self.dir / "accounts.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["accounts"], [])

    def test_remove_unknown_account_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/accounts/remove", {"name": "nope"})
        self.assertEqual(ctx.exception.code, 400)

    def test_unknown_panel_endpoint_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/nonsense", {})
        self.assertEqual(ctx.exception.code, 404)

    def test_logs_endpoint(self):
        self.gateway.log("panel test line")
        _s, payload = self.get_json("/api/panel/logs?lines=20")
        self.assertTrue(payload["ok"])
        self.assertTrue(any("panel test line" in line for line in payload["lines"]))


class TestPanelAuth(PanelHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.gateway.cfg["api_keys"] = ["panel-key"]

    def test_state_requires_key(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get_json("/api/panel/state")
        self.assertEqual(ctx.exception.code, 401)

    def test_state_with_key(self):
        _s, payload = self.get_json("/api/panel/state",
                                    headers={"Authorization": "Bearer panel-key"})
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["config"]["auth_required"])

    def test_writes_require_key(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/accounts/remove", {"name": "a"})
        self.assertEqual(ctx.exception.code, 401)
        self.assertIsNotNone(self.gateway.pool.get("a"))

    def test_page_itself_stays_public(self):
        status, _response = self.get("/panel")
        self.assertEqual(status, 200)


class TestQuotaAutoRefresh(PanelHarness, unittest.TestCase):
    """面板 state() 轮询 → _refresh_all(only_stale=True) 走轻量刷新 + 秒级节流。"""

    def test_light_refresh_throttled_per_account(self):
        panel = self.gateway.panel
        pool = self.gateway.pool
        acc = pool.get("a")
        self.assertTrue(acc.session_valid)
        calls = []
        orig = pool.refresh_quota_light

        def counting(a):
            calls.append(a.name)
            return orig(a)

        pool.refresh_quota_light = counting
        try:
            acc.quota_updated_at = int(time.time())  # 刚刷过 → 跳过
            panel._refresh_all(only_stale=True)
            self.assertEqual(calls, [])
            acc.quota_updated_at = int(time.time()) - 3600  # 过期 → 刷
            panel._refresh_all(only_stale=True)
            self.assertEqual(calls, ["a"])
        finally:
            pool.refresh_quota_light = orig

    def test_state_reports_refresh_cadence(self):
        _status, payload = self.get_json("/api/panel/state")
        self.assertEqual(payload["config"]["panel_quota_refresh_seconds"], 60)


if __name__ == "__main__":
    unittest.main()
