import unittest

from nostr_dvm.paid_mode_guard import SettlementCredits, install_guard


class FakeRail:
    def __init__(self):
        self.settled = False

    def check_settled(self, _url):
        if self.settled:
            return True, "proof"
        return False, None


class FakeWorker:
    def __init__(self):
        self._PAYMENT_RAIL = FakeRail()
        self.GROQ_KEY = "present"
        self.PAID_ONLY = False
        self.inference_calls = 0

    @staticmethod
    def extract_amount_msat(_event):
        return 999999

    def run_inference(self, _kind, _content):
        self.inference_calls += 1
        return "ok"


class PaidGuardTests(unittest.TestCase):
    def test_credit_is_one_use(self):
        credits = SettlementCredits()
        self.assertFalse(credits.consume())
        credits.add()
        self.assertTrue(credits.consume())
        self.assertFalse(credits.consume())

    def test_amount_tag_is_not_payment_and_unsettled_inference_fails(self):
        worker = FakeWorker()
        install_guard(worker)
        self.assertTrue(worker.PAID_ONLY)
        self.assertEqual(worker.extract_amount_msat({"tags": [["amount", "999999"]]}), 0)
        with self.assertRaises(PermissionError):
            worker.run_inference(5002, "unpaid")
        self.assertEqual(worker.inference_calls, 0)

    def test_verified_settlement_allows_exactly_one_inference(self):
        worker = FakeWorker()
        install_guard(worker)
        worker._PAYMENT_RAIL.settled = True
        self.assertEqual(worker._PAYMENT_RAIL.check_settled("verify"), (True, "proof"))
        self.assertEqual(worker.run_inference(5002, "paid"), "ok")
        self.assertEqual(worker.inference_calls, 1)
        with self.assertRaises(PermissionError):
            worker.run_inference(5002, "second attempt")

    def test_missing_rail_fails_preflight(self):
        worker = FakeWorker()
        worker._PAYMENT_RAIL = None
        with self.assertRaises(RuntimeError):
            install_guard(worker)


if __name__ == "__main__":
    unittest.main()
