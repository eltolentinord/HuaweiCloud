# coding: utf-8
"""Seguridad y observabilidad: request ID, cabeceras, 500 seguros, CORS, acceso a la API
interna, SSRF, salud/métricas y logs con contexto."""

import io
import json
import logging
import os
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.db_helpers import SqliteTestCase, make_keyring
from tests.helpers import FAKE_AK, FAKE_SK
from tests.scan_helpers import ScanWorld, seed_account, server, simulated_huawei
from tests.test_admin_api import AdminApiTestCase

from core.clients import ClientFactory
from core.credentials import HuaweiCredentials
from core.observability import METRICS, ContextFilter, JsonFormatter, Metrics, request_id_var, scan_id_var
from core.validation import InvalidValueError, validate_endpoint_domain, validate_region_id
from routers.middleware import install_middlewares
from routers.security import ENV_ADMIN_TOKEN
from scanning.engine import scan_account
from tenancy import accounts, clients, projects
from tenancy.errors import ValidationFailedError

TOKEN = "t" * 40


def middleware_app():
    app = FastAPI()
    install_middlewares(app)

    @app.get("/api/ok")
    def ok():
        return {"request_id": request_id_var.get()}

    @app.get("/api/boom")
    def boom():
        raise ValueError(f"internal detail {FAKE_SK}")
    return app


class TestMiddlewares(unittest.TestCase):
    def setUp(self):
        self.http = TestClient(middleware_app(), raise_server_exceptions=False)

    def test_request_id_generated_propagated_and_validated(self):
        response = self.http.get("/api/ok")
        rid = response.headers["X-Request-ID"]
        self.assertEqual(response.json()["request_id"], rid)
        self.assertEqual(len(rid), 32)
        self.assertEqual(self.http.get("/api/ok", headers={"X-Request-ID": "abc-123"}).headers["X-Request-ID"],
                         "abc-123")
        injected = self.http.get("/api/ok", headers={"X-Request-ID": "x" * 200}).headers["X-Request-ID"]
        self.assertNotEqual(injected, "x" * 200)

    def test_security_headers_and_no_store(self):
        headers = self.http.get("/api/ok").headers
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_unhandled_errors_never_leak(self):
        response = self.http.get("/api/boom")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "Error interno del servidor.")
        self.assertTrue(response.json()["request_id"])
        self.assertNotIn(FAKE_SK, response.text)
        self.assertNotIn("internal detail", response.text)

    def test_cors_is_explicit(self):
        with mock.patch.dict(os.environ, {"INVENTORY_CORS_ORIGINS": "https://panel.example,*"}):
            http = TestClient(middleware_app())
        allowed = http.options("/api/ok", headers={"Origin": "https://panel.example",
                                                   "Access-Control-Request-Method": "GET"})
        self.assertEqual(allowed.headers.get("access-control-allow-origin"), "https://panel.example")
        denied = http.options("/api/ok", headers={"Origin": "https://evil.example",
                                                  "Access-Control-Request-Method": "GET"})
        self.assertNotIn("access-control-allow-origin", denied.headers)
        no_cors = TestClient(middleware_app()).get("/api/ok", headers={"Origin": "https://panel.example"})
        self.assertNotIn("access-control-allow-origin", no_cors.headers)


class TestMainAppSystemEndpoints(unittest.TestCase):
    def setUp(self):
        from app import app
        self.app = app

    def test_healthz_and_readyz_without_database(self):
        http = TestClient(self.app)
        self.assertEqual(http.get("/healthz").json(), {"status": "ok"})
        with mock.patch("routers.system.get_engine", side_effect=__import__("db.session").session.DatabaseNotConfiguredError()):
            self.assertEqual(http.get("/readyz").json()["database"], "not_configured")

    def test_readyz_hides_database_errors(self):
        broken = mock.MagicMock()
        broken.connect.side_effect = RuntimeError("password authentication failed for user secret-user")
        with mock.patch("routers.system.get_engine", return_value=broken):
            response = TestClient(self.app).get("/readyz")
        self.assertEqual((response.status_code, response.json()["database"]), (503, "error"))
        self.assertNotIn("secret-user", response.text)

    def test_metrics_access_control(self):
        self.assertEqual(TestClient(self.app).get("/metrics").status_code, 200)
        remote = TestClient(self.app, client=("203.0.113.9", 4000))
        self.assertEqual(remote.get("/metrics").status_code, 401)
        with mock.patch.dict(os.environ, {ENV_ADMIN_TOKEN: TOKEN}):
            self.assertEqual(remote.get("/metrics").status_code, 401)
            self.assertEqual(remote.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code, 401)
            ok = remote.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(ok.status_code, 200)
        self.assertIn("http_requests_total", ok.text)


class TestAdminApiProtection(AdminApiTestCase):
    def test_remote_clients_rejected_without_token(self):
        remote = TestClient(self.app, client=("198.51.100.7", 5555))
        self.assertEqual(remote.get("/api/admin/clients").status_code, 401)
        self.assertEqual(self.call("GET", "/api/admin/clients").status_code, 200)  # local

    def test_token_required_when_configured(self):
        with mock.patch.dict(os.environ, {ENV_ADMIN_TOKEN: TOKEN}):
            self.assertEqual(self.call("GET", "/api/admin/clients").status_code, 401)  # ni siquiera local
            ok = self.call("GET", "/api/admin/clients", headers={"Authorization": f"Bearer {TOKEN}"})
            self.assertEqual(ok.status_code, 200)

    def test_short_token_is_rejected_as_configuration(self):
        with mock.patch.dict(os.environ, {ENV_ADMIN_TOKEN: "short"}):
            # Token inválido → se ignora y vuelve el modo "solo local" (nunca abre la API).
            remote = TestClient(self.app, client=("198.51.100.7", 5555))
            self.assertEqual(remote.get("/api/admin/clients", headers={"Authorization": "Bearer short"}).status_code,
                             401)


class TestSsrfValidation(SqliteTestCase):
    def setUp(self):
        super().setUp()
        self.client = clients.create_client(self.session, name="Acme")

    def create(self, **kw):
        return accounts.create_account(self.session, self.keyring, client_id=self.client.id, name=kw.pop("name", "a"),
                                       ak=FAKE_AK, sk=FAKE_SK, **kw)

    def test_endpoint_domain_allowlist(self):
        self.assertEqual(self.create(name="eu", endpoint_domain="MyHuaweiCloud.eu").endpoint_domain, "myhuaweicloud.eu")
        for bad in ("evil.example", "myhuaweicloud.com.evil.example", "127.0.0.1", "localhost", "x/../y"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationFailedError):
                    self.create(name=f"bad-{bad}", endpoint_domain=bad)
        with mock.patch.dict(os.environ, {"INVENTORY_ALLOWED_ENDPOINT_DOMAINS": "huawei-dedicated.example"}):
            self.assertTrue(self.create(name="hcs", endpoint_domain="huawei-dedicated.example"))

    def test_region_formats(self):
        with self.assertRaises(ValidationFailedError):
            self.create(name="r", iam_region_id="ap-southeast-3.evil.example/")
        account = self.create(name="ok", iam_region_id="la-north-2")
        with self.assertRaises(ValidationFailedError):
            projects.add_project(self.session, self.client.id, account.id, huawei_project_id="p",
                                 region_id="x@evil.example")
        with self.assertRaises(ValidationFailedError):
            accounts.update_account(self.session, self.client.id, account.id, endpoint_domain="evil.example")
        for good in ("ap-southeast-3", "eu-west-101", "cn-north-4", "la-north-2"):
            self.assertEqual(validate_region_id(good), good)

    def test_client_factory_refuses_bad_targets(self):
        with self.assertRaises(InvalidValueError):
            ClientFactory(HuaweiCredentials(FAKE_AK, FAKE_SK), endpoint_domain="evil.example")
        factory = ClientFactory(HuaweiCredentials(FAKE_AK, FAKE_SK))
        region_cls = mock.MagicMock()
        region_cls.value_of.side_effect = KeyError("unknown")
        with self.assertRaises(InvalidValueError):
            factory.resolve_region("x.evil.example/", region_cls, "ecs")
        self.assertEqual(factory.resolve_region("xx-new-9", region_cls, "ecs").endpoint,
                         "https://ecs.xx-new-9.myhuaweicloud.com")
        with self.assertRaises(InvalidValueError):
            validate_endpoint_domain("")


class TestStructuredLogging(unittest.TestCase):
    def test_json_formatter_includes_context_and_extra(self):
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "hola %s", ("mundo",), None)
        token = scan_id_var.set("scan-1")
        try:
            ContextFilter().filter(record)
        finally:
            scan_id_var.reset(token)
        record.service = "ecs"
        data = json.loads(JsonFormatter().format(record))
        self.assertEqual((data["msg"], data["scan_id"], data["service"], data["request_id"]),
                         ("hola mundo", "scan-1", "ecs", None))

    def test_metrics_render(self):
        metrics = Metrics()
        metrics.inc("x_total", service="ecs")
        metrics.inc("x_total", 2, service="ecs")
        metrics.observe("d_seconds", 0.5, route='/a"b')
        text = metrics.render()
        self.assertIn('x_total{service="ecs"} 3', text)
        self.assertIn('d_seconds_count{route="/a\\"b"} 1', text)


class TestScanContextAndMetrics(SqliteTestCase):
    def test_worker_logs_carry_scan_and_task_ids_and_metrics_count(self):
        client, account, _ = seed_account(self.session, self.keyring)
        world = ScanWorld()
        world.servers = {"p-sg": [server("srv-1")], "p-mx": []}
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(ContextFilter())
        handler.setFormatter(JsonFormatter())
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        before = METRICS.value("scans_total", status="completed")
        try:
            with simulated_huawei(world):
                run_id = scan_account(self.factory, self.keyring, client_id=client.id, account_id=account.id,
                                      services=["ecs"])
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)
        lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.startswith("{")]
        collector_lines = [l for l in lines if l["logger"] == "core.engine"]
        self.assertTrue(collector_lines)
        self.assertTrue(all(l["scan_id"] == str(run_id) and l["task_id"] for l in collector_lines))
        task_events = [l for l in lines if l.get("event") == "scan_task"]
        self.assertEqual({l["service"] for l in task_events}, {"ecs"})
        self.assertEqual(METRICS.value("scans_total", status="completed"), before + 1)
        self.assertNotIn(FAKE_SK, stream.getvalue())
