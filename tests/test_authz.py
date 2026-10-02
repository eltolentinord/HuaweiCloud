# coding: utf-8
"""Autorización: matriz de roles, 404/403 y rutas protegidas por permiso."""

import unittest

from tests.db_helpers import SqliteTestCase
from tests.helpers import FAKE_AK, FAKE_SK
from tests.scan_helpers import simulated_huawei
from tests.test_scan_api import ScanApiTestCase

from core.authz import (
    AccessDenied,
    Permission,
    Principal,
    Role,
    ROLE_PERMISSIONS,
    can,
    ensure,
    principal_for_user,
)
from db.models import User
from routers.security import get_principal
from tenancy import clients

A, B = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"


def user(**roles):
    return Principal(subject="u", kind="user", client_roles=roles)


class TestMatrix(unittest.TestCase):
    def test_roles_are_cumulative(self):
        self.assertTrue(ROLE_PERMISSIONS[Role.VIEWER] < ROLE_PERMISSIONS[Role.OPERATOR] < ROLE_PERMISSIONS[Role.ADMIN])

    def test_viewer_operator_admin(self):
        viewer, operator, admin = user(**{A: "viewer"}), user(**{A: "operator"}), user(**{A: "admin"})
        self.assertTrue(can(viewer, Permission.INVENTORY_READ, A))
        self.assertFalse(can(viewer, Permission.SCANS_RUN, A))
        self.assertTrue(can(operator, Permission.SCANS_RUN, A))
        self.assertTrue(can(operator, Permission.PROJECTS_MANAGE, A))
        self.assertFalse(can(operator, Permission.CREDENTIALS_MANAGE, A))
        self.assertTrue(can(admin, Permission.CREDENTIALS_MANAGE, A))
        self.assertFalse(can(admin, Permission.CLIENTS_MANAGE, A))  # solo plataforma

    def test_isolation_between_clients(self):
        admin_a = user(**{A: "admin"})
        self.assertFalse(can(admin_a, Permission.INVENTORY_READ, B))
        with self.assertRaises(AccessDenied) as ctx:
            ensure(admin_a, Permission.INVENTORY_READ, B)
        self.assertEqual(ctx.exception.status_code, 404)  # no revela que B exista
        with self.assertRaises(AccessDenied) as ctx:
            ensure(user(**{A: "viewer"}), Permission.SCANS_RUN, A)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_platform_admin_and_unknown_roles(self):
        platform = Principal(subject="ops", kind="service", platform_role="admin")
        self.assertTrue(all(can(platform, p, B) for p in Permission))
        self.assertIsNone(user(**{A: "superuser"}).role_for(A))
        self.assertEqual(user(**{A: "viewer"}).visible_client_ids(), frozenset({A}))
        self.assertIsNone(platform.visible_client_ids())


class TestPrincipalFromDatabase(SqliteTestCase):
    def test_roles_loaded_from_user_client_roles(self):
        acme = clients.create_client(self.session, name="Acme")
        person = User(email="ana@example.com")
        self.session.add(person)
        self.session.flush()
        clients.grant_access(self.session, user_id=person.id, client_id=acme.id, role="operator")
        principal = principal_for_user(self.session, person.id)
        self.assertEqual(principal.role_for(acme.id), Role.OPERATOR)
        person.is_active = False
        self.assertIsNone(principal_for_user(self.session, person.id))


class TestApiAuthorization(ScanApiTestCase):
    def as_principal(self, **roles):
        self.app.dependency_overrides[get_principal] = lambda: user(**roles)

    def test_viewer_reads_but_cannot_change(self):
        self.as_principal(**{str(self.cid): "viewer"})
        base = f"/api/clients/{self.cid}/accounts/{self.aid}"
        self.assertEqual(self.call("GET", f"{base}/resources").status_code, 200)
        self.assertEqual(self.call("GET", f"/api/clients/{self.cid}/overview").status_code, 200)
        self.assertEqual(self.start().status_code, 403)
        self.assertEqual(self.call("PUT", f"/api/admin/clients/{self.cid}/accounts/{self.aid}/credentials",
                                   json={"ak": FAKE_AK, "sk": FAKE_SK}).status_code, 403)
        self.assertEqual(self.call("POST", "/api/admin/clients", json={"name": "X"}).status_code, 403)
        self.assertEqual(self.call("DELETE", f"/api/admin/clients/{self.cid}").status_code, 403)

    def test_viewer_cannot_see_other_clients(self):
        self.as_principal(**{str(self.cid): "viewer"})
        self.assertEqual([c["id"] for c in self.call("GET", "/api/admin/clients").json()], [str(self.cid)])
        other = f"/api/clients/{self.other_cid}/accounts/{self.other_aid}"
        for path in (f"{other}/resources", f"{other}/stats", f"/api/admin/clients/{self.other_cid}",
                     f"/api/admin/clients/{self.other_cid}/accounts"):
            self.assertEqual(self.call("GET", path).status_code, 404, path)
        self.app.dependency_overrides.pop(get_principal)
        scan_id = self.start(cid=self.other_cid, aid=self.other_aid, services=("ecs",)).json()["id"]
        self.as_principal(**{str(self.cid): "viewer"})
        self.assertEqual(self.call("GET", f"/api/scans/{scan_id}", params={"client_id": str(self.other_cid)}).status_code,
                         404)

    def test_operator_runs_scans_but_not_credentials(self):
        self.as_principal(**{str(self.cid): "operator"})
        self.assertEqual(self.start().status_code, 202)
        self.assertEqual(self.call("PUT", f"/api/admin/clients/{self.cid}/accounts/{self.aid}/credentials",
                                   json={"ak": FAKE_AK, "sk": FAKE_SK}).status_code, 403)

    def test_client_admin_manages_accounts_not_clients(self):
        self.as_principal(**{str(self.cid): "admin"})
        self.assertEqual(self.call("PUT", f"/api/admin/clients/{self.cid}/accounts/{self.aid}/credentials",
                                   json={"ak": FAKE_AK, "sk": FAKE_SK}).status_code, 200)
        self.assertEqual(self.call("PATCH", f"/api/admin/clients/{self.cid}", json={"name": "x"}).status_code, 403)

    def test_unauthenticated_principal_failure_propagates(self):
        from fastapi import HTTPException

        def deny():
            raise HTTPException(status_code=401, detail="no")
        self.app.dependency_overrides[get_principal] = deny
        self.assertEqual(self.call("GET", f"/api/clients/{self.cid}/overview").status_code, 401)
