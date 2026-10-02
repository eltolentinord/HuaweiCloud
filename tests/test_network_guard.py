# coding: utf-8
"""La guardia de red de tests/__init__.py bloquea cualquier llamada real a Huawei Cloud."""

import socket
import unittest

from tests import ExternalNetworkBlocked
from tests.helpers import FAKE_AK, FAKE_SK

from collectors.ecs import EcsCollector
from core.clients import ClientFactory
from core.credentials import HuaweiCredentials
from core.engine import run_collector
from collectors.base import CollectorContext


class TestNetworkGuard(unittest.TestCase):
    def test_external_sockets_are_blocked(self):
        with self.assertRaises(ExternalNetworkBlocked):
            socket.create_connection(("ecs.ap-southeast-3.myhuaweicloud.com", 443), timeout=1)
        with self.assertRaises(ExternalNetworkBlocked):
            socket.socket().connect(("8.8.8.8", 53))

    def test_a_real_sdk_call_cannot_leave_the_machine(self):
        clients = ClientFactory(HuaweiCredentials(FAKE_AK, FAKE_SK))
        ctx = CollectorContext(clients=clients, region="ap-southeast-3", project_id="0" * 32)
        run = run_collector(EcsCollector(), ctx)  # cliente REAL del SDK, sin mocks
        self.assertIsNotNone(run.error)
        self.assertEqual(run.error.kind, "network")  # nunca llega a Huawei
