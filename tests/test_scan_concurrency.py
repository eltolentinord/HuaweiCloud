# coding: utf-8
"""Fase 3B: multi-proyecto, multi-región y ejecución concurrente controlada (SQLite).

Los workers no tocan la base de datos (escritor único), por eso SQLite en memoria
es válido aquí. La concurrencia ENTRE escaneos se prueba sobre PostgreSQL real en
``test_postgres_concurrency.py``.
"""

import io
import logging
import threading
import time
from collections import defaultdict

from sqlalchemy import select, text

from tests.helpers import FAKE_AK, FAKE_SK, api_error
from tests.scan_helpers import seed_account, server, simulated_huawei
from tests.test_scanning import ScanTestCase
from tests.test_throttling import throttled

from core.throttling import CallGate
from db.models import InventoryResource
from scanning.engine import scan_account
from scanning.settings import ScanSettings
from tenancy.errors import ValidationFailedError

PROJECTS = (("p-sg-a", "ap-southeast-3"), ("p-sg-b", "ap-southeast-3"),  # 2 proyectos, misma región
            ("p-mx", "la-north-2"), ("p-cl", "la-south-2"))


class Tracker:
    """Mide la concurrencia real por servicio, región y total (thread-safe)."""

    def __init__(self, delay=0.03):
        self.delay = delay
        self.lock = threading.Lock()
        self.active = defaultdict(int)
        self.peak = defaultdict(int)

    def wrap(self, handler, service):
        def tracked(region, project, request):
            keys = ("total", f"service:{service}", f"region:{region}")
            with self.lock:
                for k in keys:
                    self.active[k] += 1
                    self.peak[k] = max(self.peak[k], self.active[k])
            try:
                time.sleep(self.delay)
                return handler(region, project, request)
            finally:
                with self.lock:
                    for k in keys:
                        self.active[k] -= 1
        return tracked


class ConcurrencyTestCase(ScanTestCase):
    services = ("ecs", "evs", "obs")

    def setUp(self):
        super().setUp()
        self.client, self.account, self.projects = seed_account(
            self.session, self.keyring, client_name="Multi", projects_spec=PROJECTS)
        self.world.servers = {hid: [server(f"srv-{hid}-1"), server(f"srv-{hid}-2")] for hid, _ in PROJECTS}
        self.world.volumes = {hid: [{"id": f"vol-{hid}", "size": 10}] for hid, _ in PROJECTS}
        self.tracker = Tracker()
        base = self.world.handlers()
        for method, service in (("list_servers_details", "ecs"), ("list_volumes", "evs"), ("list_buckets", "obs")):
            self.world.extra[method] = self.tracker.wrap(base[method], service)

    def by_scope(self, service="ecs"):
        rows = self.session.scalars(select(InventoryResource).where(InventoryResource.account_id == self.account.id,
                                                                    InventoryResource.service == service))
        result = defaultdict(set)
        for r in rows:
            result[r.project_id].add((r.provider_id, r.deleted_at is None))
        return result


class TestMultiProjectMultiRegion(ConcurrencyTestCase):
    def test_every_project_scanned_with_its_own_project_id_and_region(self):
        run = self.scan(settings=ScanSettings(max_workers=4))
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.total_tasks, 1 + 4 * 2)  # OBS global + 4 proyectos × (ECS, EVS)
        ecs_calls = {(region, project) for m, region, project in self.world.calls if m == "list_servers_details"}
        self.assertEqual(ecs_calls, {(region, hid) for hid, region in PROJECTS})
        self.assertEqual(sorted(run.stats["regions"]), ["ap-southeast-3", "la-north-2", "la-south-2"])
        scopes = self.by_scope()
        self.assertEqual(len(scopes), 4)  # un ámbito por proyecto, aunque compartan región
        self.assertEqual(scopes[self.projects["p-sg-b"].id], {("srv-p-sg-b-1", True), ("srv-p-sg-b-2", True)})

    def test_same_region_projects_are_independent_scopes(self):
        self.scan(services=["ecs"])
        self.world.servers["p-sg-a"] = []
        run = self.scan(services=["ecs"])
        scopes = self.by_scope()
        self.assertEqual(run.total_deleted, 2)
        self.assertTrue(all(not alive for _, alive in scopes[self.projects["p-sg-a"].id]))
        self.assertTrue(all(alive for _, alive in scopes[self.projects["p-sg-b"].id]))  # misma región, otro proyecto

    def test_region_filter(self):
        run = self.scan(services=["ecs"], regions=["ap-southeast-3"])
        self.assertEqual(run.total_tasks, 2)
        self.assertEqual({r for _, r, _ in self.world.calls}, {"ap-southeast-3"})
        self.assertEqual(run.stats["filters"]["regions"], ["ap-southeast-3"])

    def test_project_filter_and_untouched_scopes(self):
        self.scan(services=["ecs"])
        self.world.servers = {hid: [] for hid, _ in PROJECTS}  # todo "desaparece"...
        run = self.scan(services=["ecs"], project_ids=[self.projects["p-mx"].id])
        self.assertEqual(run.total_tasks, 1)
        scopes = self.by_scope()
        self.assertTrue(all(not alive for _, alive in scopes[self.projects["p-mx"].id]))
        for hid in ("p-sg-a", "p-sg-b", "p-cl"):  # ...pero solo se escaneó p-mx
            self.assertTrue(all(alive for _, alive in scopes[self.projects[hid].id]), hid)

    def test_invalid_filters_rejected(self):
        with self.assertRaises(ValidationFailedError):
            self.scan(regions=["eu-west-101"])
        other_client, other_account, other_projects = seed_account(
            self.session, self.keyring, client_name="Other", projects_spec=(("p-x", "ap-southeast-3"),))
        with self.assertRaises(ValidationFailedError):  # proyecto de otra cuenta
            self.scan(project_ids=[other_projects["p-x"].id])

    def test_obs_once_with_parallel_workers(self):
        self.scan(settings=ScanSettings(max_workers=8))
        self.assertEqual(len([c for c in self.world.calls if c[0] == "list_buckets"]), 1)


class TestControlledConcurrency(ConcurrencyTestCase):
    def test_limits_respected_and_work_actually_parallel(self):
        settings = ScanSettings(max_workers=4, per_service=2, per_region=2)
        run = self.scan(settings=settings)
        peak = self.tracker.peak
        self.assertLessEqual(peak["total"], 4)
        self.assertLessEqual(max(v for k, v in peak.items() if k.startswith("service:")), 2)
        self.assertLessEqual(max(v for k, v in peak.items() if k.startswith("region:")), 2)
        self.assertGreaterEqual(peak["total"], 2, "debería haber ejecución paralela real")
        self.assertEqual(run.stats["concurrency"]["max_workers"], 4)
        self.assertLessEqual(run.stats["concurrency"]["peak"]["per_service"], 2)

    def test_sequential_mode(self):
        self.scan(settings=ScanSettings(max_workers=1))
        self.assertEqual(self.tracker.peak["total"], 1)

    def test_parallel_and_sequential_produce_same_inventory(self):
        self.scan(settings=ScanSettings(max_workers=1))
        sequential = self.by_scope()
        self.world.servers["p-mx"].append(server("srv-new"))
        self.world.servers["p-cl"] = []
        run = self.scan(settings=ScanSettings(max_workers=8, per_service=8, per_region=8))
        parallel = self.by_scope()
        self.assertEqual((run.total_created, run.total_deleted), (1, 2))
        self.assertEqual(parallel[self.projects["p-sg-a"].id], sequential[self.projects["p-sg-a"].id])
        self.assertIn(("srv-new", True), parallel[self.projects["p-mx"].id])

    def test_global_call_gate_across_workers(self):
        gate = CallGate(1)
        with simulated_huawei(self.world, gate=gate):
            scan_account(self.factory, self.keyring, client_id=self.client.id, account_id=self.account.id,
                         services=list(self.services), settings=ScanSettings(max_workers=8))
        self.assertEqual(gate.peak, 1)  # el límite de proceso manda sobre max_workers
        self.assertEqual(self.tracker.peak["total"], 1)

    def test_failure_in_one_project_does_not_block_or_delete(self):
        self.scan(services=["ecs"])
        base = self.world.extra["list_servers_details"]

        def flaky(region, project, request):
            if project == "p-mx":
                raise api_error(500, "Internal error", "ECS.5000")
            return base(region, project, request)
        self.world.extra["list_servers_details"] = flaky
        self.world.servers["p-sg-a"] = []  # en p-sg-a sí desaparecen
        run = self.scan(services=["ecs"], settings=ScanSettings(max_workers=4))
        self.assertEqual(run.status, "completed_with_errors")
        scopes = self.by_scope()
        self.assertTrue(all(alive for _, alive in scopes[self.projects["p-mx"].id]))       # falló: intacto
        self.assertTrue(all(not alive for _, alive in scopes[self.projects["p-sg-a"].id]))  # éxito: borrado lógico

    def test_denied_and_partial_never_delete_in_parallel(self):
        self.scan(services=["ecs", "evs"])
        self.world.servers = {hid: [] for hid, _ in PROJECTS}
        self.world.volumes = {hid: [] for hid, _ in PROJECTS}
        self.world.failures["list_servers_details"] = api_error(403, "Forbidden", "Ecs.0403")
        self.world.failures["list_volumes"] = api_error(401, "Insufficient authentication for action "
                                                             "evs:volumes:list", "EVS.0003")
        run = self.scan(services=["ecs", "evs"], settings=ScanSettings(max_workers=4))
        self.assertEqual((run.status, run.total_deleted, run.total_errors), ("completed_with_warnings", 0, 0))
        rows = self.session.scalars(select(InventoryResource).where(InventoryResource.account_id == self.account.id))
        self.assertTrue(all(r.deleted_at is None for r in rows))


class TestThrottlingDuringScan(ConcurrencyTestCase):
    def test_throttling_is_retried_and_counted(self):
        attempts = defaultdict(int)
        base = self.world.extra["list_servers_details"]

        def throttle_twice(region, project, request):
            attempts[project] += 1
            if attempts[project] <= 2:
                raise throttled()
            return base(region, project, request)
        self.world.extra["list_servers_details"] = throttle_twice
        run = self.scan(services=["ecs"], settings=ScanSettings(max_workers=4))
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.stats["throttle_retries"], 2 * len(PROJECTS))
        self.assertEqual(run.total_resources, 2 * len(PROJECTS))

    def test_persistent_throttling_fails_task_without_deleting(self):
        self.scan(services=["ecs"])

        def always(region, project, request):
            raise throttled()
        self.world.extra["list_servers_details"] = always
        run = self.scan(services=["ecs"])
        self.assertEqual((run.status, run.total_deleted), ("failed", 0))
        self.assertEqual(run.stats["throttle_retries"], 3 * len(PROJECTS))  # 4 intentos por tarea
        rows = self.session.scalars(select(InventoryResource).where(InventoryResource.service == "ecs"))
        self.assertTrue(all(r.deleted_at is None for r in rows))


class TestNoSecretsDuringParallelScan(ConcurrencyTestCase):
    def test_logs_and_database_free_of_secrets(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            self.world.servers["p-mx"] = [server("srv-s", f"leak-{FAKE_SK}", metadata={"k": FAKE_AK})]
            self.world.failures["list_volumes"] = api_error(401, f"bad {FAKE_AK} {FAKE_SK}", "APIGW.0301")
            self.scan(settings=ScanSettings(max_workers=4))
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)
        dump = stream.getvalue() + "".join(
            repr(self.session.execute(text(f"SELECT * FROM {t}")).all())
            for t in ("cloud_accounts", "scan_runs", "scan_tasks", "resources"))
        self.assertNotIn(FAKE_AK, dump)
        self.assertNotIn(FAKE_SK, dump)
