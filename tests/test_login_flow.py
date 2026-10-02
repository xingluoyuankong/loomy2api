"""跳转登录（添加账号）流程的测试。

覆盖：state 会话生命周期 + 面板 start/send/submit/poll 四端点 + 登录后热加载进池。
全部离线（FakeAccountClient），不发真实短信、不碰真实账号。
"""

from __future__ import annotations

import json
import time
import unittest

from loomy2api.login_flow import DONE, ERROR, LoginFlow, SENT, FlowError
from tests.test_panel import PanelHarness


class TestLoginFlowUnit(unittest.TestCase):
    def test_start_returns_state_and_url_ready_fields(self):
        flow = LoginFlow(ttl=60)
        item = flow.start(phone="13800000000", name="main")
        self.assertTrue(item["state"])
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["phone"], "13800000000")

    def test_get_unknown_returns_none(self):
        self.assertIsNone(LoginFlow().get("nope"))
        self.assertIsNone(LoginFlow().get(""))

    def test_update_persists_fields(self):
        flow = LoginFlow()
        s = flow.start()["state"]
        flow.update(s, msgid="m1", status=SENT)
        item = flow.get(s)
        self.assertEqual(item["msgid"], "m1")
        self.assertEqual(item["status"], SENT)

    def test_update_unknown_raises(self):
        with self.assertRaises(FlowError):
            LoginFlow().update("nope", status=SENT)

    def test_expiry(self):
        flow = LoginFlow(ttl=30)
        s = flow.start()["state"]
        flow.update(s, status=SENT)
        with flow._lock:                       # 手动把 updated 推回过去
            flow._flows[s]["updated"] = time.time() - 31
        self.assertIsNone(flow.get(s))

    def test_poll_view_pending_then_done(self):
        flow = LoginFlow()
        s = flow.start()["state"]
        view = flow.poll_view(s)
        self.assertFalse(view["done"])
        self.assertEqual(view["status"], "pending")
        flow.finish(s, account="main", userid="u1")
        view = flow.poll_view(s)
        self.assertTrue(view["done"])
        self.assertEqual(view["account"], "main")
        self.assertEqual(view["userid"], "u1")

    def test_poll_view_expired(self):
        flow = LoginFlow()
        view = flow.poll_view("nope")
        self.assertFalse(view["done"])
        self.assertEqual(view["status"], "expired")
        self.assertTrue(view["error"])

    def test_fail_keeps_session_alive_for_retry(self):
        flow = LoginFlow()
        s = flow.start()["state"]
        flow.fail(s, "验证码错误")
        view = flow.poll_view(s)
        self.assertFalse(view["done"])
        self.assertEqual(view["status"], ERROR)
        self.assertIn("验证码", view["error"])
        # 还能重试：再次 update 即可
        flow.update(s, status=SENT, error="")
        self.assertEqual(flow.poll_view(s)["status"], SENT)

    def test_capacity_evicts_oldest(self):
        flow = LoginFlow(ttl=600, max_entries=3)
        for _ in range(8):
            flow.start()
        self.assertLessEqual(len(flow.snapshot()), 3)

    def test_prune(self):
        flow = LoginFlow(ttl=30)
        flow.start()
        with flow._lock:
            for v in flow._flows.values():
                v["updated"] = time.time() - 31
        self.assertEqual(flow.prune(), 1)


class TestJumpLoginEndpoints(PanelHarness, unittest.TestCase):
    def _post(self, path, payload):
        import urllib.request
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        return json.loads(urllib.request.urlopen(req, timeout=30).read())

    def _start(self, phone="", name=""):
        return self._post("/api/panel/login/start", {"phone": phone, "name": name})

    def test_start_creates_sms_session(self):
        r = self._start()
        self.assertTrue(r["ok"])
        self.assertTrue(r["state"])
        self.assertGreater(r["expires_in"], 0)

    def test_poll_pending(self):
        state = self._start()["state"]
        _status, payload = self.get_json("/api/panel/login/poll?state=" + state)
        self.assertFalse(payload["done"])
        self.assertEqual(payload["status"], "pending")

    def test_poll_unknown_state_reports_expired(self):
        _status, payload = self.get_json("/api/panel/login/poll?state=nope")
        self.assertFalse(payload["done"])
        self.assertEqual(payload["status"], "expired")

    def test_send_then_submit_adds_account_to_pool(self):
        start = self._start(phone="13800000001", name="jump")
        state = start["state"]
        sent = self._post("/api/panel/login/send", {"state": state,
                                                    "phone": "13800000001"})
        self.assertTrue(sent["ok"])
        self.assertEqual(sent["msgid"], "msgid-test")

        result = self._post("/api/panel/login/submit", {"state": state, "code": "123456"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["account"], "jump")
        acc = self.gateway.pool.get("jump")
        self.assertIsNotNone(acc)
        self.assertTrue(acc.session_valid)
        self.assertEqual(acc.loginid, "13800000001")

        # 轮询变成 done —— 面板据此自动刷新
        _s, payload = self.get_json("/api/panel/login/poll?state=" + state)
        self.assertTrue(payload["done"])
        self.assertEqual(payload["account"], "jump")

    def test_submit_without_send_still_goes_through(self):
        """没先点「发送」也**不本地拦截** —— 用户已经收到短信的情形下，
        因为簿记缺失把人挡回去是错的。msg 直接交上游判断。"""
        import urllib.error
        import urllib.request
        state = self._start()["state"]
        body = json.dumps({"state": state, "code": "123456"}).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/api/panel/login/submit", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            self.assertEqual(resp.status, 200)           # 假 client 登录成功
        except urllib.error.HTTPError as e:
            # 若上游拒绝，错误必须来自上游（checkCode），而不是本地簿记校验
            self.assertEqual(e.code, 400)
            self.assertNotIn("发送", e.read().decode("utf-8"))

    def test_submit_unknown_state_still_goes_through(self):
        """state 丢了（服务重启）也一样放行 —— phone 由请求/兜底记录提供。"""
        import urllib.error
        import urllib.request
        body = json.dumps({"state": "nope", "phone": "13900001234",
                           "code": "123456"}).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/api/panel/login/submit", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            self.assertEqual(resp.status, 200)
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)
            self.assertNotIn("会话", e.read().decode("utf-8"))

    def test_auto_generated_name_uses_phone_suffix(self):
        state = self._start(phone="13900001234")["state"]
        self._post("/api/panel/login/send", {"state": state, "phone": "13900001234"})
        result = self._post("/api/panel/login/submit", {"state": state, "code": "123456"})
        self.assertEqual(result["account"], "loomy-1234")

    def test_same_name_different_phone_gets_suffix(self):
        """同名但**不同手机号** → 新建一个带后缀的账号（绝不复用别人的登录态）。"""
        for phone in ("13800000001", "13800000002"):
            state = self._start(phone=phone, name="dup")["state"]
            self._post("/api/panel/login/send", {"state": state, "phone": phone})
            self._post("/api/panel/login/submit", {"state": state, "code": "123456"})
        self.assertIsNotNone(self.gateway.pool.get("dup"))
        self.assertIsNotNone(self.gateway.pool.get("dup-2"))
        self.assertEqual(self.gateway.pool.get("dup").loginid, "13800000001")
        self.assertEqual(self.gateway.pool.get("dup-2").loginid, "13800000002")

    def test_same_name_same_phone_relogs_into_same_account(self):
        """同名 + 同手机号 → 视为重新登录，不会多出一个账号。"""
        for _ in range(2):
            state = self._start(phone="13800000007", name="relog")["state"]
            self._post("/api/panel/login/send", {"state": state, "phone": "13800000007"})
            self._post("/api/panel/login/submit", {"state": state, "code": "123456"})
        names = [a.name for a in self.gateway.pool.accounts if a.name.startswith("relog")]
        self.assertEqual(names, ["relog"])

    def test_new_login_clears_old_cooldown(self):
        self.gateway.pool.add_account("jump", loginid="13800000001")
        acc = self.gateway.pool.get("jump")
        acc.cool(600, "soft", "stale")
        state = self._start(phone="13800000001", name="jump")["state"]
        self._post("/api/panel/login/send", {"state": state, "phone": "13800000001"})
        self._post("/api/panel/login/submit", {"state": state, "code": "123456"})
        self.assertEqual(acc.cooldown_until, 0.0)
        self.assertTrue(acc.session_valid)


if __name__ == "__main__":
    unittest.main()
