"""分级冷却 / 熔断 / 会话粘性 / 在途租约的池级测试。

全部离线：不碰真实账号、不发请求。
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from loomy2api.account import Account
from loomy2api.pool import AccountPool, next_day_4am
from tests.support import make_config


class CooldownTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = make_config(self.dir, "http://127.0.0.1:1/api/v1")
        self._write([
            {"name": "a", "session": "s1", "expireAt": int(time.time()) + 14 * 86400},
            {"name": "b", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        self.pool = AccountPool(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, accounts):
        (self.dir / "accounts.json").write_text(
            json.dumps({"accounts": accounts}), encoding="utf-8")

    def _acc(self, name="a"):
        return self.pool.get(name)


class TestGradedCooldown(CooldownTestCase):
    def test_429_soft_cools_and_records_model(self):
        acc = self._acc()
        self.pool.report_failure(acc, 429, "rate limited", model="deepseek-v4-flash-0731")
        self.assertGreater(acc.cooldown_until, time.time())
        self.assertEqual(acc.cool_kind, "soft")
        self.assertTrue(acc.model_cooled("deepseek-v4-flash-0731"))
        # 模型级限流不该把账号整个打死（换模型还能用）
        self.assertFalse(acc.model_cooled("spark-x"))

    def test_429_backoff_grows_then_caps(self):
        acc = self._acc()
        self.cfg["soft_rate_base_seconds"] = 10
        self.cfg["soft_rate_max_seconds"] = 25
        acc.cool_kind = ""                       # 模拟"冷却已过期"
        acc.cooldown_until = 0
        self.pool.report_failure(acc, 429, "r1")
        first = acc.cooldown_until - time.time()
        acc.cool_kind = ""
        acc.cooldown_until = 0
        self.pool.report_failure(acc, 429, "r2")
        second = acc.cooldown_until - time.time()
        acc.cool_kind = ""
        acc.cooldown_until = 0
        self.pool.report_failure(acc, 429, "r3")
        third = acc.cooldown_until - time.time()
        self.assertGreater(second, first)
        self.assertLessEqual(third, 25.5)        # 封顶生效

    def test_429_while_already_cooling_does_not_extend(self):
        """冷却中的兜底探测再次 429 不得把冷却越堆越厚（原版踩过的坑）。"""
        acc = self._acc()
        self.cfg["soft_rate_base_seconds"] = 600
        self.cfg["soft_rate_max_seconds"] = 7200
        self.pool.report_failure(acc, 429, "first")
        first_until = acc.cooldown_until
        for _ in range(5):
            self.pool.report_failure(acc, 429, "probe again")
        self.assertAlmostEqual(acc.cooldown_until, first_until, delta=0.5)

    def test_429_with_reset_wall_clock_aligns(self):
        acc = self._acc()
        self.cfg["soft_rate_max_seconds"] = 7200
        reset_at = time.time() + 300
        self.pool.report_failure(acc, 429, "rate limited", reset_at=reset_at)
        self.assertAlmostEqual(acc.cooldown_until, reset_at, delta=1.0)

    def test_429_reset_wall_clock_is_capped(self):
        acc = self._acc()
        self.cfg["soft_rate_max_seconds"] = 60
        self.pool.report_failure(acc, 429, "rl", reset_at=time.time() + 99999)
        self.assertLessEqual(acc.cooldown_until - time.time(), 61)

    def test_402_hard_cools_until_next_4am(self):
        acc = self._acc()
        self.pool.report_failure(acc, 402, "quota exhausted")
        self.assertEqual(acc.cool_kind, "hard")
        expected = next_day_4am()
        self.assertAlmostEqual(acc.cooldown_until, expected, delta=1.0)

    def test_401_drops_session_and_cools(self):
        acc = self._acc()
        self.pool.report_failure(acc, 401, "unauthorized")
        self.assertFalse(acc.session_valid)
        self.assertEqual(acc.session, "")
        self.assertTrue(acc.in_cooldown)

    def test_404_short_cooldown(self):
        acc = self._acc()
        self.cfg["not_found_cooldown_seconds"] = 60
        self.pool.report_failure(acc, 404, "not found")
        self.assertLessEqual(acc.cooldown_until - time.time(), 61)

    def test_5xx_does_not_cool_the_account(self):
        acc = self._acc()
        self.pool.report_failure(acc, 504, "upstream hiccup")
        self.assertFalse(acc.in_cooldown)
        self.assertTrue(acc.session_valid)

    def test_breaker_trips_after_threshold(self):
        acc = self._acc()
        self.cfg["breaker_threshold"] = 3
        self.cfg["breaker_cooldown_seconds"] = 10
        for _ in range(2):
            self.pool.report_failure(acc, 503, "boom")
        self.assertFalse(acc.in_breaker)
        self.pool.report_failure(acc, 503, "boom")
        self.assertTrue(acc.in_breaker)

    def test_success_clears_soft_cooldown_and_breaker(self):
        acc = self._acc()
        self.pool.report_failure(acc, 429, "rl")
        acc.note_error(threshold=1, base=10)
        self.assertTrue(acc.in_breaker)
        self.pool.report_success(acc)
        self.assertFalse(acc.in_breaker)
        self.assertEqual(acc.soft_streak, 0)
        self.assertEqual(acc.cooldown_until, 0.0)


class TestPoolRouting(CooldownTestCase):
    def test_weighted_strategy_never_returns_a_cooled_account(self):
        self.cfg["strategy"] = "weighted"
        self.pool.report_failure(self._acc("a"), 401, "dead")
        for _ in range(20):
            picked = self.pool.acquire()
            self.assertEqual(picked.name, "b")
            self.pool.release(picked)

    def test_round_robin_rotates(self):
        self.cfg["strategy"] = "round_robin"
        names = []
        for _ in range(4):
            acc = self.pool.acquire()
            names.append(acc.name)
            self.pool.release(acc)
        self.assertEqual(names[:2], ["a", "b"])
        self.assertEqual(names[2:], ["a", "b"])

    def test_weighted_prefers_richer_account_most_of_the_time(self):
        """weighted 是加权随机：余额多的应该明显更常被选中，但不是每次都它。"""
        self.cfg["strategy"] = "weighted"
        self.pool.get("a").available = 900
        self.pool.get("b").available = 10
        wins = 0
        for _ in range(80):
            acc = self.pool.acquire()
            if acc.name == "a":
                wins += 1
            self.pool.release(acc)
        self.assertGreater(wins, 45, "余额多的账号应占多数")
        self.assertLess(wins, 80, "加权随机不应退化成严格最大（那是 balance）")

    def test_sticky_binds_same_session_to_same_account(self):
        self.cfg["strategy"] = "round_robin"
        first = self.pool.acquire(session_key_value="c-1")
        self.pool.release(first)
        for _ in range(5):
            again = self.pool.acquire(session_key_value="c-1")
            self.pool.release(again)
            self.assertEqual(again.name, first.name)

    def test_sticky_unbind_moves_to_another_account(self):
        self.cfg["strategy"] = "round_robin"
        first = self.pool.acquire(session_key_value="c-1")
        self.pool.release(first)
        self.pool.sticky.unbind("c-1", first.name)
        second = self.pool.acquire(session_key_value="c-1")
        self.pool.release(second)
        self.assertNotEqual(second.name, first.name)

    def test_inflight_limit_skips_busy_account(self):
        self.cfg["max_inflight_per_account"] = 1
        self.pool.apply_config()
        held = self.pool.acquire()               # 占住一个号
        other = self.pool.acquire(exclude=[held.name])
        self.assertNotEqual(other.name, held.name)
        self.pool.release(held)
        self.pool.release(other)

    def test_model_cooldown_falls_back_to_other_account(self):
        model = "deepseek-v4-flash-0731"
        self.pool.report_failure(self._acc("a"), 429, "rl", model=model)
        acc = self.pool.acquire(model=model)
        self.assertEqual(acc.name, "b")
        self.pool.release(acc)

    def test_apply_config_rebuilds_components(self):
        self.cfg["sticky_ttl_seconds"] = 0
        self.cfg["max_inflight_per_account"] = 3
        self.pool.apply_config()
        self.assertEqual(self.pool.sticky.ttl, 0.0)
        self.assertEqual(self.pool.inflight.limit, 3)


class TestNextDay4AM(unittest.TestCase):
    def test_before_4am_returns_today(self):
        import datetime as dt
        now = dt.datetime(2026, 10, 1, 2, 30, 0)
        target = dt.datetime.fromtimestamp(next_day_4am(now.timestamp()))
        self.assertEqual((target.day, target.hour), (1, 4))

    def test_after_4am_returns_tomorrow(self):
        import datetime as dt
        now = dt.datetime(2026, 10, 1, 9, 0, 0)
        target = dt.datetime.fromtimestamp(next_day_4am(now.timestamp()))
        self.assertEqual((target.day, target.hour), (2, 4))

    def test_crosses_month_end(self):
        import datetime as dt
        now = dt.datetime(2026, 10, 31, 23, 0, 0)
        target = dt.datetime.fromtimestamp(next_day_4am(now.timestamp()))
        self.assertEqual((target.month, target.day), (11, 1))


if __name__ == "__main__":
    unittest.main()
