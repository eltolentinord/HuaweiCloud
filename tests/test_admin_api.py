# coding: utf-8
"""API multi-cliente: respuestas sin secretos, logs sin secretos, aislamiento (SQLite)."""

import io
import logging
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.db_helpers import make_keyring, sqlite_session_factory
from tests.helpers import FAKE_AK, FAKE_SK, FakeClient, FakeFactory, api_error
from tests.test_discovery import MOS, MX, SG

from db.session import get_db, session_scope
from routers.admin import get_cipher, install_admin_api
from tenancy.catalog_sync import sync_catalog

SECRETS = (FAKE_AK, FAKE_SK)


class AdminApiTestCase(unittest.TestCase):
    def setUp(self):
        self.factory = sqlite_session_factory()
        with session_scope(self.factory) as session:
            sync_catalog(session)
        self.keyring = make_keyring()
        app = FastAPI()
        install_admin_api(app)

        def db_override():
            with session_scope(self.factory) as session:
                yield session
        app.dependency_overrides[get_db] = db_override
        app.dependency_overrides[get_cipher] = lambda: self.keyring
        self.app = app
        self.http = TestClient(app)
        self.responses = []

        # Captura TODOS los logs (DEBUG) para comprobar que no contienen secretos.
        self.log_stream = io.StringIO()
        self.log_handler = logging.StreamHandler(self.log_stream)
        root = logging.getLogger()
        self.previous_level = root.level
        root.addHandler(self.log_handler)
        root.setLevel(logging.DEBUG)

    def tearDown(self):
        root = logging.getLogger()
        root.removeHandler(self.log_handler)
        root.setLevel(self.previous_level)
        self.factory.kw["bind"].dispose()
        logs = self.log_stream.getvalue()
        for secret in SECRETS:
            self.assertNotIn(secret, logs, "secreto en logs")
            for response in self.responses:
                self.assertNotIn(secret, response.text, f"secreto en respuesta {response.request.url}")

    def call(self, method, url, **kwargs):
        response = self.http.request(method, url, **kwargs)
        self.responses.append(response)
        return response

    def new_client(self, name="Acme"):
        return self.call("POST", "/api/admin/clients", json={"name": name}).json()

    def new_account(self, client_id, name="prod"):
        response = self.call("POST", f"/api/admin/clients/{client_id}/accounts",
                             json={"name": name, "ak": FAKE_AK, "sk": FAKE_SK})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()


class TestClientsAndAccounts(AdminApiTestCase):
    def test_client_crud(self):
        client = self.new_client("Compañía Ñandú")
        self.assertEqual(client["slug"], "compania-nandu")
        cid = client["id"]
        self.assertEqual(self.call("PATCH", f"/api/admin/clients/{cid}", json={"status": "suspended"}).json()["status"],
                         "suspended")
        self.assertEqual(len(self.call("GET", "/api/admin/clients").json()), 1)
        self.assertEqual(self.call("POST", "/api/admin/clients", json={"name": "x", "slug": client["slug"]}).status_code, 409)
        self.assertEqual(self.call("DELETE", f"/api/admin/clients/{cid}").status_code, 204)
        self.assertEqual(self.call("GET", f"/api/admin/clients/{cid}").status_code, 404)

    def test_account_responses_never_include_credentials(self):
        cid = self.new_client()["id"]
        account = self.new_account(cid)
        forbidden = {"ak", "sk", "ak_ciphertext", "sk_ciphertext"}
        self.assertFalse(forbidden & set(account))
        self.assertEqual(account["status"], "pending")
        listed = self.call("GET", f"/api/admin/clients/{cid}/accounts").json()
        self.assertFalse(forbidden & set(listed[0]))
        replaced = self.call("PUT", f"/api/admin/clients/{cid}/accounts/{account['id']}/credentials",
                             json={"ak": FAKE_AK, "sk": FAKE_SK})
        self.assertEqual(replaced.status_code, 200)

    def test_validation_errors_do_not_echo_secrets(self):
        cid = self.new_client()["id"]
        response = self.call("POST", f"/api/admin/clients/{cid}/accounts", json={"ak": FAKE_AK, "sk": FAKE_SK})
        self.assertEqual(response.status_code, 422)  # falta name: el body NO se devuelve
        response = self.call("POST", f"/api/admin/clients/{cid}/accounts",
                             json={"name": "x", "ak": FAKE_AK, "sk": FAKE_SK * 30})
        self.assertEqual(response.status_code, 422)

    def test_isolation_between_clients(self):
        acme, globex = self.new_client("Acme")["id"], self.new_client("Globex")["id"]
        account = self.new_account(acme)
        for method, suffix in (("GET", ""), ("PATCH", ""), ("DELETE", ""), ("GET", "/projects"),
                               ("POST", "/discover-projects")):
            kwargs = {"json": {"name": "hack"}} if method == "PATCH" else {}
            response = self.call(method, f"/api/admin/clients/{globex}/accounts/{account['id']}{suffix}", **kwargs)
            self.assertEqual(response.status_code, 404, (method, suffix))
        self.assertEqual(self.call("GET", f"/api/admin/clients/{globex}/accounts").json(), [])


class TestDiscoveryAndInventory(AdminApiTestCase):
    def setUp(self):
        super().setUp()
        self.cid = self.new_client()["id"]
        self.aid = self.new_account(self.cid)["id"]
        self.base = f"/api/admin/clients/{self.cid}/accounts/{self.aid}"

    def discover(self, items=None, exc=None):
        with mock.patch("tenancy.projects.IamProjectSource") as source_cls:
            if exc:
                source_cls.return_value.list_projects.side_effect = exc
            else:
                source_cls.return_value.list_projects.return_value = items
            return self.call("POST", f"{self.base}/discover-projects")

    def test_discover_projects(self):
        response = self.discover([SG, MX, MOS])
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["created"], len(body["projects"]), body["skipped"][0]["name"]), (2, 2, "MOS"))
        account = self.call("GET", self.base).json()
        self.assertEqual(account["status"], "active")

    def test_discovery_failure_is_safe_and_persisted(self):
        response = self.discover(exc=api_error(401, f"denied for {FAKE_SK}", "APIGW.0301"))
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.call("GET", self.base).json()["status"], "invalid")

    def test_inventory_by_account_without_browser_credentials(self):
        self.discover([SG])
        project = self.call("GET", f"{self.base}/projects").json()[0]
        fake = FakeClient(list_servers_details=lambda r: SimpleNamespace(servers=[{"id": "s1", "name": "web"}],
                                                                           count=1))
        with mock.patch("core.engine.ClientFactory", lambda creds, **kw: FakeFactory(fake)):
            response = self.call("POST", f"/api/clients/{self.cid}/accounts/{self.aid}/inventory",
                                 json={"project_id": project["id"], "service": "ecs"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(set(body), {"service", "region", "project_id_masked", "total_tablas", "resumen",
                                     "errores", "avisos", "tables"})
        self.assertEqual(body["region"], "ap-southeast-3")
        self.assertEqual(body["tables"][0]["filas"][0]["Nombre ECS"], "web")

    def test_inventory_error_from_huawei_is_safe(self):
        self.discover([SG])
        project = self.call("GET", f"{self.base}/projects").json()[0]

        def boom(request):
            raise api_error(401, f"signature {FAKE_AK} {FAKE_SK}", "APIGW.0301")
        with mock.patch("core.engine.ClientFactory", lambda creds, **kw: FakeFactory(FakeClient(list_servers_details=boom))):
            response = self.call("POST", f"/api/clients/{self.cid}/accounts/{self.aid}/inventory",
                                 json={"project_id": project["id"], "service": "ecs"})
        self.assertEqual(response.status_code, 502)

    def test_project_toggle_and_manual_add(self):
        added = self.call("POST", f"{self.base}/projects",
                          json={"huawei_project_id": "manual-1", "region_id": "la-north-2"})
        self.assertEqual(added.status_code, 201)
        toggled = self.call("PATCH", f"{self.base}/projects/{added.json()['id']}", json={"is_enabled": False})
        self.assertFalse(toggled.json()["is_enabled"])
        response = self.call("POST", f"/api/clients/{self.cid}/accounts/{self.aid}/inventory",
                             json={"project_id": added.json()["id"], "service": "ecs"})
        self.assertEqual(response.status_code, 409)


class TestConfigurationErrors(unittest.TestCase):
    def test_missing_keys_and_database_return_503(self):
        app = FastAPI()
        install_admin_api(app)
        http = TestClient(app)
        with mock.patch.dict("os.environ", {"INVENTORY_ENCRYPTION_KEYS": "", "DATABASE_URL": ""}):
            from db import session as db_session
            from routers import admin
            db_session.get_engine.cache_clear()
            db_session.get_session_factory.cache_clear()
            admin._keyring_from_env.cache_clear()
            self.assertEqual(http.get("/api/admin/clients").status_code, 503)

            factory = sqlite_session_factory()

            def db_override():
                with session_scope(factory) as session:
                    yield session
            app.dependency_overrides[get_db] = db_override
            cid = http.post("/api/admin/clients", json={"name": "Acme"}).json()["id"]
            response = http.post(f"/api/admin/clients/{cid}/accounts",
                                 json={"name": "p", "ak": FAKE_AK, "sk": FAKE_SK})
            self.assertEqual(response.status_code, 503)
            self.assertNotIn(FAKE_SK, response.text)


class TestAdminApiDisabledByDefault(unittest.TestCase):
    def test_main_app_does_not_expose_admin_routes(self):
        from app import app
        http = TestClient(app)
        self.assertEqual(http.get("/api/admin/clients").status_code, 404)

    def test_legacy_inventory_422_does_not_echo_ak(self):
        from app import app
        response = TestClient(app).post("/api/inventory", json={"ak": FAKE_AK, "project_id": "p", "region": "r"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(FAKE_AK, response.text)


if __name__ == "__main__":
    unittest.main()
