# coding: utf-8
"""Protección de llamadas: reintento SOLO ante throttling (429/APIGW.0308) y límite global."""

import threading
import time
import unittest
from types import SimpleNamespace

from tests.helpers import FAKE_AK, FAKE_SK, api_error

from core.clients import ClientFactory
from core.credentials import HuaweiCredentials
from core.throttling import CallGate, GuardedClient, RetryCounter, RetryPolicy, call_guarded, is_throttling
from scanning.settings import MAX_WORKERS_CAP, ScanSettings


def throttled():
    return api_error(429, "The throttling threshold has been reached: policy user over ratelimit,limit:10",
                     "APIGW.0308")


class Flaky:
    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0

    def __call__(self, request=None):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return "ok"


class TestRetry(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        self.policy = RetryPolicy(max_attempts=4, base_delay=1.0, max_delay=16.0, sleep=self.sleeps.append)
        self.counter = RetryCounter()

    def call(self, func):
        return call_guarded(func, policy=self.policy, gate=CallGate(4), counter=self.counter, label="t")

    def test_detects_throttling(self):
        self.assertTrue(is_throttling(throttled()))
        self.assertTrue(is_throttling(api_error(429, "x", "OTHER")))
        self.assertFalse(is_throttling(api_error(403, "x", "APIGW.0302")))
        self.assertFalse(is_throttling(ValueError()))

    def test_retries_throttling_then_succeeds(self):
        func = Flaky([throttled(), throttled()])
        self.assertEqual(self.call(func), "ok")
        self.assertEqual((func.calls, self.counter.count, len(self.sleeps)), (3, 2, 2))
        self.assertTrue(all(0 <= s <= 16 for s in self.sleeps))

    def test_gives_up_after_max_attempts(self):
        func = Flaky([throttled()] * 10)
        with self.assertRaises(Exception) as ctx:
            self.call(func)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(func.calls, 4)

    def test_never_retries_other_errors(self):
        for error in (api_error(401, "x", "APIGW.0301"), api_error(401, "x", "VPN.0003"),
                      api_error(403, "x", "SYS.0403"), api_error(500, "x", "ECS.0500")):
            with self.subTest(status=error.status_code):
                func = Flaky([error])
                with self.assertRaises(Exception):
                    self.call(func)
                self.assertEqual(func.calls, 1)
        self.assertEqual(self.sleeps, [])

    def test_backoff_is_bounded(self):
        policy = RetryPolicy(base_delay=1.0, max_delay=16.0)
        for attempt in range(10):
            self.assertLessEqual(policy.delay(attempt), 16.0)


class TestGate(unittest.TestCase):
    def test_limits_concurrent_calls_across_threads(self):
        gate = CallGate(2)

        def slow():
            time.sleep(0.03)
            return 1
        threads = [threading.Thread(target=call_guarded, args=(slow,),
                                    kwargs=dict(policy=RetryPolicy(), gate=gate, counter=RetryCounter()))
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((gate.peak, gate.active), (2, 0))

    def test_gate_released_on_error(self):
        gate = CallGate(1)
        with self.assertRaises(Exception):
            call_guarded(Flaky([api_error(500, "x", "E")]), policy=RetryPolicy(), gate=gate, counter=RetryCounter())
        self.assertEqual(gate.active, 0)


class TestGuardedClient(unittest.TestCase):
    def test_wraps_methods_only(self):
        inner = SimpleNamespace(list_things=Flaky([throttled()]), close=lambda: "closed", region="r")
        counter = RetryCounter()
        client = GuardedClient(inner, policy=RetryPolicy(sleep=lambda s: None), gate=CallGate(1), counter=counter)
        self.assertEqual(client.list_things(), "ok")
        self.assertEqual(counter.count, 1)
        self.assertEqual((client.close(), client.region), ("closed", "r"))

    def test_client_factory_returns_guarded_clients(self):
        built = SimpleNamespace(list_servers_details=lambda req: "servers")

        class Builder:
            def with_credentials(self, c): return self
            def with_region(self, r): return self
            def build(self): return built

        client_cls = SimpleNamespace(new_builder=lambda: Builder())
        region_cls = SimpleNamespace(value_of=lambda rid: SimpleNamespace(id=rid))
        factory = ClientFactory(HuaweiCredentials(ak=FAKE_AK, sk=FAKE_SK), gate=CallGate(3))
        client = factory.create(client_cls, region_cls, "ecs", "ap-southeast-3", "p")
        self.assertIsInstance(client, GuardedClient)
        self.assertEqual(client.list_servers_details(None), "servers")


class TestScanSettings(unittest.TestCase):
    def test_conservative_defaults(self):
        self.assertEqual(ScanSettings().as_dict(), {"max_workers": 4, "per_service": 2, "per_region": 3})

    def test_bounds_are_enforced(self):
        s = ScanSettings(max_workers=500, per_service=99, per_region=0)
        self.assertEqual((s.max_workers, s.per_service, s.per_region), (MAX_WORKERS_CAP, MAX_WORKERS_CAP, 1))
        self.assertEqual(ScanSettings(max_workers=0).max_workers, 1)

    def test_from_env(self):
        s = ScanSettings.from_env({"INVENTORY_SCAN_MAX_WORKERS": "6", "INVENTORY_SCAN_PER_SERVICE": "3",
                                   "INVENTORY_SCAN_PER_REGION": "x"})
        self.assertEqual(s.as_dict(), {"max_workers": 6, "per_service": 3, "per_region": 3})
        self.assertEqual(ScanSettings.from_env({}, max_workers=1).as_dict(),
                         {"max_workers": 1, "per_service": 1, "per_region": 1})


if __name__ == "__main__":
    unittest.main()
