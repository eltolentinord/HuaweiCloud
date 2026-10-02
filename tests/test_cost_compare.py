# coding: utf-8
"""Comparador de costos por región: origen real del inventario, destino editable,
precios solo de fuentes oficiales (catálogo BSS simulado o tabla), aislamiento y
seguridad. Ninguna llamada real a Huawei Cloud."""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import select

from tests.helpers import FAKE_AK, FAKE_SK, api_error
from tests.scan_helpers import server
from tests.test_scan_api import ScanApiTestCase

from core.authz import Principal
from costs.catalog import Flavor, PriceCatalog, Quote
from costs.comparison import CostComparison, SideCost, difference
from costs.configuration import Component, ConfigurationError, ResourceConfiguration, destination_from_payload
from costs.resolver import PricedComponent, PriceResolver
from db.models import AuditEvent, InventoryResource, PriceCatalogEntry
from db.session import session_scope
from routers.security import get_principal

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
SG, HK, MX = "ap-southeast-3", "ap-southeast-1", "la-north-2"
PRICES = {  # (región, producto, spec, tamaño) -> precio mensual oficial (simulado en tests)
    (SG, "ecs", "s6.small.1.linux", 0): "10.00", (SG, "evs", "GPSSD", 40): "4.00",
    (SG, "ip", "5_bgp", 0): "3.00", (SG, "bandwidth", "19_bgp", 5): "20.00", (SG, "evs", "SSD", 100): "15.00",
    (HK, "ecs", "s6.small.1.linux", 0): "12.00", (HK, "ecs", "s6.medium.2.linux", 0): "18.00",
    (HK, "evs", "GPSSD", 40): "5.00", (HK, "ip", "5_bgp", 0): "3.50", (HK, "bandwidth", "19_bgp", 5): "22.00",
    (HK, "evs", "SSD", 100): "15.00",
}


def quote(region, product, spec, size, amount, mode="monthly", currency="USD"):
    return Quote(region=region, product=product, spec=spec, size=size, billing_mode=mode, amount=Decimal(amount),
                 period="month" if mode == "monthly" else "hour", currency=currency, fetched_at=NOW,
                 source_detail="ListRateOnPeriodDetail, official_website_amount")


class CostCompareApiTestCase(ScanApiTestCase):
    def setUp(self):
        super().setUp()
        self.world.servers = {"p-sg": [
            server("srv-1", "web", flavor={"id": "s6.small.1", "name": "s6.small.1", "vcpus": "1", "ram": 1024},
                   metadata={"os_type": "Linux", "image_name": "Ubuntu 22.04 server 64bit"},
                   addresses={"net": [{"addr": "192.168.0.10", "OS-EXT-IPS:type": "fixed"},
                                      {"addr": "110.1.1.1", "OS-EXT-IPS:type": "floating"}]},
                   **{"os-extended-volumes:volumes_attached": [{"id": "vol-1"}]}),
            server("srv-2", "db", flavor={"id": "s6.medium.2", "name": "s6.medium.2", "vcpus": "1", "ram": 2048})],
            "p-mx": []}
        self.world.volumes = {"p-sg": [
            {"id": "vol-1", "name": "web-sys", "size": 40, "volume_type": "GPSSD", "status": "in-use",
             "attachments": [{"server_id": "srv-1"}]},
            {"id": "vol-2", "name": "data", "size": 100, "volume_type": "SSD", "status": "available", "attachments": []}],
            "p-mx": []}
        self.world.extra["list_publicips"] = lambda region, project, request: SimpleNamespace(publicips=[
            {"id": "eip-1", "public_ip_address": "110.1.1.1", "type": "5_bgp", "bandwidth_size": 5,
             "bandwidth_share_type": "PER", "status": "ACTIVE"}] if project == "p-sg" else [])
        response = self.start(("ecs", "evs", "eip", "obs"))
        self.assertEqual(response.status_code, 202, response.text)
        with session_scope(self.factory) as session:
            PriceCatalog(session).store(quote(r, p, s, z, a) for (r, p, s, z), a in PRICES.items())
            self.ids = {r.provider_id: r.id for r in session.scalars(select(InventoryResource))}
        self.base = f"/api/clients/{self.cid}/accounts/{self.aid}/cost-compare"

    def compare(self, *items, region=HK, path=None, **extra):
        body = {"region": region, "items": [
            {"resource_id": str(self.ids[pid]), "origin_assumptions": {"bandwidth_mode": "bandwidth"}, **kw}
            for pid, kw in items], **extra}
        return self.call("POST", path or f"{self.base}/compare", json=body)

    def snapshot(self):
        with session_scope(self.factory) as session:
            return {r.provider_id: (r.raw_hash, dict(r.attributes or {}), r.region, r.updated_at)
                    for r in session.scalars(select(InventoryResource))}


class TestOriginAndComparison(CostCompareApiTestCase):
    def test_origin_from_real_inventory(self):
        body = self.call("GET", f"{self.base}/origin/{self.ids['srv-1']}").json()
        config = body["config"]
        self.assertEqual(body["resource"]["provider_id"], "srv-1")
        self.assertEqual((config["kind"], config["region"], config["flavor"], config["vcpus"], config["ram_gb"],
                          config["os_type"]), ("ecs", SG, "s6.small.1", 1, 1.0, "linux"))
        self.assertEqual([(d["volume_type"], d["size_gb"]) for d in config["disks"]], [("GPSSD", 40)])
        self.assertEqual([(i["ip_type"], i["bandwidth_mbps"], i["bandwidth_mode"]) for i in config["public_ips"]],
                         [("5_bgp", 5, None)])
        self.assertTrue(any("tráfico" in n for n in config["notes"]))  # se pide el dato, no se supone

    def test_same_configuration_in_another_region(self):
        before = self.snapshot()
        calls = len(self.world.calls)
        response = self.compare(("srv-1", {}))
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["items"][0]
        self.assertEqual((item["origin"]["total"], item["destination"]["total"]), ("37.00", "42.50"))
        self.assertEqual((item["difference"], item["difference_percent"], item["currency"]), ("5.50", "14.86", "USD"))
        self.assertEqual(item["destination_config"]["region"], HK)
        self.assertEqual(item["origin"]["updated_at"], NOW.isoformat())
        self.assertIn("BSS", item["origin"]["sources"][0])
        summary = response.json()["summary"]
        self.assertEqual((summary["billing_mode"], summary["compared"]), ("monthly", 1))
        self.assertIn("mensual", summary["period"])
        self.assertEqual(self.snapshot(), before)            # el inventario no cambia
        self.assertEqual(len(self.world.calls), calls)       # ni se llama a Huawei

    def test_destination_flavor_is_editable_without_touching_origin(self):
        item = self.compare(("srv-1", {"destination": {"flavor": "s6.medium.2", "vcpus": 1, "ram_gb": 2}})).json()["items"][0]
        self.assertEqual(item["origin_config"]["flavor"], "s6.small.1")
        self.assertEqual(item["destination_config"]["flavor"], "s6.medium.2")
        self.assertEqual((item["destination"]["total"], item["difference"], item["difference_percent"]),
                         ("48.50", "11.50", "31.08"))

    def test_missing_price_is_never_invented(self):
        item = self.compare(("srv-1", {}), region=MX).json()["items"][0]
        self.assertIsNone(item["destination"]["total"])
        self.assertFalse(item["comparable"])
        self.assertIsNone(item["difference"])
        self.assertTrue(all(l["monthly_amount"] is None and "no disponible" in l["reason"].lower()
                            for l in item["destination"]["lines"]))
        self.assertEqual(item["origin"]["total"], "37.00")

    def test_unknown_origin_data_blocks_only_that_price(self):
        item = self.compare(("srv-2", {}), region=HK).json()["items"][0]
        self.assertIsNone(item["origin"]["total"])
        self.assertIn("sistema operativo", item["origin"]["lines"][0]["reason"])
        fixed = self.call("POST", f"{self.base}/compare", json={"region": HK, "items": [
            {"resource_id": str(self.ids["srv-2"]), "origin_assumptions": {"os_type": "linux"}}]}).json()["items"][0]
        self.assertEqual(fixed["origin_config"]["os_type"], "linux")
        self.assertTrue(any("indicado por el usuario" in w for w in fixed["warnings"]))

    def test_multiple_resources_and_summary(self):
        body = self.compare(("srv-1", {}), ("vol-2", {}), ("srv-2", {})).json()
        self.assertEqual([i["resource"]["provider_id"] for i in body["items"]], ["srv-1", "vol-2", "srv-2"])
        self.assertEqual(body["items"][1]["difference_percent"], "0.00")
        summary = body["summary"]
        self.assertEqual((summary["items"], summary["compared"], summary["not_compared"]), (3, 2, 1))
        self.assertEqual(summary["by_currency"], [{"currency": "USD", "origin": "52.00", "destination": "57.50",
                                                   "difference": "5.50", "difference_percent": "10.58"}])

    def test_validation_of_destination(self):
        for destination in ({"flavor": "../etc"}, {"region": "http://evil"}, {"region": "zz-nowhere-9"},
                            {"disks": [{"volume_type": "GOLD", "size_gb": 10}]},
                            {"disks": [{"volume_type": "SSD", "size_gb": 0}]},
                            {"public_ips": [{"ip_type": "5_bgp", "bandwidth_mbps": 99999}]}):
            with self.subTest(destination=destination):
                self.assertEqual(self.compare(("srv-1", {"destination": destination})).status_code, 422)
        bad = self.call("POST", f"{self.base}/compare", json={"region": HK, "items": [
            {"resource_id": str(self.ids["srv-1"]), "origin_assumptions": {"flavor": "x"}}]})
        self.assertEqual(bad.status_code, 422)  # el origen real no se puede reescribir

    def test_unsupported_resource_type(self):
        response = self.compare(("logs", {}))
        self.assertEqual(response.status_code, 422)
        self.assertIn("no admite", response.json()["detail"] if isinstance(response.json()["detail"], str)
                      else str(response.json()["detail"]))


class TestIsolationAndSecurity(CostCompareApiTestCase):
    def test_other_client_cannot_see_or_use_resources(self):
        other_base = f"/api/clients/{self.other_cid}/accounts/{self.other_aid}/cost-compare"
        self.assertEqual(self.call("GET", f"{other_base}/origin/{self.ids['srv-1']}").status_code, 404)
        self.assertEqual(self.compare(("srv-1", {}), path=f"{other_base}/compare").status_code, 404)
        cross = f"/api/clients/{self.other_cid}/accounts/{self.aid}/cost-compare/options"
        self.assertEqual(self.call("GET", cross).status_code, 404)

    def test_roles(self):
        self.app.dependency_overrides[get_principal] = lambda: Principal(
            subject="v", kind="user", client_roles={str(self.cid): "viewer"})
        self.assertEqual(self.compare(("srv-1", {})).status_code, 200)
        self.assertEqual(self.compare(("srv-1", {}), path=f"{self.base}/prices/refresh").status_code, 403)
        self.assertEqual(self.call("POST", f"{self.base}/flavors/refresh", params={"region": HK}).status_code, 403)
        other = f"/api/clients/{self.other_cid}/accounts/{self.other_aid}/cost-compare/options"
        self.assertEqual(self.call("GET", other).status_code, 404)

    def test_no_secrets_in_any_response(self):
        texts = [self.call("GET", f"{self.base}/options", params={"region": HK}).text,
                 self.call("GET", f"{self.base}/origin/{self.ids['srv-1']}").text,
                 self.compare(("srv-1", {})).text,
                 self.compare(("srv-1", {}), path=f"{self.base}/compare/export?format=csv").content.decode("utf-8-sig")]
        for text in texts:
            self.assertNotIn(FAKE_AK, text)
            self.assertNotIn(FAKE_SK, text)

    def test_exports_reuse_existing_system(self):
        xlsx = self.compare(("srv-1", {}), path=f"{self.base}/compare/export?format=xlsx")
        self.assertEqual(xlsx.status_code, 200)
        self.assertIn("spreadsheetml", xlsx.headers["content-type"])
        self.assertIn("comparacion_regiones_", xlsx.headers["content-disposition"])
        csv_text = self.compare(("srv-1", {}), path=f"{self.base}/compare/export?format=csv").content.decode("utf-8-sig")
        self.assertIn("Diferencia %", csv_text.splitlines()[0])
        self.assertIn("14.86", csv_text)


class FakeBss:
    def __init__(self, fail_spec=None):
        self.requests = []
        self.fail_spec = fail_spec

    def list_rate_on_period_detail(self, request):
        product = request.body.product_infos[0]
        self.requests.append(product)
        if product.resource_spec == self.fail_spec:
            raise api_error(403, f"denied {FAKE_AK} {FAKE_SK}", "CBC.0150")
        return SimpleNamespace(currency="USD", official_website_rating_result=SimpleNamespace(
            official_website_amount=Decimal("7.25"), measure_id=1))


class TestOfficialSources(CostCompareApiTestCase):
    def test_refresh_prices_stores_official_quotes_with_source_and_date(self):
        fake = FakeBss(fail_spec="5_bgp")
        with mock.patch("costs.huawei_pricing.default_bss_builder", lambda clients: fake):
            response = self.compare(("srv-1", {}), region=MX, path=f"{self.base}/prices/refresh")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertNotIn(FAKE_AK, response.text)
        self.assertNotIn(FAKE_SK, response.text)
        self.assertTrue(any("5_bgp" in f["component"] for f in body["failures"]))
        mx = [p for p in fake.requests if p.region == MX]
        ecs = next(p for p in mx if p.resource_type == "hws.resource.type.vm")
        self.assertEqual((ecs.resource_spec, ecs.cloud_service_type, ecs.period_type, ecs.period_num),
                         ("s6.small.1.linux", "hws.service.type.ec2", 2, 1))
        disk = next(p for p in mx if p.resource_type == "hws.resource.type.volume")
        self.assertEqual((disk.resource_size, disk.size_measure_id), (40, 17))
        with session_scope(self.factory) as session:
            row = PriceCatalog(session).lookup(region=MX, product="ecs", spec="s6.small.1.linux",
                                               billing_mode="monthly")
            self.assertEqual((row.amount, row.currency, row.source), (Decimal("7.25"), "USD", "huawei_bss"))
            self.assertIsNotNone(row.fetched_at)
            event = session.scalars(select(AuditEvent).where(AuditEvent.action == "prices.refresh")).one()
            self.assertNotIn(FAKE_AK, str(event.details))
        item = self.compare(("srv-1", {}), region=MX).json()["items"][0]
        self.assertIsNone(item["destination"]["total"])  # la IP falló: sin total, sin inventar
        self.assertEqual(item["destination"]["lines"][0]["monthly_amount"], "7.25")

    def test_refresh_flavors_and_flavor_checks(self):
        flavors = [SimpleNamespace(id="s6.medium.2", vcpus="1", ram=2048,
                                   os_extra_specs=SimpleNamespace(condoperationstatus="normal",
                                                                  ecsperformancetype="normal", ecsgeneration="s6")),
                   SimpleNamespace(id="old.flavor", vcpus="1", ram=1024,
                                   os_extra_specs=SimpleNamespace(condoperationstatus="abandon"))]
        fake = SimpleNamespace(list_flavors=lambda request: SimpleNamespace(flavors=flavors))
        with mock.patch("costs.huawei_pricing.default_ecs_builder", lambda clients, region, project: fake):
            response = self.call("POST", f"{self.base}/flavors/refresh", params={"region": MX})
        self.assertEqual(response.json(), {"region": MX, "flavors": 1})
        options = self.call("GET", f"{self.base}/options", params={"region": MX}).json()
        self.assertEqual([f["flavor_id"] for f in options["flavors"]], ["s6.medium.2"])
        self.assertTrue(next(r for r in options["regions"] if r["id"] == MX)["has_project"])
        self.assertFalse(next(r for r in options["regions"] if r["id"] == HK)["has_project"])
        missing = self.compare(("srv-1", {}), region=MX).json()["items"][0]
        self.assertTrue(any("no aparece en la lista oficial" in w for w in missing["warnings"]))
        known = self.compare(("srv-1", {"destination": {"flavor": "s6.medium.2", "vcpus": 8}}), region=MX).json()["items"][0]
        self.assertEqual((known["destination_config"]["vcpus"], known["destination_config"]["ram_gb"]), (1, 2.0))
        self.assertTrue(any("depende del flavor" in w for w in known["warnings"]))

    def test_refresh_without_project_in_region(self):
        with mock.patch("costs.huawei_pricing.default_bss_builder", lambda clients: FakeBss()):
            body = self.compare(("srv-1", {}), region=HK, path=f"{self.base}/prices/refresh").json()
        self.assertTrue(all("Project ID" in f["reason"] for f in body["failures"] if f["region"] == HK))
        self.assertIn(HK, {f["region"] for f in body["failures"]})


# ---------------------------------------------------------------- unidades puras
class TestFormulas(unittest.TestCase):
    def test_difference(self):
        self.assertEqual(difference(Decimal("37"), Decimal("42.5")), (Decimal("5.5"), Decimal("14.86")))
        self.assertEqual(difference(Decimal("40"), Decimal("30")), (Decimal("-10"), Decimal("-25.00")))
        self.assertEqual(difference(Decimal("0"), Decimal("5")), (Decimal("5"), None))
        self.assertEqual(difference(None, Decimal("5")), (None, None))

    def priced(self, amount, currency="USD", label="x"):
        return PricedComponent(Component("ecs", "a", label=label),
                               monthly_amount=Decimal(amount) if amount else None, currency=currency)

    def test_totals_never_mix_currencies_or_skip_missing(self):
        self.assertEqual(SideCost.from_lines([self.priced("1"), self.priced("2")]).total, Decimal("3"))
        self.assertIsNone(SideCost.from_lines([self.priced("1"), self.priced("2", "CNY")]).total)
        side = SideCost.from_lines([self.priced("1"), self.priced(None, label="disco")])
        self.assertIsNone(side.total)
        self.assertIn("disco", side.reason)
        comparison = CostComparison(SideCost.from_lines([self.priced("1")]), SideCost.from_lines([self.priced("1", "CNY")]))
        self.assertFalse(comparison.comparable)
        self.assertIn("monedas", comparison.reason)


class TestResolverSources(unittest.TestCase):
    def setUp(self):
        from tests.db_helpers import sqlite_session_factory  # noqa: PLC0415
        self.session = sqlite_session_factory()()

    def tearDown(self):
        self.session.close()

    def test_on_demand_hourly_price_times_hours(self):
        catalog = PriceCatalog(self.session)
        catalog.store([quote(SG, "ecs", "s6.small.1.linux", 0, "0.05", mode="on_demand")])
        line = PriceResolver(catalog, billing_mode="on_demand", hours_per_month=730).price(
            Component("ecs", "s6.small.1.linux"), SG)
        self.assertEqual((line.monthly_amount, line.unit_period), (Decimal("36.50"), "hour"))
        self.assertIn("× 730 h", line.basis)
        monthly = PriceResolver(catalog, billing_mode="monthly").price(Component("ecs", "s6.small.1.linux"), SG)
        self.assertIsNone(monthly.monthly_amount)  # no mezcla modos de cobro

    def test_size_must_match_exactly(self):
        catalog = PriceCatalog(self.session)
        catalog.store([quote(SG, "evs", "SSD", 100, "15")])
        self.assertIsNone(PriceResolver(catalog).price(Component("evs", "SSD", size=200), SG).monthly_amount)

    def test_official_price_table_fallback(self):
        from costs.pricing import PriceTableProvider  # noqa: PLC0415
        handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        handle.write("service,resource_type,region,match_attribute,match_value,quantity_attribute,unit_price,currency,source\n"
                     "evs,evs.volume,*,volume_type,SSD,size_gb,0.10,USD,Calculadora 2026-10\n")
        handle.close()
        try:
            table = PriceTableProvider.from_csv(__import__("pathlib").Path(handle.name))
            line = PriceResolver(PriceCatalog(self.session), table=table).price(Component("evs", "SSD", size=100), SG)
        finally:
            os.unlink(handle.name)
        self.assertEqual((line.monthly_amount, line.currency), (Decimal("10.00"), "USD"))
        self.assertIn("Calculadora 2026-10", line.basis)

    def test_destination_payload_rejects_unknown_region(self):
        origin = ResourceConfiguration(kind="ecs", region=SG, flavor="s6.small.1", os_type="linux")
        with self.assertRaises(ConfigurationError):
            destination_from_payload(origin, {}, region="xx-none-1", known_regions=[SG, HK])
        self.assertEqual(destination_from_payload(origin, {"flavor": "S6.Large.2"}, region=HK,
                                                  known_regions=[SG, HK]).flavor, "s6.large.2")

    def test_flavor_catalog_replaces_region_list(self):
        catalog = PriceCatalog(self.session)
        catalog.replace_flavors(SG, [Flavor("a", 1, 1024), Flavor("b", 2, 4096)], fetched_at=NOW)
        catalog.replace_flavors(SG, [Flavor("b", 2, 4096)], fetched_at=NOW)
        self.assertEqual([f.flavor_id for f in catalog.flavors(SG)], ["b"])
        self.assertEqual(self.session.query(PriceCatalogEntry).count(), 0)


if __name__ == "__main__":
    unittest.main()
