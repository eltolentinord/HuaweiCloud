# coding: utf-8
"""Auditoría: quién hizo qué, sin secretos, con permisos y aislamiento."""

from datetime import datetime, timedelta, timezone

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
