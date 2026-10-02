# coding: utf-8
"""Historial de cambios por recurso y comparación entre escaneos (SQLite)."""

from types import SimpleNamespace

from sqlalchemy import select

from tests.helpers import FAKE_SK, api_error
from tests.scan_helpers import server
from tests.test_scanning import ScanTestCase

from db.models import InventoryResource, ResourceChange
from repositories.resources import field_diff
from scanning.history import compare_scans, diff_events
from tenancy.errors import NotFoundError, ValidationFailedError


class HistoryTestCase(ScanTestCase):
    services = ("ecs",)

    def events(self, run=None, change_type=None):
        query = select(ResourceChange)
        if run is not None:
            query = query.where(ResourceChange.scan_run_id == run.id)
        if change_type:
            query = query.where(ResourceChange.change_type == change_type)
        return list(self.session.scalars(query))

    def compare(self, a, b, client_id=None):
        return compare_scans(self.session, client_id=client_id or self.client.id, account_id=self.account.id,
                             from_scan=a.id, to_scan=b.id)


class TestChangeEvents(HistoryTestCase):
    def test_first_scan_records_created(self):
        run = self.scan()
        events = self.events(run)
        self.assertEqual(sorted(e.provider_id for e in events), ["srv-1", "srv-2", "srv-3"])
        self.assertEqual({e.change_type for e in events}, {"created"})
        self.assertTrue(all(e.raw_hash_after and not e.raw_hash_before for e in events))

    def test_unchanged_scan_records_nothing(self):
        self.scan()
        self.assertEqual(self.events(self.scan()), [])

    def test_update_records_changed_fields(self):
        self.scan()
        self.world.servers["p-sg"][0] = server("srv-1", "web-v2", flavor={"vcpus": "4", "ram": 8192})
        run = self.scan()
        [event] = self.events(run)
        self.assertEqual((event.change_type, event.provider_id), ("updated", "srv-1"))
        fields = {c["field"]: c for c in event.changed_fields}
        self.assertEqual((fields["name"]["before"], fields["name"]["after"]), ("web", "web-v2"))
        self.assertEqual((fields["attributes.vcpus"]["before"], fields["attributes.vcpus"]["after"]), (2, 4))
        self.assertIn("raw", fields)
        self.assertNotEqual(event.raw_hash_before, event.raw_hash_after)

    def test_delete_and_restore(self):
        self.scan()
        self.world.servers["p-sg"] = [server("srv-1", "web")]
        deleted = self.events(self.scan(), "deleted")
        self.assertEqual([e.provider_id for e in deleted], ["srv-2"])
        self.world.servers["p-sg"].append(server("srv-2", "db"))
        restored = self.events(self.scan(), "restored")
        self.assertEqual([e.provider_id for e in restored], ["srv-2"])

    def test_failed_or_partial_scans_record_no_deletions(self):
        self.scan()
        self.world.failures["list_servers_details"] = api_error(500, "boom", "ECS.500")
        self.assertEqual(self.events(self.scan()), [])

    def test_events_never_contain_secrets(self):
        self.scan()
        self.world.servers["p-sg"][0] = server("srv-1", "web", metadata={"token": "t-1", "note": FAKE_SK})
        run = self.scan()
        dump = repr([(e.name, e.changed_fields) for e in self.events(run)])
        self.assertNotIn(FAKE_SK, dump)
        self.assertNotIn("t-1", dump)


class TestFieldDiff(HistoryTestCase):
    def test_diff_details(self):
        row = InventoryResource(name="a", status="ACTIVE", region="r", enterprise_project_id="0",
                                provider_created_at=None, tags={"env": "dev", "old": "x"},
                                attributes={"cpu": 1, "big": "y" * 1000}, raw_hash="h1")
        values = dict(name="a", status="SHUTOFF", region="r", enterprise_project_id="0",
                      provider_created_at=None, tags={"env": "prod", "new": "1"},
                      attributes={"cpu": 1, "big": "z" * 1000}, raw_hash="h2")
        fields = {c["field"]: c for c in field_diff(row, values)}
        self.assertEqual(set(fields), {"status", "tags.env", "tags.old", "tags.new", "attributes.big", "raw"})
        self.assertLessEqual(len(fields["attributes.big"]["after"]), 302)  # truncado
        self.assertEqual(field_diff(row, {**values, **{k: getattr(row, k) for k in values}}), [])


class TestCompareScans(HistoryTestCase):
    def test_added_removed_modified_by_stable_identity(self):
        first = self.scan()
        self.world.servers["p-sg"] = [server("srv-1", "renamed"), server("srv-new", "db")]  # srv-2 → fuera
        second = self.scan()
        result = self.compare(first, second)
        by_id = {i.provider_id: i for i in result.items}
        self.assertEqual(by_id["srv-new"].category, "added")
        self.assertEqual(by_id["srv-2"].category, "removed")
        self.assertEqual(by_id["srv-1"].category, "modified")  # renombrado: misma identidad
        self.assertIn("name", {c["field"] for c in by_id["srv-1"].changed_fields})
        self.assertNotIn("srv-3", by_id)  # sin cambios
        self.assertEqual(result.summary()["counts"], {"added": 1, "removed": 1, "modified": 1, "transient": 0})

    def test_intermediate_scans_are_merged(self):
        first = self.scan()
        self.world.servers["p-sg"] = [server("srv-1", "v2"), server("srv-2", "db"), server("tmp", "tmp")]
        self.scan()
        self.world.servers["p-sg"] = [server("srv-1", "v3"), server("srv-2", "db")]
        last = self.scan()
        result = self.compare(first, last)
        by_id = {i.provider_id: i for i in result.items}
        self.assertEqual(by_id["tmp"].category, "transient")
        name = next(c for c in by_id["srv-1"].changed_fields if c["field"] == "name")
        self.assertEqual((name["before"], name["after"]), ("web", "v3"))  # primer antes → último después
        self.assertEqual(result.intermediate_runs, 1)

    def test_delete_then_restore_with_no_net_change_is_modified(self):
        first = self.scan()
        kept = self.world.servers["p-sg"]
        self.world.servers["p-sg"] = []
        self.scan()
        self.world.servers["p-sg"] = kept
        result = self.compare(first, self.scan())
        self.assertEqual({i.category for i in result.items}, {"modified"})
        self.assertEqual(result.items[0].events, ["deleted", "restored"])

    def test_reversed_order_is_swapped(self):
        first, second = self.scan(), self.scan()
        result = self.compare(second, first)
        self.assertTrue(result.swapped)
        self.assertEqual(result.from_run.id, first.id)

    def test_validation_and_isolation(self):
        first = self.scan()
        with self.assertRaises(ValidationFailedError):
            self.compare(first, first)
        from tests.scan_helpers import seed_account
        other, _, _ = seed_account(self.session, self.keyring, client_name="Otro",
                                   projects_spec=(("p-x", "ap-southeast-3"),))
        with self.assertRaises(NotFoundError):
            self.compare(first, self.scan(), client_id=other.id)

    def test_diff_events_pure(self):
        e = lambda t: SimpleNamespace(resource_id=1, change_type=t, service="ecs", resource_type="ecs.server",
                                      provider_id="x", region="r", name="n", changed_fields=[])
        self.assertEqual(diff_events([e("created")])[0].category, "added")
        self.assertEqual(diff_events([e("updated"), e("deleted")])[0].category, "removed")
