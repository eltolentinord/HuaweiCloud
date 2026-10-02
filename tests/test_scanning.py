# coding: utf-8
"""Motor de escaneo persistente: ScanRun/ScanTask, upsert, deleted_at, OBS global (SQLite)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select, text

from tests.db_helpers import SqliteTestCase, make_keyring
from tests.helpers import FAKE_AK, FAKE_SK, api_error
from tests.scan_helpers import ScanWorld, seed_account, server, simulated_huawei

from collectors.base import ALL_GRANTED_EPS
from core.serialization import REDACTED, fingerprint
from db.models import InventoryResource, ScanRun, ScanTask
from scanning.engine import create_scan, execute_scan, plan_tasks, scan_account
from scanning.settings import ScanSettings
from tenancy import accounts
from tenancy.errors import ConflictError, InvalidStateError, NotFoundError, ValidationFailedError


class ScanTestCase(SqliteTestCase):
    services = ("ecs", "evs", "obs")

    def setUp(self):
        super().setUp()
        self.client, self.account, self.projects = seed_account(self.session, self.keyring)
        self.world = ScanWorld()
        self.world.servers = {"p-sg": [server("srv-1", "web"), server("srv-2", "db")],
                              "p-mx": [server("srv-3", "mx-app")]}
        self.world.volumes = {"p-sg": [{"id": "vol-1", "size": 40}], "p-mx": []}
        self.world.buckets = [{"name": "logs-sg", "location": "ap-southeast-3"},
                              {"name": "backup-mx", "location": "la-north-2"}]

    def scan(self, services=None, client_id=None, account_id=None, keyring=None, settings=None, **filters):
        with simulated_huawei(self.world):
            run_id = scan_account(self.factory, keyring or self.keyring,
                                  client_id=client_id or self.client.id,
                                  account_id=account_id or self.account.id,
                                  services=services or self.services, settings=settings, **filters)
        self.session.expire_all()
        return self.session.get(ScanRun, run_id)

    def tasks(self, run):
        return {(t.service, t.region): t for t in self.session.scalars(
            select(ScanTask).where(ScanTask.scan_run_id == run.id))}

    def resources(self, **filters):
        query = select(InventoryResource)
        for key, value in filters.items():
            query = query.where(getattr(InventoryResource, key) == value)
        return {r.provider_id: r for r in self.session.scalars(query)}


class TestScanRun(ScanTestCase):
    def test_successful_run(self):
        run = self.scan()
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.total_tasks, 5)  # OBS 1 (global) + 2 proyectos × (ECS, EVS)
        self.assertEqual((run.total_resources, run.total_created, run.total_errors), (6, 6, 0))
        self.assertIsNotNone(run.started_at)
        self.assertIsNotNone(run.finished_at)
        self.assertGreaterEqual(run.duration_ms, 0)
        self.assertEqual(run.stats["tasks_by_status"], {"succeeded": 5})
        self.assertEqual(run.stats["regions"], ["ap-southeast-3", "la-north-2"])

    def test_tasks_record_project_service_and_counts(self):
        tasks = self.tasks(self.scan())
        ecs_sg = tasks[("ecs", "ap-southeast-3")]
        self.assertEqual((ecs_sg.status, ecs_sg.resource_count, ecs_sg.created_count), ("succeeded", 2, 2))
        self.assertEqual(ecs_sg.project_id, self.projects["p-sg"].id)
        self.assertEqual(tasks[("ecs", "la-north-2")].project_id, self.projects["p-mx"].id)
        self.assertIsNotNone(ecs_sg.duration_ms)
        self.assertEqual(tasks[("obs", "ap-southeast-3")].scope, "global")
        self.assertIsNone(tasks[("obs", "ap-southeast-3")].project_id)

    def test_multiple_projects_and_regions_use_their_own_project_id(self):
        self.scan()
        ecs_calls = {(region, project) for method, region, project in self.world.calls
                     if method == "list_servers_details"}
        self.assertEqual(ecs_calls, {("ap-southeast-3", "p-sg"), ("la-north-2", "p-mx")})
        rows = self.resources(service="ecs")
        self.assertEqual((rows["srv-1"].region, rows["srv-3"].region), ("ap-southeast-3", "la-north-2"))
        self.assertEqual(rows["srv-3"].project_id, self.projects["p-mx"].id)

    def test_obs_is_scanned_once_per_account(self):
        self.scan()
        obs_calls = [c for c in self.world.calls if c[0] == "list_buckets"]
        self.assertEqual(len(obs_calls), 1)
        buckets = self.resources(service="obs")
        self.assertEqual({b: r.region for b, r in buckets.items()},
                         {"logs-sg": "ap-southeast-3", "backup-mx": "la-north-2"})
        self.assertTrue(all(r.project_id is None and r.scope_key == "account" for r in buckets.values()))

    def test_plan_tasks_global_once(self):
        catalog = [SimpleNamespace(id="obs", scope="global"), SimpleNamespace(id="ecs", scope="regional")]
        plan = plan_tasks(list(self.projects.values()), catalog)
        self.assertEqual([(p.service, p.scope) for p in plan],
                         [("obs", "global"), ("ecs", "regional"), ("ecs", "regional")])


class TestUpsert(ScanTestCase):
    def test_insert_fields(self):
        self.scan()
        row = self.resources()["srv-1"]
        self.assertEqual((row.service, row.resource_type, row.name, row.status), ("ecs", "ecs.server", "web", "ACTIVE"))
        self.assertEqual(row.provider_created_at.replace(tzinfo=None), datetime(2026, 1, 15, 10, 22))
        self.assertEqual(row.attributes["vcpus"], 2)
        self.assertEqual(row.first_seen, row.last_seen)
        self.assertIsNone(row.deleted_at)

    def test_raw_hash_is_fingerprint_of_stored_raw(self):
        self.scan()
        row = self.resources()["srv-1"]
        self.assertEqual(row.raw_hash, fingerprint(row.raw))
        self.assertEqual(len(row.raw_hash), 64)

    def test_unchanged_resource(self):
        first = self.scan()
        before = self.resources()["srv-1"]
        first_seen, raw_hash = before.first_seen, before.raw_hash
        second = self.scan()
        row = self.resources()["srv-1"]
        self.assertEqual((second.total_created, second.total_updated, second.total_deleted), (0, 0, 0))
        self.assertEqual(row.first_seen, first_seen)
        self.assertEqual(row.raw_hash, raw_hash)
        self.assertEqual(row.last_run_id, second.id)
        self.assertNotEqual(first.id, second.id)

    def test_updated_resource(self):
        self.scan()
        old_hash = self.resources()["srv-1"].raw_hash
        self.world.servers["p-sg"][0] = server("srv-1", "web-renamed")
        run = self.scan()
        row = self.resources()["srv-1"]
        self.assertEqual((run.total_updated, run.total_created), (1, 0))
        self.assertEqual(row.name, "web-renamed")
        self.assertNotEqual(row.raw_hash, old_hash)
        self.assertEqual(self.tasks(run)[("ecs", "ap-southeast-3")].updated_count, 1)

    def test_disappeared_resource_marked_deleted_not_removed(self):
        self.scan()
        self.world.servers["p-sg"] = [server("srv-1", "web")]
        run = self.scan()
        self.assertEqual(run.total_deleted, 1)
        row = self.resources()["srv-2"]
        self.assertIsNotNone(row.deleted_at)
        self.assertIsNone(self.resources()["srv-1"].deleted_at)
        # Reaparece: se restaura (deleted_at vuelve a NULL) y cuenta como alta.
        self.world.servers["p-sg"].append(server("srv-2", "db"))
        run = self.scan()
        self.assertIsNone(self.resources()["srv-2"].deleted_at)
        self.assertEqual(run.total_created, 1)

    def test_deletion_scoped_to_project_and_service(self):
        self.scan()
        self.world.servers["p-mx"] = []
        self.scan(services=["ecs"])
        rows = self.resources()
        self.assertIsNotNone(rows["srv-3"].deleted_at)
        self.assertIsNone(rows["srv-1"].deleted_at)  # otro proyecto
        self.assertIsNone(rows["vol-1"].deleted_at)  # otro servicio no escaneado

    def test_same_provider_id_in_two_projects(self):
        self.world.servers = {"p-sg": [server("dup-1")], "p-mx": [server("dup-1")]}
        self.scan(services=["ecs"])
        rows = list(self.session.scalars(select(InventoryResource).where(InventoryResource.provider_id == "dup-1")))
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.region for r in rows}, {"ap-southeast-3", "la-north-2"})

    def test_two_accounts_with_same_provider_id(self):
        other_client, other_account, other_projects = seed_account(
            self.session, self.keyring, client_name="Globex", projects_spec=(("p-sg", "ap-southeast-3"),))
        self.scan(services=["ecs"])
        self.scan(services=["ecs"], client_id=other_client.id, account_id=other_account.id)
        rows = list(self.session.scalars(select(InventoryResource).where(InventoryResource.provider_id == "srv-1")))
        self.assertEqual({r.account_id for r in rows}, {self.account.id, other_account.id})


class TestFailures(ScanTestCase):
    def test_failed_service_keeps_other_data(self):
        self.world.failures["list_buckets"] = api_error(500, "Internal error", "OBS.500")
        run = self.scan()
        self.assertEqual(run.status, "completed_with_errors")
        self.assertEqual(run.total_errors, 1)
        task = self.tasks(run)[("obs", "ap-southeast-3")]
        self.assertEqual((task.status, task.http_status, task.error_code, task.error_count),
                         ("failed", 500, "OBS.500", 1))
        self.assertEqual(task.request_id, "req-test")
        self.assertEqual(len(self.resources(service="ecs")), 3)  # ECS/EVS sí se guardaron

    def test_failed_service_never_marks_deletions(self):
        self.scan()
        cases = [(401, "APIGW.0301", "authentication"), (401, "ECS.0003", "authorization"),
                 (403, "ECS.0403", "permission"), (404, "APIGW.0101", "unavailable"), (500, "ECS.0500", "api")]
        for status, code, kind in cases:
            with self.subTest(status=status, code=code):
                self.world.failures["list_servers_details"] = api_error(status, "boom", code)
                run = self.scan(services=["ecs"])
                self.assertEqual(run.total_deleted, 0)
                self.assertTrue(all(r.deleted_at is None for r in self.resources(service="ecs").values()))
                kinds = {t.error_kind for t in self.tasks(run).values() if t.service == "ecs"}
                self.assertEqual(kinds, {kind})

    def test_invalid_credentials_fail_run_and_stop_calling_huawei(self):
        for method in ("list_servers_details", "list_volumes", "list_buckets"):
            self.world.failures[method] = api_error(401, "Incorrect IAM authentication information",
                                                    "APIGW.0301")
        run = self.scan(settings=ScanSettings(max_workers=1))
        self.assertEqual(run.status, "failed")
        self.assertEqual(len(self.world.calls), 1)  # secuencial: tras el primer 401 APIGW no se insiste
        statuses = sorted(t.status for t in self.tasks(run).values())
        self.assertEqual(statuses, ["failed"] + ["skipped"] * 4)
        self.assertTrue(all(t.error_kind == "authentication" for t in self.tasks(run).values()))

        self.world.calls.clear()
        run = self.scan(settings=ScanSettings(max_workers=2, per_service=2, per_region=2))
        self.assertLessEqual(len(self.world.calls), 2)  # solo las que ya estaban en vuelo
        self.assertEqual(run.status, "failed")

    def test_partial_coverage_never_marks_deletions(self):
        state = {"items": [{"id": "waf-1"}, {"id": "waf-2"}], "eps_denied": False}

        def waf(region, project, request):
            if request.enterprise_project_id == ALL_GRANTED_EPS and state["eps_denied"]:
                raise api_error(403, "EPS denied")
            items = state["items"] if request.enterprise_project_id else state["items"][:1]
            return SimpleNamespace(items=items, total=len(items))
        self.world.extra["list_instance"] = waf
        self.scan(services=["waf"])
        state["eps_denied"] = True
        run = self.scan(services=["waf"])
        self.assertEqual(run.status, "completed_with_warnings")
        self.assertEqual({t.status for t in self.tasks(run).values()}, {"partial"})
        self.assertEqual(run.total_deleted, 0)
        self.assertTrue(all(r.deleted_at is None for r in self.resources(service="waf").values()))

    def test_wrong_master_key_fails_run_without_calls(self):
        run = self.scan(keyring=make_keyring())
        self.assertEqual(run.status, "failed")
        self.assertEqual({t.status for t in self.tasks(run).values()}, {"skipped"})
        self.assertEqual(self.world.calls, [])

    def test_unexpected_collector_error_is_internal_and_safe(self):
        def broken(region, project, request):
            raise ValueError(f"unexpected {FAKE_SK}")
        self.world.extra["list_servers_details"] = broken
        run = self.scan(services=["ecs"])
        task = next(iter(self.tasks(run).values()))
        self.assertEqual(task.error_kind, "internal")
        self.assertNotIn(FAKE_SK, task.error_message_safe)


class TestPreconditionsAndIsolation(ScanTestCase):
    def test_other_client_cannot_scan_account(self):
        other, _, _ = seed_account(self.session, self.keyring, client_name="Globex",
                                   projects_spec=(("p-x", "ap-southeast-3"),))
        with self.assertRaises(NotFoundError):
            create_scan(self.session, client_id=other.id, account_id=self.account.id)

    def test_concurrent_scan_rejected_but_stale_allowed(self):
        run = create_scan(self.session, client_id=self.client.id, account_id=self.account.id)
        self.session.commit()
        with self.assertRaises(ConflictError):
            create_scan(self.session, client_id=self.client.id, account_id=self.account.id)
        silence = datetime.now(timezone.utc) - timedelta(hours=3)  # sin latido: proceso caído
        run.created_at = run.updated_at = silence
        self.session.commit()
        self.session.expire_all()
        create_scan(self.session, client_id=self.client.id, account_id=self.account.id)
        self.session.commit()
        old = self.session.get(ScanRun, run.id)
        self.assertEqual(old.status, "failed")
        self.assertIn("abandonado", old.error_message_safe)
        self.assertEqual({t.status for t in self.tasks(old).values()}, {"skipped"})

    def test_requires_enabled_projects_and_valid_account(self):
        for project in self.projects.values():
            project.is_enabled = False
        with self.assertRaises(InvalidStateError):
            create_scan(self.session, client_id=self.client.id, account_id=self.account.id)
        for project in self.projects.values():
            project.is_enabled = True
        accounts.update_account(self.session, self.client.id, self.account.id, status="disabled")
        with self.assertRaises(InvalidStateError):
            create_scan(self.session, client_id=self.client.id, account_id=self.account.id)

    def test_unknown_service_rejected(self):
        with self.assertRaises(ValidationFailedError):
            create_scan(self.session, client_id=self.client.id, account_id=self.account.id, services=["nope"])

    def test_execute_only_pending_runs(self):
        run = self.scan(services=["ecs"])
        with self.assertRaises(InvalidStateError):
            execute_scan(self.factory, self.keyring, run.id)


class TestNoSecretsInDatabase(ScanTestCase):
    def test_secrets_never_persisted(self):
        self.world.servers["p-sg"] = [server(
            "srv-x", f"name-with-{FAKE_AK}",
            metadata={"note": f"pasted {FAKE_SK}", "Authorization": "Bearer token-123"},
            adminPass="P@ss", **{"OS-EXT-SRV-ATTR:user_data": "IyEvYmluL2Jhc2g="},
        )]
        self.world.failures["list_volumes"] = api_error(401, f"bad {FAKE_AK} {FAKE_SK}")
        self.scan()
        dump = []
        for table in ("cloud_accounts", "scan_runs", "scan_tasks", "resources"):
            dump.append(repr(self.session.execute(text(f"SELECT * FROM {table}")).all()))
        everything = "\n".join(dump)
        for secret in (FAKE_AK, FAKE_SK, "Bearer token-123", "P@ss", "IyEvYmluL2Jhc2g="):
            self.assertNotIn(secret, everything, secret)
        row = self.resources()["srv-x"]
        self.assertEqual(row.raw["metadata"]["Authorization"], REDACTED)
        self.assertEqual(row.raw["adminPass"], REDACTED)
        self.assertIn(REDACTED, row.name)
