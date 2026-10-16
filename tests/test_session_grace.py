# -*- coding: utf-8 -*-
"""本地 session TTL 过期 != 上游凭证失效（2026-10-16 回归）。

根因：``Account.expire_at`` 不是上游下发的，是本地按
``constants.SESSION_EXPIRE_SECONDS``（客户端声称的 14 天）编出来的 ——
登录响应里根本没有可解析的过期字段。而 14 天严重偏短：实测 loomy-3147
本地「已过期 0.28 天」，上游 ``GET /points/records`` 仍回 HTTP 200
（available=3811）。

旧代码到期即 ``session_valid=False`` -> ``pool.usable()`` 把它滤掉 ->
流量全挤到唯一剩下的号 -> 那个号一遇上游 502/504 抖动就直接 502 甩给
客户端（日志里的 `账号池里没有可用账号（session 均不可用）`）。
凭证真伪只能由上游 401/403 终裁。
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from loomy2api import constants as C
from loomy2api.account import Account, _parse_expire_at
from loomy2api.pool import AccountPool, PoolError
from tests.support import make_config


class TestSessionGrace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = make_config(self.dir, "http://127.0.0.1:1/api/v1")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, accounts):
        (self.dir / "accounts.json").write_text(
            json.dumps({"accounts": accounts}), encoding="utf-8")

    def _pool(self, accounts, **overrides):
        if overrides:
            self.cfg.update(overrides)
        self._write(accounts)
        return AccountPool(self.cfg)

    # ------------------------------------------------------------ account

    def test_expired_ttl_still_valid_within_grace(self):
        """本地 14 天 TTL 过了但还在宽限内 -> 仍可用（核心回归）。"""
        acc = Account(name="a", session="s1",
                      expire_at=int(time.time()) - 2 * 86400)
        self.assertTrue(acc.session_stale)          # 本地确实过期了
        self.assertTrue(acc.session_valid)          # 但不判死

    def test_beyond_grace_is_invalid(self):
        acc = Account(name="a", session="s1",
                      expire_at=int(time.time()) - (C.SESSION_GRACE_SECONDS + 10))
        self.assertTrue(acc.session_stale)
        self.assertFalse(acc.session_valid)

    def test_no_session_is_invalid(self):
        self.assertFalse(Account(name="a", session="", expire_at=0).session_valid)
        self.assertFalse(Account(name="a", session="", expire_at=time.time()).session_valid)

    def test_zero_expire_is_unknown_and_valid(self):
        self.assertTrue(Account(name="a", session="s1", expire_at=0).session_valid)
        self.assertFalse(Account(name="a", session="s1", expire_at=0).session_stale)

    # --------------------------------------------------------------- pool

    def test_usable_prefers_valid_stale_is_reserve(self):
        """本地过期的号当预备队：有有效候选时不掺入，全员过期才放行。"""
        pool = self._pool([
            {"name": "loomy-3147", "session": "s1",
             "expireAt": int(time.time()) - 6 * 3600},
            {"name": "loomy-7280", "session": "s2",
             "expireAt": int(time.time()) + 2 * 86400},
        ])
        self.assertEqual([a.name for a in pool.usable()], ["loomy-7280"])
        # 7280 遇上游 502 进冷却后，3147 必须顶上（这正是当时全站 502 的根因）
        pool.report_failure(pool.get("loomy-7280"), 502, "bad gateway")
        pool.get("loomy-7280").cooldown_until = time.time() + 300
        self.assertEqual([a.name for a in pool.usable()], ["loomy-3147"])

    def test_acquire_prefers_locally_valid_over_stale(self):
        pool = self._pool([
            {"name": "stale", "session": "s1",
             "expireAt": int(time.time()) - 6 * 3600},
            {"name": "fresh", "session": "s2",
             "expireAt": int(time.time()) + 2 * 86400},
        ], strategy="balance")
        self.assertEqual(pool.acquire().name, "fresh")

    def test_acquire_returns_stale_when_only_option(self):
        """只剩本地过期的号：放行交给上游 401 终裁，而不是直接 502。"""
        pool = self._pool([
            {"name": "stale", "session": "s1",
             "expireAt": int(time.time()) - 6 * 3600},
        ])
        self.assertEqual(pool.acquire().name, "stale")

    def test_real_401_still_clears_session(self):
        """上游真的拒绝时，旧的清 session + 冷却行为必须保留。"""
        pool = self._pool([
            {"name": "a", "session": "s1",
             "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool.report_failure(pool.get("a"), 401, "invalid session")
        acc = pool.get("a")
        self.assertEqual(acc.session, "")
        self.assertFalse(acc.session_valid)
        self.assertTrue(acc.in_cooldown)

    def test_ensure_session_silent_within_grace(self):
        """宽限内 + 无密码：静默放行，不刷「请尽快短信重登」。"""
        logs = []
        self._write([
            {"name": "a", "loginid": "13800000001", "session": "s1",
             "expireAt": int(time.time()) - 3600},
        ])
        pool = AccountPool(self.cfg, logger=logs.append)
        acc = pool.get("a")
        pool.ensure_session(acc)
        pool.ensure_session(acc)
        self.assertEqual([m for m in logs if "重登" in m or "续期" in m], [])

    def test_warn_once_beyond_grace(self):
        """超出宽限期才告警，且同一批 session 只报一次。"""
        logs = []
        self._write([
            {"name": "a", "loginid": "13800000001", "session": "s1",
             "expireAt": int(time.time()) - (C.SESSION_GRACE_SECONDS + 86400)},
        ])
        pool = AccountPool(self.cfg, logger=logs.append)
        acc = pool.get("a")
        # ensure_session 对无 session 的才抛；这里有 session 所以走 warn 分支
        for _ in range(3):
            pool._warn_session_stale(acc)
        hits = [m for m in logs if "本地已过期" in m]
        self.assertEqual(len(hits), 1)

    def test_grace_days_zero_means_use_default(self):
        """session_grace_days 配 0 / 不配 = 用 constants 的 30 天默认。"""
        pool = self._pool([
            {"name": "a", "session": "s1",
             "expireAt": int(time.time()) - 60},
        ], session_grace_days=0)
        acc = pool.get("a")
        self.assertEqual(acc.grace_seconds, 0)          # 未覆盖
        self.assertTrue(acc.session_valid)              # 走 constants 默认
        self.assertEqual(C.SESSION_GRACE_SECONDS, 30 * 86400)

    def test_grace_days_positive_override_applied(self):
        """配正数 -> 覆盖 constants 默认，load() 时就已注入。"""
        pool = self._pool([
            {"name": "a", "session": "s1",
             "expireAt": int(time.time()) - 40 * 86400},
        ], session_grace_days=45)
        acc = pool.get("a")
        self.assertEqual(acc.grace_seconds, 45 * 86400)
        self.assertTrue(acc.session_valid)              # 45 天宽限 > 40 天过期

    def test_grace_days_shortens_grace(self):
        acc = Account(name="a", session="s1", grace_seconds=2 * 86400,
                      expire_at=int(time.time()) - 86400)
        self.assertTrue(acc.session_valid)
        acc2 = Account(name="a", session="s1", grace_seconds=2 * 86400,
                       expire_at=int(time.time()) - 3 * 86400)
        self.assertFalse(acc2.session_valid)

    # -------------------------------------------------- 上游真实过期字段

    def test_parse_expire_at_absolute_seconds(self):
        now = 1_800_000_000
        self.assertEqual(
            _parse_expire_at({"expireAt": now + 86400}, now), now + 86400)

    def test_parse_expire_at_millis(self):
        now = 1_800_000_000
        self.assertEqual(_parse_expire_at({"expireTime": (now + 86400) * 1000}, now),
                         now + 86400)

    def test_parse_expire_at_relative_seconds(self):
        now = 1_800_000_000
        self.assertEqual(_parse_expire_at({"expire": 1209600}, now), now + 1209600)
        self.assertEqual(_parse_expire_at({"expire_at": 1209600}, now), now + 1209600)
        self.assertEqual(_parse_expire_at({"expiresIn": 3600}, now), now + 3600)

    def test_parse_expire_at_missing_falls_back(self):
        self.assertEqual(_parse_expire_at({"session": "s"}, 123), 0)
        self.assertEqual(_parse_expire_at({"expireAt": 0}, 123), 0)
        self.assertEqual(_parse_expire_at({"expireAt": "abc"}, 123), 0)

    def test_extract_session_returns_expire_at(self):
        from loomy2api.account import _extract_session
        now = 1_800_000_000
        got = _extract_session({"data": {"session": "s", "userid": "u",
                                        "expireAt": now + 86400}})
        self.assertEqual(got["session"], "s")
        self.assertEqual(got["expire_at"], now + 86400)


if __name__ == "__main__":
    unittest.main()
