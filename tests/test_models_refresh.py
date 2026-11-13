"""Model catalogue recovery uses a short retry interval until upstream succeeds."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from loomy2api.pool import PoolError
from loomy2api.server import Gateway
from tests.support import make_config


class ModelRefreshRecoveryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = make_config(Path(self.tmp.name), "http://127.0.0.1:1")
        self.gateway = Gateway(cfg, log=lambda *_args: None)
        self.gateway.pool = mock.Mock()
        self.account = SimpleNamespace(session="fixture-session")
        self.gateway.pool.acquire.return_value = self.account
        self.live_models = [{"id": "live-model-fixture", "name": "Live fixture"}]
        self.gateway.models_client.models = mock.Mock(
            return_value={"data": self.live_models})

    def run_refresh_cycles(self, cycles):
        """Run the actual loop without threads, sleeping, or network calls."""
        stop = mock.Mock()
        stop.wait.side_effect = [False] * cycles + [True]
        with mock.patch("loomy2api.server.threading.Event", return_value=stop), \
                mock.patch("loomy2api.server.threading.Thread") as thread:
            self.gateway.start_models_refresher()
            thread.call_args.kwargs["target"]()
        return [call.args[0] for call in stop.wait.call_args_list]

    def test_recovers_after_an_account_is_added(self):
        self.gateway.pool.acquire.side_effect = PoolError("no usable account")
        fallback = self.gateway.refresh_models()
        self.assertTrue(fallback)
        self.gateway.models_client.models.assert_not_called()

        self.gateway.pool.acquire.side_effect = None
        self.assertEqual(self.run_refresh_cycles(1), [60, 24 * 3600])
        self.assertEqual(self.gateway.catalogue(), self.live_models)
        self.gateway.models_client.models.assert_called_once_with(
            self.account.session)
        self.gateway.pool.release.assert_called_once_with(self.account)

    def test_failed_recovery_keeps_fallback_and_short_retry(self):
        self.gateway.models_client.models.side_effect = OSError("upstream offline")
        fallback = self.gateway.refresh_models()
        self.assertEqual(self.run_refresh_cycles(2), [60, 60, 60])
        self.assertEqual(self.gateway.catalogue(), fallback)
        self.assertEqual(self.gateway.models_client.models.call_count, 3)
        self.assertEqual(self.gateway.pool.release.call_count, 3)

    def test_failed_refresh_keeps_last_good_catalogue_and_interval(self):
        self.gateway.cfg["models_refresh_hours"] = 2
        self.gateway.refresh_models()
        self.gateway.models_client.models.side_effect = OSError("upstream offline")
        self.assertEqual(self.run_refresh_cycles(1), [2 * 3600, 2 * 3600])
        self.assertEqual(self.gateway.catalogue(), self.live_models)

    def test_empty_response_keeps_last_good_catalogue(self):
        self.gateway.refresh_models()
        self.gateway.models_client.models.return_value = {"data": []}
        self.assertEqual(self.run_refresh_cycles(1), [24 * 3600, 24 * 3600])
        self.assertEqual(self.gateway.catalogue(), self.live_models)

    def test_empty_upstream_catalogue_still_uses_short_retry(self):
        self.gateway.models_client.models.return_value = {"data": []}
        self.gateway.refresh_models()
        self.gateway.catalogue()
        self.assertEqual(self.run_refresh_cycles(1), [60, 60])
        self.assertTrue(self.gateway.catalogue())

    def test_stop_does_not_start_an_upstream_request(self):
        self.gateway.catalogue()
        self.assertEqual(self.run_refresh_cycles(0), [60])
        self.gateway.models_client.models.assert_not_called()
