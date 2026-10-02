"""微信扫码登录（真·跳转授权）的测试。

覆盖：授权 URL 拼装（与客户端逐字段一致）、回调解析容错、
以及面板 start / complete / bind 四个端点（bind=1 直登、bind=0 绑手机）。
全部离线（FakeAccountClient），不真扫码、不碰真实账号。
"""

from __future__ import annotations

import json
import re
import unittest
import urllib.parse

from loomy2api import constants as C
from loomy2api import wechat
from tests.test_panel import PanelHarness


class TestAuthUrl(unittest.TestCase):
    def test_matches_client_shape(self):
        url = wechat.build_auth_url("deadbeef")
        self.assertTrue(url.startswith("https://open.weixin.qq.com/connect/qrconnect?"))
        self.assertTrue(url.endswith("#wechat_redirect"))
        q = urllib.parse.parse_qs(url.split("?", 1)[1].split("#", 1)[0])
        self.assertEqual(q["appid"], [C.WECHAT_APP_ID])
        self.assertEqual(q["redirect_uri"], [C.WECHAT_REDIRECT_URI])
        self.assertEqual(q["response_type"], ["code"])
        self.assertEqual(q["scope"], ["snsapi_login"])
        self.assertEqual(q["state"], ["deadbeef"])

    def test_redirect_uri_is_url_encoded(self):
        url = wechat.build_auth_url("s1")
        self.assertIn("redirect_uri=https%3A%2F%2Floomy.xunfei.cn%2F", url)

    def test_requires_state(self):
        with self.assertRaises(ValueError):
            wechat.build_auth_url("")

    def test_appid_comes_from_client_env(self):
        # 从客户端 .env.prod 解出的微信开放平台网站应用 AppID
        self.assertEqual(C.WECHAT_APP_ID, "wx18d60be432287cf8")
        self.assertEqual(C.WECHAT_REDIRECT_URI,
                         "https://loomy.xunfei.cn/oauth/wechat/callback")


class TestParseCallback(unittest.TestCase):
    def test_full_url(self):
        got = wechat.parse_callback(
            "https://loomy.xunfei.cn/oauth/wechat/callback?code=ABC123&state=s1")
        self.assertEqual(got, {"code": "ABC123", "state": "s1"})

    def test_bare_query(self):
        got = wechat.parse_callback("code=ABC-123_xy&state=s2")
        self.assertEqual(got["code"], "ABC-123_xy")
        self.assertEqual(got["state"], "s2")

    def test_bare_code(self):
        code = "081Abc2XyZdefGhIjKlMnOpQr"
        self.assertEqual(wechat.parse_callback(code)["code"], code)

    def test_short_bare_input_is_rejected_with_guidance(self):
        """粘进来一小段别的东西时，要提示「去 404 页复制地址栏」而不是含糊报错。"""
        with self.assertRaises(ValueError) as ctx:
            wechat.parse_callback("abcd")
        self.assertIn("Ctrl", str(ctx.exception))

    def test_strips_quotes_and_whitespace(self):
        code = "081Abc2XyZdefGhIjKlMnOpQr"
        self.assertEqual(wechat.parse_callback(f'  "{code}"  ')["code"], code)

    def test_url_with_fragment(self):
        got = wechat.parse_callback(
            "https://loomy.xunfei.cn/cb?code=XYZ&state=s3#wechat_redirect")
        self.assertEqual(got["code"], "XYZ")

    def test_wechat_error_is_reported(self):
        with self.assertRaises(ValueError) as ctx:
            wechat.parse_callback("https://loomy.xunfei.cn/cb?errmsg=access_denied")
        self.assertIn("access_denied", str(ctx.exception))

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            wechat.parse_callback("")
        with self.assertRaises(ValueError):
            wechat.parse_callback("   ")

    def test_illegal_code_rejected(self):
        with self.assertRaises(ValueError):
            wechat.parse_callback("code=has%20space&state=s")

    def test_is_callback_heuristic(self):
        self.assertTrue(wechat.is_wechat_callback("code=a&state=b"))
        self.assertTrue(wechat.is_wechat_callback("ABC123"))
        self.assertFalse(wechat.is_wechat_callback(""))
        self.assertFalse(wechat.is_wechat_callback("not a url"))


class TestWechatEndpoints(PanelHarness, unittest.TestCase):
    def _post(self, path, payload):
        import urllib.request
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        return json.loads(urllib.request.urlopen(req, timeout=30).read())

    def _start(self):
        return self._post("/api/panel/login/wechat/start", {})

    # -- 生成授权链接 -------------------------------------------------
    def test_start_returns_qrconnect_url(self):
        r = self._start()
        self.assertTrue(r["ok"])
        self.assertIn("open.weixin.qq.com/connect/qrconnect", r["url"])
        self.assertIn("state=" + r["state"], r["url"])
        self.assertIn("loomy.xunfei.cn", r["url"])
        self.assertTrue(r["hint"])

    def test_start_url_has_our_state(self):
        r = self._start()
        q = urllib.parse.parse_qs(r["url"].split("?", 1)[1].split("#", 1)[0])
        self.assertEqual(q["state"], [r["state"]])

    # -- bind=1：已绑手机 → 直接换 session ----------------------------
    def test_complete_with_bound_wechat_logs_in_directly(self):
        self.gateway.pool.client.wechat_bind = "1"
        state = self._start()["state"]
        callback = f"https://loomy.xunfei.cn/oauth/wechat/callback?code=WXCODE1&state={state}"
        r = self._post("/api/panel/login/wechat/complete",
                       {"state": state, "callback": callback})
        self.assertTrue(r["ok"])
        self.assertFalse(r.get("needs_phone"))
        acc = self.gateway.pool.get(r["account"])
        self.assertIsNotNone(acc)
        self.assertTrue(acc.session_valid)
        self.assertEqual(acc.session, "wx-session")
        self.assertEqual(self.gateway.pool.client.wechat_skips, ["rcode-wechat-test"])

    def test_poll_reports_done_after_complete(self):
        self.gateway.pool.client.wechat_bind = "1"
        state = self._start()["state"]
        self._post("/api/panel/login/wechat/complete",
                   {"state": state, "callback": f"https://loomy.xunfei.cn/cb?code=C&state={state}"})
        _s, payload = self.get_json("/api/panel/login/poll?state=" + state)
        self.assertTrue(payload["done"])

    # -- bind=0：新微信 → 绑手机（注册） ------------------------------
    def test_complete_with_unbound_wechat_asks_for_phone(self):
        self.gateway.pool.client.wechat_bind = "0"
        state = self._start()["state"]
        r = self._post("/api/panel/login/wechat/complete",
                       {"state": state, "callback": f"https://loomy.xunfei.cn/cb?code=WXNEW&state={state}"})
        self.assertTrue(r["needs_phone"])
        _s, payload = self.get_json("/api/panel/login/poll?state=" + state)
        self.assertEqual(payload["status"], "needs_phone")

    def test_bind_phone_completes_registration(self):
        self.gateway.pool.client.wechat_bind = "0"
        state = self._start()["state"]
        self._post("/api/panel/login/wechat/complete",
                   {"state": state, "callback": f"https://loomy.xunfei.cn/cb?code=WXNEW2&state={state}"})
        sent = self._post("/api/panel/login/wechat/bind/send",
                          {"state": state, "phone": "13800000009"})
        self.assertEqual(sent["msgid"], "msgid-wechat-test")
        r = self._post("/api/panel/login/wechat/bind/submit",
                       {"state": state, "code": "654321"})
        self.assertTrue(r["ok"])
        acc = self.gateway.pool.get(r["account"])
        self.assertEqual(acc.session, "wx-session")
        self.assertEqual(acc.loginid, "13800000009")

    def test_bind_submit_before_send_is_rejected(self):
        import urllib.error
        import urllib.request
        self.gateway.pool.client.wechat_bind = "0"
        state = self._start()["state"]
        self._post("/api/panel/login/wechat/complete",
                   {"state": state, "callback": f"https://loomy.xunfei.cn/cb?code=W&state={state}"})
        body = json.dumps({"state": state, "code": "654321"}).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/api/panel/login/wechat/bind/submit", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(ctx.exception.code, 400)

    # -- 错误分支 ------------------------------------------------------
    def test_state_mismatch_is_rejected(self):
        import urllib.error
        import urllib.request
        state = self._start()["state"]
        body = json.dumps({
            "state": state,
            "callback": "https://loomy.xunfei.cn/cb?code=C&state=WRONG_STATE",
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/api/panel/login/wechat/complete", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(ctx.exception.code, 400)

    def test_bad_callback_is_rejected(self):
        import urllib.error
        import urllib.request
        state = self._start()["state"]
        body = json.dumps({"state": state, "callback": "不是链接"}).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/api/panel/login/wechat/complete", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(ctx.exception.code, 400)

    def test_unknown_state_is_rejected(self):
        import urllib.error
        import urllib.request
        body = json.dumps({"state": "nope", "callback": "code=C&state=nope"}).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/api/panel/login/wechat/complete", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(ctx.exception.code, 400)

    def test_auth_failure_surfaces_error(self):
        import urllib.error
        import urllib.request
        self.gateway.pool.client.wechat_fail_code = "BADCODE"
        state = self._start()["state"]
        body = json.dumps({
            "state": state,
            "callback": f"https://loomy.xunfei.cn/cb?code=BADCODE&state={state}",
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/api/panel/login/wechat/complete", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(ctx.exception.code, 400)
        _s, payload = self.get_json("/api/panel/login/poll?state=" + state)
        self.assertEqual(payload["status"], "error")
        self.assertIn("鉴权", payload["error"])

    def test_code_is_only_used_once_per_flow(self):
        """同一 state 完成后再提交应被状态机挡住（已完成）。"""
        self.gateway.pool.client.wechat_bind = "1"
        state = self._start()["state"]
        self._post("/api/panel/login/wechat/complete",
                   {"state": state, "callback": f"https://loomy.xunfei.cn/cb?code=ONCE&state={state}"})
        self.assertEqual(len(self.gateway.pool.client.wechat_auths), 1)


class TestQrParsing(unittest.TestCase):
    """二维码 uuid 提取 + 长轮询响应解析（纯函数，不联网）。"""

    PAGE = ('<html>...<script>// @cunjin 下面的变量是给开发者工具用的\n'
            'var fordevtool = "https://long.open.weixin.qq.com/connect/l/qrconnect'
            '?uuid=051g9z6Y2aSgnl2T"</script>...</html>')

    def test_extract_uuid_from_fordevtool(self):
        m = re.search(r'fordevtool\s*=\s*"([^"]*uuid=([A-Za-z0-9_\-]+))"', self.PAGE)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(2), "051g9z6Y2aSgnl2T")

    def test_qr_image_url(self):
        self.assertEqual(wechat.qr_image_url("ABC123"),
                         "https://open.weixin.qq.com/connect/qrcode/ABC123")

    def test_errcode_constants(self):
        self.assertEqual(wechat.ERR_WAITING, 408)
        self.assertEqual(wechat.ERR_SCANNED, 404)
        self.assertEqual(wechat.ERR_CONFIRMED, 405)
        self.assertEqual(wechat.ERR_CANCELLED, 403)
        self.assertEqual(wechat.ERR_EXPIRED, 402)


class TestQrEndpoints(PanelHarness, unittest.TestCase):
    """二维码主链路：服务端取码，用户零操作。"""

    def setUp(self):
        super().setUp()
        self.pool_client = self.gateway.pool.client

    def _post(self, path, payload):
        import urllib.request
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        return json.loads(urllib.request.urlopen(req, timeout=30).read())

    def _patch(self, qr=None, poll=None):
        from unittest import mock
        patches = []
        if qr is not None:
            patches.append(mock.patch.object(wechat, "fetch_login_qr", qr))
        if poll is not None:
            patches.append(mock.patch.object(wechat, "poll_login", poll))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_qr_start_returns_image_url(self):
        self._patch(qr=lambda **kw: {"uuid": "U1", "qr_url": wechat.qr_image_url("U1"),
                                     "auth_url": "x", "page_bytes": 1})
        r = self._post("/api/panel/login/wechat/qr/start", {})
        self.assertTrue(r["ok"])
        self.assertEqual(r["qr_url"], "https://open.weixin.qq.com/connect/qrcode/U1")
        self.assertTrue(r["state"])

    def test_qr_poll_waiting(self):
        self._patch(qr=lambda **kw: {"uuid": "U1", "qr_url": "q", "auth_url": "x",
                                     "page_bytes": 1},
                    poll=lambda uuid, last="", timeout=0: {
                        "errcode": wechat.ERR_WAITING, "code": "", "text": "等待扫码", "raw": ""})
        state = self._post("/api/panel/login/wechat/qr/start", {})["state"]
        r = self._post("/api/panel/login/wechat/qr/poll", {"state": state})
        self.assertEqual(r["status"], "waiting")

    def test_qr_poll_scanned(self):
        self._patch(qr=lambda **kw: {"uuid": "U1", "qr_url": "q", "auth_url": "x",
                                     "page_bytes": 1},
                    poll=lambda uuid, last="", timeout=0: {
                        "errcode": wechat.ERR_SCANNED, "code": "", "text": "已扫码", "raw": ""})
        state = self._post("/api/panel/login/wechat/qr/start", {})["state"]
        r = self._post("/api/panel/login/wechat/qr/poll", {"state": state})
        self.assertEqual(r["status"], "scanned")

    def test_qr_poll_expired(self):
        self._patch(qr=lambda **kw: {"uuid": "U1", "qr_url": "q", "auth_url": "x",
                                     "page_bytes": 1},
                    poll=lambda uuid, last="", timeout=0: {
                        "errcode": wechat.ERR_EXPIRED, "code": "", "text": "过期", "raw": ""})
        state = self._post("/api/panel/login/wechat/qr/start", {})["state"]
        r = self._post("/api/panel/login/wechat/qr/poll", {"state": state})
        self.assertEqual(r["status"], "expired")

    def test_qr_poll_confirmed_logs_in(self):
        """扫码确认 → 服务端拿到 code → 换 session → 进池。"""
        self.pool_client.wechat_bind = "1"
        self._patch(qr=lambda **kw: {"uuid": "U1", "qr_url": "q", "auth_url": "x",
                                     "page_bytes": 1},
                    poll=lambda uuid, last="", timeout=0: {
                        "errcode": wechat.ERR_CONFIRMED, "code": "WXCODE_OK",
                        "text": "已确认", "raw": ""})
        state = self._post("/api/panel/login/wechat/qr/start", {})["state"]
        r = self._post("/api/panel/login/wechat/qr/poll", {"state": state})
        self.assertTrue(r["ok"])
        acc = self.gateway.pool.get(r["account"])
        self.assertIsNotNone(acc)
        self.assertEqual(acc.session, "wx-session")
        self.assertEqual(self.pool_client.wechat_auths, [("WXCODE_OK", "wx")])

    def test_qr_poll_unbound_wechat_asks_for_phone(self):
        self.pool_client.wechat_bind = "0"
        self._patch(qr=lambda **kw: {"uuid": "U1", "qr_url": "q", "auth_url": "x",
                                     "page_bytes": 1},
                    poll=lambda uuid, last="", timeout=0: {
                        "errcode": wechat.ERR_CONFIRMED, "code": "WXCODE_NEW",
                        "text": "已确认", "raw": ""})
        state = self._post("/api/panel/login/wechat/qr/start", {})["state"]
        r = self._post("/api/panel/login/wechat/qr/poll", {"state": state})
        self.assertTrue(r["needs_phone"])

    def test_qr_poll_survives_transport_error(self):
        """长轮询超时/断连不该让前端炸掉，应继续等待。"""
        def boom(uuid, last="", timeout=0):
            raise TimeoutError("read timed out")
        self._patch(qr=lambda **kw: {"uuid": "U1", "qr_url": "q", "auth_url": "x",
                                     "page_bytes": 1}, poll=boom)
        state = self._post("/api/panel/login/wechat/qr/start", {})["state"]
        r = self._post("/api/panel/login/wechat/qr/poll", {"state": state})
        self.assertEqual(r["status"], "waiting")

    def test_qr_poll_unknown_state_rejected(self):
        import urllib.error
        import urllib.request
        body = json.dumps({"state": "nope"}).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/api/panel/login/wechat/qr/poll", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
