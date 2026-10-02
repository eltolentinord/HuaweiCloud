# coding: utf-8
"""Compatibilidad del formato de tablas/KPIs con el frontend y la fachada inventory.py."""

import unittest
from types import SimpleNamespace
from unittest import mock

from tests.helpers import FAKE_AK, FAKE_PROJECT, FAKE_SK, REGION, FakeClient, FakeFactory, api_error, context

from collectors.ecs import EcsCollector
from core.models import Resource
from mock_data_MOCK import MOCK_RESPONSE
from presentation.tables import TABLE_SPECS, build_service_view
from tests.test_collectors import ECS_SERVER
import inventory

# Columnas exactas del inventory.py original (contrato con static/app.js y Excel).
LEGACY_COLUMNS = {
    "ECS": ["Nombre ECS", "Estado", "Región", "Zona AZ", "Flavor", "vCPU", "RAM (GB)", "IP privada",
            "IP pública", "Security Group", "Sistema operativo", "Fecha de creación"],
    "EVS - Volúmenes": ["Nombre", "Estado", "Región", "Zona AZ", "Tamaño (GB)", "Tipo", "Server ID",
                        "Fecha de creación"],
    "VPCs": ["Nombre", "Estado", "CIDR", "Región", "Fecha de creación"],
    "Subredes": ["Nombre", "Estado", "CIDR", "Gateway IP", "Zona AZ", "Fecha de creación"],
    "Security Groups": ["Nombre", "ID", "Descripción", "Región"],
    "VPN Gateways": ["Nombre", "ID", "Estado", "Nombre de conexión", "Fecha de creación"],
    "Conexiones VPN": ["Nombre", "ID", "Estado", "Subredes locales", "Subredes remotas", "Fecha de creación"],
    "Buckets OBS": ["Nombre", "Ubicación", "Fecha de creación"],
    "Elastic IPs": ["IP pública", "IP privada", "Estado", "Puerto ID", "ID", "Región", "Creado"],
    "CBR - Vaults": ["Nombre", "ID", "Descripción", "AZ", "Región", "Creado"],
    "ELB - Load Balancers": ["Nombre", "Estado", "IP VIP", "VPC ID", "AZs", "Región", "Creado"],
    "RDS - Instancias": ["Nombre", "Estado", "Motor", "Versión", "Tipo", "Región", "VPC ID", "Creado"],
    "DCS - Instancias": ["Nombre", "Estado", "Motor", "Versión", "Spec", "Puerto", "IP", "AZs", "Región"],
    "NAT Gateways": ["Nombre", "Estado", "Spec", "Router ID", "Red", "IP NAT", "Región", "Creado"],
    "Cloud Eye - Alarmas": ["Nombre", "Alarm ID", "Habilitada", "Namespace", "Producto", "Región"],
    "HSS - Servidores protegidos": ["Hostname", "IP", "SO", "Protección", "Estado host", "Agente", "Región"],
    "WAF - Instancias": ["Nombre", "ID", "Estado", "Run status", "Acceso", "Región", "Zona", "Service IP", "VPC ID"],
    "CFW - Firewalls": ["Nombre", "ID", "Estado", "Tipo Servicio", "Tipo HA", "Motor", "Flavor", "Región"],
}


def resource(**kw):
    base = dict(service="obs", resource_type="obs.bucket", provider_id="b", name="b", status=None,
                region=REGION, project_id=FAKE_PROJECT)
    base.update(kw)
    return Resource(**base)


class TestTableContract(unittest.TestCase):
    def test_titles_and_columns_unchanged(self):
        specs = {spec.title: list(c for c, _ in spec.columns) for specs in TABLE_SPECS.values() for spec in specs}
        self.assertEqual(specs, LEGACY_COLUMNS)

    def test_ecs_columns_match_mock_data(self):
        self.assertEqual(LEGACY_COLUMNS["ECS"], MOCK_RESPONSE["tables"][0]["columnas"])

    def test_ecs_row_and_summary(self):
        client = FakeClient(list_servers_details=lambda r: SimpleNamespace(servers=[ECS_SERVER, {"id": "s2"}], count=2))
        resources = EcsCollector().collect(context(client)).resources
        tables, summary = build_service_view("ecs", resources)
        row = tables[0]["filas"][0]
        self.assertEqual(row["Nombre ECS"], ECS_SERVER["name"])
        self.assertEqual(row["Sistema operativo"], "Ubuntu 22.04 server 64bit")
        self.assertEqual(row["IP pública"], "47.0.0.1")
        self.assertEqual(row["Security Group"], "web-sg, ssh-sg")
        self.assertEqual((row["vCPU"], row["RAM (GB)"]), (2, 4.0))
        self.assertEqual(row["_detalle"]["ECS ID"], "srv-1")
        self.assertEqual(row["_detalle"]["Tags"], "env=prod, team")
        empty = tables[0]["filas"][1]
        self.assertEqual((empty["Nombre ECS"], empty["IP privada"], empty["IP pública"]), ("-", "-", "-"))
        self.assertEqual(summary, [
            {"label": "Total ECS", "value": 2}, {"label": "ECS activas", "value": 1},
            {"label": "ECS apagadas", "value": 0}, {"label": "Total vCPU", "value": 2},
            {"label": "Total RAM (GB)", "value": 4.0},
        ])

    def test_generic_detail_keeps_first_40_raw_keys(self):
        raw = {f"k{i}": i for i in range(60)}
        tables, _ = build_service_view("obs", [resource(raw=raw)])
        self.assertEqual(len(tables[0]["filas"][0]["_detalle"]), 40)

    def test_empty_service_still_returns_table(self):
        tables, summary = build_service_view("vpc", [])
        self.assertEqual([t["titulo"] for t in tables], ["VPCs", "Subredes", "Security Groups"])
        self.assertTrue(all(t["filas"] == [] for t in tables))
        self.assertEqual([s["value"] for s in summary], [0, 0, 0])


class TestFacade(unittest.TestCase):
    """``inventory.consultar_servicio`` con el SDK simulado de extremo a extremo."""

    def run_facade(self, service, client):
        with mock.patch("core.engine.ClientFactory", lambda creds, **kw: FakeFactory(client)):
            return inventory.consultar_servicio(service, FAKE_AK, FAKE_SK, FAKE_PROJECT, REGION)

    def test_obs_shows_only_requested_region_and_counts_others(self):
        buckets = SimpleNamespace(bucket=[
            {"name": "here", "location": REGION}, {"name": "there", "location": "sa-brazil-1"},
            {"name": "unknown", "location": ""},
        ])
        result = self.run_facade("obs", FakeClient(list_buckets=lambda r: SimpleNamespace(buckets=buckets)))
        self.assertEqual([f["Nombre"] for f in result["tablas"][0]["filas"]], ["here"])
        self.assertIn({"label": "Buckets en otras regiones", "value": 2}, result["resumen"])

    def test_error_and_warning_shapes(self):
        def fail(status):
            def handler(request):
                raise api_error(status, "denied", "APIGW.0301" if status == 401 else "TEST.0403")
            return handler
        result = self.run_facade("ecs", FakeClient(list_servers_details=fail(401)))
        self.assertEqual(result["tablas"], [])
        self.assertEqual(result["errores"][0]["tipo"], "error")
        self.assertEqual(result["errores"][0]["servicio"], "ECS")
        result = self.run_facade("ecs", FakeClient(list_servers_details=fail(403)))
        self.assertEqual(result["errores"][0]["tipo"], "aviso")

    def test_all_services_continue_after_failures(self):
        result = self.run_facade("todos", FakeClient(
            list_servers_details=lambda r: SimpleNamespace(servers=[{"id": "s1"}], count=1)))
        # ECS responde; el resto falla (el cliente falso no tiene sus métodos) sin detener la consulta.
        self.assertEqual(result["tablas"][0]["titulo"], "ECS")
        self.assertEqual(len(result["errores"]), 14)
        self.assertNotIn(FAKE_SK, str(result))


if __name__ == "__main__":
    unittest.main()
