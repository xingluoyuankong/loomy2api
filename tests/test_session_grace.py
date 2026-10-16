# -*- coding: utf-8 -*-
"""v2 模型回归：本地 TTL 不做门槛、役龄调度、实测寿命学习、401 终裁。

模型（2026-10-16 定稿）：
* ``expire_at`` 是本地按客户端声称的 14 天编的假值（登录响应无过期字段），
  只用于展示/维护播报，**绝不参与路由判定**。
* ``session_valid`` = 手里有 session。凭证死活唯一权威 = 上游 401/403。
* 有密码的号按「役龄 ≥ session_renew_days（或实测寿命×0.8）」主动重登；
  无密码的号静默服役，维护通道每份 session 只播报一次役龄事实。
* 上游 401/403 时记录实测寿命（observed_lifetime，落盘）。
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from loomy2api import constants as C
from loomy2api.account import Account, _parse_expire_at, _extract_session
from loomy2api.pool import AccountPool, PoolError
from tests.support import make_config


class TestSessionModelV2(unittest.TestCase):
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

    # ------------------------------------------------------ 路由不做 TTL 门槛

    def test_claimed_expired_ttl_still_routes_normally(self):
        """loomy-3147 回归：本地「已过期」的号正常参与轮换，不当预备队。"""
        pool = self._pool([
            {"name": "loomy-3147", "session": "s1",
             "expireAt": int(time.time()) - 6 * 3600, "obtainedAt": int(time.time()) - 14 * 86400},
            {"name": "loomy-7280", "session": "s2",
             "expireAt": int(time.time()) + 2 * 86400, "obtainedAt": int(time.time()) - 11 * 86400},
        ], strategy="round_robin")
        self.assertTrue(pool.get("loomy-3147").session_valid)
        self.assertTrue(pool.get("loomy-3147").session_stale)   # TTL 过了，仅展示
        names = {pool.acquire().name for _ in range(4)}
        self.assertEqual(names, {"loomy-3147", "loomy-7280"})   # 正常轮换

    def test_expired_ttl_account_is_usable_even_when_alone(self):
        pool = self._pool([
            {"name": "a", "session": "s1", "expireAt": int(time.time()) - 86400},
        ])
        self.assertEqual([x.name for x in pool.usable()], ["a"])
        self.assertEqual(pool.acquire().name, "a")

    def test_real_death_is_empty_session(self):
        """唯一会把号踢出池子的是「手里没 session」（真实 401 清空后）。"""
        pool = self._pool([
            {"name": "a", "session": "s1", "expireAt": int(time.time()) + 9e4},
        ])
        pool.report_failure(pool.get("a"), 401, "invalid session")
        self.assertEqual(pool.get("a").session, "")
        self.assertFalse(pool.get("a").session_valid)
        self.assertEqual(pool.usable(), [])

    # ---------------------------------------------------- 实测寿命学习

    def test_401_records_observed_lifetime(self):
        obtained = int(time.time()) - 15 * 86400
        pool = self._pool([
            {"name": "a", "session": "s1", "expireAt": obtained + 14 * 86400,
             "obtainedAt": obtained},
        ])
        pool.report_failure(pool.get("a"), 401, "invalid session")
        acc = pool.get("a")
        self.assertGreater(acc.observed_lifetime, 14.9 * 86400)
        self.assertLess(acc.observed_lifetime, 15.1 * 86400)

    def test_403_also_records_lifetime(self):
        obtained = int(time.time()) - 20 * 86400
        pool = self._pool([
            {"name": "a", "session": "s1", "obtainedAt": obtained},
        ])
        pool.report_failure(pool.get("a"), 403, "forbidden")
        self.assertGreater(pool.get("a").observed_lifetime, 19.9 * 86400)

    def test_observed_lifetime_persists(self):
        obtained = int(time.time()) - 16 * 86400
        pool = self._pool([
            {"name": "a", "session": "s1", "obtainedAt": obtained},
        ])
        pool.report_failure(pool.get("a"), 401, "x")
        pool.save(force=True)
        raw = json.loads((self.dir / "accounts.json").read_text(encoding="utf-8"))
        self.assertGreater(raw["accounts"][0]["observedLifetime"], 15.9 * 86400)

    def test_observed_lifetime_recorded_only_once(self):
        obtained = int(time.time()) - 10 * 86400
        pool = self._pool([
            {"name": "a", "session": "s1", "obtainedAt": obtained},
        ])
        pool.report_failure(pool.get("a"), 401, "x")
        first = pool.get("a").observed_lifetime
        pool.get("a").obtained_at = obtained - 9 * 86400   # 改役龄再拒一次
        pool.report_failure(pool.get("a"), 401, "x")
        self.assertEqual(pool.get("a").observed_lifetime, first)  # 不被覆盖

    # ---------------------------------------------------- 役龄调度（密码号）

    def _pw_account(self, age_days, **extra):
        now = int(time.time())
        data = {"name": "pw", "loginid": "13800000001", "password": "p",
                "session": "s1", "obtainedAt": now - int(age_days * 86400),
                "expireAt": now + 86400}
        data.update(extra)
        return data

    def test_password_account_renewed_by_age(self):
        pool = self._pool([self._pw_account(11)], session_renew_days=10)
        acc = pool.get("pw")
        with mock.patch.object(pool.client, "fill") as fill:
            fill.side_effect = lambda a: (setattr(a, "session", "sNEW"),
                                          setattr(a, "obtained_at", int(time.time())))
            pool.ensure_session(acc)
        self.assertEqual(acc.session, "sNEW")
        self.assertEqual(fill.call_count, 1)

    def test_password_account_young_not_renewed(self):
        pool = self._pool([self._pw_account(3)], session_renew_days=10)
        acc = pool.get("pw")
        with mock.patch.object(pool.client, "fill") as fill:
            pool.ensure_session(acc)
        self.assertEqual(fill.call_count, 0)
        self.assertEqual(acc.session, "s1")

    def test_renew_threshold_uses_learned_lifetime(self):
        acc = Account(name="pw", loginid="1", password="p",
                      observed_lifetime=int(20 * 86400))
        pool = self._pool([])
        # 实测 20 天 → 阈值 min(10, 16) = 10（不冒进）
        self.assertEqual(pool._renew_after_days(acc), 10.0)
        acc2 = Account(name="pw", loginid="1", password="p",
                       observed_lifetime=int(6 * 86400))
        # 实测 6 天 → 阈值 min(10, 4.8) = 4.8（提前续）
        self.assertAlmostEqual(pool._renew_after_days(acc2), 4.8)

    def test_no_session_with_password_logs_in(self):
        pool = self._pool([{"name": "pw", "loginid": "1", "password": "p",
                            "session": "", "expireAt": 0}])
        acc = pool.get("pw")
        with mock.patch.object(pool.client, "fill") as fill:
            fill.side_effect = lambda a: (setattr(a, "session", "sNEW"),
                                          setattr(a, "obtained_at", int(time.time())))
            pool.ensure_session(acc)
        self.assertEqual(acc.session, "sNEW")

    # ---------------------------------------------------- 无密码号静默

    def test_no_password_account_silent_while_serving(self):
        logs = []
        now = int(time.time())
        self._write([{"name": "np", "loginid": "13800000001", "session": "s1",
                      "obtainedAt": now - 20 * 86400,
                      "expireAt": now - 6 * 86400}])   # 本地「已过期」6 天
        pool = AccountPool(self.cfg, logger=logs.append)
        acc = pool.get("np")
        for _ in range(5):
            pool.ensure_session(acc)                   # 不抛错、不刷日志
        self.assertEqual([m for m in logs if "重登" in m or "剩余" in m
                          or "短信" in m], [])
        self.assertTrue(acc.session_valid)

    def test_maintenance_note_once_per_session(self):
        logs = []
        now = int(time.time())
        self._write([{"name": "np", "loginid": "13800000001", "session": "s1",
                      "obtainedAt": now - 20 * 86400,
                      "expireAt": now - 6 * 86400}])
        pool = AccountPool(self.cfg, logger=logs.append)
        acc = pool.get("np")
        for _ in range(3):
            pool._note_session_age(acc)
        notes = [m for m in logs if "已服役" in m]
        self.assertEqual(len(notes), 1)
        self.assertIn("20", notes[0])                  # 报役龄事实
        self.assertIn("401", notes[0])                 # 指明权威判据

    def test_maintenance_note_skips_young_and_password_accounts(self):
        logs = []
        now = int(time.time())
        self._write([
            {"name": "young", "loginid": "13800000001", "session": "s1",
             "obtainedAt": now - 2 * 86400, "expireAt": now + 12 * 86400},
            {"name": "aged_pw", "loginid": "13800000002", "password": "p",
             "session": "s2", "obtainedAt": now - 30 * 86400,
             "expireAt": now - 16 * 86400},
        ])
        pool = AccountPool(self.cfg, logger=logs.append)
        pool._note_session_age(pool.get("young"))
        pool._note_session_age(pool.get("aged_pw"))
        self.assertEqual([m for m in logs if "已服役" in m], [])

    # ---------------------------------------------------- 展示字段

    def test_public_dict_age_fields(self):
        now = int(time.time())
        acc = Account(name="a", loginid="1", session="s1",
                      obtained_at=now - 5 * 86400,
                      observed_lifetime=int(18 * 86400))
        d = acc.public_dict()
        self.assertAlmostEqual(d["session_age_days"], 5.0, places=1)
        self.assertAlmostEqual(d["session_observed_lifetime_days"], 18.0, places=1)
        self.assertFalse(d["has_password"])

    # ---------------------------------------------------- 上游真实过期字段解析

    def test_parse_expire_at_absolute_seconds(self):
        now = 1_800_000_000
        self.assertEqual(_parse_expire_at({"expireAt": now + 86400}, now), now + 86400)

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
        now = 1_800_000_000
        got = _extract_session({"data": {"session": "s", "userid": "u",
                                        "expireAt": now + 86400}})
        self.assertEqual(got["session"], "s")
        self.assertEqual(got["expire_at"], now + 86400)


if __name__ == "__main__":
    unittest.main()
