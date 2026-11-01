import unittest
from loomy2api.panel import Panel
from loomy2api.pool import PoolError


class FakeAcc:
    def __init__(self, name):
        self.name = name; self.enabled = True; self.session = "sess-" + name
        self.proxy = None; self.identity = {}


class FakeGw:
    def __init__(self):
        self.bound = []
    def points_first_login(self, session, proxy=None, device_id=""):
        name = session.replace("sess-", "")
        return {"code": "000000", "data": {"alreadyProcessed": True, "currentBalance": 100,
            "invitationCodes": [{"inviteCode": "CODE-" + name, "status": "active", "usedCount": 0, "maxUses": 20}]}}
    def query_activation(self, session, proxy=None):
        return {"code": "000000", "data": {"activated": False}}
    def bind_invite_code(self, session, invite, device_id="", proxy=None):
        self.bound.append((session, invite))
        return {"code": "000000", "desc": "ok", "data": {}}


class FakePool:
    def __init__(self, gw):
        self.accounts = [FakeAcc("loomy-A"), FakeAcc("loomy-B")]
        self.gateway = gw
    def save(self): pass


class FakeGateway:
    def __init__(self):
        self.pool = FakePool(FakeGw())
        self.cfg = {}


class InviteTestCase(unittest.TestCase):
    def setUp(self):
        self.panel = Panel.__new__(Panel)
        self.panel.gw = FakeGateway()
        self.panel.log = lambda *a, **k: None
    def test_tasks_returns_all_accounts(self):
        out = self.panel.platform_tasks()
        self.assertEqual([a["account"] for a in out["accounts"]], ["loomy-A", "loomy-B"])
        self.assertEqual(out["accounts"][1]["checkin"]["invite_codes"][0]["inviteCode"], "CODE-loomy-B")
        # backward compat: top-level is first account
        self.assertEqual(out["checkin"]["invite_codes"][0]["inviteCode"], "CODE-loomy-A")
    def test_bind_to_named_account(self):
        out = self.panel.bind_invite({"invite_code": "XYZ", "account": "loomy-B"})
        self.assertTrue(out["ok"]); self.assertEqual(out["account"], "loomy-B")
        self.assertEqual(self.panel.gw.pool.gateway.bound, [("sess-loomy-B", "XYZ")])
    def test_bind_default_first_account(self):
        out = self.panel.bind_invite({"invite_code": "XYZ"})
        self.assertEqual(out["account"], "loomy-A")
    def test_bind_unknown_account_errors(self):
        with self.assertRaises(PoolError):
            self.panel.bind_invite({"invite_code": "XYZ", "account": "nope"})
    def test_tasks_no_account_errors(self):
        self.panel.gw.pool.accounts = []
        with self.assertRaises(PoolError):
            self.panel.platform_tasks()


if __name__ == "__main__":
    unittest.main()


class RedeemTestCase(unittest.TestCase):
    def setUp(self):
        self.panel = Panel.__new__(Panel)
        self.panel.gw = FakeGateway()
        self.panel.log = lambda *a, **k: None
        # FakeGw redeem
        def redeem(session, code, proxy=None):
            return {"code": "000000", "desc": "ok", "data": {}}
        self.panel.gw.pool.gateway.redeem_code = redeem
        self.panel.gw.pool.refresh_quota = lambda acc: None
    def test_redeem_to_named_account(self):
        out = self.panel.redeem({"code": "ABC", "account": "loomy-B"})
        self.assertTrue(out["ok"]); self.assertEqual(out["account"], "loomy-B")
    def test_redeem_unknown_account_errors(self):
        with self.assertRaises(PoolError):
            self.panel.redeem({"code": "ABC", "account": "nope"})
