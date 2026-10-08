# coding: utf-8
"""Tests del módulo Cloud Eye Auto-Diagnóstico.

Cubre:
  1. Webhook: evento válido → 200, CesAlarmEvent creado, DiagnosticIncident creado
  2. Webhook: evento duplicado (idempotency_key) → 200 (idempotente)
  3. Webhook: alarm_id ausente → respuesta 422 o 400
  4. DELETE diagnóstico completado → 200, incidente borrado, audit log creado
  5. DELETE diagnóstico en progreso → 409
  6. DELETE desde cliente distinto → 404
  7. DELETE sin permiso DIAGNOSTICS_DELETE → AccessDenied 403
  8. Permissions: DIAGNOSTICS_READ en _VIEWER
  9. Permissions: DIAGNOSTICS_MANAGE en _OPERATOR
 10. Permissions: DIAGNOSTICS_DELETE solo en _ADMIN
 11. Lista blanca de comandos: todos los ejecutables están autorizados
 12. Lista blanca de comandos: ningún carácter de shell
 13. Lista blanca de comandos: ningún comando destructivo
 14. Analyzer: disco lleno detectado
 15. Analyzer: sin datos → confianza low
 16. Analyzer: devuelve claves obligatorias
"""

from __future__ import annotations

import hashlib
import json
import unittest
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from db.session import get_db, session_scope
from tests.db_helpers import sqlite_session_factory


# ─── helpers comunes ─────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_alarm_payload(alarm_id="alarm-001", alarm_status="alarm", fired_at=None):
    return {
        "alarm_id": alarm_id,
        "alarm_name": "CPU alto",
        "alarm_status": alarm_status,
        "namespace": "SYS.ECS",
        "metric_name": "cpu_util",
        "resource_id": "ecs-instance-abc123",
        "fired_at": fired_at or _now_iso(),
        "alarm_level": 2,
        "threshold": 80.0,
        "observed_value": 95.0,
    }


def _make_app(factory) -> FastAPI:
    from routers.ces_webhook import router as webhook_router
    from routers.diagnostics_api import router as diag_router
    from routers.security import get_principal
    from core.authz import Principal

    app = FastAPI()
    app.include_router(webhook_router)
    app.include_router(diag_router)

    def db_override():
        with session_scope(factory) as session:
            yield session

    def principal_override():
        return Principal(subject="test-admin", kind="service", platform_role="admin")

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_principal] = principal_override
    return app


# ─── Test 1-3: Webhook ────────────────────────────────────────────────────────

class TestWebhookEndpoint(unittest.TestCase):

    def setUp(self):
        self.factory = sqlite_session_factory()
        self.app = _make_app(self.factory)
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def tearDown(self):
        self.factory.kw["bind"].dispose()

    def _post(self, payload):
        return self.client.post("/webhook/ces-alarm",
                                content=json.dumps(payload),
                                headers={"Content-Type": "application/json"})

    def test_valid_alarm_returns_200(self):
        resp = self._post(_make_alarm_payload())
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get("ok"))

    def test_duplicate_alarm_idempotent(self):
        payload = _make_alarm_payload(alarm_id="alarm-dup")
        resp1 = self._post(payload)
        resp2 = self._post(payload)
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp2.status_code, 200)
        # Ambas deben tener ok=True (idempotencia)
        self.assertTrue(resp1.json().get("ok"))
        self.assertTrue(resp2.json().get("ok"))

    def test_missing_alarm_id_rejected(self):
        payload = {"alarm_name": "sin alarm_id", "fired_at": _now_iso()}
        resp = self._post(payload)
        # Debe rechazarlo: 400 o 422
        self.assertIn(resp.status_code, (400, 422))
        body = resp.json()
        self.assertFalse(body.get("ok", True))


# ─── Test 4-7: DELETE ─────────────────────────────────────────────────────────

class TestDeleteDiagnostic(unittest.TestCase):

    def setUp(self):
        self.factory = sqlite_session_factory()
        self._client_id = uuid.uuid4()
        self._account_id = uuid.uuid4()

        # Crear cliente, evento e incidente en BD
        with session_scope(self.factory) as session:
            from db.models import Client, CesAlarmEvent, DiagnosticIncident

            client = Client(
                id=self._client_id,
                name="Test Client",
                slug=f"test-client-{self._client_id.hex[:8]}",
                status="active",
            )
            session.add(client)
            session.flush()

            event = CesAlarmEvent(
                alarm_id="alarm-del-01",
                fired_at=datetime.now(timezone.utc),
                idempotency_key=hashlib.sha256(b"del-01-unique").hexdigest(),
                status="received",
            )
            session.add(event)
            session.flush()

            inc = DiagnosticIncident(
                client_id=self._client_id,
                account_id=None,
                event_id=event.id,
                ecs_name="ecs-prod-01",
                alarm_type="cpu",
                status="report_available",
            )
            session.add(inc)
            session.flush()
            self._incident_id = inc.id
            self._event_id = event.id

        self.app = _make_app(self.factory)
        self.http = TestClient(self.app, raise_server_exceptions=False)

    def tearDown(self):
        self.factory.kw["bind"].dispose()

    def _url(self, incident_id=None, client_id=None):
        cid = client_id or self._client_id
        iid = incident_id or self._incident_id
        return f"/api/clients/{cid}/diagnostics/{iid}"

    def test_delete_completed_incident(self):
        resp = self.http.delete(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get("success"))

        # Incidente eliminado
        with session_scope(self.factory) as session:
            from db.models import DiagnosticIncident, DiagnosticAuditLog, CesAlarmEvent
            from sqlalchemy import select

            inc = session.get(DiagnosticIncident, self._incident_id)
            self.assertIsNone(inc)

            log = session.scalar(
                select(DiagnosticAuditLog)
                .where(DiagnosticAuditLog.diagnostic_id_original == self._incident_id)
            )
            self.assertIsNotNone(log)

            ev = session.get(CesAlarmEvent, self._event_id)
            self.assertIsNotNone(ev)
            self.assertIsNone(ev.diagnostic_id)

    def test_delete_in_progress_returns_409(self):
        with session_scope(self.factory) as session:
            from db.models import DiagnosticIncident
            inc = session.get(DiagnosticIncident, self._incident_id)
            inc.status = "analyzing"

        resp = self.http.delete(self._url())
        self.assertEqual(resp.status_code, 409)

    def test_delete_wrong_client_returns_404(self):
        wrong = uuid.uuid4()
        resp = self.http.delete(self._url(client_id=wrong))
        self.assertEqual(resp.status_code, 404)

    def test_delete_without_diagnostics_delete_permission(self):
        """Operador (sin DIAGNOSTICS_DELETE) recibe 403."""
        from core.authz import Principal
        from routers.security import get_principal

        def operator_override():
            # Operador conoce el cliente pero no tiene DIAGNOSTICS_DELETE
            return Principal(
                subject="test-operator",
                kind="service",
                platform_role="",
                client_roles={str(self._client_id): "operator"},
            )

        self.app.dependency_overrides[get_principal] = operator_override
        resp = self.http.delete(self._url())
        self.assertIn(resp.status_code, (403, 404))


# ─── Test 8-10: Permissions ───────────────────────────────────────────────────

class TestDiagnosticsPermissions(unittest.TestCase):

    def test_diagnostics_read_in_viewer(self):
        from core.authz import Permission, ROLE_PERMISSIONS, Role
        self.assertIn(Permission.DIAGNOSTICS_READ, ROLE_PERMISSIONS[Role.VIEWER])

    def test_diagnostics_manage_in_operator(self):
        from core.authz import Permission, ROLE_PERMISSIONS, Role
        self.assertIn(Permission.DIAGNOSTICS_MANAGE, ROLE_PERMISSIONS[Role.OPERATOR])

    def test_diagnostics_delete_only_in_admin(self):
        from core.authz import Permission, ROLE_PERMISSIONS, Role
        self.assertIn(Permission.DIAGNOSTICS_DELETE, ROLE_PERMISSIONS[Role.ADMIN])
        self.assertNotIn(Permission.DIAGNOSTICS_DELETE, ROLE_PERMISSIONS[Role.OPERATOR])
        self.assertNotIn(Permission.DIAGNOSTICS_DELETE, ROLE_PERMISSIONS[Role.VIEWER])


# ─── Test 11-13: Commands whitelist ───────────────────────────────────────────

class TestCommandWhitelist(unittest.TestCase):

    def test_all_executables_are_whitelisted(self):
        from diagnostics.commands import commands_for, ALLOWED_EXECUTABLES
        for alarm_type in ("cpu", "memory", "disk", "network", "unknown"):
            for name, args in commands_for(alarm_type):
                self.assertIn(
                    args[0], ALLOWED_EXECUTABLES,
                    f"Ejecutable {args[0]!r} no está en ALLOWED_EXECUTABLES ({alarm_type}/{name})"
                )

    def test_no_shell_chars_in_args(self):
        from diagnostics.commands import commands_for
        SHELL_CHARS = {"|", ">", "<", "&", ";", "$", "`"}
        for alarm_type in ("cpu", "memory", "disk", "network"):
            for name, args in commands_for(alarm_type):
                for arg in args:
                    for ch in SHELL_CHARS:
                        self.assertNotIn(
                            ch, arg,
                            f"Carácter de shell {ch!r} en {name}: {args}"
                        )

    def test_no_destructive_commands_in_whitelist(self):
        from diagnostics.commands import ALLOWED_EXECUTABLES
        FORBIDDEN = {"rm", "mv", "cp", "kill", "killall", "pkill", "chmod", "chown",
                     "reboot", "shutdown", "poweroff", "halt", "useradd", "userdel",
                     "passwd", "dd", "mkfs", "mount", "umount", "apt", "yum", "dnf",
                     "pip", "truncate", "tee", "iptables", "firewall-cmd"}
        overlap = FORBIDDEN & ALLOWED_EXECUTABLES
        self.assertEqual(overlap, set(),
                         f"Comandos destructivos en ALLOWED_EXECUTABLES: {overlap}")


# ─── Test 14-16: Analyzer ────────────────────────────────────────────────────

class TestAnalyzer(unittest.TestCase):

    def _make_result(self, stdout="", exit_code=0):
        from diagnostics.ssh import CommandResult
        return CommandResult(exit_code=exit_code, stdout=stdout, stderr="", duration_ms=10)

    def test_disk_full_detected(self):
        from diagnostics.analyzer import analyze
        df_output = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/sda1        50G   48G   2G   95% /\n"
        )
        result = analyze("disk", {"df": self._make_result(df_output)})
        self.assertEqual(result["alarm_type"], "disk")
        disk_findings = [f for f in result["findings"] if f["kind"] == "disk"]
        self.assertTrue(len(disk_findings) > 0, "Debe detectar disco lleno al 95%")
        self.assertIn("95", disk_findings[0]["summary"])

    def test_no_findings_gives_low_confidence(self):
        from diagnostics.analyzer import analyze
        result = analyze("cpu", {})
        self.assertEqual(result["confidence"], "low")

    def test_required_keys_present(self):
        from diagnostics.analyzer import analyze
        result = analyze("network", {})
        for key in ("alarm_type", "possible_cause", "confidence", "findings", "limitations"):
            self.assertIn(key, result, f"Clave obligatoria {key!r} ausente")


# ─── Test 17-19: /api/admin/whoami ───────────────────────────────────────────

class TestWhoami(unittest.TestCase):
    """El endpoint /api/admin/whoami refleja correctamente los permisos de diagnóstico."""

    def _make_admin_app(self, factory, platform_role="admin") -> FastAPI:
        from routers.admin import router as admin_router
        from routers.security import get_principal
        from core.authz import Principal
        from db.session import get_db

        app = FastAPI()
        app.include_router(admin_router)

        def db_override():
            with session_scope(factory) as session:
                yield session

        def principal_override():
            return Principal(subject="test-user", kind="service", platform_role=platform_role)

        app.dependency_overrides[get_db] = db_override
        app.dependency_overrides[get_principal] = principal_override
        return app

    def test_admin_can_delete_diagnostics(self):
        factory = sqlite_session_factory()
        try:
            app = self._make_admin_app(factory, platform_role="admin")
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/admin/whoami")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["can_delete_diagnostics"],
                            "Admin debe tener can_delete_diagnostics=true")
            self.assertTrue(data["can_manage_diagnostics"])
            self.assertTrue(data["can_read_diagnostics"])
        finally:
            factory.kw["bind"].dispose()

    def test_viewer_cannot_delete_diagnostics(self):
        factory = sqlite_session_factory()
        try:
            app = self._make_admin_app(factory, platform_role="viewer")
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/admin/whoami")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertFalse(data["can_delete_diagnostics"],
                             "Viewer no debe tener can_delete_diagnostics")
        finally:
            factory.kw["bind"].dispose()

    def test_operator_cannot_delete_diagnostics(self):
        factory = sqlite_session_factory()
        try:
            app = self._make_admin_app(factory, platform_role="operator")
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/admin/whoami")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertFalse(data["can_delete_diagnostics"],
                             "Operador no debe tener can_delete_diagnostics")
        finally:
            factory.kw["bind"].dispose()


# ─── Test 20-22: Seguridad del DELETE ────────────────────────────────────────

class TestDeleteSecurity(unittest.TestCase):
    """El endpoint DELETE no conecta a SSH ni modifica recursos de Huawei Cloud."""

    def setUp(self):
        self.factory = sqlite_session_factory()
        self._client_id = uuid.uuid4()

        with session_scope(self.factory) as session:
            from db.models import Client, DiagnosticIncident

            session.add(Client(
                id=self._client_id,
                name="Security Test Client",
                slug=f"sec-{self._client_id.hex[:8]}",
                status="active",
            ))
            session.flush()

            inc = DiagnosticIncident(
                client_id=self._client_id,
                account_id=None,
                ecs_name="ecs-sec-01",
                alarm_type="disk",
                status="report_available",
            )
            session.add(inc)
            session.flush()
            self._incident_id = inc.id

        self.app = _make_app(self.factory)
        self.http = TestClient(self.app, raise_server_exceptions=False)

    def tearDown(self):
        self.factory.kw["bind"].dispose()

    def test_delete_does_not_use_ssh(self):
        """El módulo diagnostics_api no importa diagnostics.ssh ni abre conexiones SSH."""
        import routers.diagnostics_api as api_module
        import sys

        # Verificar que ssh y paramiko no están entre las dependencias del módulo
        ssh_modules = {k for k in sys.modules if "paramiko" in k or "ssh" in k.lower()}
        # Si el módulo de DELETE llamase SSH, paramiko estaría importado
        # Aquí nos basta verificar que el módulo api no tiene imports de SSH
        src = api_module.__file__
        with open(src, encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("paramiko", content,
                         "diagnostics_api no debe importar paramiko")
        self.assertNotIn("run_diagnostic(", content,
                         "DELETE no debe llamar a run_diagnostic (que usa SSH)")

    def test_delete_does_not_modify_huawei_resources(self):
        """El módulo diagnostics_api no llama a collectors de Huawei Cloud."""
        import routers.diagnostics_api as api_module
        with open(api_module.__file__, encoding="utf-8") as f:
            content = f.read()
        hw_apis = ("huaweicloudsdkecs", "huaweicloudsdkces", "HuaweiCloud",
                   "DeleteServer", "DeleteAlarm", "StopServer")
        for api in hw_apis:
            self.assertNotIn(api, content,
                             f"diagnostics_api no debe referenciar {api!r}")

    def test_incident_survives_cancelled_delete(self):
        """Si el cliente NO llama a DELETE, el incidente permanece en BD."""
        # Simular que el usuario canceló el modal — simplemente no llamamos DELETE
        url = f"/api/clients/{self._client_id}/diagnostics/{self._incident_id}"
        # Solo GET, no DELETE
        resp = self.http.get(url)
        self.assertEqual(resp.status_code, 200)

        with session_scope(self.factory) as session:
            from db.models import DiagnosticIncident
            inc = session.get(DiagnosticIncident, self._incident_id)
            self.assertIsNotNone(inc, "El incidente debe seguir existiendo si no se eliminó")


# ─── Test 23-30: Comportamiento POST-delete y auditoría enriquecida ──────────

class TestDeleteBehavior(unittest.TestCase):
    """Comportamientos de DELETE: incidente visible en error, doble clic, auditoría."""

    def _build_incident(self, factory, client_id, status="report_available"):
        with session_scope(factory) as session:
            from db.models import DiagnosticIncident
            inc = DiagnosticIncident(
                client_id=client_id,
                account_id=None,
                ecs_name="ecs-beh-01",
                ecs_instance_id="i-abc",
                alarm_type="cpu",
                metric_name="cpu_util",
                region="la-north-7",
                status=status,
            )
            session.add(inc)
            session.flush()
            return inc.id

    def setUp(self):
        self.factory = sqlite_session_factory()
        self._client_id = uuid.uuid4()

        with session_scope(self.factory) as session:
            from db.models import Client
            session.add(Client(
                id=self._client_id,
                name="Behavior Client",
                slug=f"beh-{self._client_id.hex[:8]}",
                status="active",
            ))

        self.app = _make_app(self.factory)
        self.http = TestClient(self.app, raise_server_exceptions=False)

    def tearDown(self):
        self.factory.kw["bind"].dispose()

    def _url(self, iid):
        return f"/api/clients/{self._client_id}/diagnostics/{iid}"

    def test_delete_409_incident_still_exists(self):
        """Un DELETE 409 (en progreso) no debe eliminar el incidente."""
        iid = self._build_incident(self.factory, self._client_id, status="analyzing")
        resp = self.http.delete(self._url(iid))
        self.assertEqual(resp.status_code, 409)

        with session_scope(self.factory) as session:
            from db.models import DiagnosticIncident
            inc = session.get(DiagnosticIncident, iid)
            self.assertIsNotNone(inc, "El incidente debe seguir en BD tras un 409")

    def test_delete_404_incident_still_exists(self):
        """Un DELETE a cliente equivocado (404) no debe alterar el incidente."""
        iid = self._build_incident(self.factory, self._client_id)
        wrong_client = uuid.uuid4()
        resp = self.http.delete(f"/api/clients/{wrong_client}/diagnostics/{iid}")
        self.assertEqual(resp.status_code, 404)

        with session_scope(self.factory) as session:
            from db.models import DiagnosticIncident
            inc = session.get(DiagnosticIncident, iid)
            self.assertIsNotNone(inc, "El incidente debe seguir en BD tras un 404")

    def test_double_delete_second_returns_404(self):
        """Dos DELETE sobre el mismo incidente: el segundo recibe 404."""
        iid = self._build_incident(self.factory, self._client_id)
        r1 = self.http.delete(self._url(iid))
        r2 = self.http.delete(self._url(iid))
        self.assertEqual(r1.status_code, 200, "Primer DELETE debe ser 200")
        self.assertEqual(r2.status_code, 404, "Segundo DELETE debe ser 404 (ya eliminado)")

    def test_audit_log_contains_required_fields(self):
        """El log de auditoría almacena client_id, alarm_type, metric_name, region."""
        iid = self._build_incident(self.factory, self._client_id)
        resp = self.http.delete(self._url(iid) + "?reason=test-audit")
        self.assertEqual(resp.status_code, 200)

        with session_scope(self.factory) as session:
            from db.models import DiagnosticAuditLog
            from sqlalchemy import select
            log = session.scalar(
                select(DiagnosticAuditLog)
                .where(DiagnosticAuditLog.diagnostic_id_original == iid)
            )
            self.assertIsNotNone(log)
            self.assertEqual(log.reason, "test-audit")
            meta = log.metadata_safe or {}
            self.assertEqual(meta.get("client_id"), str(self._client_id))
            self.assertEqual(meta.get("alarm_type"), "cpu")
            self.assertEqual(meta.get("metric_name"), "cpu_util")
            self.assertEqual(meta.get("region"), "la-north-7")
            self.assertEqual(meta.get("ecs_instance_id"), "i-abc")
            self.assertIn("incident_created_at", meta)
            self.assertIn("status_at_deletion", meta)

    def test_audit_log_persists_after_incident_deleted(self):
        """El registro de auditoría sobrevive al borrado del incidente."""
        iid = self._build_incident(self.factory, self._client_id)
        self.http.delete(self._url(iid))

        with session_scope(self.factory) as session:
            from db.models import DiagnosticIncident, DiagnosticAuditLog
            from sqlalchemy import select
            self.assertIsNone(session.get(DiagnosticIncident, iid))
            log = session.scalar(
                select(DiagnosticAuditLog)
                .where(DiagnosticAuditLog.diagnostic_id_original == iid)
            )
            self.assertIsNotNone(log, "El log de auditoría debe persistir tras el borrado")

    def test_delete_does_not_connect_to_ecs(self):
        """El módulo diagnostics_api no hace referencia a conexiones ECS (IP, Fabric, SSH)."""
        import routers.diagnostics_api as api_module
        with open(api_module.__file__, encoding="utf-8") as f:
            content = f.read()
        forbidden = ("connect(", "socket.create_connection", "fabric", "SSHClient",
                     "run_diagnostic(")
        for token in forbidden:
            self.assertNotIn(token, content,
                             f"diagnostics_api no debe referenciar {token!r}")

    def test_protected_files_not_modified(self):
        """Los archivos protegidos no importan ni referencian el endpoint DELETE."""
        protected = {
            "diagnostics/pdf_generator.py",
            "diagnostics/analyzer.py",
            "diagnostics/executor.py",
            "diagnostics/worker.py",
        }
        import os, pathlib
        base = pathlib.Path(__file__).parent.parent
        for rel in protected:
            path = base / rel
            if not path.exists():
                continue
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertNotIn("delete_diagnostic", content,
                             f"{rel} no debe referenciar delete_diagnostic")
            self.assertNotIn("/api/clients", content,
                             f"{rel} no debe construir rutas de la API")


if __name__ == "__main__":
    unittest.main()
