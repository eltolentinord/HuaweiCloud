# coding: utf-8
"""IAM Discovery multi-tenant: validación, Projects, Regions, Enterprise Projects, permisos
denegados, aislamiento entre clientes y portafolio. Sin llamadas reales a Huawei Cloud."""

import unittest
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import select

from tests.db_helpers import SqliteTestCase
from tests.helpers import FAKE_AK, FAKE_SK, FakeClient, api_error
from tests.scan_helpers import seed_account, server
from tests.test_admin_api import AdminApiTestCase
from tests.test_discovery import MOS, MX, SG
from tests.test_scan_api import ScanApiTestCase

from huaweicloudsdkeps.v1 import EpDetail, ListEnterpriseProjectRequest

from core.authz import Principal
from core.discovery import (
    DiscoveredEnterpriseProject,
    EpsEnterpriseProjectSource,
    eps_region_for,
    parse_enterprise_projects,
)
from db.models import CloudAccount, EnterpriseProject
from db.session import session_scope
from routers.security import get_principal
from tenancy import iam_discovery

EP_DEFAULT = DiscoveredEnterpriseProject(id="0", name="default", status=1, type="prod")
EP_FIN = DiscoveredEnterpriseProject(id="ep-fin", name="finanzas", status=1, type="prod")
EP_LAB = DiscoveredEnterpriseProject(id="ep-lab", name="laboratorio", status=2, type="poc")


def patched_sources(projects=None, projects_exc=None, eps=None, eps_exc=None):
    """Sustituye las fuentes OFICIALES (IAM y EPS) por dobles de prueba."""
    iam = mock.patch("tenancy.projects.IamProjectSource")
    eps_patch = mock.patch("tenancy.iam_discovery.EpsEnterpriseProjectSource")

    class Both:
        def __enter__(self):
            iam_cls, eps_cls = iam.__enter__(), eps_patch.__enter__()
            if projects_exc:
                iam_cls.return_value.list_projects.side_effect = projects_exc
            else:
                iam_cls.return_value.list_projects.return_value = projects or []
            if eps_exc:
                eps_cls.return_value.list_enterprise_projects.side_effect = eps_exc
            else:
                eps_cls.return_value.list_enterprise_projects.return_value = eps or []
            self.iam, self.eps = iam_cls, eps_cls
            return self

        def __exit__(self, *exc):
            eps_patch.__exit__(*exc)
            iam.__exit__(*exc)

    return Both()


# ---------------------------------------------------------------- fuente EPS (SDK real)
class TestEpsSource(unittest.TestCase):
    def test_paginates_with_real_sdk_models_and_global_client(self):
        requests, created = [], {}
        items = [EpDetail(id=str(i), name=f"ep-{i}", status=1, type="prod") for i in range(1001)]

        def list_enterprise_project(request):
            requests.append(request)
            page = items[request.offset: request.offset + request.limit]
            return SimpleNamespace(enterprise_projects=page, total_count=len(items))

        class Factory:
            def create_global(self, client_cls, region_cls, prefix, region_id, domain_id=None):
                created.update(cls=client_cls.__name__, prefix=prefix, region=region_id, domain=domain_id)
                return FakeClient(list_enterprise_project=list_enterprise_project)

        found = EpsEnterpriseProjectSource(Factory(), domain_id="dom-1").list_enterprise_projects()
        self.assertEqual(len(found), 1001)
        self.assertTrue(all(isinstance(r, ListEnterpriseProjectRequest) for r in requests))
        self.assertEqual([(r.offset, r.limit) for r in requests], [(0, 1000), (1000, 1000)])
        self.assertEqual(created, {"cls": "EpsClient", "prefix": "eps", "region": "cn-north-4", "domain": "dom-1"})

    def test_region_and_parsing(self):
        self.assertEqual(eps_region_for("myhuaweicloud.com"), "cn-north-4")
        self.assertEqual(eps_region_for("myhuaweicloud.eu"), "eu-west-101")
        parsed = parse_enterprise_projects([{"id": 0, "name": "default", "status": 1},
                                            {"id": "0", "name": "duplicado"}, {"id": "x", "name": ""}])
        self.assertEqual([(p.id, p.name) for p in parsed], [("0", "default")])


# ---------------------------------------------------------------- sincronización
class TestEnterpriseSync(SqliteTestCase):
    def setUp(self):
        super().setUp()
        _, self.account, _ = seed_account(self.session, self.keyring)

    def test_create_update_and_missing_are_never_deleted(self):
        first = iam_discovery.sync_enterprise_projects(self.session, self.account, [EP_DEFAULT, EP_FIN, EP_FIN])
        self.assertEqual(sorted(first.created), ["0", "ep-fin"])
        renamed = DiscoveredEnterpriseProject(id="ep-fin", name="finanzas-2", status=1, type="prod")
        second = iam_discovery.sync_enterprise_projects(self.session, self.account, [EP_DEFAULT, renamed, EP_LAB])
        self.assertEqual((second.unchanged, second.updated, second.created), (["0"], ["ep-fin"], ["ep-lab"]))
        third = iam_discovery.sync_enterprise_projects(self.session, self.account, [EP_DEFAULT])
        self.assertEqual(sorted(third.missing), ["ep-fin", "ep-lab"])
        rows = {r.huawei_ep_id: r for r in self.session.scalars(select(EnterpriseProject))}
        self.assertEqual(len(rows), 3)                                   # nada se borra
        self.assertEqual((rows["ep-fin"].present, rows["ep-fin"].name), (False, "finanzas-2"))


# ---------------------------------------------------------------- API: validar y descubrir
class IamApiTestCase(AdminApiTestCase):
    def setUp(self):
        super().setUp()
        self.cid = self.new_client()["id"]
        self.aid = self.new_account(self.cid)["id"]
        self.admin = f"/api/admin/clients/{self.cid}/accounts/{self.aid}"
        self.read = f"/api/clients/{self.cid}"

    def validate(self, **sources):
        with patched_sources(**sources):
            return self.call("POST", f"{self.admin}/iam/validate")

    def discover(self, **sources):
        with patched_sources(**sources):
            return self.call("POST", f"{self.admin}/iam/discover")

    def account_row(self):
        with session_scope(self.factory) as session:
            row = session.get(CloudAccount, __import__("uuid").UUID(self.aid))
            return row.status, row.discovery_report, row.ak_ciphertext, row.sk_ciphertext


class TestValidate(IamApiTestCase):
    def test_valid_credentials_do_not_sync_anything(self):
        body = self.validate(projects=[SG, MX]).json()
        self.assertEqual((body["authenticated"], body["account_status"], body["projects_visible"]), (True, "active", 2))
        self.assertEqual(self.call("GET", f"{self.read}/projects").json(), [])

    def test_invalid_credentials(self):
        response = self.validate(projects_exc=api_error(401, f"bad {FAKE_AK} {FAKE_SK}", "APIGW.0301"))
        body = response.json()
        self.assertEqual((body["authenticated"], body["account_status"]), (False, "invalid"))
        self.assertNotIn(FAKE_AK, response.text)
        self.assertNotIn(FAKE_SK, response.text)

    def test_valid_but_without_permission_to_list_projects(self):
        body = self.validate(projects_exc=api_error(403, "no policy", "IAM.0002")).json()
        self.assertEqual((body["authenticated"], body["account_status"]), (True, "active"))
        self.assertIn("Permiso insuficiente", body["message"])


class TestDiscover(IamApiTestCase):
    def test_full_discovery(self):
        response = self.discover(projects=[SG, MX, MOS], eps=[EP_DEFAULT, EP_FIN])
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["steps"]["projects"]["status"], body["steps"]["projects"]["count"]), ("ok", 2))
        self.assertEqual((body["steps"]["enterprise_projects"]["status"], body["steps"]["enterprise_projects"]["count"]), ("ok", 2))
        self.assertEqual(body["regions"], ["ap-southeast-3", "la-north-2"])
        self.assertTrue(body["complete"])
        self.assertEqual([p["region_id"] for p in self.call("GET", f"{self.read}/projects").json()],
                         ["ap-southeast-3", "la-north-2"])
        self.assertEqual([r["region_id"] for r in self.call("GET", f"{self.read}/regions").json()],
                         ["ap-southeast-3", "la-north-2"])
        self.assertEqual({e["name"] for e in self.call("GET", f"{self.read}/enterprise-projects").json()},
                         {"default", "finanzas"})
        card = self.call("GET", f"{self.read}/accounts").json()[0]
        self.assertEqual((card["connected"], card["projects"], card["enterprise_projects"]), (True, 2, 2))
        self.assertEqual(card["discovery_steps"]["enterprise_projects"]["status"], "ok")

    def test_enterprise_projects_denied_does_not_fail_the_account(self):
        body = self.discover(projects=[SG], eps_exc=api_error(403, "Policy doesn't allow eps:enterpriseProjects:list",
                                                             "EPS.0004")).json()
        self.assertEqual(body["steps"]["projects"]["status"], "ok")
        step = body["steps"]["enterprise_projects"]
        self.assertEqual(step["status"], "denied")
        self.assertTrue(step["message"].startswith("Permiso insuficiente"))
        self.assertFalse(body["complete"])
        status, report, *_ = self.account_row()
        self.assertEqual((status, report["steps"]["enterprise_projects"]["status"]), ("active", "denied"))

    def test_projects_denied_still_discovers_enterprise_projects(self):
        body = self.discover(projects_exc=api_error(403, "denied", "IAM.0002"), eps=[EP_DEFAULT]).json()
        self.assertEqual((body["steps"]["projects"]["status"], body["steps"]["enterprise_projects"]["status"]),
                         ("denied", "ok"))
        self.assertEqual(self.account_row()[0], "active")

    def test_authentication_failure_stops_and_is_safe(self):
        response = self.discover(projects_exc=api_error(401, f"bad signature {FAKE_AK} {FAKE_SK}", "APIGW.0301"),
                                 eps=[EP_DEFAULT])
        self.assertEqual(response.status_code, 502)
        self.assertNotIn(FAKE_SK, response.text)
        status, report, ak, sk = self.account_row()
        self.assertEqual((status, report["steps"]["enterprise_projects"]["status"]), ("invalid", "skipped"))
        self.assertNotIn(FAKE_AK, str(report))
        self.assertNotIn(FAKE_AK.encode(), ak)          # credenciales siguen cifradas
        self.assertNotIn(FAKE_SK.encode(), sk)
        self.assertEqual(self.call("GET", f"{self.read}/enterprise-projects").json(), [])

    def test_unreachable_huawei_is_a_failure_not_a_permission_problem(self):
        err = ConnectionError("timeout")
        response = self.discover(projects_exc=err, eps_exc=err)
        self.assertEqual(response.status_code, 502)

    def test_multiple_accounts_in_one_client(self):
        self.discover(projects=[SG], eps=[EP_DEFAULT])
        second = self.new_account(self.cid, name="desarrollo")["id"]
        with patched_sources(projects=[MX], eps=[EP_LAB]):
            self.call("POST", f"/api/admin/clients/{self.cid}/accounts/{second}/iam/discover")
        everything = self.call("GET", f"{self.read}/projects").json()
        self.assertEqual({(p["account"], p["region_id"]) for p in everything},
                         {("prod", "ap-southeast-3"), ("desarrollo", "la-north-2")})
        only = self.call("GET", f"{self.read}/projects", params={"account_id": second}).json()
        self.assertEqual([p["region_id"] for p in only], ["la-north-2"])
        self.assertEqual(len(self.call("GET", f"{self.read}/accounts").json()), 2)

    def test_application_authorization(self):
        self.app.dependency_overrides[get_principal] = lambda: Principal(
            subject="v", kind="user", client_roles={self.cid: "viewer"})
        self.assertEqual(self.validate(projects=[SG]).status_code, 403)
        self.assertEqual(self.discover(projects=[SG]).status_code, 403)
        self.assertEqual(self.call("GET", f"{self.read}/projects").status_code, 200)


# ---------------------------------------------------------------- aislamiento y permisos efectivos
class TestTenantIsolationAndPermissions(ScanApiTestCase):
    """Tenant A (Acme) y Tenant B (Globex), cada uno con su cuenta y sus recursos."""

    def setUp(self):
        super().setUp()
        self.world.servers = {"p-sg": [server("srv-a1", "a-web")], "p-mx": [], "p-gx": [server("srv-b1", "b-web")]}
        self.assertEqual(self.start(("ecs", "obs")).status_code, 202)
        self.assertEqual(self.start(("ecs",), cid=self.other_cid, aid=self.other_aid).status_code, 202)
        self.a = f"/api/clients/{self.cid}"
        self.b = f"/api/clients/{self.other_cid}"

    def as_user_of(self, client_id):
        self.app.dependency_overrides[get_principal] = lambda: Principal(
            subject="u", kind="user", client_roles={str(client_id): "viewer"})

    def test_queries_for_tenant_a_never_return_tenant_b(self):
        summary = self.call("GET", f"{self.a}/accounts/{self.aid}/resource-summary").json()
        self.assertEqual(summary["by_service"], {"ecs": 1, "obs": 1})
        projects_a = self.call("GET", f"{self.a}/projects").json()
        self.assertNotIn("p-gx", {p["huawei_project_id"] for p in projects_a})
        other_project = self.call("GET", f"{self.b}/projects").json()[0]["id"]
        self.assertEqual(self.call("GET", f"{self.a}/accounts/{self.aid}/resource-summary",
                                   params={"project_id": other_project}).status_code, 404)
        for path in ("resource-summary", "permissions"):
            self.assertEqual(self.call("GET", f"{self.a}/accounts/{self.other_aid}/{path}").status_code, 404)
        self.assertEqual(self.call("GET", f"{self.a}/projects", params={"account_id": str(self.other_aid)}).status_code, 404)

    def test_user_of_tenant_a_cannot_reach_tenant_b(self):
        self.as_user_of(self.cid)
        for path in ("accounts", "projects", "regions", "enterprise-projects", f"accounts/{self.other_aid}/permissions"):
            self.assertEqual(self.call("GET", f"{self.b}/{path}").status_code, 404, path)
        portfolio = self.call("GET", "/api/clients").json()
        self.assertEqual([c["id"] for c in portfolio], [str(self.cid)])
        self.assertEqual(portfolio[0]["resources"], 2)

    def test_platform_admin_portfolio(self):
        cards = {c["name"]: c for c in self.call("GET", "/api/clients").json()}
        self.assertEqual((cards["Acme"]["resources"], cards["Globex"]["resources"]), (2, 1))
        self.assertEqual(cards["Acme"]["regions"], ["ap-southeast-3", "la-north-2"])
        self.assertIsNotNone(cards["Acme"]["last_scan"])
        text = self.call("GET", "/api/clients").text
        self.assertNotIn(FAKE_AK, text)
        self.assertNotIn(FAKE_SK, text)

    def test_permission_denied_is_per_service_not_global(self):
        self.world.failures["list_buckets"] = api_error(403, "Access Denied", "AccessDenied")
        self.start(("ecs", "obs"))
        self.start(("ecs",))  # un escaneo posterior sin OBS no borra lo sabido de OBS
        perms = {s["service"]: s["status"] for s in self.call("GET", f"{self.a}/accounts/{self.aid}/permissions").json()["services"]}
        self.assertEqual((perms["ecs"], perms["obs"], perms["hss"]), ("read", "denied", "not_checked"))
        card = self.call("GET", f"{self.a}/accounts").json()[0]
        self.assertEqual((card["connected"], card["limited_services"]), (card["status"] == "active", ["obs"]))
        self.assertEqual(self.call("GET", f"{self.a}/accounts/{self.aid}/resource-summary").json()["by_service"],
                         {"ecs": 1, "obs": 1})  # el inventario conocido de OBS se conserva


class TestClientsPage(unittest.TestCase):
    def test_page_csp_escaping_and_internal_routes(self):
        import re  # noqa: PLC0415

        from fastapi.testclient import TestClient  # noqa: PLC0415

        from app import app  # noqa: PLC0415
        http = TestClient(app)
        page = http.get("/clientes")
        self.assertEqual(page.status_code, 200)
        self.assertIn('src="/static/clients.js?v=', page.text)
        csp = page.headers["Content-Security-Policy"]
        for host in re.findall(r'(?:src|href)="(https://[^/"]+)', page.text):
            self.assertIn(host, csp)
        script = http.get("/static/clients.js").text
        self.assertIn("function esc(", script)
        self.assertNotRegex(script, r"\b(alert|confirm|prompt)\(")
        self.assertNotIn("localStorage.setItem(\"ak", script)
        for path in re.findall(r"`(/api/[^`$?]*)", script):
            self.assertTrue(path.startswith(("/api/admin/clients", "/api/clients")), path)
        self.assertIn('id="replaceCredentials"', script)
        self.assertIn("/credentials`", script)
        self.assertIn("/dashboard?client=", script)                       # enlace con el cliente ya elegido
        self.assertIn('linked.get("client")', http.get("/static/dashboard.js").text)
        for other in ("/dashboard", "/costos/comparar-regiones", "/clientes"):   # barra lateral común
            text = http.get(other).text
            self.assertIn('href="/clientes"', text)
            self.assertEqual(text.count('aria-current="page"'), 1, other)
        self.assertIn('id="clientNav"', page.text)                               # submenú solo en /clientes
        self.assertNotIn('id="clientNav"', http.get("/dashboard").text)
        self.assertIn('href="/dashboard"', http.get("/").text)                    # página heredada intacta


class TestRejectedCredentialsInScan(ScanApiTestCase):
    def test_card_reports_credentials_rejected_after_validation(self):
        self.world.failures["list_servers_details"] = api_error(401, f"Incorrect IAM authentication information {FAKE_AK}",
                                                              "APIGW.0301")
        self.start(("ecs", "obs"))
        card = self.call("GET", f"/api/clients/{self.cid}/accounts").json()[0]
        self.assertTrue(card["last_scan_auth_failed"])
        perms = {s["service"]: s["status"] for s in self.call(
            "GET", f"/api/clients/{self.cid}/accounts/{self.aid}/permissions").json()["services"]}
        self.assertEqual(perms["ecs"], "error")
        self.assertNotEqual(perms["obs"], "denied")  # no se confunde con falta de permiso


class TestCredentialsReplacementAndEnterpriseFilter(IamApiTestCase):
    def test_replace_credentials_resets_validation_without_leaking(self):
        self.validate(projects=[SG])
        with session_scope(self.factory) as session:
            before = session.get(CloudAccount, __import__("uuid").UUID(self.aid))
            old_ak, old_sk = before.ak_ciphertext, before.sk_ciphertext
        new_ak, new_sk = "NEWAKFAKE0000TESTONLY", "NEWSKFAKE0000TESTONLYSECRET"
        response = self.call("PUT", f"{self.admin}/credentials", json={"ak": new_ak, "sk": new_sk})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "pending")
        for secret in (new_ak, new_sk, FAKE_AK, FAKE_SK):
            self.assertNotIn(secret, response.text)
        status, _, ak, sk = self.account_row()
        self.assertEqual(status, "pending")
        self.assertNotEqual((ak, sk), (old_ak, old_sk))
        self.assertNotIn(new_sk.encode(), sk)                        # se guarda cifrada
        self.app.dependency_overrides[get_principal] = lambda: Principal(
            subject="v", kind="user", client_roles={self.cid: "viewer"})
        self.assertEqual(self.call("PUT", f"{self.admin}/credentials", json={"ak": "x", "sk": "y"}).status_code, 403)


class TestResourcesByEnterpriseProject(ScanApiTestCase):
    def test_filter_and_isolation(self):
        self.world.servers = {"p-sg": [server("s1", "fin", enterprise_project_id="ep-fin"),
                                       server("s2", "def", enterprise_project_id="0")], "p-mx": [],
                              "p-gx": [server("g1", "otro", enterprise_project_id="ep-fin")]}
        self.start(("ecs",))
        self.start(("ecs",), cid=self.other_cid, aid=self.other_aid)
        page = self.call("GET", f"/api/clients/{self.cid}/accounts/{self.aid}/resources",
                         params={"enterprise_project_id": "ep-fin"}).json()
        self.assertEqual([r["provider_id"] for r in page["items"]], ["s1"])      # nunca g1 (otro cliente)
        self.assertEqual(self.call("GET", f"/api/clients/{self.cid}/accounts/{self.aid}/resources",
                                   params={"enterprise_project_id": "x" * 65}).status_code, 422)
