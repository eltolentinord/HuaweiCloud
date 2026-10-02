# coding: utf-8
"""Retención: simulación por defecto, límites seguros y coherencia del historial."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from tests.scan_helpers import server
from tests.test_scanning import ScanTestCase

from db.models import InventoryResource, ResourceChange, ScanRun, ScanTask
from scanning.engine import create_scan
from scanning.history import compare_scans
from scanning.maintenance import prune

FUTURE = datetime.now(timezone.utc) + timedelta(days=400)  # "dentro de 400 días": todo es antiguo


class TestPrune(ScanTestCase):
    services = ("ecs",)

    def setUp(self):
        super().setUp()
        self.runs = []
        for i in range(5):
            self.world.servers["p-sg"] = [server("srv-1", f"web-v{i}"), server("srv-2", "db")]
            self.runs.append(self.scan())

    def count(self, model):
        return self.session.scalar(select(func.count()).select_from(model))

    def test_dry_run_changes_nothing(self):
        report = prune(self.session, keep_days=30, keep_min=2, now=FUTURE)
        self.assertEqual((report.applied, report.scans), (False, 3))
        self.assertGreater(report.scan_changes, 0)
        self.assertEqual(self.count(ScanRun), 5)

    def test_apply_keeps_most_recent_and_history_stays_coherent(self):
        prune(self.session, keep_days=30, keep_min=2, apply=True, now=FUTURE)
        self.session.commit()
        kept = list(self.session.scalars(select(ScanRun).order_by(ScanRun.sequence)))
        self.assertEqual([r.sequence for r in kept], [4, 5])
        kept_ids = {r.id for r in kept}
        self.assertEqual({t.scan_run_id for t in self.session.scalars(select(ScanTask))}, kept_ids)
        self.assertFalse(self.session.scalars(select(ResourceChange).where(
            ResourceChange.scan_run_id.not_in([r.id for r in kept]))).all())
        result = compare_scans(self.session, client_id=self.client.id, account_id=self.account.id,
                               from_scan=kept[0].id, to_scan=kept[1].id)
        name = next(c for c in result.items[0].changed_fields if c["field"] == "name")
        self.assertEqual((name["before"], name["after"]), ("web-v3", "web-v4"))
        self.assertEqual(self.count(InventoryResource), 3)  # el inventario no se toca

    def test_recent_and_active_scans_are_never_deleted(self):
        self.assertEqual(prune(self.session, keep_days=30, keep_min=1, apply=True).scans, 0)  # todos recientes
        create_scan(self.session, client_id=self.client.id, account_id=self.account.id, services=["ecs"])
        self.session.commit()
        prune(self.session, keep_days=1, keep_min=1, apply=True, now=FUTURE)
        statuses = sorted(r.status for r in self.session.scalars(select(ScanRun)))
        self.assertEqual(statuses, ["pending"])  # el activo es el más reciente y además no es terminal

    def test_purge_deleted_resources(self):
        self.world.servers["p-sg"] = [server("srv-1", "web-v4")]
        self.scan()  # srv-2 desaparece
        self.assertEqual(prune(self.session, purge_deleted_days=30, now=FUTURE).deleted_resources, 1)
        prune(self.session, keep_days=10_000, keep_min=100, purge_deleted_days=30, apply=True, now=FUTURE)
        self.session.commit()
        self.assertEqual(sorted(r.provider_id for r in self.session.scalars(select(InventoryResource))),
                         ["srv-1", "srv-3"])

    def test_invalid_parameters(self):
        for kwargs in ({"keep_days": 0}, {"keep_min": 0}, {"purge_deleted_days": 0}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    prune(self.session, **kwargs)
