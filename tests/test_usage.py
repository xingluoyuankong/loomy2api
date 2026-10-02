"""用量台账 / 账号级代理 / 新面板视图的测试。"""

from __future__ import annotations

import json
import time
import unittest

from loomy2api.usage import UsageLedger, extract_numbers
from tests.test_panel import PanelHarness


class TestUsageLedger(unittest.TestCase):
    def test_extract_numbers_tolerates_missing_fields(self):
        self.assertEqual(extract_numbers(None)["points"], 0)
        self.assertEqual(extract_numbers({})["prompt_tokens"], 0)
        nums = extract_numbers({"prompt_tokens": 10, "completion_tokens": 5,
                                "points_consumed": 3,
                                "completion_tokens_details": {"reasoning_tokens": 2}})
        self.assertEqual(nums, {"prompt_tokens": 10, "completion_tokens": 5,
                                "reasoning_tokens": 2, "points": 3})

    def test_input_output_alias(self):
        nums = extract_numbers({"input_tokens": 7, "output_tokens": 4})
        self.assertEqual((nums["prompt_tokens"], nums["completion_tokens"]), (7, 4))

    def test_record_and_recent(self):
        led = UsageLedger()
        led.record(account="a", model="m", status=200,
                   usage={"prompt_tokens": 10, "completion_tokens": 20, "points_consumed": 5},
                   latency=1.5, ttfb=0.4)
        recent = led.recent()
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["points"], 5)
        self.assertEqual(recent[0]["ttfb"], 0.4)

    def test_recent_is_newest_first(self):
        led = UsageLedger()
        for i in range(3):
            led.record(account=f"a{i}", model="m")
        names = [e["account"] for e in led.recent()]
        self.assertEqual(names, ["a2", "a1", "a0"])

    def test_filter_by_account_and_model(self):
        led = UsageLedger()
        led.record(account="a", model="m1")
        led.record(account="b", model="m1")
        led.record(account="a", model="m2")
        self.assertEqual(len(led.recent(account="a")), 2)
        self.assertEqual(len(led.recent(model="m1")), 2)

    def test_summary_aggregates(self):
        led = UsageLedger()
        led.record(account="a", model="m1", status=200,
                   usage={"prompt_tokens": 10, "completion_tokens": 20, "points_consumed": 5})
        led.record(account="a", model="m1", status=500, error="boom")
        led.record(account="b", model="m2", status=200,
                   usage={"prompt_tokens": 1, "completion_tokens": 2, "points_consumed": 1})
        s = led.summary()
        self.assertEqual(s["totals"]["requests"], 3)
        self.assertEqual(s["totals"]["ok"], 2)
        self.assertEqual(s["totals"]["failed"], 1)
        self.assertEqual(s["totals"]["points"], 6)
        by_model = {r["name"]: r for r in s["by_model"]}
        self.assertEqual(by_model["m1"]["requests"], 2)
        self.assertEqual(by_model["m2"]["points"], 1)

    def test_summary_sorted_by_points(self):
        led = UsageLedger()
        led.record(account="a", model="cheap", usage={"points_consumed": 1})
        led.record(account="a", model="pricey", usage={"points_consumed": 99})
        self.assertEqual(led.summary()["by_model"][0]["name"], "pricey")

    def test_ring_buffer_caps(self):
        led = UsageLedger(max_entries=10)
        for i in range(50):
            led.record(account=f"a{i}")
        self.assertEqual(len(led.recent(limit=100)), 10)

    def test_clear(self):
        led = UsageLedger()
        led.record(account="a")
        self.assertEqual(led.clear(), 1)
        self.assertEqual(led.summary()["totals"]["requests"], 0)


class TestAccountProxy(PanelHarness, unittest.TestCase):
    def test_add_account_with_proxy(self):
        status, payload = self.post("/api/panel/accounts", {
            "name": "proxied", "session": "s9", "proxy": "http://127.0.0.1:7890",
            "login": False})
        self.assertTrue(payload["ok"])
        acc = self.gateway.pool.get("proxied")
        self.assertEqual(acc.proxy, "http://127.0.0.1:7890")

    def test_proxies_view_reports_effective(self):
        self.gateway.cfg["proxy"] = "http://global:1080"
        self.gateway.pool.set_proxy("a", "http://per-account:7890")
        _status, payload = self.get_json("/api/panel/proxies")
        self.assertEqual(payload["global_proxy"], "http://global:1080")
        row = [a for a in payload["accounts"] if a["name"] == "a"][0]
        self.assertEqual(row["effective"], "http://per-account:7890")
        self.assertEqual(row["source"], "账号级")

    def test_proxy_falls_back_to_global(self):
        self.gateway.cfg["proxy"] = "http://global:1080"
        _status, payload = self.get_json("/api/panel/proxies")
        row = [a for a in payload["accounts"] if a["name"] == "a"][0]
        self.assertEqual(row["source"], "全局")
        self.assertEqual(row["effective"], "http://global:1080")

    def test_set_and_clear_proxy(self):
        self.post("/api/panel/accounts/proxy", {"name": "a", "proxy": "socks5://x:1080"})
        self.assertEqual(self.gateway.pool.get("a").proxy, "socks5://x:1080")
        self.post("/api/panel/accounts/proxy", {"name": "a", "proxy": ""})
        self.assertEqual(self.gateway.pool.get("a").proxy, "")

    def test_proxy_persisted_to_accounts_file(self):
        self.post("/api/panel/accounts/proxy", {"name": "a", "proxy": "http://p:1"})
        raw = json.loads((self.dir / "accounts.json").read_text(encoding="utf-8"))
        row = [a for a in raw["accounts"] if a["name"] == "a"][0]
        self.assertEqual(row["proxy"], "http://p:1")


class TestNewPanelViews(PanelHarness, unittest.TestCase):
    def test_usage_view(self):
        _status, payload = self.get_json("/api/panel/usage")
        self.assertTrue(payload["ok"])
        self.assertIn("summary", payload)
        self.assertIn("recent", payload)

    def test_points_view(self):
        _status, payload = self.get_json("/api/panel/points")
        self.assertTrue(payload["ok"])
        self.assertIn("totals", payload)
        self.assertIn("accounts", payload)

    def test_models_view_exposes_multiplier(self):
        _status, payload = self.get_json("/api/panel/models")
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["models"])
        self.assertIn("multiplier", payload["models"][0])
        self.assertIn("default_model", payload)

    def test_jobs_view(self):
        _status, payload = self.get_json("/api/panel/jobs")
        keys = {j["key"] for j in payload["jobs"]}
        self.assertEqual(keys, {"session_keeper", "quota_refresh", "sticky_prune"})

    def test_run_job_manually(self):
        _status, payload = self.post("/api/panel/jobs/run", {"key": "sticky_prune"})
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["job"]["key"], "sticky_prune")
        self.assertEqual(payload["job"]["runs"], 1)

    def test_run_unknown_job_rejected(self):
        import urllib.error
        import urllib.request
        body = json.dumps({"key": "nope"}).encode("utf-8")
        request = urllib.request.Request(
            self.base + "/api/panel/jobs/run", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=30)
        self.assertEqual(ctx.exception.code, 400)

    def test_state_carries_usage_totals(self):
        _status, payload = self.get_json("/api/panel/state")
        self.assertIn("usage", payload)
        self.assertIn("requests", payload["usage"])
        self.assertIn("pool", payload)


if __name__ == "__main__":
    unittest.main()
