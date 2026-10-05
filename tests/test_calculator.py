# coding: utf-8
"""Calculadora de precios: ítems ECS/EVS/EIP, modos mensual/anual/por uso, lista con total y
exportación. Todo simulado: sin llamadas a Huawei Cloud."""

import csv
import io
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from openpyxl import load_workbook

from tests.helpers import FAKE_AK, FAKE_SK, api_error
from tests.test_cost_compare import HK, MX, SG, CostCompareApiTestCase

from core.authz import Principal
from costs import huawei_pricing
from costs.calculator import item_from_payload, price_item, price_list, quotable
from costs.catalog import Flavor, PriceCatalog, Quote
from costs.configuration import Component, ConfigurationError
from db.session import session_scope
from routers.security import get_principal

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
REGIONS = [SG, MX, HK]


def q(region, product, spec, size, amount, mode="monthly", period=None):
    period = period or {"monthly": "month", "yearly": "year", "on_demand": "hour"}[mode]
    return Quote(region=region, product=product, spec=spec, size=size, billing_mode=mode, amount=Decimal(amount),
                 period=period, currency="USD", fetched_at=NOW, source_detail="test")


def ecs_item(mode="monthly", duration=1, quantity=1, eip=None, data_disks=(), region=MX, flavor="x1.2u.4g"):
    return {"region": region, "billing_mode": mode, "duration": duration, "quantity": quantity, "product": "ecs",
            "config": {"flavor": flavor, "os_type": "linux", "system_disk": {"volume_type": "GPSSD", "size_gb": 40},
                       "data_disks": list(data_disks), "eip": eip}}


class TestItems(unittest.TestCase):
    def test_ecs_components_and_bss_codes(self):
        item = item_from_payload(ecs_item(eip={"ip_type": "5_bgp", "bandwidth_mode": "bandwidth", "bandwidth_mbps": 10},
                                          data_disks=[{"volume_type": "SSD", "size_gb": 100}]), REGIONS)
        self.assertEqual([(c.product, c.spec, c.size) for c in item.components()], [
            ("ecs", "x1.2u.4g.linux", 0), ("evs", "GPSSD", 40), ("evs", "SSD", 100), ("ip", "5_bgp", 0),
            ("bandwidth", "19_bgp", 10)])
        traffic = item_from_payload({"region": MX, "billing_mode": "monthly", "duration": 2, "quantity": 1,
                                     "product": "eip", "config": {"ip_type": "5_sbgp", "bandwidth_mode": "traffic",
                                                                  "bandwidth_mbps": 50, "traffic_gb": 300}}, REGIONS)
        self.assertEqual([(c.product, c.spec, c.size) for c in traffic.components()],
                         [("ip", "5_sbgp", 0), ("traffic", "12_sbgp", 50)])
        self.assertEqual(traffic.traffic_gb, 300)
        self.assertEqual(quotable([traffic])[(MX, "on_demand")][0].product, "traffic")   # tráfico: siempre por uso
        self.assertEqual(quotable([traffic])[(MX, "monthly")][0].product, "ip")

    def test_validation(self):
        bad = [dict(ecs_item(), region="xx-1"), dict(ecs_item(), billing_mode="weekly"), ecs_item(duration=10),
               ecs_item(mode="yearly", duration=4), ecs_item(quantity=0), dict(ecs_item(), product="rds"),
               ecs_item(flavor="BAD!"), {"region": MX, "product": "evs", "config": {"volume_type": "X", "size_gb": 10}}]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(ConfigurationError):
                item_from_payload(payload, REGIONS)


class TestPricing(CostCompareApiTestCase):
    def setUp(self):
        super().setUp()
        with session_scope(self.factory) as session:
            store = PriceCatalog(session)
            store.store([q(MX, "ecs", "x1.2u.4g.linux", 0, "30.00"), q(MX, "evs", "GPSSD", 40, "4.00"),
                         q(MX, "ecs", "x1.2u.4g.linux", 0, "300.00", "yearly"), q(MX, "evs", "GPSSD", 40, "40.00", "yearly"),
                         q(MX, "ecs", "x1.2u.4g.linux", 0, "0.05", "on_demand"), q(MX, "evs", "GPSSD", 40, "0.01", "on_demand"),
                         q(MX, "ip", "5_bgp", 0, "3.00"), q(MX, "traffic", "12_bgp", 5, "0.10", "on_demand", "gb")])
            store.replace_flavors(MX, [Flavor("x1.2u.4g", 2, 4096, "computingv3", "x1", status="normal")], fetched_at=NOW)
        self.calc = f"/api/clients/{self.cid}/accounts/{self.aid}/calculator"

    def priced(self, payload):
        with session_scope(self.factory) as session:
            return price_item(PriceCatalog(session), item_from_payload(payload, REGIONS), has_project=True)

    def test_monthly_yearly_and_on_demand(self):
        self.assertEqual(self.priced(ecs_item(duration=3, quantity=2))["total"], "204.00")      # (30+4)×3×2
        yearly = self.priced(ecs_item(mode="yearly", duration=2))
        self.assertEqual(yearly["total"], "680.00")                                              # anual oficial ×2
        self.assertEqual(yearly["lines"][0]["unit"], "año")
        self.assertEqual(self.priced(ecs_item(mode="on_demand", duration=730))["total"], "43.80")  # (0.05+0.01)×730

    def test_traffic_uses_estimated_gb_and_missing_price_blocks_total(self):
        eip = {"ip_type": "5_bgp", "bandwidth_mode": "traffic", "bandwidth_mbps": 5, "traffic_gb": 200}
        priced = self.priced({"region": MX, "billing_mode": "monthly", "duration": 1, "quantity": 1, "product": "eip",
                              "config": eip})
        self.assertEqual(priced["total"], "23.00")                                               # 3 + 0.10×200
        missing = self.priced(ecs_item(mode="yearly", eip={"ip_type": "5_bgp", "bandwidth_mode": "bandwidth",
                                                             "bandwidth_mbps": 5}))
        self.assertIsNone(missing["total"])
        self.assertIn("Precio no disponible", missing["reason"])

    def test_list_totals_only_complete_items(self):
        with session_scope(self.factory) as session:
            catalog = PriceCatalog(session)
            items = [price_item(catalog, item_from_payload(p, REGIONS), has_project=True)
                     for p in (ecs_item(), ecs_item(region=SG))]
        result = price_list(items)
        self.assertEqual((result["totals"], result["complete"], result["incomplete"]),
                         ([{"currency": "USD", "total": "34.00"}], 1, 1))

    # -------------------------------------------------------------- API
    def test_options_with_flavor_prices(self):
        body = self.call("GET", f"{self.calc}/options", params={"region": MX}).json()
        self.assertTrue(body["has_project"])
        self.assertEqual(body["flavors"][0]["price"], {"amount": "30.00000000", "currency": "USD", "period": "month"})
        self.assertFalse(next(r for r in body["regions"] if r["id"] == HK)["has_project"])
        self.assertEqual([m["id"] for m in body["modes"]], ["monthly", "yearly", "on_demand"])

    def test_quote_list_and_server_side_recalculation(self):
        quote = self.call("POST", f"{self.calc}/quote", json=ecs_item()).json()
        self.assertEqual(quote["total"], "34.00")
        tampered = dict(ecs_item(), total="0.01", lines=[])
        listed = self.call("POST", f"{self.calc}/list", json={"items": [tampered, ecs_item(region=HK)]}).json()
        self.assertEqual(listed["totals"], [{"currency": "USD", "total": "34.00"}])
        hk = listed["items"][1]
        self.assertFalse(hk["has_project"])
        self.assertTrue(any("Sin Project" in w for w in hk["warnings"]))
        self.assertEqual(self.call("POST", f"{self.calc}/quote", json=ecs_item(duration=99)).status_code, 422)

    def test_exports(self):
        body = {"items": [ecs_item(quantity=2), ecs_item(region=HK)]}
        xlsx = self.call("POST", f"{self.calc}/export", params={"format": "xlsx"}, json=body)
        self.assertEqual(xlsx.status_code, 200, xlsx.text)
        wb = load_workbook(io.BytesIO(xlsx.content))
        self.assertEqual(wb.sheetnames, ["Resumen", "Detalle"])
        values = [c.value for row in wb["Resumen"].iter_rows() for c in row]
        self.assertIn("68.00", values)
        self.assertIn("Precio no disponible", values)
        pdf = self.call("POST", f"{self.calc}/export", params={"format": "pdf"}, json=body)
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertEqual(pdf.headers["content-type"], "application/pdf")
        rows = list(csv.reader(io.StringIO(self.call("POST", f"{self.calc}/export", params={"format": "csv"},
                                                     json=body).content.decode("utf-8-sig"))))
        self.assertEqual(rows[0][:3], ["#", "Producto", "Región"])
        self.assertEqual(rows[-1][7], "68.00")

    def test_refresh_prices_yearly_and_traffic(self):
        seen = []

        class FakeBss:
            def list_rate_on_period_detail(self, request):
                info = request.body.product_infos[0]
                seen.append(("period", info.period_type, info.resource_spec))
                return SimpleNamespace(currency="USD", official_website_rating_result=SimpleNamespace(
                    official_website_amount=Decimal("100.00"), measure_id=1))

            def list_on_demand_resource_ratings(self, request):
                info = request.body.product_infos[0]
                seen.append(("demand", info.usage_factor, info.resource_spec))
                return SimpleNamespace(currency="USD", official_website_amount=Decimal("0.09"), measure_id=1)

        item = {"region": MX, "billing_mode": "yearly", "duration": 1, "quantity": 1, "product": "eip",
                "config": {"ip_type": "5_bgp", "bandwidth_mode": "traffic", "bandwidth_mbps": 5, "traffic_gb": 10}}
        with mock.patch("costs.huawei_pricing.default_bss_builder", lambda clients: FakeBss()):
            response = self.call("POST", f"{self.calc}/prices/refresh", json={"items": [item, ecs_item(region=HK)]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(("period", 3, "5_bgp"), seen)                 # anual: period_type 3
        self.assertIn(("demand", "upflow", "12_bgp"), seen)         # tráfico: por uso, upflow
        self.assertTrue(all(f["region"] == HK and "Sin Project" in f["reason"] for f in response.json()["failures"]))
        priced = self.call("POST", f"{self.calc}/quote", json=item).json()
        self.assertEqual(priced["total"], "100.90")                 # 100 (IP 1 año) + 0.09 × 10 GB

    def test_refresh_errors_are_safe_and_role_checked(self):
        class Denied:
            def list_rate_on_period_detail(self, request):
                raise api_error(403, f"Access denied {FAKE_AK} {FAKE_SK}", "CBC.0151")

        with mock.patch("costs.huawei_pricing.default_bss_builder", lambda clients: Denied()):
            response = self.call("POST", f"{self.calc}/prices/refresh", json={"items": [ecs_item()]})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(FAKE_AK, response.text)
        self.assertNotIn(FAKE_SK, response.text)
        self.app.dependency_overrides[get_principal] = lambda: Principal(
            subject="v", kind="user", client_roles={str(self.cid): "viewer"})
        self.assertEqual(self.call("POST", f"{self.calc}/prices/refresh", json={"items": [ecs_item()]}).status_code, 403)
        self.assertEqual(self.call("POST", f"{self.calc}/quote", json=ecs_item()).status_code, 200)

    def test_isolation(self):
        other = f"/api/clients/{self.other_cid}/accounts/{self.aid}/calculator/quote"
        self.assertEqual(self.call("POST", other, json=ecs_item()).status_code, 404)


RDS_CODES = {"service": "hws.service.type.rds", "instance": "hws.resource.type.rds.vm",
             "storage": "hws.resource.type.rds.volume"}
OBS_CODES = {"service": "hws.service.type.obs", "resource": "hws.resource.type.obs", "usage": "storageSize"}


def rds_item(codes=RDS_CODES, region=MX, mode="monthly"):
    return {"region": region, "billing_mode": mode, "duration": 2, "quantity": 1, "product": "rds",
            "config": {"engine": "MySQL", "version": "8.0", "instance_mode": "ha", "spec_code": "rds.mysql.n1.large.2.ha",
                       "storage_type": "CLOUDSSD", "storage_gb": 100, "codes": codes}}


def obs_item():
    return {"region": MX, "billing_mode": "monthly", "duration": 1, "quantity": 1, "product": "obs",
            "config": {"storage_class": "STANDARD", "storage_gb": 500, "measure_id": 10, "codes": OBS_CODES}}


class TestRdsAndObs(CostCompareApiTestCase):
    def setUp(self):
        super().setUp()
        self.calc = f"/api/clients/{self.cid}/accounts/{self.aid}/calculator"

    def save_codes(self):
        with session_scope(self.factory) as session:
            store = PriceCatalog(session)
            store.replace_bss_codes([{"kind": "service", "code": RDS_CODES["service"], "name": "RDS"},
                                     {"kind": "resource", "code": RDS_CODES["instance"], "parent_code": RDS_CODES["service"]},
                                     {"kind": "resource", "code": RDS_CODES["storage"], "parent_code": RDS_CODES["service"]},
                                     {"kind": "service", "code": OBS_CODES["service"], "name": "OBS"},
                                     {"kind": "resource", "code": OBS_CODES["resource"], "parent_code": OBS_CODES["service"]}],
                                    fetched_at=NOW, kinds=("service", "resource"))
            store.replace_bss_codes([{"kind": "usage", "code": "storageSize", "parent_code": OBS_CODES["resource"]}],
                                    fetched_at=NOW, kinds=("usage",), parent=OBS_CODES["resource"])

    def test_components_use_confirmed_codes_only(self):
        confirmed = {"service": {RDS_CODES["service"]}, "resource": {RDS_CODES["instance"], RDS_CODES["storage"]},
                     "usage": set()}
        item = item_from_payload(rds_item(), REGIONS, confirmed)
        comps = item.components()
        self.assertEqual([(c.product, c.spec, c.size, c.codes) for c in comps], [
            ("rds", "rds.mysql.n1.large.2.ha", 0, (RDS_CODES["service"], RDS_CODES["instance"], None)),
            ("rds_storage", "CLOUDSSD", 100, (RDS_CODES["service"], RDS_CODES["storage"], 17))])
        unconfirmed = item_from_payload(rds_item(), REGIONS, {"service": set(), "resource": set(), "usage": set()})
        self.assertIn("no está en la lista oficial", unconfirmed.components()[0].problem)
        missing = item_from_payload(rds_item(codes={}), REGIONS, confirmed)
        self.assertIn("faltan los códigos BSS", missing.components()[0].problem)
        self.assertEqual(quotable([missing]), {})          # nunca se consulta a Huawei sin códigos confirmados
        with self.assertRaises(ConfigurationError):
            item_from_payload(dict(rds_item(), config=dict(rds_item()["config"], engine="Oracle")), REGIONS)

    def test_quote_rds_and_obs_with_official_prices(self):
        self.save_codes()
        with session_scope(self.factory) as session:
            PriceCatalog(session).store([q(MX, "rds", "rds.mysql.n1.large.2.ha", 0, "150.00"),
                                         q(MX, "rds_storage", "CLOUDSSD", 100, "12.00"),
                                         q(MX, "obs_storage", "STANDARD", 0, "0.02", "on_demand", "gb")])
        rds = self.call("POST", f"{self.calc}/quote", json=rds_item()).json()
        self.assertEqual(rds["total"], "324.00")                        # (150 + 12) × 2 meses
        obs = self.call("POST", f"{self.calc}/quote", json=obs_item()).json()
        self.assertEqual(obs["total"], "10.00")                         # 0.02 × 500 GB
        self.assertTrue(any("OBS" in w for w in obs["warnings"]))
        export = self.call("POST", f"{self.calc}/export", params={"format": "pdf"}, json={"items": [rds_item(), obs_item()]})
        self.assertTrue(export.content.startswith(b"%PDF"))

    def test_refresh_rds_catalog_and_flavors_endpoint(self):
        class FakeRds:
            def list_datastores(self, request):
                return SimpleNamespace(data_stores=[SimpleNamespace(name="8.0"), SimpleNamespace(name="5.7")])

            def list_flavors(self, request):
                return SimpleNamespace(flavors=[
                    SimpleNamespace(vcpus="2", ram=4, spec_code=f"rds.mysql.n1.large.2.ha", instance_mode="ha",
                                    az_status={"mx-a": "normal"}),
                    SimpleNamespace(vcpus="2", ram=4, spec_code="rds.mysql.n1.large.2", instance_mode="single",
                                    az_status={"mx-a": "sellout"})])

        with mock.patch("costs.huawei_pricing.default_rds_builder", lambda c, r, p: FakeRds()):
            response = self.call("POST", f"{self.calc}/rds/refresh", params={"region": MX, "engine": "MySQL"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["flavors"], {"8.0": 2, "5.7": 2})
        body = self.call("GET", f"{self.calc}/rds/flavors", params={"region": MX, "engine": "MySQL"}).json()
        self.assertEqual((body["version"], body["versions"]), ("8.0", ["8.0", "5.7"]))
        single = next(f for f in body["flavors"] if f["instance_mode"] == "single")
        self.assertFalse(single["available"])
        self.assertEqual(self.call("POST", f"{self.calc}/rds/refresh",
                                   params={"region": HK, "engine": "MySQL"}).status_code, 422)   # sin Project
        self.assertEqual(self.call("GET", f"{self.calc}/rds/flavors",
                                   params={"region": MX, "engine": "Oracle"}).status_code, 422)

    def test_refresh_bss_codes_and_usage(self):
        class FakeBss:
            def list_service_types(self, request):
                items = [SimpleNamespace(service_type_code="hws.service.type.obs", service_type_name="OBS",
                                         abbreviation="OBS")] if request.offset == 0 else []
                return SimpleNamespace(service_types=items, total_count=1)

            def list_resource_types(self, request):
                return SimpleNamespace(resource_types=[SimpleNamespace(
                    resource_type_code="hws.resource.type.obs", resource_type_name="Bucket",
                    service_type_code="hws.service.type.obs")], total_count=1)

            def list_usage_types(self, request):
                return SimpleNamespace(usage_types=[SimpleNamespace(code="storageSize", name="Storage")], total_count=1)

        with mock.patch("costs.huawei_pricing.default_bss_builder", lambda clients: FakeBss()):
            self.assertEqual(self.call("POST", f"{self.calc}/bss-codes/refresh").json(), {"codes": 2})
            usage = self.call("POST", f"{self.calc}/bss-codes/usage/refresh", params={"resource": "hws.resource.type.obs"})
        self.assertEqual(usage.json(), {"resource": "hws.resource.type.obs", "codes": 1})
        listed = self.call("GET", f"{self.calc}/bss-codes", params={"search": "obs", "service": "hws.service.type.obs",
                                                                    "resource": "hws.resource.type.obs"}).json()
        self.assertEqual([s["code"] for s in listed["services"]], ["hws.service.type.obs"])
        self.assertEqual([u["code"] for u in listed["usages"]], ["storageSize"])
        self.assertEqual(self.call("POST", f"{self.calc}/bss-codes/usage/refresh",
                                   params={"resource": "hws.resource.type.nope"}).status_code, 422)

    def test_obs_refresh_quotes_on_demand_with_usage_factor(self):
        self.save_codes()
        seen = []

        class FakeBss:
            def list_on_demand_resource_ratings(self, request):
                info = request.body.product_infos[0]
                seen.append((info.usage_factor, info.usage_measure_id, info.cloud_service_type, info.resource_type))
                return SimpleNamespace(currency="USD", official_website_amount=Decimal("0.02"), measure_id=1)

        with mock.patch("costs.huawei_pricing.default_bss_builder", lambda clients: FakeBss()):
            response = self.call("POST", f"{self.calc}/prices/refresh", json={"items": [obs_item()]})
        self.assertEqual(response.json()["stored"], 1)
        self.assertEqual(seen, [("storageSize", 10, OBS_CODES["service"], OBS_CODES["resource"])])
        self.assertEqual(self.call("POST", f"{self.calc}/quote", json=obs_item()).json()["total"], "10.00")


class TestQuoteComponentModes(unittest.TestCase):
    def test_yearly_period_type_and_unknown_mode(self):
        captured = {}

        class Fake:
            def list_rate_on_period_detail(self, request):
                captured["type"] = request.body.product_infos[0].period_type
                return SimpleNamespace(currency="USD", official_website_rating_result=SimpleNamespace(
                    official_website_amount=Decimal("12"), measure_id=1))

        quote = huawei_pricing.quote_component(Fake(), project_id="p", region=MX, billing_mode="yearly",
                                               component=Component("ip", "5_bgp"))
        self.assertEqual((captured["type"], quote.period, quote.billing_mode), (3, "year", "yearly"))
        with self.assertRaises(huawei_pricing.PricingDataError):
            huawei_pricing.quote_component(Fake(), project_id="p", region=MX, billing_mode="weekly",
                                           component=Component("ip", "5_bgp"))


class TestCalculatorPage(unittest.TestCase):
    def test_page_and_sidebar(self):
        from fastapi.testclient import TestClient
        from app import app
        http = TestClient(app)
        page = http.get("/costos/calculadora").text
        for marker in ('data-product="ecs"', 'id="ecsFlavors"', 'id="list"', 'data-export="pdf"',
                       'src="/static/calculator.js?v='):
            self.assertIn(marker, page)
        self.assertIn('href="/costos/calculadora"', http.get("/clientes").text)
        script = http.get("/static/calculator.js").text
        for marker in ("/calculator", "/prices/refresh", "function esc("):
            self.assertIn(marker, script)


if __name__ == "__main__":
    unittest.main()
