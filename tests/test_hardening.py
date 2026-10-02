"""安全加固 / 可观测性的端到端测试（hermetic，本地假上游）。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import unittest

from tests.test_panel import PanelHarness


class TestHealthAndHeaders(PanelHarness, unittest.TestCase):
    def test_healthz_carries_service_identity(self):
        for path in ("/health", "/healthz"):
            _status, payload = self.get_json(path)
            self.assertEqual(payload["service"], "loomy2api")
            self.assertEqual(payload["name"], "loomy2api")
            self.assertIn("healthy", payload)
            self.assertIn("total", payload)
            self.assertIn("sticky", payload)

    def test_panel_page_sends_security_headers(self):
        _status, response = self.get("/panel")
        headers = response.headers
        csp = headers.get("Content-Security-Policy") or ""
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")

    def test_security_headers_can_be_disabled(self):
        self.gateway.cfg["security_headers"] = False
        _status, response = self.get("/panel")
        self.assertIsNone(response.headers.get("Content-Security-Policy"))


class TestPanelConfigApi(PanelHarness, unittest.TestCase):
    def test_get_config_masks_keys(self):
        self.gateway.cfg["api_keys"] = ["sk-super-secret"]
        # 设了 Key 之后面板接口也要带 Key（/healthz 除外）
        _status, payload = self.get_json(
            "/api/panel/config", {"Authorization": "Bearer sk-super-secret"})
        self.assertTrue(payload["ok"])
        self.assertIn("config", payload)
        self.assertIn("restart_keys", payload)
        self.assertNotIn("sk-super-secret", json.dumps(payload))

    def test_post_config_patches_and_hot_applies(self):
        body = json.dumps({"config": {"pick_top_n": 7}}).encode("utf-8")
        request = urllib.request.Request(
            self.base + "/api/panel/config", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        response = urllib.request.urlopen(request, timeout=30)
        payload = json.loads(response.read())
        self.assertTrue(payload["ok"])
        self.assertEqual(self.gateway.cfg["pick_top_n"], 7)
        self.assertEqual(payload["restart_required"], [])

    def test_post_config_reports_restart_keys(self):
        body = json.dumps({"config": {"port": 19999}}).encode("utf-8")
        request = urllib.request.Request(
            self.base + "/api/panel/config", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        payload = json.loads(urllib.request.urlopen(request, timeout=30).read())
        self.assertIn("port", payload["restart_required"])

    def test_post_config_rejects_missing_object(self):
        body = json.dumps({"nope": 1}).encode("utf-8")
        request = urllib.request.Request(
            self.base + "/api/panel/config", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=30)
        self.assertEqual(ctx.exception.code, 400)


class TestConstantTimeAuth(PanelHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.gateway.cfg["api_keys"] = ["sk-correct-key"]

    def _get(self, headers):
        request = urllib.request.Request(self.base + "/v1/models", headers=headers)
        return urllib.request.urlopen(request, timeout=30)

    def test_correct_key_accepted(self):
        response = self._get({"Authorization": "Bearer sk-correct-key"})
        self.assertEqual(response.status, 200)

    def test_wrong_key_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get({"Authorization": "Bearer sk-wrong-key"})
        self.assertEqual(ctx.exception.code, 401)

    def test_no_key_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get({})
        self.assertEqual(ctx.exception.code, 401)

    def test_key_with_quotes_still_accepted(self):
        response = self._get({"Authorization": 'Bearer "sk-correct-key"'})
        self.assertEqual(response.status, 200)

    def test_health_stays_public(self):
        _status, payload = self.get_json("/healthz")
        self.assertEqual(payload["service"], "loomy2api")


if __name__ == "__main__":
    unittest.main()
