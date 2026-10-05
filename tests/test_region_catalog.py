# coding: utf-8
"""Catálogo por región (flavors con estado/zonas y tipos de disco) y configuración libre
en varias regiones. Todo simulado: sin llamadas a Huawei Cloud."""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from tests.helpers import FAKE_AK, FAKE_SK, api_error
from tests.test_cost_compare import HK, MX, SG, CostCompareApiTestCase, quote

from core.authz import Principal
from costs import huawei_pricing
from costs.catalog import Flavor, PriceCatalog, VolumeType
from costs.flavor_names import descripcion_flavor, disco, estado, familia, lista_zonas, zonas_flavor
from db.models import AuditEvent
from db.session import session_scope
from routers.security import get_principal

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def flavor_api(fid, vcpus, ram, status="normal", perf="computingv3", az=None, **extra):
    specs = SimpleNamespace(condoperationstatus=status, ecsperformancetype=perf, ecsgeneration="x1",
                            condoperationaz=az, ecsinstance_architecture=extra.get("arch"),
                            infocpuname=extra.get("cpu"), infogpuname=None, quotamax_rate=extra.get("rate"),
                            quotamax_pps=extra.get("pps"))
    return SimpleNamespace(id=fid, vcpus=str(vcpus), ram=ram, os_extra_specs=specs)


def volume_api(name, zones, sold_out=""):
    return SimpleNamespace(name=name, extra_specs=SimpleNamespace(
        reske_yavailability_zones=zones, os_vendor_extendedsold_out_availability_zones=sold_out))


class TestNombresYParseo(unittest.TestCase):
    def test_readable_names_never_invent(self):
        self.assertEqual(familia("computingv3"), "Uso general mejorado")
        self.assertEqual(familia("nuevo_tipo"), "nuevo_tipo")
        self.assertEqual(familia(None), "Sin familia")
        self.assertEqual((estado(None), estado("sellout"), estado("obt")), ("Disponible", "Agotado", "Beta pública"))
        self.assertEqual((disco("gpssd"), disco("XYZ")), ("SSD de uso general", "XYZ"))
        self.assertEqual(descripcion_flavor(4, 8192, "computingv3"), "4 vCPU · 8 GB · Uso general mejorado")

    def test_zone_formats(self):
        self.assertEqual(zonas_flavor("la-north-2a(normal),la-north-2b(sellout)"),
                         {"la-north-2a": "normal", "la-north-2b": "sellout"})
        self.assertEqual(zonas_flavor(None), {})
        self.assertEqual(lista_zonas("az2, az1,,az1"), ["az1", "az2"])

    def test_fetch_flavors_keeps_sellout_and_beta_drops_abandon(self):
        response = SimpleNamespace(flavors=[
            flavor_api("x0.4u.8g", 4, 8192, az="mx-a(normal),mx-b(sellout)", rate="8000", pps="800000", cpu="Intel"),
            flavor_api("s6.large.2", 2, 4096, status="sellout", perf="normal"),
            flavor_api("kc1.large.2", 2, 4096, status="obt", perf="kunpeng_computing", arch="arm64"),
            flavor_api("old.flavor", 1, 1024, status="abandon")])
        fake = SimpleNamespace(list_flavors=lambda request: response)
        flavors = huawei_pricing.fetch_flavors(None, region=MX, project_id="p", client_builder=lambda *a: fake)
        self.assertEqual([f.flavor_id for f in flavors], ["x0.4u.8g", "s6.large.2", "kc1.large.2"])
        x0 = flavors[0]
        self.assertEqual((x0.status, x0.az_status, str(x0.max_bandwidth_gbps), x0.max_pps, x0.cpu_name),
                         ("normal", {"mx-a": "normal", "mx-b": "sellout"}, "8.00", 800000, "Intel"))
        self.assertEqual((flavors[1].status, flavors[2].status, flavors[2].architecture), ("sellout", "obt", "arm64"))

    def test_fetch_volume_types(self):
        fake = SimpleNamespace(cinder_list_volume_types=lambda request: SimpleNamespace(volume_types=[
            volume_api("GPSSD", "mx-a,mx-b", "mx-b"), volume_api("SAS", "mx-a"), volume_api("", "mx-a")]))
        types = huawei_pricing.fetch_volume_types(None, region=MX, project_id="p", client_builder=lambda *a: fake)
        self.assertEqual(types, [VolumeType("GPSSD", ("mx-a", "mx-b"), ("mx-b",)), VolumeType("SAS", ("mx-a",), ())])


class RegionCatalogApiTestCase(CostCompareApiTestCase):
    def seed_catalog(self):
        with session_scope(self.factory) as session:
            store = PriceCatalog(session)
            store.replace_flavors(MX, [
                Flavor("s6.small.1", 1, 1024, "normal", "s6", status="normal"),
                Flavor("x1.1u.1g", 1, 1024, "computingv3", "x1", status="normal"),
                Flavor("s6.medium.2", 1, 2048, "normal", "s6", status="sellout")], fetched_at=NOW)
            store.replace_flavors(SG, [Flavor("s6.small.1", 1, 1024, "normal", "s6", status="normal")],
                                  fetched_at=NOW)
            store.replace_volume_types(MX, [VolumeType("GPSSD", ("mx-a",), ()),
                                            VolumeType("SSD", ("mx-a",), ("mx-a",))], fetched_at=NOW)
            store.replace_volume_types(SG, [VolumeType("GPSSD", ("sg-a",), ())], fetched_at=NOW)
            store.store([quote(MX, "ecs", "s6.small.1.linux", 0, "9.00"), quote(MX, "evs", "GPSSD", 40, "4.50")])

    def simulate(self, regions, path="simulate", **config):
        body = {"regions": regions, "config": {"flavor": "s6.small.1", "os_type": "linux",
                                               "system_disk": {"volume_type": "GPSSD", "size_gb": 40}, **config}}
        return self.call("POST", f"{self.base}/{path}", json=body)


class TestCatalogApi(RegionCatalogApiTestCase):
    def test_empty_catalog_says_so(self):
        body = self.call("GET", f"{self.base}/catalog", params={"region": MX}).json()
        self.assertEqual((body["flavors"], body["volume_types"], body["flavors_updated_at"]), ([], [], None))
        self.assertTrue(body["has_project"])
        self.assertFalse(self.call("GET", f"{self.base}/catalog", params={"region": HK}).json()["has_project"])

    def test_catalog_with_details(self):
        self.seed_catalog()
        body = self.call("GET", f"{self.base}/catalog", params={"region": MX}).json()
        sellout = next(f for f in body["flavors"] if f["flavor_id"] == "s6.medium.2")
        self.assertEqual((sellout["estado"], sellout["available"], sellout["familia"]), ("Agotado", False, "Uso general"))
        ssd = next(v for v in body["volume_types"] if v["name"] == "SSD")
        self.assertEqual((ssd["tipo"], ssd["available_zones"]), ("Ultra alto I/O (SSD)", []))

    def test_unknown_region_and_isolation(self):
        self.assertEqual(self.call("GET", f"{self.base}/catalog", params={"region": "xx-1"}).status_code, 422)
        other = f"/api/clients/{self.other_cid}/accounts/{self.aid}/cost-compare/catalog"
        self.assertEqual(self.call("GET", other, params={"region": MX}).status_code, 404)

    def test_refresh_saves_flavors_even_if_evs_is_denied(self):
        flavors = SimpleNamespace(list_flavors=lambda r: SimpleNamespace(flavors=[flavor_api("x0.4u.8g", 4, 8192)]))

        def denied(request):
            raise api_error(403, f"Policy doesn't allow evs:types:get {FAKE_AK} {FAKE_SK}", "EVS.0003")
        volumes = SimpleNamespace(cinder_list_volume_types=denied)
        with mock.patch("costs.huawei_pricing.default_ecs_builder", lambda c, r, p: flavors), \
                mock.patch("costs.huawei_pricing.default_evs_builder", lambda c, r, p: volumes):
            response = self.call("POST", f"{self.base}/catalog/refresh", params={"region": MX})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["flavors"], body["volume_types"]), (1, None))
        self.assertEqual((body["errors"][0]["servicio"], body["errors"][0]["http_status"]), ("evs", 403))
        self.assertEqual(body["errors"][0]["accion_iam"], "evs:types:get")
        self.assertNotIn(FAKE_AK, response.text)
        self.assertNotIn(FAKE_SK, response.text)
        with session_scope(self.factory) as session:
            self.assertTrue(session.query(AuditEvent).filter_by(action="catalog.refresh").count())

    def test_refresh_requires_project_and_role(self):
        self.assertEqual(self.call("POST", f"{self.base}/catalog/refresh", params={"region": HK}).status_code, 422)
        self.app.dependency_overrides[get_principal] = lambda: Principal(
            subject="v", kind="user", client_roles={str(self.cid): "viewer"})
        self.assertEqual(self.call("POST", f"{self.base}/catalog/refresh", params={"region": MX}).status_code, 403)
        self.assertEqual(self.call("GET", f"{self.base}/catalog", params={"region": MX}).status_code, 200)


class TestSimulation(RegionCatalogApiTestCase):
    def test_availability_prices_and_cheapest(self):
        self.seed_catalog()
        body = self.simulate([SG, MX, HK]).json()
        by_region = {r["region"]: r for r in body["regions"]}
        mx, sg, hk = by_region[MX], by_region[SG], by_region[HK]
        self.assertEqual(mx["flavor_check"]["state"], "disponible")
        self.assertEqual([e["flavor_id"] for e in mx["flavor_check"]["equivalents"]], ["x1.1u.1g"])
        self.assertEqual(mx["cost"]["total"], "13.50")
        self.assertEqual(sg["cost"]["total"], "14.00")   # precios de CostCompareApiTestCase (10 + 4)
        self.assertEqual(hk["flavor_check"]["state"], "sin_catalogo")
        self.assertFalse(hk["has_project"])
        self.assertTrue(any("Sin Project" in w for w in hk["warnings"]))
        summary = body["summary"]
        self.assertEqual((summary["cheapest_region"], summary["cheapest_total"]), (MX, "13.50"))
        self.assertEqual(summary["reference_region"], SG)
        # HK no tiene Project, pero sí precios oficiales ya guardados: se usan (sin consultar a Huawei).
        self.assertEqual(hk["cost"]["total"], "17.00")
        self.assertEqual(summary["incomplete_regions"], [])
        self.assertEqual(next(d for d in summary["differences"] if d["region"] == HK)["difference"], "3.00")
        self.assertEqual(next(d for d in summary["differences"] if d["region"] == MX)["difference"], "-0.50")

    def test_sold_out_missing_flavor_and_disk_states(self):
        self.seed_catalog()
        body = self.simulate([MX], flavor="s6.medium.2",
                             data_disks=[{"volume_type": "SSD", "size_gb": 100},
                                         {"volume_type": "ESSD", "size_gb": 100}]).json()
        mx = body["regions"][0]
        self.assertEqual(mx["flavor_check"]["state"], "agotado")
        self.assertEqual([d["state"] for d in mx["disk_checks"]], ["disponible", "agotado", "no_existe"])
        self.assertIsNone(mx["cost"]["total"])     # faltan precios: nunca se suma un total parcial
        self.assertIn("Precio no disponible", mx["cost"]["reason"])
        missing = self.simulate([MX], flavor="z9.huge.9").json()["regions"][0]["flavor_check"]
        self.assertEqual(missing["state"], "no_existe")

    def test_validation(self):
        self.assertEqual(self.simulate([MX], flavor="BAD FLAVOR!").status_code, 422)
        self.assertEqual(self.simulate(["xx-1"]).status_code, 422)
        self.assertEqual(self.simulate([MX], system_disk={"volume_type": "NOPE", "size_gb": 40}).status_code, 422)
        self.assertEqual(self.simulate([MX] * 7).status_code, 422)

    def test_refresh_prices_only_regions_with_project(self):
        calls = []

        class FakeBss:
            def list_rate_on_period_detail(self, request):
                calls.append(request.body.product_infos[0].region)
                from decimal import Decimal
                return SimpleNamespace(currency="USD", official_website_rating_result=SimpleNamespace(
                    official_website_amount=Decimal("8.00"), measure_id=1))

        with mock.patch("costs.huawei_pricing.default_bss_builder", lambda clients: FakeBss()):
            response = self.simulate([MX, HK], path="simulate/prices/refresh",
                                     eip={"ip_type": "5_bgp", "bandwidth_mbps": 10, "bandwidth_mode": "bandwidth"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(set(calls), {MX})
        self.assertEqual(body["stored"], 4)   # ecs, evs, ip, ancho de banda
        self.assertTrue(all("Sin Project" in f["reason"] for f in body["failures"] if f["region"] == HK))


class TestComparatorPage(unittest.TestCase):
    def test_page_has_new_tabs_and_script(self):
        from fastapi.testclient import TestClient
        from app import app
        http = TestClient(app)
        page = http.get("/costos/comparar-regiones").text
        for marker in ('data-cc-tab="libre"', 'data-cc-tab="catalogo"', 'id="paneRecurso"', 'id="catFlavors"',
                       'id="freeRegions"', 'src="/static/region_catalog.js?v='):
            self.assertIn(marker, page)
        script = http.get("/static/region_catalog.js").text
        self.assertIn("/catalog/refresh", script)
        self.assertIn("/simulate", script)
        self.assertIn("function esc(", script)
        self.assertIn('"cc:account"', http.get("/static/costs_compare.js").text)


if __name__ == "__main__":
    unittest.main()
