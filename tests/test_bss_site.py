# coding: utf-8
"""BSS por sitio: las cuentas International usan BSS International (``bss-intl``), nunca
el BSS de China (``cn-north-1``), en las tres rutas de BSS. Todo simulado: sin red."""

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import huaweicloudsdkbss.v2 as bss_china
import huaweicloudsdkbssintl.v2 as bss_intl
from fastapi.testclient import TestClient
from huaweicloudsdkbss.v2.region.bss_region import BssRegion
from huaweicloudsdkbssintl.v2.region.bssintl_region import BssintlRegion

from tests.helpers import FAKE_AK, FAKE_PROJECT, FAKE_SK, api_error

from app import app
from core import bss
from core.errors import SITE_MISMATCH, SITE_MISMATCH_MESSAGE, classify_exception, public_error
from costs import billing, huawei_pricing
from costs.configuration import Component
from services import huawei_costs

CHINA_ENDPOINT = "https://bss.myhuaweicloud.com"
INTL_ENDPOINT = "https://bss-intl.myhuaweicloud.com"
SITE_MISMATCH_TEXT = "Access denied. The customer does not belong to the website you are now at."


def site_mismatch_error():
    return api_error(403, f"{SITE_MISMATCH_TEXT} {FAKE_AK} {FAKE_SK}", "CBC.0156", "req-cbc-0156")


class FakeFactory:
    """``ClientFactory`` simulado: registra ``create_global`` y devuelve un cliente falso."""

    def __init__(self, client):
        self.client = client
        self.calls = []
        self.secrets = (FAKE_AK, FAKE_SK)

    def create_global(self, client_cls, region_cls, endpoint_prefix, region_id, domain_id=None):
        self.calls.append((client_cls, region_cls, endpoint_prefix, region_id))
        return self.client


class RecordingBss:
    """Cliente BSS simulado para las cuatro operaciones usadas por la plataforma."""

    def __init__(self, error=None):
        self.requests = []
        self.error = error

    def _record(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error

    def list_customerself_resource_records(self, request):
        self._record(request)
        return SimpleNamespace(fee_records=[], total_count=0, currency="USD")

    def list_on_demand_resource_ratings(self, request):
        self._record(request)
        return SimpleNamespace(official_website_amount=Decimal("0.05"), measure_id=1, currency="USD")

    def list_rate_on_period_detail(self, request):
        self._record(request)
        return SimpleNamespace(currency="USD", official_website_rating_result=SimpleNamespace(
            official_website_amount=Decimal("7.25"), measure_id=1))

    def list_costs(self, request):
        self._record(request)
        return bss_intl.ListCostsResponse(cost_data=[], total_count=0, currency="USD")


class FakeBuilder:
    """``BssintlClient.new_builder()`` simulado: captura credenciales y región sin red."""

    def __init__(self, client):
        self.client = client
        self.region = None
        self.credentials = None

    def with_credentials(self, credentials):
        self.credentials = credentials
        return self

    def with_region(self, region):
        self.region = region
        return self

    def build(self):
        return self.client


class TestSiteRegistry(unittest.TestCase):
    def test_default_site_is_international(self):
        site = bss.get_bss_site()
        self.assertEqual((site.name, site.package, site.region_id), ("international", "huaweicloudsdkbssintl",
                                                                     "ap-southeast-1"))
        self.assertEqual(site.endpoint, INTL_ENDPOINT)

    def test_international_region_resolves_to_bss_intl_endpoint_in_the_sdk(self):
        site = bss.get_bss_site("international")
        self.assertEqual(BssintlRegion.value_of(site.region_id).endpoints[0], INTL_ENDPOINT)

    def test_china_and_europe_stay_available_for_later(self):
        self.assertEqual(BssRegion.value_of(bss.get_bss_site("china").region_id).endpoints[0], CHINA_ENDPOINT)
        self.assertEqual(BssintlRegion.value_of(bss.get_bss_site("europe").region_id).endpoints[0],
                         "https://bss.myhuaweicloud.eu")

    def test_unknown_site_is_rejected(self):
        with self.assertRaises(ValueError):
            bss.get_bss_site("mars")

    def test_default_models_come_from_the_international_sdk(self):
        self.assertIs(bss.bss_models(), bss_intl)
        self.assertIs(bss.bss_models("china"), bss_china)


class TestInternationalClient(unittest.TestCase):
    def test_account_client_uses_bss_international_and_not_cn_north_1(self):
        factory = FakeFactory(RecordingBss())
        bss.create_bss_client(factory)
        (client_cls, region_cls, prefix, region_id), = factory.calls
        self.assertIs(client_cls, bss_intl.BssintlClient)
        self.assertIs(region_cls, BssintlRegion)
        self.assertEqual((prefix, region_id), ("bss", "ap-southeast-1"))
        self.assertNotEqual(region_id, "cn-north-1")
        self.assertNotEqual(region_cls.value_of(region_id).endpoints[0], CHINA_ENDPOINT)

    def test_in_memory_keys_client_uses_bss_international(self):
        builder = FakeBuilder(RecordingBss())
        with mock.patch.object(bss_intl.BssintlClient, "new_builder", return_value=builder), \
                mock.patch.object(bss_china.BssClient, "new_builder",
                                  side_effect=AssertionError("no debe usarse BSS China")):
            bss.create_bss_client_with_keys(FAKE_AK, FAKE_SK)
        self.assertEqual(builder.region.id, "ap-southeast-1")
        self.assertEqual(builder.region.endpoints[0], INTL_ENDPOINT)
        self.assertNotIn(FAKE_SK, repr(builder.region))


class TestThreeBssPaths(unittest.TestCase):
    """Las tres funciones de BSS piden el cliente International y sus modelos."""

    def assert_international(self, factory, fake):
        (client_cls, _region_cls, _prefix, region_id), = factory.calls
        self.assertIs(client_cls, bss_intl.BssintlClient)
        self.assertEqual(region_id, "ap-southeast-1")
        self.assertTrue(fake.requests)
        for request in fake.requests:
            self.assertTrue(type(request).__module__.startswith("huaweicloudsdkbssintl."), type(request))

    def test_billing_resource_records(self):
        fake = RecordingBss()
        factory = FakeFactory(fake)
        result = billing.fetch_resource_bills(factory, "2026-09")
        self.assertEqual(result["records"], [])
        self.assert_international(factory, fake)

    def test_pricing_quotes_on_demand_and_monthly(self):
        for mode in ("on_demand", "monthly"):
            with self.subTest(mode=mode):
                fake = RecordingBss()
                factory = FakeFactory(fake)
                quotes, failures = huawei_pricing.fetch_quotes(
                    factory, project_id=FAKE_PROJECT, region="la-north-2", billing_mode=mode,
                    components=[Component(product="ecs", spec="s6.small.1.linux", label="ECS")])
                self.assertEqual((len(quotes), failures), (1, []))
                self.assert_international(factory, fake)

    def test_legacy_cost_center_list_costs(self):
        fake = RecordingBss()
        builder = FakeBuilder(fake)
        with mock.patch.object(bss_intl.BssintlClient, "new_builder", return_value=builder), \
                mock.patch.object(bss_china.BssClient, "new_builder",
                                  side_effect=AssertionError("no debe usarse BSS China")):
            result = huawei_costs.consultar_mes(FAKE_AK, FAKE_SK, "2026-09", "ORIGINAL_COST", "NET_AMOUNT")
        self.assertEqual(result["records"], [])
        self.assertEqual(builder.region.endpoints[0], INTL_ENDPOINT)
        self.assertTrue(type(fake.requests[0]).__module__.startswith("huaweicloudsdkbssintl."))


class TestSiteMismatchClassification(unittest.TestCase):
    def test_cbc_0156_is_site_mismatch_not_permission(self):
        error = classify_exception(site_mismatch_error(), service="bss", secrets=(FAKE_AK, FAKE_SK))
        self.assertEqual((error.kind, error.severity, error.http_status, error.error_code),
                         (SITE_MISMATCH, "error", 403, "CBC.0156"))
        self.assertEqual(error.request_id, "req-cbc-0156")
        self.assertEqual(error.safe_explanation, SITE_MISMATCH_MESSAGE)
        self.assertIsNone(error.iam_action)
        public = public_error(error.to_legacy())
        for text in (error.message, str(public)):
            self.assertNotIn(FAKE_AK, text)
            self.assertNotIn(FAKE_SK, text)

    def test_other_bss_403_codes_are_still_permission(self):
        error = classify_exception(api_error(403, "Access denied.", "CBC.0151"), service="bss")
        self.assertEqual(error.kind, "permission")

    def test_pricing_failure_explains_site_mismatch_without_secrets(self):
        factory = FakeFactory(RecordingBss(error=site_mismatch_error()))
        quotes, failures = huawei_pricing.fetch_quotes(
            factory, project_id=FAKE_PROJECT, region="la-north-2", billing_mode="monthly",
            components=[Component(product="ecs", spec="s6.small.1.linux", label="ECS")])
        self.assertEqual(quotes, [])
        self.assertEqual(failures[0]["reason"], SITE_MISMATCH_MESSAGE)
        self.assertNotIn(FAKE_AK, str(failures))
        self.assertNotIn(FAKE_SK, str(failures))


class TestLegacyCostsEndpoint(unittest.TestCase):
    def test_cbc_0156_returns_safe_site_message(self):
        builder = FakeBuilder(RecordingBss(error=site_mismatch_error()))
        with mock.patch.object(bss_intl.BssintlClient, "new_builder", return_value=builder):
            response = TestClient(app).post("/api/costs/compare", json={
                "ak": FAKE_AK, "sk": FAKE_SK, "month_a": "2026-08", "month_b": "2026-09"})
        body = response.json()
        self.assertTrue(body["error"])
        self.assertEqual((body["http_status"], body["error_code"]), (403, "CBC.0156"))
        self.assertEqual(body["message"], SITE_MISMATCH_MESSAGE)
        self.assertNotIn(FAKE_AK, response.text)
        self.assertNotIn(FAKE_SK, response.text)


if __name__ == "__main__":
    unittest.main()
