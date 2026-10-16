"""Account-pool tests: strategies, cooldown, persistence, session renewal."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from loomy2api.account import Account
from loomy2api.pool import AccountPool, PoolError
from tests.support import make_config


class TestPool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = make_config(self.dir, "http://127.0.0.1:1/api/v1")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, accounts):
        (self.dir / "accounts.json").write_text(
            json.dumps({"accounts": accounts}), encoding="utf-8")

    def test_load_and_snapshot(self):
        self._write([
            {"name": "a", "loginid": "13800000001", "password": "p",
             "session": "s1", "expireAt": int(time.time()) + 14 * 86400},
            {"name": "b", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)
        self.assertEqual([a.name for a in pool.accounts], ["a", "b"])
        snap = pool.snapshot()
        self.assertEqual(snap["count"], 2)
        self.assertEqual(snap["usable"], 2)
        self.assertNotIn('"password":', json.dumps(snap))   # secrets stay out

    def test_balance_strategy_prefers_richest(self):
        self._write([
            {"name": "poor", "session": "s1", "expireAt": int(time.time()) + 14 * 86400},
            {"name": "rich", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        # balance 是确定性策略（默认的 weighted 是加权随机，不适合断言单次结果）
        self.cfg["strategy"] = "balance"
        pool = AccountPool(self.cfg)
        pool.get("poor").available = 10
        pool.get("rich").available = 900
        self.assertEqual(pool.acquire().name, "rich")

    def test_round_robin_strategy(self):
        self._write([
            {"name": "a", "session": "s1", "expireAt": int(time.time()) + 14 * 86400},
            {"name": "b", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        self.cfg["strategy"] = "round_robin"
        pool = AccountPool(self.cfg)
        self.assertEqual([pool.acquire().name for _ in range(4)], ["a", "b", "a", "b"])

    def test_lru_strategy(self):
        self._write([
            {"name": "old", "session": "s1", "expireAt": int(time.time()) + 14 * 86400},
            {"name": "new", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        self.cfg["strategy"] = "lru"
        pool = AccountPool(self.cfg)
        pool.get("old").last_used = 100
        pool.get("new").last_used = 200
        self.assertEqual(pool.acquire().name, "old")

    def test_cooldown_skips_account_and_recovers(self):
        self._write([
            {"name": "a", "session": "s1", "expireAt": int(time.time()) + 14 * 86400},
            {"name": "b", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)
        pool.report_failure(pool.get("a"), 401, "invalid session")
        self.assertEqual(pool.acquire().name, "b")
        self.assertEqual(pool.usable()[0].name, "b")
        # a 401 clears the session, so it needs credentials to come back
        self.assertFalse(pool.get("a").session_valid)

    def test_exhausted_account_is_skipped(self):
        self._write([
            {"name": "a", "session": "s1", "expireAt": int(time.time()) + 14 * 86400},
            {"name": "b", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)
        pool.get("a").available = 0
        self.assertEqual(pool.acquire().name, "b")

    def test_acquire_raises_when_pool_empty(self):
        self._write([])
        pool = AccountPool(self.cfg)
        with self.assertRaises(PoolError):
            pool.acquire()

    def test_disabled_account_ignored(self):
        self._write([
            {"name": "a", "session": "s1", "expireAt": int(time.time()) + 86400,
             "enabled": False},
            {"name": "b", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)
        self.assertEqual([a.name for a in pool.usable()], ["b"])

    def test_add_remove_persists(self):
        self._write([])
        pool = AccountPool(self.cfg)
        pool.add_account("x", loginid="13800000009", password="pw")
        pool.flush()
        saved = json.loads((self.dir / "accounts.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["accounts"][0]["name"], "x")
        self.assertTrue(pool.remove_account("x"))
        self.assertFalse(pool.remove_account("x"))

    def test_duplicate_add_rejected(self):
        self._write([{"name": "x", "session": "s", "expireAt": int(time.time()) + 9e4}])
        pool = AccountPool(self.cfg)
        with self.assertRaises(PoolError):
            pool.add_account("x")

    def test_report_success_decrements_quota(self):
        self._write([{"name": "a", "session": "s", "expireAt": int(time.time()) + 9e4}])
        pool = AccountPool(self.cfg)
        acc = pool.get("a")
        acc.available = 100
        pool.report_success(acc, {"points_consumed": 3})
        self.assertEqual(acc.points_used, 3)
        self.assertEqual(acc.available, 97)
        self.assertEqual(acc.requests, 1)


    # ---- 2026-10-13 回归：僵尸账号不能杀死整个池 ----
    def test_ensure_session_valid_no_password_warns_and_allows(self):
        """session 有效但无密码做提前续期：放行+告警，不抛错。

        实锤：loomy-3147 剩 2.3 天 < 3 天续期阈值，无密码，
        旧逻辑直接抛 PoolError，等于提前 3 天把好号判死刑，
        流量全挤到单号上。
        """
        self._write([
            {"name": "zombie", "loginid": "13800000001",
             "session": "s1", "expireAt": int(time.time()) + 2 * 86400},
        ])
        pool = AccountPool(self.cfg)
        acc = pool.get("zombie")
        self.assertTrue(acc.session_valid)
        self.assertLess(acc.days_left, 3)
        pool.ensure_session(acc)                      # 不抛错
        self.assertEqual(pool.acquire().name, "zombie")

    def test_ensure_session_invalid_no_password_raises(self):
        """session 真过期了且无密码：照样抛错（防御）。"""
        self._write([
            {"name": "dead", "loginid": "13800000001",
             "session": "", "expireAt": 0},
        ])
        pool = AccountPool(self.cfg)
        with self.assertRaises(PoolError):
            pool.ensure_session(pool.get("dead"))

    def test_acquire_skips_expired_account(self):
        self._write([
            {"name": "dead", "session": "s1",
             "expireAt": int(time.time()) - 100},
            {"name": "alive", "session": "s2",
             "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)
        self.assertEqual(pool.acquire().name, "alive")

    def test_acquire_failover_loop_on_ensure_failure(self):
        """_pick 选中但 ensure_session 失败 → 换下一个候选，不抛错。"""
        from unittest import mock
        self._write([
            {"name": "a", "session": "s1",
             "expireAt": int(time.time()) + 14 * 86400},
            {"name": "b", "session": "s2",
             "expireAt": int(time.time()) + 14 * 86400},
        ])
        self.cfg["strategy"] = "balance"
        pool = AccountPool(self.cfg)
        pool.get("a").available = 9999      # balance 策略必选 a
        pool.get("b").available = 1
        real_ensure = pool.ensure_session

        def flaky(acc, **kw):
            if acc.name == "a":
                raise PoolError("session dead")
            return real_ensure(acc, **kw)

        with mock.patch.object(pool, "ensure_session", flaky):
            self.assertEqual(pool.acquire().name, "b")

    def test_acquire_all_dead_raises(self):
        from unittest import mock
        self._write([
            {"name": "a", "session": "s1",
             "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)
        with mock.patch.object(pool, "ensure_session",
                               side_effect=PoolError("dead")):
            with self.assertRaises(PoolError):
                pool.acquire()

    def test_sticky_expiry_forces_rotation(self):
        """绑定过期后重新选号：强制换号，不回旧号（2026-10-13 主人要轮换）。"""
        self._write([
            {"name": "a", "session": "s1",
             "expireAt": int(time.time()) + 14 * 86400},
            {"name": "b", "session": "s2",
             "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)
        pool.sticky.ttl = 0.05          # 50ms，快速过期
        acc1 = pool.acquire(session_key_value="c-1")
        pool.release(acc1)
        time.sleep(0.1)                # 绑定过期
        acc2 = pool.acquire(session_key_value="c-1")
        pool.release(acc2)
        # 必须换号：a→b 或 b→a，不许原地踏步
        self.assertNotEqual(acc2.name, acc1.name)



class TestSessionLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = make_config(self.dir, "http://127.0.0.1:1/api/v1")

    def tearDown(self):
        self.tmp.cleanup()

    def test_expiring_session_triggers_relogin(self):
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "pw",
             "session": "stale", "expireAt": int(time.time()) + 3600},   # < 3 days
        ])
        pool = AccountPool(self.cfg)

        calls = []

        class FakeClient:
            def fill(self, account):
                calls.append(account.name)
                account.session = "fresh"
                account.expire_at = int(time.time()) + 14 * 86400
                return account

        pool.client = FakeClient()
        pool.acquire()
        self.assertEqual(calls, ["a"])
        self.assertEqual(pool.get("a").session, "fresh")

    def test_no_credentials_raises_actionable_error(self):
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [{"name": "a"}])
        pool = AccountPool(self.cfg)
        with self.assertRaises(PoolError) as ctx:
            pool.acquire()
        self.assertIn("loginid/password", str(ctx.exception))

    def test_tick_refreshes_quota(self):
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "session": "s", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)

        class FakeGateway:
            def quota(self, session, proxy=None):
                return {"balance": 5, "daily_balance": 6, "available": 11}

        pool.gateway = FakeGateway()
        pool.tick()
        acc = pool.get("a")
        self.assertEqual((acc.balance, acc.daily_balance, acc.available), (5, 6, 11))

    def test_tick_clears_session_on_401(self):
        from loomy2api.upstream import UpstreamError
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "session": "s", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)

        class FakeGateway:
            def quota(self, session, proxy=None):
                raise UpstreamError("nope", 401)

        pool.gateway = FakeGateway()
        pool.tick()
        self.assertFalse(pool.get("a").session_valid)

    def test_save_sets_owner_only_permissions(self):
        import os
        import stat
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "session": "s", "password": "pw",
             "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)
        pool.save(force=True)
        mode = stat.S_IMODE(os.stat(self.dir / "accounts.json").st_mode)
        self.assertEqual(mode, 0o600)

    def test_refresh_quota_light_single_upstream_call(self):
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "session": "s", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)
        calls = []

        class FakeGateway:
            # 注意：故意不定义 points_totals / points_first_login ——
            # 轻量刷新要是敢调它们，这里直接 AttributeError 炸掉
            def quota(self, session, proxy=None):
                calls.append(session)
                return {"balance": 5, "daily_balance": 6, "available": 11}

        pool.gateway = FakeGateway()
        acc = pool.get("a")
        pool.refresh_quota_light(acc)
        self.assertEqual(calls, ["s"])
        self.assertEqual((acc.balance, acc.daily_balance, acc.available), (5, 6, 11))
        self.assertTrue(acc.quota_updated_at > 0)

    def test_refresh_quota_light_failure_keeps_stale(self):
        from loomy2api.upstream import UpstreamError
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "session": "s", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)

        class FakeGateway:
            def quota(self, session, proxy=None):
                raise UpstreamError("boom", 500)

        pool.gateway = FakeGateway()
        acc = pool.get("a")
        pool.refresh_quota_light(acc)
        # 失败不更新时间戳 → 下次轮询会重试，而不是静默认旧值
        self.assertEqual(acc.quota_updated_at, 0)
        self.assertIsNone(acc.available)

    def test_refresh_quota_full_updates_heavy_fields(self):
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "session": "s", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)

        class FakeGateway:
            def points_totals(self, session, proxy=None):
                return {"points_used": 42}

            def points_first_login(self, session, proxy=None):
                return {"data": {"dailyConsumed": 7}}

            def quota(self, session, proxy=None):
                return {"balance": 5, "daily_balance": 6, "available": 11}

        pool.gateway = FakeGateway()
        acc = pool.get("a")
        pool.refresh_quota(acc)
        self.assertEqual(acc.points_used, 42)
        self.assertEqual(acc.daily_consumed, 7)
        self.assertEqual(acc.available, 11)

if __name__ == "__main__":
    unittest.main()
