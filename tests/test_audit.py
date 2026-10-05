# coding: utf-8
"""Auditoría: quién hizo qué, sin secretos, con permisos y aislamiento."""

import threading
from datetime import datetime, timedelta, timezone
from unittest import mock

from sqlalchemy import select, text

from tests.db_helpers import SqliteTestCase
from tests.helpers import FAKE_AK, FAKE_SK
from tests.scan_helpers import ScanWorld, seed_account, server, simulated_huawei
from tests.test_scan_api import ScanApiTestCase

from core.authz import Principal
from db.models import AuditEvent
from routers.security import get_principal
from scanning import worker
from tenancy import audit, schedules


class TestAuditApi(ScanApiTestCase):
    def events(self, client_id=None, **params):
        response = self.call("GET", f"/api/clients/{client_id or self.cid}/audit", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_admin_actions_are_recorded_without_secrets(self):
        base = f"/api/admin/clients/{self.cid}/accounts"
        account = self.call("POST", base, json={"name": "nueva", "ak": FAKE_AK, "sk": FAKE_SK}).json()
        self.call("PUT", f"{base}/{account['id']}/credentials", json={"ak": FAKE_AK, "sk": FAKE_SK})
        self.call("PATCH", f"{base}/{account['id']}", json={"name": "renombrada"})
        self.call("DELETE", f"{base}/{account['id']}")
        body = self.events()
        actions = [e["action"] for e in body["items"]]
        for action in ("account.create", "account.credentials.replace", "account.update", "account.delete"):
            self.assertIn(action, actions)
        replaced = next(e for e in body["items"] if e["action"] == "account.credentials.replace")
        self.assertEqual(set(replaced["details"]), {"key_version"})
        self.assertEqual((replaced["actor_subject"], replaced["actor_kind"]), ("local", "local"))
        self.assertTrue(replaced["request_id"])
        update = next(e for e in body["items"] if e["action"] == "account.update")
        self.assertEqual(update["details"]["fields"], ["name"])
        everything = self.call("GET", f"/api/clients/{self.cid}/audit", params={"limit": 1000}).text
        self.assertNotIn(FAKE_AK, everything)
        self.assertNotIn(FAKE_SK, everything)

    def test_scans_and_schedules_are_recorded(self):
        self.start()
        base = f"/api/clients/{self.cid}/accounts/{self.aid}/schedules"
        sid = self.call("POST", base, json={"interval_minutes": 60, "services": ["ecs"]}).json()["id"]
        self.call("PATCH", f"{base}/{sid}", json={"enabled": False})
        self.call("DELETE", f"{base}/{sid}")
        actions = [e["action"] for e in self.events()["items"]]
        self.assertIn("scan.start", actions)
        self.assertEqual([a for a in actions if a.startswith("schedule.")],
                         ["schedule.delete", "schedule.update", "schedule.create"])  # más reciente primero
        scan = self.events(action="scan.start")["items"][0]
        self.assertEqual((scan["details"]["trigger"], scan["target_type"]), ("api", "scan"))

    def test_user_actor_and_permissions(self):
        self.app.dependency_overrides[get_principal] = lambda: Principal(
            subject="ana@example.com", kind="user", client_roles={str(self.cid): "operator"})
        self.start()
        self.assertEqual(self.call("GET", f"/api/clients/{self.cid}/audit").status_code, 403)  # operator no lee
        self.app.dependency_overrides[get_principal] = lambda: Principal(
            subject="jefe@example.com", kind="user", client_roles={str(self.cid): "admin"})
        scan = self.events(action="scan.start")["items"][0]
        self.assertEqual((scan["actor_subject"], scan["actor_kind"]), ("ana@example.com", "user"))
        self.assertEqual(self.call("GET", f"/api/clients/{self.other_cid}/audit").status_code, 404)


class TestAuditCore(SqliteTestCase):
    def test_details_whitelist_and_redaction(self):
        event = audit.record(self.session, audit.cli_principal(), "test.action",
                             details={"name": "x", "sk": FAKE_SK, "token": "t", "password": "p", "random": 1})
        self.assertEqual(event.details, {"name": "x"})
        self.assertTrue(event.actor_subject.startswith("cli:"))

    def test_worker_records_scheduled_scans(self):
        client, account, _ = seed_account(self.session, self.keyring)
        schedules.create_schedule(self.session, client_id=client.id, account_id=account.id, interval_minutes=60,
                                  services=["ecs"], first_run_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        self.session.commit()
        world = ScanWorld()
        world.servers = {"p-sg": [server("srv-1")], "p-mx": []}
        with simulated_huawei(world):
            worker.run_once(self.factory, self.keyring)
        self.session.expire_all()
        [event] = self.session.scalars(select(AuditEvent).where(AuditEvent.action == "scan.start")).all()
        self.assertEqual((event.actor_subject, event.details["trigger"]), ("worker", "schedule"))

    def test_audit_survives_account_and_client_deletion(self):
        client, account, _ = seed_account(self.session, self.keyring)
        audit.record(self.session, audit.cli_principal(), "account.create", client_id=client.id,
                     account_id=account.id, target=("account", account.id))
        self.session.delete(client)
        self.session.commit()
        self.assertEqual(self.session.execute(text("SELECT count(*) FROM audit_events")).scalar(), 1)


class TestAuditOrdering(SqliteTestCase):
    """El orden del registro es el orden real de los eventos, aunque el reloj repita o retroceda."""

    def setUp(self):
        super().setUp()
        self.client, self.account, _ = seed_account(self.session, self.keyring)

    def record_many(self, count):
        for i in range(count):
            audit.record(self.session, audit.cli_principal(), f"test.e{i}", client_id=self.client.id)
        self.session.commit()

    def listed(self):
        rows, _ = audit.list_for_client(self.session, self.client.id, limit=1000)
        return [r.action for r in rows if r.action.startswith("test.")]

    def test_burst_of_events_keeps_insertion_order(self):
        self.record_many(300)  # muchas en el mismo "tick" del reloj del sistema
        self.assertEqual(self.listed(), [f"test.e{i}" for i in reversed(range(300))])
        stamps = [r.occurred_at for r in self.session.scalars(select(AuditEvent).where(
            AuditEvent.action.like("test.%")).order_by(AuditEvent.occurred_at))]
        self.assertEqual(len(set(stamps)), 300)

    def test_frozen_and_backwards_clock(self):
        # El reloj es del proceso: la hora simulada debe ir por delante de la última marca emitida.
        frozen = audit.next_timestamp() + timedelta(milliseconds=20)
        clock = iter([frozen, frozen, frozen - timedelta(milliseconds=10), frozen + timedelta(milliseconds=5)])
        with mock.patch("tenancy.audit._utcnow", lambda: next(clock)):
            stamps = [audit.next_timestamp() for _ in range(4)]
        self.assertTrue(all(a < b for a, b in zip(stamps, stamps[1:])), stamps)
        self.assertEqual(stamps[-1], frozen + timedelta(milliseconds=5))  # vuelve al reloj real cuando avanza
        later = frozen + timedelta(milliseconds=6)
        with mock.patch("tenancy.audit._utcnow", lambda: later):
            self.record_many(5)
        self.assertEqual(self.listed(), [f"test.e{i}" for i in reversed(range(5))])

    def test_concurrent_threads_never_share_a_timestamp(self):
        stamps, lock = [], threading.Lock()

        def take():
            values = [audit.next_timestamp() for _ in range(500)]
            with lock:
                stamps.extend(values)

        threads = [threading.Thread(target=take) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(stamps)), 4000)
