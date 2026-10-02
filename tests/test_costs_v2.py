# coding: utf-8
"""Costos por recurso/servicio/región/proyecto: tabla de precios, facturación BSS (mock),
comparación de meses, API y exportación. Sin llamadas reales ni precios inventados."""

import csv
import io
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from huaweicloudsdkbss.v2 import ResFeeRecordV2

from tests.helpers import FAKE_AK, FAKE_SK, api_error
from tests.scan_helpers import server
from tests.test_inventory_api import InventoryApiTestCase

from core.authz import Principal
from costs import billing, pricing
from costs.report import CostLine, CostReport
from db.models import InventoryResource
from routers.security import get_principal

HEADER = "service,resource_type,region,match_attribute,match_value,quantity_attribute,unit_price,currency,source\n"


def price_table(rows: str) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    handle.write(HEADER + rows)
    handle.close()
    return Path(handle.name)


def resource(**kw):
    base = dict(service="ecs", resource_type="ecs.server", region="ap-southeast-3", provider_id="srv-1",
                attributes={}, name="n", project_id=None)
    base.update(kw)
    return InventoryResource(**base)


class TestPriceTable(unittest.TestCase):
    def test_validation_errors(self):
        for rows, header in (("", "service,unit_price\n"), ("ecs,ecs.server,*,,,,abc,USD,x\n", HEADER),
                             ("ecs,ecs.server,*,,,,-1,USD,x\n", HEADER), ("ecs,ecs.server,*,,,,1,,x\n", HEADER)):
            path = price_table(rows) if header == HEADER else Path(tempfile.mktemp(suffix=".csv"))
            if header != HEADER:
                path.write_text(header, encoding="utf-8")
            with self.subTest(rows=rows):
                with self.assertRaises(pricing.PriceTableError):
                    pricing.PriceTableProvider.from_csv(path)
        with self.assertRaises(pricing.PriceTableError):
            pricing.PriceTableProvider.from_csv(Path("no-existe.csv"))

    def test_specificity_and_quantity(self):
        provider = pricing.PriceTableProvider.from_csv(price_table(
            "ecs,ecs.server,*,,,,10,USD,generic\n"
            "ecs,ecs.server,ap-southeast-3,flavor_name,s6.large.2,,40.50,USD,contract\n"
            "evs,evs.volume,*,volume_type,GPSSD,size_gb,0.1,USD,calc\n"))
        self.assertEqual(provider.price(resource(attributes={"flavor_name": "s6.large.2"}))[0], Decimal("40.50"))
        self.assertEqual(provider.price(resource(region="la-north-2", attributes={"flavor_name": "s6.large.2"}))[0],
                         Decimal("10"))
        volume = resource(service="evs", resource_type="evs.volume", attributes={"volume_type": "GPSSD", "size_gb": 40})
        amount, currency, basis = provider.price(volume)
        self.assertEqual((amount, currency), (Decimal("4.0"), "USD"))
        self.assertIn("size_gb", basis)
        self.assertIsNone(provider.price(resource(service="obs", resource_type="obs.bucket")))
        self.assertIsNone(provider.price(resource(service="evs", resource_type="evs.volume",
                                                  attributes={"volume_type": "GPSSD", "size_gb": "x"})))

    def test_no_pricing_never_invents_amounts(self):
        report = pricing.estimate([resource()], pricing.NoPricingProvider(), {})
        self.assertEqual((report.lines, len(report.unpriced)), ([], 1))
        self.assertIn("INVENTORY_PRICE_TABLE", report.message)
        self.assertEqual(report.totals(), {})

    def test_currencies_are_never_mixed(self):
        report = CostReport(kind="estimate", source="t", lines=[
            CostLine("a", "ecs", "r1", Decimal("10"), "USD"), CostLine("b", "ecs", "r1", Decimal("5"), "EUR"),
            CostLine("c", "evs", "r2", Decimal("2.555"), "USD")])
        totals = report.totals()
        self.assertEqual((totals["USD"]["total"], totals["EUR"]["total"]), ("12.56", "5.00"))
        self.assertEqual(totals["USD"]["by_service"], {"ecs": "10.00", "evs": "2.56"})


def bss_page(records, total):
    return SimpleNamespace(fee_records=records, total_count=total, currency="USD")


class TestBilling(unittest.TestCase):
    def records(self, n):
        return [ResFeeRecordV2(resource_id=f"r-{i % 3}", region="ap-southeast-3", cloud_service_type="hws.service.type.ecs",
                               amount=1.25, official_amount=2.0, resource_name=f"name-{i}") for i in range(n)]

    def test_fetch_paginates_with_sdk_models(self):
        all_records = self.records(150)
        requests = []

        def handler(request):
            requests.append(request)
            return bss_page(all_records[request.offset: request.offset + request.limit], len(all_records))
        client = SimpleNamespace(list_customerself_resource_records=handler)
        bills = billing.fetch_resource_bills(None, "2026-09", client_builder=lambda clients: client)
        self.assertEqual((len(bills["records"]), bills["currency"]), (150, "USD"))
        self.assertEqual([r.offset for r in requests], [0, 100])
        self.assertEqual({(r.cycle, r.statistic_type, r.method) for r in requests}, {("2026-09", 1, "oneself")})

    def test_report_groups_and_matches_inventory(self):
        bills = {"currency": "USD", "records": [
            {"resource_id": "srv-1", "amount": "1.10", "official_amount": "2", "region": "ap-southeast-3"},
            {"resource_id": "srv-1", "amount": "0.90", "official_amount": "1", "region": "ap-southeast-3"},
            {"resource_id": "unknown-bucket", "amount": "3", "cloud_service_type": "hws.service.type.obs",
             "region": "la-north-2"}]}
        report = billing.build_actual_report("2026-09", bills, [resource()], {})
        [line] = report.lines
        self.assertEqual((line.amount, line.official_amount, line.basis), (Decimal("2.00"), Decimal("3"), "facturado"))
        [unmatched] = report.unmatched
        self.assertEqual((unmatched.provider_id, unmatched.service), ("unknown-bucket", "hws.service.type.obs"))
        self.assertEqual(report.totals()["USD"]["total"], "5.00")

    def test_compare_months_reuses_existing_analysis(self):
        a = CostReport(kind="actual", source="s", period="2026-08",
                       lines=[CostLine("x", "ecs", "r", Decimal("10"), "USD")])
        b = CostReport(kind="actual", source="s", period="2026-09",
                       lines=[CostLine("x", "ecs", "r", Decimal("15"), "USD")])
        analysis = billing.compare_months(a, b)
        self.assertEqual(analysis["monedas"][0]["diferencia"], 5.0)


class TestCostsApi(InventoryApiTestCase):
    def costs(self, kind, **params):
        return self.call("GET", f"{self.base}/costs/{kind}", params=params)

    def test_estimate_without_source(self):
        with mock.patch.dict(os.environ, {"INVENTORY_PRICE_TABLE": ""}):
            body = self.costs("estimate").json()
        self.assertEqual((body["kind"], body["source"], body["totals"]), ("estimate", "ninguna", {}))
        self.assertEqual(body["coverage"]["unpriced"], 5)
        self.assertIn("No hay fuente de precios", body["message"])

    def test_estimate_with_price_table_and_exports(self):
        path = price_table("ecs,ecs.server,*,,,,20,USD,contrato-demo\n")
        with mock.patch.dict(os.environ, {"INVENTORY_PRICE_TABLE": str(path)}):
            body = self.costs("estimate").json()
            self.assertEqual(body["totals"]["USD"]["total"], "80.00")  # 4 ECS × 20
            self.assertEqual(body["totals"]["USD"]["by_project"], {"p-sg": "60.00", "p-mx": "20.00"})
            self.assertEqual(body["coverage"], {"priced": 4, "unpriced": 1, "unmatched_bills": 0})
            rows = list(csv.DictReader(io.StringIO(self.costs("estimate", format="csv").content.decode("utf-8-sig"))))
            self.assertEqual(len(rows), 4)
            self.assertEqual(self.costs("estimate", format="xlsx").status_code, 200)
        with mock.patch.dict(os.environ, {"INVENTORY_PRICE_TABLE": "missing.csv"}):
            self.assertEqual(self.costs("estimate").status_code, 422)

    def test_actual_and_compare_with_mocked_bss(self):
        months = {"2026-08": "1.00", "2026-09": "4.00"}

        def fake_fetch(clients, month):
            self.assertEqual(clients.secrets, (FAKE_AK, FAKE_SK))  # credenciales de la BD, no del navegador
            return {"currency": "USD", "records": [{"resource_id": "srv-1", "amount": months[month],
                                                    "official_amount": "9", "region": "ap-southeast-3"}]}
        with mock.patch("costs.billing.fetch_resource_bills", side_effect=fake_fetch):
            body = self.costs("actual", month="2026-09").json()
            self.assertEqual((body["kind"], body["period"], body["totals"]["USD"]["total"]), ("actual", "2026-09", "4.00"))
            compare = self.costs("compare", month_a="2026-08", month_b="2026-09").json()
        self.assertEqual(compare["analysis"]["monedas"][0]["diferencia"], 3.0)
        self.assertEqual(compare["by_project"], [{"currency": "USD", "project": "p-sg", "mes_a": "1.00", "mes_b": "4.00"}])

    def test_bss_errors_are_safe(self):
        with mock.patch("costs.billing.fetch_resource_bills",
                        side_effect=api_error(403, f"Policy doesn't allow bss:bill:view {FAKE_SK}", "CBC.0150")):
            response = self.costs("actual", month="2026-09")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"]["categoria"], "permission")

    def test_validation_permissions_and_isolation(self):
        self.assertEqual(self.costs("actual", month="2026-13").status_code, 422)
        self.assertEqual(self.costs("compare", month_a="2026-09", month_b="2026-09").status_code, 422)
        self.assertEqual(self.call("GET", f"/api/clients/{self.other_cid}/accounts/{self.aid}/costs/estimate").status_code, 404)
        self.app.dependency_overrides[get_principal] = lambda: Principal(
            subject="v", kind="user", client_roles={str(self.cid): "viewer"})
        self.assertEqual(self.costs("estimate").status_code, 200)
