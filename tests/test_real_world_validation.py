# coding: utf-8
"""Validación basada en la primera prueba REAL contra Huawei Cloud (ap-southeast-3).

Resultado real observado:
- ECS, EVS, VPC, Subnet y Security Group: 1 recurso cada uno, coherentes entre sí.
- CBR, ELB, RDS, NAT, CES, HSS, WAF, CFW: HTTP 403 (servicio no usado o sin permiso).
- VPN: HTTP 401 ``VPN.0003`` "Insufficient authentication for action vpn:vpnGateways:list".

Estos tests reproducen esas respuestas con los MODELOS REALES del SDK (mismas clases
que devuelve Huawei) y con los códigos de error observados. Ningún dato es real.
"""

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from tests.db_helpers import SqliteTestCase
from tests.helpers import FAKE_AK, FAKE_PROJECT, FAKE_SK, REGION, FakeClient, FakeFactory, api_error, context
from tests.scan_helpers import ScanWorld, seed_account, simulated_huawei

from huaweicloudsdkcore.exceptions import exceptions as sdk_exceptions
from huaweicloudsdkecs.v2 import (
    ServerAddress,
    ServerDetail,
    ServerExtendVolumeAttachment,
    ServerFlavor,
    ServerImage,
    ServerSecurityGroup,
)
from huaweicloudsdkevs.v2 import Attachment, VolumeDetail
from huaweicloudsdkvpc.v3 import SecurityGroup, SubnetCidr, Virsubnet, Vpc

from collectors.ecs import EcsCollector
from collectors.evs import EvsCollector
from collectors.vpc import VpcCollector
from core.errors import AUTH_PREFIX, classify_exception
from db.models import InventoryResource, ScanRun, ScanTask
from presentation.tables import build_service_view
from scanning.engine import scan_account

VPN_401 = ("VPN.0003", "Insufficient authentication for action vpn:vpnGateways:list")
OBSERVED_403 = {  # servicio → código observado en la prueba real
    "cbr": "BackupService.0403", "elb": "SYS.0403", "rds": "DBS.01280003", "nat": "SYS.0403",
    "ces": "403", "hss": "common.01010013", "waf": "WAF.00012006", "cfw": "00000010",
}

# ------------------------------------------------- fixtures con modelos del SDK
VPC_ID, SUBNET_ID, SG_ID, SERVER_ID, VOLUME_ID = "vpc-0001", "subnet-0001", "sg-0001", "srv-0001", "vol-0001"


def real_server() -> ServerDetail:
    return ServerDetail(
        id=SERVER_ID, name="ecs-demo", status="ACTIVE", created="2026-09-30T12:00:00Z",
        updated="2026-09-30T12:05:00Z", tenant_id="tenant", user_id="user", key_name="kp-demo",
        flavor=ServerFlavor(id="s6.medium.2", name="s6.medium.2", vcpus="1", ram="2048", disk="0"),
        image=ServerImage(id="img-0001"),
        metadata={"image_name": "Ubuntu 22.04 server 64bit", "os_type": "Linux", "os_bit": "64",
                  "vpc_id": VPC_ID},
        addresses={VPC_ID: [ServerAddress(version="4", addr="192.168.0.10", os_ext_ip_stype="fixed",
                                          os_ext_ips_ma_cmac_addr="fa:16:3e:00:00:01",
                                          os_ext_ip_sport_id="port-0001")]},
        security_groups=[ServerSecurityGroup(name="sg-demo", id=SG_ID)],
        os_extended_volumesvolumes_attached=[ServerExtendVolumeAttachment(
            id=VOLUME_ID, delete_on_termination="true", boot_index="0", device="/dev/vda")],
        os_ext_a_zavailability_zone="ap-southeast-3a", enterprise_project_id="0",
        tags=["env=demo"], os_ext_srv_att_ruser_data="IyEvYmluL2Jhc2g=",
    )


def real_volume() -> VolumeDetail:
    return VolumeDetail(
        id=VOLUME_ID, name="ecs-demo-volume-0000", status="in-use", size=40, volume_type="GPSSD",
        availability_zone="ap-southeast-3a", bootable="true", enterprise_project_id="0",
        created_at="2026-09-30T12:00:01.000000", updated_at="2026-09-30T12:01:00.000000",
        attachments=[Attachment(server_id=SERVER_ID, volume_id=VOLUME_ID, device="/dev/vda",
                                attachment_id="att-1", id=VOLUME_ID)],
        metadata={}, multiattach=False, encrypted=False,
    )


def real_vpc() -> Vpc:
    return Vpc(id=VPC_ID, name="vpc-default", cidr="192.168.0.0/16", status="ACTIVE",
               enterprise_project_id="0", description="")


def real_subnet() -> Virsubnet:
    return Virsubnet(id=SUBNET_ID, name="subnet-default", vpc_id=VPC_ID, status="ACTIVE",
                     zone_id="ap-southeast-3a",
                     subnet_cidrs=[SubnetCidr(cidr="192.168.0.0/24", gateway_ip="192.168.0.1",
                                              enable_dhcp=True, ip_version="4")])


def real_security_group() -> SecurityGroup:
    return SecurityGroup(id=SG_ID, name="sg-demo", description="default", enterprise_project_id="0")


def single_page(field, item):
    return lambda *args: SimpleNamespace(**{field: [item], "page_info": None, "count": 1})


class TestRealCollectorsContract(unittest.TestCase):
    """ECS/EVS/VPC funcionaron en real: la normalización con modelos del SDK no debe regresar."""

    def test_ecs_real_model(self):
        client = FakeClient(list_servers_details=lambda r: SimpleNamespace(servers=[real_server()], count=1))
        r = EcsCollector().collect(context(client)).resources[0]
        self.assertEqual((r.provider_id, r.name, r.status), (SERVER_ID, "ecs-demo", "ACTIVE"))
        a = r.attributes
        self.assertEqual(a["availability_zone"], "ap-southeast-3a")
        self.assertEqual((a["vcpus"], a["ram_gb"], a["flavor_name"]), (1, 2.0, "s6.medium.2"))
        self.assertEqual((a["private_ips"], a["public_ips"]), (["192.168.0.10"], []))
        self.assertEqual(a["mac_addresses"], ["fa:16:3e:00:00:01"])
        self.assertEqual((a["security_groups"], a["volume_ids"], a["networks"]), (["sg-demo"], [VOLUME_ID], [VPC_ID]))
        self.assertEqual(a["os"], "Ubuntu 22.04 server 64bit")
        self.assertEqual(r.raw["OS-EXT-SRV-ATTR:user_data"], "[REDACTED]")

    def test_evs_real_model(self):
        client = FakeClient(list_volumes=lambda r: SimpleNamespace(volumes=[real_volume()], count=1))
        r = EvsCollector().collect(context(client)).resources[0]
        self.assertEqual((r.provider_id, r.status, r.enterprise_project_id), (VOLUME_ID, "in-use", "0"))
        self.assertEqual((r.attributes["size_gb"], r.attributes["server_id"]), (40, SERVER_ID))

    def test_vpc_subnet_and_security_group_real_models(self):
        client = FakeClient(list_vpcs=single_page("vpcs", real_vpc()),
                            list_virsubnets=single_page("virsubnets", real_subnet()),
                            list_security_groups=single_page("security_groups", real_security_group()))
        by_type = {r.resource_type: r for r in VpcCollector().collect(context(client)).resources}
        self.assertEqual(set(by_type), {"vpc.vpc", "vpc.subnet", "vpc.security_group"})
        self.assertEqual(by_type["vpc.vpc"].attributes["cidr"], "192.168.0.0/16")
        subnet = by_type["vpc.subnet"].attributes
        self.assertEqual((subnet["cidr"], subnet["gateway_ip"], subnet["vpc_id"], subnet["availability_zone"]),
                         ("192.168.0.0/24", "192.168.0.1", VPC_ID, "ap-southeast-3a"))
        self.assertEqual(by_type["vpc.security_group"].provider_id, SG_ID)

    def test_cross_service_coherence(self):
        """Como en la prueba real: el disco del ECS es el volumen EVS y su red es la VPC."""
        ecs = EcsCollector().collect(context(FakeClient(
            list_servers_details=lambda r: SimpleNamespace(servers=[real_server()], count=1)))).resources[0]
        evs = EvsCollector().collect(context(FakeClient(
            list_volumes=lambda r: SimpleNamespace(volumes=[real_volume()], count=1)))).resources[0]
        self.assertIn(evs.provider_id, ecs.attributes["volume_ids"])
        self.assertEqual(evs.attributes["server_id"], ecs.provider_id)
        self.assertIn(VPC_ID, ecs.attributes["networks"])
        tables, _ = build_service_view("ecs", [ecs])
        self.assertEqual(tables[0]["filas"][0]["Security Group"], "sg-demo")


class TestObservedErrorClassification(unittest.TestCase):
    def classify(self, exc, service="vpn"):
        return classify_exception(exc, service=service, region=REGION, project_id=FAKE_PROJECT,
                                  secrets=(FAKE_AK, FAKE_SK))

    def test_vpn_0003_is_authorization_warning_not_bad_credentials(self):
        err = self.classify(api_error(401, VPN_401[1], VPN_401[0]))
        self.assertEqual((err.kind, err.severity, err.iam_action), ("authorization", "aviso", "vpn:vpnGateways:list"))
        legacy = err.to_legacy()
        self.assertEqual(legacy["categoria"], "authorization")
        self.assertIn("vpn:vpnGateways:list", legacy["mensaje_seguro"])
        self.assertIn("Las credenciales son válidas", legacy["mensaje_seguro"])

    def test_observed_403_codes_are_permission_warnings(self):
        for service, code in OBSERVED_403.items():
            with self.subTest(service=service):
                err = self.classify(api_error(403, "Forbidden", code), service)
                self.assertEqual((err.kind, err.severity), ("permission", "aviso"))

    def test_real_bad_credentials_are_authentication_errors(self):
        err = self.classify(api_error(401, "Incorrect IAM authentication information: AK access failed", "APIGW.0301"))
        self.assertEqual((err.kind, err.severity, err.iam_action), ("authentication", "error", None))

    def test_401_without_code_or_action_is_treated_as_authentication(self):
        sdk_error = sdk_exceptions.SdkError("req", None, "Unauthorized")
        err = self.classify(sdk_exceptions.ClientRequestException(401, sdk_error))
        self.assertEqual(err.kind, "authentication")

    def test_sdk_wrapped_http_error_is_classified(self):
        """Observado en real: GlobalCredentials sin domain_id envuelve el 401 en SdkException."""
        wrapped = sdk_exceptions.SdkException(
            "Failed to get domain id, ClientRequestException - {status_code:401,request_id:req-1,"
            "error_code:APIGW.0301,error_msg:Incorrect IAM authentication information: Unauthorized,"
            f"encoded_authorization_message:None }} {FAKE_SK}")
        err = self.classify(wrapped, "iam")
        self.assertEqual((err.kind, err.http_status, err.error_code, err.request_id),
                         ("authentication", 401, "APIGW.0301", "req-1"))
        self.assertNotIn(FAKE_SK, err.message)
        self.assertEqual(self.classify(sdk_exceptions.SdkException("unexpected"), "iam").kind, "internal")

    def test_api_not_published_in_region_is_unavailable(self):
        err = self.classify(api_error(404, "The API does not exist or has not been published in the environment",
                                      "APIGW.0101"))
        self.assertEqual((err.kind, err.severity), ("unavailable", "aviso"))

    def test_other_404_is_api_error(self):
        self.assertEqual(self.classify(api_error(404, "resource not found", "VPC.0202")).kind, "api")

    def test_server_errors_network_and_bugs_are_errors(self):
        self.assertEqual(self.classify(api_error(500, "Internal", "ECS.0500")).kind, "api")
        self.assertEqual(self.classify(api_error(503, "busy", "SYS.0503")).severity, "error")
        self.assertEqual(self.classify(sdk_exceptions.ConnectionException("getaddrinfo failed")).kind, "network")
        self.assertEqual(self.classify(sdk_exceptions.RequestTimeoutException("timeout")).kind, "network")
        self.assertEqual(self.classify(KeyError("x")).kind, "internal")


class TestLegacyInventoryWithObservedResponses(unittest.TestCase):
    """``/api/inventory`` con "todos" reproduciendo la prueba real."""

    def world_client(self):
        def deny(code):
            def handler(request):
                raise api_error(403, "Forbidden", code)
            return handler

        def vpn(request):
            raise api_error(401, VPN_401[1], VPN_401[0])

        def obs(request):
            return SimpleNamespace(buckets=SimpleNamespace(bucket=[]))

        return FakeClient(
            list_servers_details=lambda r: SimpleNamespace(servers=[real_server()], count=1),
            list_volumes=lambda r: SimpleNamespace(volumes=[real_volume()], count=1),
            list_vpcs=single_page("vpcs", real_vpc()),
            list_virsubnets=single_page("virsubnets", real_subnet()),
            list_security_groups=single_page("security_groups", real_security_group()),
            list_vgws=vpn, list_buckets=obs,
            list_publicips=lambda r: SimpleNamespace(publicips=[]),
            list_instances=lambda r: SimpleNamespace(instances=[], total_count=0, instance_num=0),
            list_vault=deny(OBSERVED_403["cbr"]), list_load_balancers=deny(OBSERVED_403["elb"]),
            list_nat_gateways=deny(OBSERVED_403["nat"]), list_alarm_rules=deny(OBSERVED_403["ces"]),
            list_protection_servers=deny(OBSERVED_403["hss"]), list_instance=deny(OBSERVED_403["waf"]),
            list_firewall_list=deny(OBSERVED_403["cfw"]),
        )

    def post(self, service):
        from app import app
        payload = {"ak": FAKE_AK, "sk": FAKE_SK, "project_id": FAKE_PROJECT, "region": REGION, "service": service}
        with mock.patch("core.engine.ClientFactory", lambda creds, **kw: FakeFactory(self.world_client())):
            return TestClient(app).post("/api/inventory", json=payload)

    def test_all_services_partial_result_with_warnings(self):
        response = self.post("todos")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        rows = {t["titulo"]: len(t["filas"]) for t in body["tables"]}
        self.assertEqual({k: rows[k] for k in ("ECS", "EVS - Volúmenes", "VPCs", "Subredes", "Security Groups")},
                         {"ECS": 1, "EVS - Volúmenes": 1, "VPCs": 1, "Subredes": 1, "Security Groups": 1})
        self.assertEqual(body["errores"], [])  # ningún 403/401 de permisos es un "error"
        warnings = {a["servicio"]: a for a in body["avisos"]}
        self.assertEqual(set(warnings), {"VPN", "CBR", "ELB", "NAT", "CES", "HSS", "WAF", "CFW"})
        vpn = warnings["VPN"]
        self.assertEqual((vpn["categoria"], vpn["http_status"], vpn["error_code"], vpn["accion_iam"]),
                         ("authorization", "401", "VPN.0003", "vpn:vpnGateways:list"))
        self.assertFalse(vpn["mensaje"].startswith(AUTH_PREFIX))  # no acusa a las credenciales
        self.assertNotIn(FAKE_SK, response.text)
        self.assertNotIn(FAKE_AK, response.text)

    def test_invalid_credentials_stop_after_first_service(self):
        calls = []

        def rejected(request):
            calls.append(request)
            raise api_error(401, "Incorrect IAM authentication information", "APIGW.0301")
        from app import app
        client = FakeClient(**{m: rejected for m in ("list_servers_details", "list_volumes", "list_vpcs")})
        payload = {"ak": FAKE_AK, "sk": FAKE_SK, "project_id": FAKE_PROJECT, "region": REGION, "service": "todos"}
        with mock.patch("core.engine.ClientFactory", lambda creds, **kw: FakeFactory(client)):
            body = TestClient(app).post("/api/inventory", json=payload).json()
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(body["errores"]), 1)
        self.assertTrue(body["errores"][0]["mensaje"].startswith(AUTH_PREFIX))
        self.assertIn("Se omitieron 14 servicio(s)", body["avisos"][0]["mensaje"])

    def test_vpn_alone_returns_warning_not_502(self):
        response = self.post("vpn")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["avisos"][0]["categoria"], "authorization")


class TestScanReproducingRealRun(SqliteTestCase):
    """Motor de escaneo con el mismo panorama: datos de ECS/EVS/VPC y avisos del resto."""

    def setUp(self):
        super().setUp()
        self.client, self.account, _ = seed_account(self.session, self.keyring,
                                                    projects_spec=(("p-sg", "ap-southeast-3"),))
        self.world = ScanWorld()
        self.world.servers = {"p-sg": [real_server()]}
        self.world.volumes = {"p-sg": [real_volume()]}
        self.world.extra.update({
            "list_vpcs": lambda reg, proj, req: SimpleNamespace(vpcs=[real_vpc()], page_info=None),
            "list_virsubnets": lambda reg, proj, req: SimpleNamespace(virsubnets=[real_subnet()], page_info=None),
            "list_security_groups": lambda reg, proj, req: SimpleNamespace(
                security_groups=[real_security_group()], page_info=None),
        })
        self.world.failures["list_vgws"] = api_error(401, VPN_401[1], VPN_401[0])
        self.world.failures["list_vault"] = api_error(403, "Forbidden", OBSERVED_403["cbr"])
        self.world.extra["list_vgws"] = lambda *a: None
        self.world.extra["list_vault"] = lambda *a: None

    def scan(self):
        with simulated_huawei(self.world):
            run_id = scan_account(self.factory, self.keyring, client_id=self.client.id,
                                  account_id=self.account.id, services=["ecs", "evs", "vpc", "vpn", "cbr"])
        self.session.expire_all()
        return self.session.get(ScanRun, run_id)

    def test_warnings_do_not_fail_the_scan(self):
        run = self.scan()
        self.assertEqual(run.status, "completed_with_warnings")
        self.assertEqual((run.total_errors, run.total_warnings, run.total_resources), (0, 2, 5))
        tasks = {t.service: t for t in self.session.query(ScanTask).filter_by(scan_run_id=run.id)}
        self.assertEqual((tasks["vpn"].status, tasks["vpn"].error_kind, tasks["vpn"].iam_action),
                         ("denied", "authorization", "vpn:vpnGateways:list"))
        self.assertEqual(tasks["vpn"].error_count, 0)
        self.assertEqual((tasks["cbr"].status, tasks["cbr"].error_kind), ("denied", "permission"))
        self.assertEqual({tasks[s].status for s in ("ecs", "evs", "vpc")}, {"succeeded"})
        types = sorted(r.resource_type for r in self.session.query(InventoryResource))
        self.assertEqual(types, ["ecs.server", "evs.volume", "vpc.security_group", "vpc.subnet", "vpc.vpc"])

    def test_repeated_scan_is_stable_and_never_deletes(self):
        self.scan()
        run = self.scan()
        self.assertEqual((run.total_created, run.total_updated, run.total_deleted), (0, 0, 0))
        self.assertTrue(all(r.deleted_at is None for r in self.session.query(InventoryResource)))
        stored = self.session.query(InventoryResource).filter_by(provider_id=SERVER_ID).one()
        self.assertEqual(stored.provider_created_at.replace(tzinfo=None), datetime(2026, 9, 30, 12, 0))
        self.assertEqual(stored.raw["OS-EXT-SRV-ATTR:user_data"], "[REDACTED]")

    def test_permission_loss_after_data_never_deletes(self):
        first = self.scan()
        self.world.failures["list_servers_details"] = api_error(403, "Forbidden", "Ecs.0403")
        run = self.scan()
        self.assertEqual(run.total_deleted, 0)
        server = self.session.query(InventoryResource).filter_by(provider_id=SERVER_ID).one()
        self.assertIsNone(server.deleted_at)
        self.assertEqual(server.last_run_id, first.id)  # el escaneo denegado no lo "vio" ni lo borró


if __name__ == "__main__":
    unittest.main()
