# coding: utf-8
"""Descubrimiento de proyectos IAM con mocks (sin llamadas reales)."""

import unittest
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import select

from tests.db_helpers import SqliteTestCase
from tests.helpers import FAKE_AK, FAKE_SK, FakeClient, api_error

from huaweicloudsdkiam.v3 import AuthProjectResult, KeystoneListAuthProjectsRequest

from core.discovery import DiscoveredProject, IamProjectSource, parse_projects, resolve_regions
from db.models import Project, Region
from tenancy import accounts, clients, projects
from tenancy.catalog_sync import sync_catalog
from tenancy.projects import DiscoveryFailedError, sync_projects

DOMAIN = "domain-0001"
SG = DiscoveredProject(id="p-sg", name="ap-southeast-3", enabled=True, parent_id=DOMAIN, domain_id=DOMAIN)
SG_SUB = DiscoveredProject(id="p-sg-sub", name="ap-southeast-3_finanzas", enabled=True, parent_id="p-sg",
                           domain_id=DOMAIN)
MX = DiscoveredProject(id="p-mx", name="la-north-2", enabled=True, parent_id=DOMAIN, domain_id=DOMAIN)
EU = DiscoveredProject(id="p-eu", name="eu-west-101", enabled=True, parent_id=DOMAIN, domain_id=DOMAIN)
MOS = DiscoveredProject(id="p-mos", name="MOS", enabled=True, parent_id=DOMAIN, domain_id=DOMAIN)
KNOWN = {"ap-southeast-3", "la-north-2", "eu-west-101"}


class TestPureDiscovery(unittest.TestCase):
    def test_parse_projects_dedupes_and_skips_domains(self):
        items = [
            {"id": "p1", "name": "ap-southeast-3", "enabled": True, "domain_id": DOMAIN},
            {"id": "p1", "name": "ap-southeast-3", "enabled": True},
            {"id": "d1", "name": "domain", "is_domain": True},
            {"id": None, "name": "sin-id"},
        ]
        self.assertEqual([p.id for p in parse_projects(items)], ["p1"])

    def test_resolve_regions(self):
        resolved, skipped = resolve_regions([SG, SG_SUB, MX, MOS], KNOWN)
        self.assertEqual({r.project.id: r.region_id for r in resolved},
                         {"p-sg": "ap-southeast-3", "p-sg-sub": "ap-southeast-3", "p-mx": "la-north-2"})
        self.assertEqual([p.name for p in skipped], ["MOS"])

    def test_subproject_without_known_parent_is_not_guessed(self):
        orphan = DiscoveredProject(id="x", name="ap-southeast-3_orphan", parent_id="unknown")
        resolved, skipped = resolve_regions([orphan], KNOWN)
        self.assertEqual((resolved, [p.id for p in skipped]), ([], ["x"]))

    def test_iam_source_uses_global_client_and_real_sdk_models(self):
        captured = {}
        fake = FakeClient(keystone_list_auth_projects=lambda r: SimpleNamespace(projects=[
            AuthProjectResult(id="p-sg", name="ap-southeast-3", enabled=True, domain_id=DOMAIN, parent_id=DOMAIN),
            AuthProjectResult(id="p-mos", name="MOS", enabled=True, domain_id=DOMAIN, parent_id=DOMAIN),
        ]))

        class Factory:
            def create_global(self, client_cls, region_cls, prefix, region_id, domain_id=None):
                captured.update(client=client_cls.__name__, prefix=prefix, region=region_id, domain=domain_id)
                return fake

        found = IamProjectSource(Factory(), region_id="la-north-2", domain_id=DOMAIN).list_projects()
        self.assertEqual([p.id for p in found], ["p-sg", "p-mos"])
        self.assertEqual(captured, {"client": "IamClient", "prefix": "iam", "region": "la-north-2",
                                    "domain": DOMAIN})
        self.assertIsInstance(fake.requests["keystone_list_auth_projects"][0], KeystoneListAuthProjectsRequest)


class StaticSource:
    def __init__(self, items=None, exc=None):
        self.items, self.exc = items or [], exc

    def list_projects(self):
        if self.exc:
            raise self.exc
        return self.items


class TestSyncAndDiscover(SqliteTestCase):
    def setUp(self):
        super().setUp()
        sync_catalog(self.session)
        self.client = clients.create_client(self.session, name="Acme")
        self.account = accounts.create_account(self.session, self.keyring, client_id=self.client.id,
                                               name="prod", ak=FAKE_AK, sk=FAKE_SK)

    def discover(self, source):
        return projects.discover_projects(self.session, self.keyring, self.client.id, self.account.id,
                                          source_factory=lambda account, clients_: source)

    def rows(self):
        return {p.huawei_project_id: p for p in self.session.scalars(select(Project))}

    def test_creates_projects_with_regions(self):
        result = self.discover(StaticSource([SG, SG_SUB, MX, MOS]))
        self.assertEqual(sorted(result.created), ["p-mx", "p-sg", "p-sg-sub"])
        self.assertEqual(result.skipped, [{"id": "p-mos", "name": "MOS"}])
        rows = self.rows()
        self.assertEqual(rows["p-sg-sub"].region_id, "ap-southeast-3")
        self.assertEqual(rows["p-sg-sub"].parent_huawei_project_id, "p-sg")
        self.assertEqual(self.account.status, "active")
        self.assertIsNotNone(self.account.last_validated_at)
        self.assertEqual(self.account.huawei_domain_id, DOMAIN)

    def test_rediscovery_updates_without_duplicates(self):
        self.discover(StaticSource([SG, MX]))
        renamed = DiscoveredProject(id="p-mx", name="la-north-2", enabled=False, parent_id=DOMAIN)
        result = self.discover(StaticSource([SG, renamed]))
        self.assertEqual((result.created, result.updated, result.unchanged), ([], ["p-mx"], ["p-sg"]))
        self.assertEqual(len(self.rows()), 2)
        self.assertFalse(self.rows()["p-mx"].huawei_enabled)
        self.assertTrue(self.rows()["p-mx"].is_enabled)  # la decisión local no se pisa

    def test_missing_projects_reported_not_deleted(self):
        self.discover(StaticSource([SG, MX]))
        result = self.discover(StaticSource([SG]))
        self.assertEqual(result.missing, ["p-mx"])
        self.assertIn("p-mx", self.rows())

    def test_sdk_known_region_created_as_unsupported(self):
        self.discover(StaticSource([EU]))
        region = self.session.get(Region, "eu-west-101")
        self.assertFalse(region.is_supported)
        self.assertEqual(self.rows()["p-eu"].region_id, "eu-west-101")

    def test_duplicate_items_in_response_do_not_duplicate_rows(self):
        sync_projects(self.session, self.account, [SG, SG])
        self.assertEqual(len(self.rows()), 1)

    def test_authentication_failure_marks_invalid_and_redacts(self):
        with self.assertRaises(DiscoveryFailedError) as ctx:
            self.discover(StaticSource(exc=api_error(401, f"bad ak {FAKE_AK} sk {FAKE_SK}", "APIGW.0301")))
        self.assertEqual(self.account.status, "invalid")
        self.assertNotIn(FAKE_SK, self.account.last_validation_error)
        self.assertNotIn(FAKE_AK, self.account.last_validation_error)
        self.assertNotIn(FAKE_SK, str(ctx.exception))
        self.assertEqual(self.rows(), {})

    def test_wrapped_domain_id_failure_marks_invalid(self):
        from huaweicloudsdkcore.exceptions import exceptions as sdk_exceptions
        wrapped = sdk_exceptions.SdkException(
            "Failed to get domain id, ClientRequestException - {status_code:401,request_id:r,"
            "error_code:APIGW.0301,error_msg:Incorrect IAM authentication information}")
        with self.assertRaises(DiscoveryFailedError) as ctx:
            self.discover(StaticSource(exc=wrapped))
        self.assertEqual((ctx.exception.error.kind, self.account.status), ("authentication", "invalid"))

    def test_permission_failure_keeps_status(self):
        with self.assertRaises(DiscoveryFailedError):
            self.discover(StaticSource(exc=api_error(403, "Forbidden")))
        self.assertEqual(self.account.status, "pending")

    def test_default_source_is_iam(self):
        with mock.patch("tenancy.projects.IamProjectSource") as source_cls:
            source_cls.return_value.list_projects.return_value = [SG]
            projects.discover_projects(self.session, self.keyring, self.client.id, self.account.id)
        kwargs = source_cls.call_args.kwargs
        self.assertEqual(kwargs, {"region_id": None, "domain_id": None})


if __name__ == "__main__":
    unittest.main()
