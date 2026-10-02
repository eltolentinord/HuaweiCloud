# coding: utf-8
"""API de inventario: búsqueda, filtros, orden, detalle, historial, estadísticas, comparación."""

from tests.scan_helpers import server
from tests.test_scan_api import ScanApiTestCase


class InventoryApiTestCase(ScanApiTestCase):
    def setUp(self):
        super().setUp()
        self.base = f"/api/clients/{self.cid}/accounts/{self.aid}"
        self.world.servers = {"p-sg": [server("srv-1", "web-prod"), server("srv-2", "db-prod"),
                                       server("srv-3", "web-dev", status="SHUTOFF")],
                              "p-mx": [server("srv-4", "mx-app")]}
        self.first = self.scan()

    def scan(self, services=("ecs", "obs")):
        response = self.start(services)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["id"]

    def get(self, path, **params):
        response = self.call("GET", path, params=params)
        return response

    def resources(self, **params):
        response = self.get(f"{self.base}/resources", **params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class TestResourceQueries(InventoryApiTestCase):
    def test_search_filters_and_pagination(self):
        self.assertEqual(self.resources(service="ecs")["total"], 4)
        self.assertEqual({i["provider_id"] for i in self.resources(search="WEB")["items"]}, {"srv-1", "srv-3"})
        self.assertEqual(self.resources(search="srv-4")["items"][0]["name"], "mx-app")
        self.assertEqual(self.resources(status="shutoff")["total"], 1)
        self.assertEqual(self.resources(region="la-north-2", service="ecs")["total"], 1)
        page = self.resources(service="ecs", limit=2, offset=2, sort="name")
        self.assertEqual((page["total"], len(page["items"])), (4, 2))
        self.assertEqual([i["name"] for i in page["items"]], ["web-dev", "web-prod"])

    def test_search_wildcards_are_literal(self):
        self.assertEqual(self.resources(search="%")["total"], 0)
        self.assertEqual(self.resources(search="_")["total"], 0)

    def test_sort_whitelist(self):
        names = [i["name"] for i in self.resources(service="ecs", sort="-name")["items"]]
        self.assertEqual(names, sorted(names, reverse=True))
        bad = self.get(f"{self.base}/resources", sort="raw;DROP TABLE resources")
        self.assertEqual(bad.status_code, 422)

    def test_deleted_filters(self):
        self.world.servers["p-sg"] = [server("srv-1", "web-prod")]
        self.scan()
        self.assertEqual(self.resources(service="ecs")["total"], 2)
        deleted = self.resources(only_deleted=True)
        self.assertEqual({i["provider_id"] for i in deleted["items"]}, {"srv-2", "srv-3"})
        self.assertEqual(self.resources(service="ecs", include_deleted=True)["total"], 4)

    def test_detail_and_history(self):
        rid = self.resources(search="srv-1")["items"][0]["id"]
        self.world.servers["p-sg"][0] = server("srv-1", "web-renamed")
        self.scan()
        detail = self.get(f"{self.base}/resources/{rid}").json()
        self.assertEqual(detail["raw"]["name"], "web-renamed")
        self.assertEqual([c["change_type"] for c in detail["recent_changes"]], ["updated", "created"])
        history = self.get(f"{self.base}/resources/{rid}/history", limit=1).json()
        self.assertEqual((history["total"], len(history["items"])), (2, 1))
        self.assertEqual(self.get(f"{self.base}/resources/00000000-0000-0000-0000-000000000000").status_code, 404)


class TestStatsAndChanges(InventoryApiTestCase):
    def test_stats(self):
        stats = self.get(f"{self.base}/stats").json()
        self.assertEqual(stats["by_service"], {"ecs": 4, "obs": 1})
        self.assertEqual(stats["by_region"], {"ap-southeast-3": 4, "la-north-2": 1})
        self.assertEqual(stats["by_status"]["SHUTOFF"], 1)
        self.assertEqual(stats["last_scan"]["id"], self.first)
        self.assertEqual(stats["last_scan_changes"], {"created": 5})

    def test_scan_changes_endpoint(self):
        self.world.servers["p-mx"] = []
        second = self.scan()
        changes = self.get(f"/api/clients/{self.cid}/scans/{second}/changes", change_type="deleted").json()
        self.assertEqual([c["provider_id"] for c in changes["items"]], ["srv-4"])
        self.assertEqual(self.get(f"/api/clients/{self.cid}/scans/{second}/changes",
                                  change_type="bogus").status_code, 422)

    def test_compare_endpoint(self):
        self.world.servers["p-sg"] = [server("srv-1", "web-v2"), server("srv-5", "new")]
        second = self.scan()
        body = self.get(f"{self.base}/scans/compare", from_scan=self.first, to_scan=second).json()
        self.assertEqual(body["summary"]["counts"], {"added": 1, "removed": 2, "modified": 1, "transient": 0})
        only_added = self.get(f"{self.base}/scans/compare", from_scan=self.first, to_scan=second,
                              category="added").json()
        self.assertEqual([i["provider_id"] for i in only_added["items"]], ["srv-5"])

    def test_overview(self):
        body = self.get(f"/api/clients/{self.cid}/overview").json()
        account = body["accounts"][0]
        self.assertEqual((account["projects"], account["total_active"]), (2, 5))
        self.assertEqual(account["regions"], ["ap-southeast-3", "la-north-2"])
        self.assertEqual(account["last_scan"]["status"], "completed")


class TestInventoryIsolation(InventoryApiTestCase):
    def test_other_client_gets_404_everywhere(self):
        rid = self.resources()["items"][0]["id"]
        other = f"/api/clients/{self.other_cid}"
        for path in (f"{other}/accounts/{self.aid}/resources", f"{other}/accounts/{self.aid}/resources/{rid}",
                     f"{other}/accounts/{self.aid}/resources/{rid}/history", f"{other}/accounts/{self.aid}/stats",
                     f"{other}/scans/{self.first}/changes"):
            self.assertEqual(self.get(path).status_code, 404, path)
        response = self.get(f"{other}/accounts/{self.other_aid}/scans/compare",
                            from_scan=self.first, to_scan=self.first)
        self.assertEqual(response.status_code, 404)
        overview = self.get(f"{other}/overview").json()
        self.assertEqual([a["id"] for a in overview["accounts"]], [str(self.other_aid)])
        # Un recurso de otra cuenta tampoco es accesible por la cuenta del otro cliente
        self.assertEqual(self.get(f"{other}/accounts/{self.other_aid}/resources/{rid}").status_code, 404)
