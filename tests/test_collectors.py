# coding: utf-8
"""Normalización y comportamiento específico de cada collector (sin red)."""

import unittest
from types import SimpleNamespace

from tests.helpers import REGION, FakeClient, api_error, context, make_items

from huaweicloudsdkcfw.v1 import FirewallInstanceVO, HttpFirewallInstanceListResponseData
from huaweicloudsdkobs.v1 import Bucket, Buckets

from collectors import COLLECTORS
from collectors.base import ALL_GRANTED_EPS
from collectors.cfw import CfwCollector
from collectors.ecs import EcsCollector
from collectors.eip import EipCollector
from collectors.evs import EvsCollector
from collectors.hss import HssCollector
from collectors.obs import ObsCollector
from collectors.vpc import VpcCollector
from collectors.vpn import VpnCollector
from collectors.waf import WafCollector
from core.serialization import REDACTED

EXPECTED_SERVICES = ["ecs", "evs", "vpc", "vpn", "obs", "eip", "cbr", "elb", "rds",
                     "dcs", "nat", "ces", "hss", "waf", "cfw"]

ECS_SERVER = {
    "id": "srv-1",
    "name": "web <prod> ñandú 🚀 =cmd",
    "status": "ACTIVE",
    "created": "2026-01-15T10:22:00Z",
    "updated": "2026-01-16T10:22:00Z",
    "OS-EXT-AZ:availability_zone": "ap-southeast-3a",
    "flavor": {"id": "s6.large.2", "name": "s6.large.2", "vcpus": "2", "ram": "4096"},
    "image": {"id": "img-123"},
    "metadata": {"image_name": "Ubuntu 22.04 server 64bit", "os_type": "Linux", "os_bit": "64"},
    "addresses": {"vpc-1": [
        {"addr": "192.168.0.10", "OS-EXT-IPS:type": "fixed", "OS-EXT-IPS-MAC:mac_addr": "fa:16:3e:00:00:01"},
        {"addr": "47.0.0.1", "OS-EXT-IPS:type": "floating"},
    ]},
    "security_groups": [{"name": "web-sg"}, {"name": "ssh-sg"}],
    "os-extended-volumes:volumes_attached": [{"id": "vol-1"}],
    "tags": ["env=prod", "team"],
    "enterprise_project_id": "0",
    "OS-EXT-SRV-ATTR:user_data": "IyEvYmluL2Jhc2gKZWNobyBmYWtl",
}


def single(service_method, response):
    return FakeClient(**{service_method: lambda request: response})


class TestRegistry(unittest.TestCase):
    def test_all_15_collectors_registered_in_historic_order(self):
        self.assertEqual(list(COLLECTORS), EXPECTED_SERVICES)

    def test_only_obs_is_global(self):
        self.assertEqual([s for s, c in COLLECTORS.items() if c.scope == "global"], ["obs"])


class TestEcsNormalization(unittest.TestCase):
    def setUp(self):
        client = single("list_servers_details", SimpleNamespace(servers=[ECS_SERVER], count=1))
        self.resource = EcsCollector().collect(context(client)).resources[0]

    def test_common_fields(self):
        r = self.resource
        self.assertEqual((r.service, r.resource_type, r.provider_id), ("ecs", "ecs.server", "srv-1"))
        self.assertEqual(r.name, ECS_SERVER["name"])  # caracteres especiales intactos
        self.assertEqual((r.status, r.region, r.created_at), ("ACTIVE", REGION, "2026-01-15T10:22:00Z"))
        self.assertEqual(r.tags, {"env": "prod", "team": ""})
        self.assertEqual(r.enterprise_project_id, "0")

    def test_operating_system_from_metadata(self):
        self.assertEqual(self.resource.attributes["os"], "Ubuntu 22.04 server 64bit")
        self.assertEqual(self.resource.attributes["image_id"], "img-123")

    def test_network_and_sizing(self):
        a = self.resource.attributes
        self.assertEqual(a["private_ips"], ["192.168.0.10"])
        self.assertEqual(a["public_ips"], ["47.0.0.1"])
        self.assertEqual(a["mac_addresses"], ["fa:16:3e:00:00:01"])
        self.assertEqual((a["vcpus"], a["ram_gb"]), (2, 4.0))
        self.assertEqual(a["security_groups"], ["web-sg", "ssh-sg"])

    def test_raw_is_preserved_but_user_data_redacted(self):
        raw = self.resource.raw
        self.assertEqual(raw["OS-EXT-SRV-ATTR:user_data"], REDACTED)
        self.assertEqual(raw["metadata"], ECS_SERVER["metadata"])
        self.assertEqual(len(self.resource.raw_hash), 64)

    def test_os_falls_back_to_image_id(self):
        server = {"id": "s2", "image": {"id": "img-9"}}
        client = single("list_servers_details", SimpleNamespace(servers=[server], count=1))
        r = EcsCollector().collect(context(client)).resources[0]
        self.assertEqual(r.attributes["os"], "img-9")

    def test_resource_without_name_or_ip(self):
        client = single("list_servers_details", SimpleNamespace(servers=[{"id": "s3"}], count=1))
        r = EcsCollector().collect(context(client)).resources[0]
        self.assertIsNone(r.name)
        self.assertEqual(r.attributes["private_ips"], [])
        self.assertEqual(r.attributes["public_ips"], [])
        self.assertEqual(r.attributes["vcpus"], 0)


class TestOtherNormalizations(unittest.TestCase):
    def test_evs_enterprise_project_from_correct_field(self):
        volume = {"id": "v1", "enterprise_project_id": "ep-123", "size": 40,
                  "attachments": [{"server_id": "srv-1"}]}
        client = single("list_volumes", SimpleNamespace(volumes=[volume], count=1))
        r = EvsCollector().collect(context(client)).resources[0]
        self.assertEqual(r.enterprise_project_id, "ep-123")
        self.assertEqual((r.attributes["size_gb"], r.attributes["server_id"]), (40, "srv-1"))

    def test_eip_v2_field_names(self):
        ip = {"id": "eip-1", "public_ip_address": "1.2.3.4", "private_ip_address": "10.0.0.5",
              "create_time": "2026-01-01 00:00:00", "status": "ACTIVE"}
        client = single("list_publicips", SimpleNamespace(publicips=[ip]))
        r = EipCollector().collect(context(client)).resources[0]
        self.assertEqual((r.attributes["public_ip"], r.attributes["private_ip"]), ("1.2.3.4", "10.0.0.5"))
        self.assertEqual(r.created_at, "2026-01-01 00:00:00")

    def test_vpc_v3_subnet_shape(self):
        subnet = {"id": "sub-1", "name": "s", "zone_id": "ap-southeast-3a", "vpc_id": "vpc-1",
                  "subnet_cidrs": [{"cidr": "10.0.0.0/24", "gateway_ip": "10.0.0.1",
                                    "enable_dhcp": True, "ip_version": "4"}]}
        empty = SimpleNamespace(page_info=None)
        client = FakeClient(
            list_vpcs=lambda r: SimpleNamespace(vpcs=[], page_info=None),
            list_virsubnets=lambda r: SimpleNamespace(virsubnets=[subnet], page_info=None),
            list_security_groups=lambda r: SimpleNamespace(security_groups=None, **vars(empty)),
        )
        r = VpcCollector().collect(context(client)).resources[0]
        self.assertEqual(r.attributes["cidr"], "10.0.0.0/24")
        self.assertEqual(r.attributes["gateway_ip"], "10.0.0.1")
        self.assertEqual(r.attributes["availability_zone"], "ap-southeast-3a")
        self.assertTrue(r.attributes["dhcp_enabled"])

    def test_vpn_connection_subnets(self):
        client = FakeClient(
            list_vgws=lambda r: SimpleNamespace(vpn_gateways=[{"id": "gw-1", "local_subnets": ["10.0.0.0/16"]}]),
            list_vpn_connections=lambda r: SimpleNamespace(
                vpn_connections=[{"id": "c1", "vgw_id": "gw-1", "peer_subnets": ["172.16.0.0/16"]}],
                page_info=None, total_count=1),
        )
        resources = VpnCollector().collect(context(client)).resources
        connection = [r for r in resources if r.resource_type == "vpn.connection"][0]
        self.assertEqual(connection.attributes["local_subnets"], ["10.0.0.0/16"])
        self.assertEqual(connection.attributes["remote_subnets"], ["172.16.0.0/16"])


class TestObs(unittest.TestCase):
    def test_real_sdk_bucket_container_and_regions(self):
        buckets = Buckets(bucket=[
            Bucket(name="logs", creation_date="2026-01-01T00:00:00Z", location=REGION),
            Bucket(name="backup-br", creation_date="2026-02-01T00:00:00Z", location="sa-brazil-1"),
            Bucket(name="legacy", creation_date="2020-01-01T00:00:00Z", location=""),
        ])
        closed = []
        client = FakeClient(list_buckets=lambda r: SimpleNamespace(buckets=buckets))
        client.close = lambda: closed.append(True)
        resources = ObsCollector().collect(context(client)).resources
        self.assertEqual({r.name: r.region for r in resources},
                         {"logs": REGION, "backup-br": "sa-brazil-1", "legacy": ""})
        self.assertEqual(closed, [True])


class TestCfw(unittest.TestCase):
    def test_body_pagination_and_nested_records(self):
        records = [FirewallInstanceVO(fw_instance_id=f"fw-{i}", name=f"fw{i}", status=2,
                                      enterprise_project_id="ep-1") for i in range(150)]

        def handler(request):
            body = request.body
            data = HttpFirewallInstanceListResponseData(
                offset=body.offset, limit=body.limit, total=len(records),
                records=records[body.offset: body.offset + body.limit])
            return SimpleNamespace(data=data)
        client = FakeClient(list_firewall_list=handler)
        resources = CfwCollector().collect(context(client)).resources
        self.assertEqual(len(resources), 150)
        self.assertEqual(resources[0].provider_id, "fw-0")
        self.assertEqual(resources[0].enterprise_project_id, "ep-1")
        self.assertEqual([r.body.offset for r in client.requests["list_firewall_list"]], [0, 100])
        self.assertTrue(all(r.enterprise_project_id is None for r in client.requests["list_firewall_list"]))


class TestEnterpriseProjects(unittest.TestCase):
    def test_hss_sends_all_granted_eps(self):
        client = single("list_host_status",
                        SimpleNamespace(data_list=[{"host_id": "h1", "host_name": "web", "private_ip": "10.0.0.1",
                                                    "os_type": "Linux", "agent_status": "online"}],
                                        total_num=1))
        result = HssCollector().collect(context(client))
        self.assertEqual(client.requests["list_host_status"][0].enterprise_project_id, ALL_GRANTED_EPS)
        r = result.resources[0]
        self.assertEqual((r.attributes["ip"], r.attributes["os"], r.attributes["agent_status"]),
                         ("10.0.0.1", "Linux", "online"))
        self.assertEqual(result.notices, [])

    def test_hss_uses_host_inventory_api_with_real_sdk_model(self):
        """Regresión de la prueba real: ListProtectionServers (/rasp/servers) daba 400 HSS.0002."""
        from huaweicloudsdkhss.v5 import Host, ListHostStatusRequest
        hosts = [Host(host_id=f"h{i}", host_name=f"web-{i}", private_ip=f"10.0.0.{i}", os_name="Ubuntu",
                      os_type="Linux", host_status="ACTIVE", agent_status="online", protect_status="opened",
                      version="hss.version.basic", resource_id=f"ecs-{i}") for i in range(130)]

        def handler(request):
            self.assertIsInstance(request, ListHostStatusRequest)
            self.assertTrue(10 <= request.limit <= 200)  # rango documentado
            return SimpleNamespace(data_list=hosts[request.offset: request.offset + request.limit],
                                   total_num=len(hosts))
        client = FakeClient(list_host_status=handler)
        resources = HssCollector().collect(context(client)).resources
        self.assertEqual(len(resources), 130)
        first = resources[0]
        self.assertEqual((first.provider_id, first.name, first.status), ("h0", "web-0", "opened"))
        self.assertEqual((first.attributes["ecs_id"], first.attributes["host_status"], first.attributes["os"]),
                         ("ecs-0", "ACTIVE", "Ubuntu"))
        self.assertEqual([r.offset for r in client.requests["list_host_status"]], [0, 100])

    def test_hss_permission_denied_is_a_warning(self):
        def handler(request):
            raise api_error(403, "Policy doesn't allow hss:hosts:list to be performed.", "HSS.1001")
        client = FakeClient(list_host_status=handler)
        from core.engine import run_collector
        run = run_collector(HssCollector(), context(client))
        self.assertEqual((run.error.kind, run.error.severity), ("permission", "aviso"))

    def test_waf_falls_back_without_eps_and_emits_notice(self):
        def handler(request):
            if request.enterprise_project_id == ALL_GRANTED_EPS:
                raise api_error(403, "EPS not authorized")
            return SimpleNamespace(items=make_items(2, "waf"), total=2)
        client = FakeClient(list_instance=handler)
        result = WafCollector().collect(context(client))
        self.assertEqual(len(result.resources), 2)
        self.assertEqual(len(result.notices), 1)
        self.assertEqual(result.notices[0].severity, "aviso")
        eps = [r.enterprise_project_id for r in client.requests["list_instance"]]
        self.assertEqual(eps, [ALL_GRANTED_EPS, None])

    def test_other_errors_are_not_retried(self):
        def handler(request):
            raise api_error(401, "bad credentials")
        client = FakeClient(list_instance=handler)
        with self.assertRaises(Exception):
            WafCollector().collect(context(client))
        self.assertEqual(len(client.requests["list_instance"]), 1)


if __name__ == "__main__":
    unittest.main()
