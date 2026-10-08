"""选号 / 会话粘性 / 在途租约的单元测试（借鉴 workbuddy2api-panel 的能力）。"""

from __future__ import annotations

import random
import time
import unittest

from loomy2api.account import Account
from loomy2api.routing import (
    InflightTracker, StickyRouter, session_key, weight_of, weighted_pick,
)


class TestSessionKey(unittest.TestCase):
    def test_explicit_conversation_id_wins(self):
        key = session_key({"conversation_id": "abc", "messages": [
            {"role": "user", "content": "hi"}]})
        self.assertEqual(key, "c-abc")

    def test_derives_stable_key_from_system_and_first_user(self):
        # 行为已变更（见 routing.session_key docstring）：用**最后一条** user
        # 消息派生。旧逻辑（system+首条）导致通用客户端每轮重发完整历史、
        # 键永远不变、多账号池退化为单账号（实测 779:13 分布）。
        # 同一条消息重试 → 同一个键（粘性只对重试生效）：
        a = session_key({"messages": [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hello"},
            {"role": "user", "content": "second turn"}]})
        b = session_key({"messages": [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hello"},
            {"role": "user", "content": "second turn"}]})
        self.assertTrue(a and a.startswith("d-"))
        self.assertEqual(a, b)
        # 不同轮次（最后一条 user 不同）→ 不同的键，轮换交还给选号策略：
        c = session_key({"messages": [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hello"},
            {"role": "user", "content": "DIFFERENT follow-up"}]})
        self.assertNotEqual(a, c)

    def test_different_conversations_get_different_keys(self):
        a = session_key({"messages": [{"role": "user", "content": "one"}]})
        b = session_key({"messages": [{"role": "user", "content": "two"}]})
        self.assertNotEqual(a, b)

    def test_multimodal_content_is_handled(self):
        key = session_key({"messages": [{"role": "user", "content": [
            {"type": "text", "text": "describe this"}]}]})
        self.assertTrue(key and key.startswith("d-"))

    def test_empty_messages_gives_none(self):
        self.assertIsNone(session_key({"messages": []}))
        self.assertIsNone(session_key({}))


class TestWeight(unittest.TestCase):
    def _acc(self, **kw):
        acc = Account(name="x")
        for k, v in kw.items():
            setattr(acc, k, v)
        return acc

    def test_more_credits_weighs_more(self):
        rich = self._acc(available=1000, last_used=time.time(), requests=1, failures=0)
        poor = self._acc(available=10, last_used=time.time(), requests=1, failures=0)
        now = time.time()
        self.assertGreater(weight_of(rich, 1000, now), weight_of(poor, 1000, now))

    def test_idle_account_gets_compensation(self):
        fresh = self._acc(available=500, last_used=time.time(), requests=1, failures=0)
        idle = self._acc(available=500, last_used=time.time() - 600, requests=1, failures=0)
        now = time.time()
        self.assertGreater(weight_of(idle, 1000, now), weight_of(fresh, 1000, now))

    def test_success_rate_matters(self):
        good = self._acc(available=500, last_used=time.time(), requests=9, failures=1)
        bad = self._acc(available=500, last_used=time.time(), requests=1, failures=9)
        now = time.time()
        self.assertGreater(weight_of(good, 1000, now), weight_of(bad, 1000, now))

    def test_weight_is_always_positive(self):
        dead = self._acc(available=0, last_used=0, requests=0, failures=0)
        self.assertGreater(weight_of(dead, 0, time.time()), 0)


class TestWeightedPick(unittest.TestCase):
    def test_returns_a_candidate(self):
        cands = [1, 2, 3]
        self.assertIn(weighted_pick(cands, lambda x: 1.0), cands)

    def test_empty_returns_none(self):
        self.assertIsNone(weighted_pick([], lambda x: 1.0))

    def test_respects_top_n(self):
        # 权重 100/1/1，top_n=1 → 永远选第一个（其余进不了短名单）
        cands = ["big", "small1", "small2"]
        weights = {"big": 100.0, "small1": 1.0, "small2": 1.0}
        rng = random.Random(7)
        picks = {weighted_pick(cands, lambda c: weights[c], top_n=1, rng=rng)
                 for _ in range(50)}
        self.assertEqual(picks, {"big"})

    def test_high_weight_wins_most_of_the_time(self):
        cands = ["a", "b"]
        weights = {"a": 100.0, "b": 1.0}
        rng = random.Random(11)
        wins = sum(1 for _ in range(400)
                   if weighted_pick(cands, lambda c: weights[c], top_n=2, rng=rng) == "a")
        self.assertGreater(wins, 300)

    def test_equal_weights_do_not_starve_the_last_candidate(self):
        """等权重 + 超过 top_n 时，排序靠后的候选也必须能进短名单（洗牌）。"""
        cands = ["a", "b", "c", "d", "e", "f", "g"]
        rng = random.Random(3)
        seen = set()
        for _ in range(200):
            seen.add(weighted_pick(cands, lambda c: 1.0, top_n=2, rng=rng))
        self.assertGreater(len(seen), 2, "等权重候选被字典序饿死")


class TestStickyRouter(unittest.TestCase):
    def test_bind_and_get(self):
        r = StickyRouter(ttl=60)
        r.bind("c-1", "accA")
        self.assertEqual(r.get("c-1"), "accA")

    def test_expiry(self):
        r = StickyRouter(ttl=0.01)
        r.bind("c-1", "accA")
        time.sleep(0.05)
        self.assertIsNone(r.get("c-1"))

    def test_rolling_renewal_keeps_active_session(self):
        # 余量放大，避免 CI/机器负载抖动导致偶发失败
        r = StickyRouter(ttl=1.0)
        r.bind("c-1", "accA")
        for _ in range(3):
            time.sleep(0.15)
            self.assertEqual(r.get("c-1"), "accA")   # 命中即续期

    def test_unbind_only_matching_name(self):
        r = StickyRouter(ttl=60)
        r.bind("c-1", "accA")
        r.unbind("c-1", "accB")                      # 名字不符 → 不解绑
        self.assertEqual(r.get("c-1"), "accA")
        r.unbind("c-1", "accA")
        self.assertIsNone(r.get("c-1"))

    def test_capacity_evicts(self):
        r = StickyRouter(ttl=60, max_entries=3)
        for i in range(10):
            r.bind(f"c-{i}", f"acc{i}")
        self.assertLessEqual(r.snapshot()["entries"], 3)

    def test_disabled_when_ttl_zero(self):
        r = StickyRouter(ttl=0)
        r.bind("c-1", "accA")
        self.assertIsNone(r.get("c-1"))

    def test_prune(self):
        r = StickyRouter(ttl=0.01)
        r.bind("c-1", "accA")
        time.sleep(0.05)
        self.assertEqual(r.prune(), 1)


class TestInflightTracker(unittest.TestCase):
    def test_disabled_when_limit_zero(self):
        t = InflightTracker(limit=0)
        for _ in range(100):
            t.acquire("a")
        self.assertFalse(t.full("a"))

    def test_full_at_limit(self):
        t = InflightTracker(limit=2)
        t.acquire("a")
        self.assertFalse(t.full("a"))
        t.acquire("a")
        self.assertTrue(t.full("a"))

    def test_release_frees_slot(self):
        t = InflightTracker(limit=1)
        t.acquire("a")
        self.assertTrue(t.full("a"))
        t.release("a")
        self.assertFalse(t.full("a"))

    def test_release_is_idempotent(self):
        t = InflightTracker(limit=1)
        t.release("a")
        t.release("a")
        self.assertFalse(t.full("a"))
        self.assertEqual(t.snapshot(), {})


if __name__ == "__main__":
    unittest.main()
