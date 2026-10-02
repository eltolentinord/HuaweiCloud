# coding: utf-8
"""Clasificación de errores y ausencia de secretos (sin red)."""

import unittest
from types import SimpleNamespace

from tests.helpers import FAKE_AK, FAKE_PROJECT, FAKE_SK, REGION, FakeClient, api_error, context

from huaweicloudsdkcore.exceptions import exceptions as sdk_exceptions

from collectors.ecs import EcsCollector
from core.credentials import HuaweiCredentials, MissingCredentialsError
from core.engine import run_collector
from core.errors import (
    AUTH_PREFIX,
    PERMISSION_WARNING,
    PaginationError,
    classify_exception,
    mask_project_id,
    mask_project_id_ascii,
    public_error,
    safe_message,
)


def failing(exc):
    def handler(request):
        raise exc
    return FakeClient(list_servers_details=handler)


class TestClassification(unittest.TestCase):
    def test_401_is_authentication_error(self):
        err = classify_exception(api_error(401, "Incorrect IAM authentication information", "APIGW.0301"),
                                 service="ecs", region=REGION, project_id=FAKE_PROJECT)
        self.assertEqual((err.kind, err.severity, err.http_status), ("authentication", "error", 401))
        self.assertEqual((err.error_code, err.request_id), ("APIGW.0301", "req-test"))
        self.assertEqual(err.project_id_masked, mask_project_id_ascii(FAKE_PROJECT))  # logs en ASCII
        self.assertEqual(err.region, REGION)

    def test_403_is_permission_warning(self):
        err = classify_exception(api_error(403, "Forbidden"), service="hss")
        legacy = err.to_legacy()
        self.assertEqual((err.kind, legacy["tipo"]), ("permission", "aviso"))
        self.assertEqual(legacy["mensaje_seguro"], PERMISSION_WARNING)
        self.assertEqual(legacy["http_status"], "403")

    def test_access_denied_text_is_warning(self):
        err = classify_exception(api_error(400, "AccessDenied: no permission"), service="obs")
        self.assertEqual(err.severity, "aviso")

    def test_other_api_errors(self):
        err = classify_exception(api_error(500, "Internal"), service="rds")
        self.assertEqual((err.kind, err.severity), ("api", "error"))

    def test_network_error(self):
        err = classify_exception(sdk_exceptions.ConnectionException("timeout to host"), service="ecs")
        self.assertEqual(err.kind, "network")

    def test_pagination_error(self):
        self.assertEqual(classify_exception(PaginationError("loop"), service="ecs").kind, "pagination")

    def test_internal_error_hides_exception_text(self):
        err = classify_exception(ValueError(f"boom {FAKE_SK}"), service="ecs", secrets=(FAKE_AK, FAKE_SK))
        self.assertEqual(err.kind, "internal")
        self.assertNotIn(FAKE_SK, err.message)
        self.assertIn("ValueError", err.message)


class TestSecretsNeverLeak(unittest.TestCase):
    def test_secrets_are_redacted_from_api_messages(self):
        err = classify_exception(api_error(400, f"bad signature for {FAKE_AK}/{FAKE_SK}"),
                                 service="ecs", secrets=(FAKE_AK, FAKE_SK))
        self.assertNotIn(FAKE_AK, str(err.to_legacy()))
        self.assertNotIn(FAKE_SK, str(err.to_legacy()))
        self.assertIn("[REDACTED]", err.message)

    def test_engine_redacts_using_factory_secrets(self):
        run = run_collector(EcsCollector(), context(failing(api_error(401, f"sk={FAKE_SK}", "APIGW.0301"))))
        self.assertFalse(run.ok)
        self.assertNotIn(FAKE_SK, str(run.error))
        self.assertIsNotNone(run.error.duration_ms)

    def test_credentials_repr_hides_secrets(self):
        creds = HuaweiCredentials(ak=FAKE_AK, sk=FAKE_SK)
        self.assertNotIn(FAKE_SK, repr(creds))
        self.assertNotIn(FAKE_AK, repr(creds))

    def test_blank_credentials_rejected(self):
        with self.assertRaises(MissingCredentialsError):
            HuaweiCredentials(ak="  ", sk="x")


class TestMessages(unittest.TestCase):
    def test_safe_message_strips_markup_and_truncates(self):
        msg = safe_message("<Error><Code>X</Code></Error>   " + "a" * 1000)
        self.assertNotIn("<", msg)
        self.assertLessEqual(len(msg), 500)

    def test_public_error_prefixes_401(self):
        out = public_error({"servicio": "ECS", "http_status": "401", "mensaje": "bad"})
        self.assertTrue(out["mensaje"].startswith(AUTH_PREFIX))
        self.assertEqual(set(out), {"tipo", "http_status", "request_id", "error_code", "mensaje", "servicio"})

    def test_mask_project_id(self):
        self.assertEqual(mask_project_id(FAKE_PROJECT), "012345…cdef")
        self.assertEqual(mask_project_id("abc"), "****")


class TestEngineIsolation(unittest.TestCase):
    def test_failure_returns_error_without_raising(self):
        run = run_collector(EcsCollector(), context(failing(api_error(403, "Forbidden"))))
        self.assertEqual(run.resources, [])
        self.assertTrue(run.error.is_warning)

    def test_success_has_duration(self):
        client = FakeClient(list_servers_details=lambda r: SimpleNamespace(servers=[], count=0))
        run = run_collector(EcsCollector(), context(client))
        self.assertTrue(run.ok)
        self.assertGreaterEqual(run.duration_ms, 0)


if __name__ == "__main__":
    unittest.main()
