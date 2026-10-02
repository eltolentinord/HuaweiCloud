# coding: utf-8
"""Cobertura por servicio: servicios sin permiso se muestran como denied/partial, no como error."""

import unittest
from types import SimpleNamespace

from tests.helpers import api_error
from tests.scan_helpers import server
from tests.test_inventory_api import InventoryApiTestCase

from scanning.coverage import service_coverage


def task(service, status, region="ap-southeast-3", count=0, iam_action=None, message=None):
    return SimpleNamespace(service=service, status=status, region=region, resource_count=count,
                           iam_action=iam_action, error_message_safe=message)


class TestSummary(unittest.TestCase):
    def summary(self, *tasks):
        return {c.service: c for c in service_coverage(tasks)}

    def test_statuses(self):
        cov = self.summary(
            task("ecs", "succeeded", count=3), task("ecs", "succeeded", "la-north-2", count=1),
            task("obs", "denied", "global", iam_action="obs:bucket:ListAllMyBuckets", message="Sin permiso"),
            task("hss", "succeeded"), task("hss", "denied", "la-north-2", iam_action="hss:hosts:list"),
            task("rds", "unavailable"), task("rds", "succeeded", "la-north-2", count=2),
            task("dcs", "failed"), task("dcs", "denied", "la-north-2"),
        )
        self.assertEqual({k: (v.status, v.complete) for k, v in cov.items()}, {
            "ecs": ("succeeded", True), "obs": ("denied", False), "hss": ("partial", False),
            "rds": ("succeeded", True), "dcs": ("failed", False)})
        self.assertEqual(cov["ecs"].resources, 4)
        self.assertEqual(cov["obs"].iam_actions, ["obs:bucket:ListAllMyBuckets"])
        self.assertEqual(cov["obs"].message, "Sin permiso")
        self.assertEqual(cov["hss"].regions_affected, ["la-north-2"])
        self.assertEqual(cov["hss"].tasks, {"succeeded": 1, "denied": 1})
        self.assertEqual(cov["rds"].regions_affected, [])

    def test_partial_task_and_running(self):
        cov = self.summary(task("evs", "partial"), task("vpc", "succeeded"), task("vpc", "running"))
        self.assertEqual((cov["evs"].status, cov["evs"].complete), ("partial", False))
        self.assertEqual(cov["vpc"].status, "running")


class TestCoverageApi(InventoryApiTestCase):
    def test_denied_service_is_reported_and_its_resources_are_kept(self):
        self.world.buckets = []
        self.world.failures["list_buckets"] = api_error(403, "Access Denied", code="AccessDenied")
        self.world.servers["p-sg"].append(server("srv-9", "new"))
        self.scan()
        stats = self.get(f"{self.base}/stats").json()
        self.assertEqual(stats["last_scan"]["status"], "completed_with_warnings")
        cov = {c["service"]: c for c in stats["last_scan_coverage"]}
        self.assertEqual((cov["ecs"]["status"], cov["ecs"]["complete"], cov["ecs"]["resources"]),
                         ("succeeded", True, 5))
        self.assertEqual((cov["obs"]["status"], cov["obs"]["complete"]), ("denied", False))
        self.assertEqual(stats["by_service"]["obs"], 1)  # el bucket inventariado antes se conserva

    def test_every_scanned_service_has_coverage(self):
        cov = self.get(f"{self.base}/stats").json()["last_scan_coverage"]
        self.assertEqual([(c["service"], c["status"]) for c in cov], [("ecs", "succeeded"), ("obs", "succeeded")])
