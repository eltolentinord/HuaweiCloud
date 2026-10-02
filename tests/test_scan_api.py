# coding: utf-8
"""API del motor de escaneo: inicio, progreso, tareas, recursos y aislamiento (SQLite)."""

from tests.helpers import FAKE_AK, FAKE_SK, api_error
from tests.scan_helpers import ScanWorld, seed_account, server, simulated_huawei
from tests.test_admin_api import AdminApiTestCase

from db.session import get_db_session_factory, session_scope


class ScanApiTestCase(AdminApiTestCase):
    def setUp(self):
        super().setUp()
        self.app.dependency_overrides[get_db_session_factory] = lambda: self.factory
        with session_scope(self.factory) as session:
            client, account, _ = seed_account(session, self.keyring)
            self.cid, self.aid = client.id, account.id
            other, other_account, _ = seed_account(session, self.keyring, client_name="Globex",
                                                   projects_spec=(("p-gx", "ap-southeast-3"),))
            self.other_cid, self.other_aid = other.id, other_account.id
        self.world = ScanWorld()
        self.world.servers = {"p-sg": [server("srv-1", "web"), server("srv-2", "db")], "p-mx": []}
        self.world.buckets = [{"name": "logs", "location": "ap-southeast-3"}]

    def start(self, services=("ecs", "obs"), cid=None, aid=None):
        with simulated_huawei(self.world):  # TestClient ejecuta la BackgroundTask antes de responder
            return self.call("POST", f"/api/clients/{cid or self.cid}/accounts/{aid or self.aid}/scans",
                             json={"services": list(services)})


class TestScanApi(ScanApiTestCase):
    def test_start_and_read_scan(self):
        response = self.start()
        self.assertEqual(response.status_code, 202, response.text)
        scan_id = response.json()["id"]
        self.assertEqual(response.json()["status"], "pending")

        detail = self.call("GET", f"/api/scans/{scan_id}", params={"client_id": str(self.cid)})
        self.assertEqual(detail.status_code, 200)
        body = detail.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["progress"], {"total": 3, "done": 3, "percent": 100.0})
        self.assertEqual((body["total_resources"], body["total_created"], body["errors"]), (3, 3, []))
        self.assertEqual({t["service"] for t in body["tasks"]}, {"ecs", "obs"})
        self.assertIsNotNone(body["duration_ms"])

        listed = self.call("GET", f"/api/clients/{self.cid}/accounts/{self.aid}/scans").json()
        self.assertEqual([r["id"] for r in listed], [scan_id])

    def test_errors_are_safe_in_scan_detail(self):
        self.world.failures["list_buckets"] = api_error(403, f"denied {FAKE_AK} {FAKE_SK}", "OBS.0403")
        scan_id = self.start().json()["id"]
        body = self.call("GET", f"/api/scans/{scan_id}", params={"client_id": str(self.cid)}).json()
        self.assertEqual(body["status"], "completed_with_warnings")  # 403 = aviso, no fallo
        self.assertEqual((body["errors"], body["total_errors"], body["total_warnings"]), ([], 0, 1))
        warning = body["warnings"][0]
        self.assertEqual((warning["service"], warning["status"], warning["http_status"], warning["error_code"],
                          warning["error_kind"]), ("obs", "denied", 403, "OBS.0403", "permission"))

    def test_resources_endpoint(self):
        self.start()
        base = f"/api/clients/{self.cid}/accounts/{self.aid}/resources"
        page = self.call("GET", base, params={"service": "ecs"}).json()
        self.assertEqual(page["total"], 2)
        self.assertTrue(all(item["raw"] is None for item in page["items"]))
        with_raw = self.call("GET", base, params={"service": "ecs", "include_raw": "true", "limit": 1}).json()
        self.assertEqual((with_raw["total"], len(with_raw["items"])), (2, 1))
        self.assertEqual(with_raw["items"][0]["raw"]["id"], with_raw["items"][0]["provider_id"])

        self.world.servers["p-sg"] = [server("srv-1", "web")]
        self.start()
        self.assertEqual(self.call("GET", base, params={"service": "ecs"}).json()["total"], 1)
        deleted = self.call("GET", base, params={"service": "ecs", "include_deleted": "true"}).json()
        self.assertEqual(deleted["total"], 2)

    def test_isolation_between_clients(self):
        scan_id = self.start().json()["id"]
        self.assertEqual(self.call("GET", f"/api/scans/{scan_id}",
                                   params={"client_id": str(self.other_cid)}).status_code, 404)
        self.assertEqual(self.call("GET", f"/api/scans/{scan_id}").status_code, 422)  # client_id obligatorio
        for path in ("scans", "resources"):
            response = self.call("GET", f"/api/clients/{self.other_cid}/accounts/{self.aid}/{path}")
            self.assertEqual(response.status_code, 404, path)
        self.assertEqual(self.start(cid=self.other_cid, aid=self.aid).status_code, 404)

    def test_region_filter_via_api(self):
        with simulated_huawei(self.world):
            response = self.call("POST", f"/api/clients/{self.cid}/accounts/{self.aid}/scans",
                                 json={"services": ["ecs"], "regions": ["la-north-2"]})
        self.assertEqual(response.status_code, 202, response.text)
        body = self.call("GET", f"/api/scans/{response.json()['id']}", params={"client_id": str(self.cid)}).json()
        self.assertEqual({t["region"] for t in body["tasks"]}, {"la-north-2"})
        self.assertEqual(body["stats"]["filters"]["regions"], ["la-north-2"])
        self.assertIn("concurrency", body["stats"])
        with simulated_huawei(self.world):
            bad = self.call("POST", f"/api/clients/{self.cid}/accounts/{self.aid}/scans",
                            json={"regions": ["eu-west-101"]})
        self.assertEqual(bad.status_code, 422)

    def test_conflict_and_validation(self):
        with simulated_huawei(self.world):
            from scanning.engine import create_scan
            with session_scope(self.factory) as session:
                create_scan(session, client_id=self.cid, account_id=self.aid)  # queda "pending"
        self.assertEqual(self.start().status_code, 409)
        self.assertEqual(self.start(services=("nope",), cid=self.other_cid, aid=self.other_aid).status_code, 422)
