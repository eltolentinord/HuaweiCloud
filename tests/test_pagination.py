# coding: utf-8
"""Estrategias de paginación y protecciones (sin red)."""

import unittest
from types import SimpleNamespace

from tests.helpers import FakeClient, context, ecs_api, make_items, marker_api, offset_api, token_api

from collectors.ecs import EcsCollector
from collectors.eip import EipCollector
from collectors.evs import EvsCollector
from collectors.vpc import VpcCollector
from collectors.waf import WafCollector
from core.errors import PaginationError
from core.pagination import (
    MarkerPagination,
    OffsetPagination,
    Page,
    PagePagination,
    SinglePage,
    TokenPagination,
    paginate,
)


def pages(*pages_):
    """fetch que devuelve las páginas indicadas en orden."""
    queue = list(pages_)
    calls = []

    def fetch(params):
        calls.append(dict(params))
        return queue.pop(0) if queue else Page(items=[])
    fetch.calls = calls
    return fetch


class TestEcsPagination(unittest.TestCase):
    """ECS usa ``offset`` como número de página: el bug histórico cortaba en 100."""

    def collect(self, n):
        servers = make_items(n, "ecs", flavor={"vcpus": "2", "ram": 4096})
        client = FakeClient(list_servers_details=ecs_api(servers))
        result = EcsCollector().collect(context(client))
        return result, client.requests["list_servers_details"]

    def test_counts(self):
        for n in (0, 1, 99, 100, 101, 250, 550, 1000):
            with self.subTest(n=n):
                result, _ = self.collect(n)
                ids = [r.provider_id for r in result.resources]
                self.assertEqual(len(ids), n)
                self.assertEqual(len(set(ids)), n)

    def test_request_uses_page_numbers_from_1(self):
        _, requests = self.collect(250)
        self.assertEqual([r.offset for r in requests], [1, 2, 3])
        self.assertTrue(all(r.limit == 100 for r in requests))

    def test_exact_multiple_stops_using_count(self):
        _, requests = self.collect(200)
        self.assertEqual(len(requests), 2)

    def test_empty_needs_single_request(self):
        result, requests = self.collect(0)
        self.assertEqual(result.resources, [])
        self.assertEqual(len(requests), 1)


class TestOffsetPagination(unittest.TestCase):
    def test_evs_multiple_pages_with_total(self):
        volumes = make_items(345, "vol", size=10)
        client = FakeClient(list_volumes=offset_api(volumes, "volumes", "count"))
        result = EvsCollector().collect(context(client))
        self.assertEqual(len(result.resources), 345)
        self.assertEqual([r.offset for r in client.requests["list_volumes"]], [0, 100, 200, 300])

    def test_api_capping_limit_is_handled_when_total_known(self):
        # La API devuelve 50 aunque se pidan 100: con total conocido se sigue paginando.
        items = make_items(180)
        fetch_api = offset_api(items, "items", "total", cap=50)

        def fetch(params):
            resp = fetch_api(SimpleNamespace(**params))
            return Page(items=resp.items, total=resp.total)
        self.assertEqual(len(paginate(fetch, OffsetPagination(limit=100))), 180)

    def test_short_page_without_total_stops(self):
        fetch = pages(Page(items=make_items(30)))
        self.assertEqual(len(paginate(fetch, OffsetPagination(limit=100))), 30)
        self.assertEqual(len(fetch.calls), 1)


class TestMarkerPagination(unittest.TestCase):
    def test_eip_uses_last_id_as_marker(self):
        ips = make_items(230, "eip")
        client = FakeClient(list_publicips=marker_api(ips, "publicips"))
        result = EipCollector().collect(context(client))
        self.assertEqual(len(result.resources), 230)
        markers = [r.marker for r in client.requests["list_publicips"]]
        self.assertEqual(markers, [None, "eip-00099", "eip-00199"])

    def test_missing_id_for_marker_raises(self):
        page = Page(items=[{"name": f"x{i}"} for i in range(5)])
        with self.assertRaises(PaginationError):
            paginate(pages(page), MarkerPagination(limit=5), id_key=None)


class TestTokenPagination(unittest.TestCase):
    def test_vpc_follows_next_marker(self):
        client = FakeClient(
            list_vpcs=token_api(make_items(250, "vpc"), "vpcs"),
            list_virsubnets=token_api(make_items(3, "sub"), "virsubnets"),
            list_security_groups=token_api([], "security_groups"),
        )
        result = VpcCollector().collect(context(client))
        types = [r.resource_type for r in result.resources]
        self.assertEqual(types.count("vpc.vpc"), 250)
        self.assertEqual(types.count("vpc.subnet"), 3)
        self.assertEqual(types.count("vpc.security_group"), 0)
        self.assertEqual([r.marker for r in client.requests["list_vpcs"]], [None, "tok-100", "tok-200"])

    def test_repeated_marker_raises(self):
        def fetch(params):
            return Page(items=make_items(2, f"p{params.get('marker')}"), next_token="same")
        with self.assertRaises(PaginationError):
            paginate(fetch, TokenPagination(limit=2))


class TestPagePagination(unittest.TestCase):
    def test_waf_uses_page_and_pagesize(self):
        items = make_items(205, "waf")

        def handler(request):
            start = (request.page - 1) * request.pagesize
            return SimpleNamespace(items=items[start:start + request.pagesize], total=len(items))
        client = FakeClient(list_instance=handler)
        result = WafCollector().collect(context(client))
        self.assertEqual(len(result.resources), 205)
        self.assertEqual([r.page for r in client.requests["list_instance"]], [1, 2, 3])


class TestProtections(unittest.TestCase):
    def test_empty_response(self):
        self.assertEqual(paginate(pages(Page(items=[])), PagePagination()), [])

    def test_partial_duplicates_are_removed(self):
        first = make_items(3)
        second = [first[2]] + make_items(2, "new")
        fetch = pages(Page(items=first), Page(items=second))
        result = paginate(fetch, OffsetPagination(limit=3))
        self.assertEqual([i["id"] for i in result], ["res-00000", "res-00001", "res-00002", "new-00000", "new-00001"])

    def test_api_ignoring_pagination_raises_instead_of_looping(self):
        same = make_items(10)

        def fetch(params):
            return Page(items=same)
        with self.assertRaises(PaginationError):
            paginate(fetch, OffsetPagination(limit=10))

    def test_invalid_page_object(self):
        with self.assertRaises(PaginationError):
            paginate(lambda params: {"items": []}, SinglePage())

    def test_invalid_item(self):
        with self.assertRaises(PaginationError):
            paginate(pages(Page(items=["not-a-dict"])), SinglePage())

    def test_max_pages_guard(self):
        counter = iter(range(10_000))

        def fetch(params):
            n = next(counter)
            return Page(items=[{"id": n}], next_token=f"t{n}")
        with self.assertRaises(PaginationError):
            paginate(fetch, TokenPagination(limit=1), max_pages=5)

    def test_items_field_not_a_list_is_invalid_response(self):
        client = FakeClient(list_volumes=lambda r: SimpleNamespace(volumes=SimpleNamespace(x=1), count=1))
        with self.assertRaises(PaginationError):
            EvsCollector().collect(context(client))

    def test_items_field_none_means_empty(self):
        client = FakeClient(list_volumes=lambda r: SimpleNamespace(volumes=None, count=None))
        self.assertEqual(EvsCollector().collect(context(client)).resources, [])


if __name__ == "__main__":
    unittest.main()
