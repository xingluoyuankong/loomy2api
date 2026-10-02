"""配置热生效 / 首启自举 / 安全响应头的测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from loomy2api.config import Config, ensure_config, load_config


class TestEnsureConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_generates_config_with_random_key(self):
        path, key = ensure_config(self.dir / "config.json")
        self.assertTrue(path.exists())
        self.assertTrue(key and key.startswith("sk-loomy-"))
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["api_keys"], [key])
        self.assertEqual(payload["port"], 17890)

    def test_two_runs_produce_different_keys(self):
        _p1, k1 = ensure_config(self.dir / "c1.json")
        _p2, k2 = ensure_config(self.dir / "c2.json")
        self.assertNotEqual(k1, k2)

    def test_existing_config_is_left_alone(self):
        target = self.dir / "config.json"
        target.write_text('{"port": 1234}', encoding="utf-8")
        _path, key = ensure_config(target)
        self.assertIsNone(key)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["port"], 1234)


class TestConfigHotReload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "config.json"
        self.path.write_text(json.dumps({
            "port": 17890,
            "strategy": "weighted",
            "_comment": "keep me",
            "unknown_key": {"nested": 1},
        }), encoding="utf-8")
        self.cfg = load_config(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_patch_merges_and_hot_applies(self):
        restart = self.cfg.patch({"pick_top_n": 9})
        self.assertEqual(self.cfg["pick_top_n"], 9)
        self.assertEqual(restart, [])

    def test_patch_reports_restart_keys(self):
        restart = self.cfg.patch({"port": 18000})
        self.assertIn("port", restart)
        self.assertEqual(self.cfg["port"], 18000)

    def test_patch_preserves_unknown_keys(self):
        self.cfg.patch({"pick_top_n": 3})
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["_comment"], "keep me")
        self.assertEqual(raw["unknown_key"], {"nested": 1})

    def test_patch_deep_merges_nested_dicts(self):
        self.cfg.patch({"extra": {"a": 1}})
        self.cfg.patch({"extra": {"b": 2}})
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["extra"], {"a": 1, "b": 2})

    def test_patch_ignores_underscore_keys(self):
        self.cfg.patch({"_secret": "nope"})
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("_secret", raw)

    def test_reload_picks_up_external_edit(self):
        self.path.write_text(json.dumps({"strategy": "lru", "port": 17890}),
                             encoding="utf-8")
        self.cfg.reload()
        self.assertEqual(self.cfg["strategy"], "lru")

    def test_public_view_masks_api_keys(self):
        self.cfg["api_keys"] = ["sk-real-secret-value"]
        view = self.cfg.public_view()
        self.assertTrue(view["api_keys_set"])
        self.assertNotIn("sk-real-secret-value", json.dumps(view))
        self.assertIn("已设置", view["api_keys"][0])

    def test_patch_rejects_non_dict(self):
        with self.assertRaises(ValueError):
            self.cfg.patch(["not", "a", "dict"])


class TestDefaults(unittest.TestCase):
    def test_new_optimization_defaults_exist(self):
        cfg = Config()
        from loomy2api.config import DEFAULTS
        for key in ("pick_top_n", "soft_rate_base_seconds", "soft_rate_max_seconds",
                    "breaker_threshold", "breaker_cooldown_seconds",
                    "sticky_ttl_seconds", "sticky_max_entries",
                    "max_inflight_per_account", "security_headers",
                    "auto_generate_config", "not_found_cooldown_seconds"):
            self.assertIn(key, DEFAULTS, f"缺少默认值 {key}")

    def test_default_strategy_is_weighted(self):
        from loomy2api.config import DEFAULTS
        self.assertEqual(DEFAULTS["strategy"], "weighted")


if __name__ == "__main__":
    unittest.main()
