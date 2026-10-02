# coding: utf-8
"""Verificación REAL controlada contra Huawei Cloud (solo lectura, opt-in).

Uso (PowerShell, credenciales SOLO en variables de entorno):

    $env:HUAWEI_AK = "..." ; $env:HUAWEI_SK = "..."
    $env:HUAWEI_REGION = "ap-southeast-3"          # opcional
    $env:HUAWEI_PROJECT_ID = "..."                 # opcional: si falta se intenta IAM
    python scripts/live_check.py [--service ecs --service vpn ...]

Qué hace:
- Ejecuta los collectors indicados (por defecto ECS, EVS, VPC y VPN) con las mismas
  piezas que la aplicación (ClientFactory → collector → normalización → clasificación).
- Solo llama a operaciones ``list_*``/``keystone_list_auth_projects``: no modifica nada.
- Imprime SOLO conteos por tipo, categoría de error, código, acción IAM y comprobaciones
  de coherencia (sí/no). Nunca imprime AK/SK, nombres, IDs, IPs ni el Project ID completo.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collectors import get_collector  # noqa: E402
from collectors.base import CollectorContext  # noqa: E402
from core.clients import ClientFactory  # noqa: E402
from core.credentials import DEFAULT_REGION, ENV_PROJECT_ID, ENV_REGION, EnvCredentialProvider  # noqa: E402
from core.discovery import IamProjectSource  # noqa: E402
from core.engine import run_collector  # noqa: E402
from core.errors import classify_exception, mask_project_id_ascii  # noqa: E402

DEFAULT_SERVICES = ["ecs", "evs", "vpc", "vpn"]


def resolve_project(env: EnvCredentialProvider, clients: ClientFactory, region: str) -> str:
    project_id = env.value(ENV_PROJECT_ID)
    if project_id:
        print(f"Project ID: variable de entorno ({mask_project_id_ascii(project_id)})")
        return project_id
    print("Project ID no definido: se intenta descubrir con IAM KeystoneListAuthProjects...")
    try:
        projects = IamProjectSource(clients, region_id=region).list_projects()
    except Exception as exc:
        error = classify_exception(exc, service="iam", region=region, secrets=clients.secrets)
        detail = f"IAM: {error.kind} HTTP {error.http_status} {error.error_code or ''} {error.iam_action or ''}"
        if error.kind == "authentication":
            raise SystemExit(f"{detail}\nLas credenciales del entorno fueron RECHAZADAS por Huawei Cloud "
                             "(AK inexistente/revocada o SK incorrecta). Revisa HUAWEI_AK/HUAWEI_SK.")
        raise SystemExit(f"{detail}\nSin permiso para listar proyectos: define HUAWEI_PROJECT_ID.")
    match = next((p for p in projects if p.name == region), None)
    print(f"IAM: {len(projects)} proyecto(s) visibles; proyecto de sistema de {region}: "
          f"{'encontrado' if match else 'NO encontrado'}")
    if match is None:
        raise SystemExit("Define HUAWEI_PROJECT_ID")
    return match.id


def coherence(resources_by_service) -> None:
    servers = [r for r in resources_by_service.get("ecs", []) if r.resource_type == "ecs.server"]
    volumes = {r.provider_id for r in resources_by_service.get("evs", [])}
    vpcs = {r.provider_id for r in resources_by_service.get("vpc", []) if r.resource_type == "vpc.vpc"}
    sgs = {r.name for r in resources_by_service.get("vpc", []) if r.resource_type == "vpc.security_group"}
    if not servers:
        return
    print("\nCoherencia entre servicios (sí/no, sin mostrar datos):")
    for index, server in enumerate(servers, start=1):
        a = server.attributes
        print(f"  ECS #{index}: discos en EVS={set(a['volume_ids']) <= volumes if a['volume_ids'] else 'sin discos'}"
              f" | red en VPC={bool(set(a['networks']) & vpcs) if vpcs else 'sin VPC'}"
              f" | SG existentes={set(a['security_groups']) <= sgs if sgs else 'sin SG'}"
              f" | SO detectado={'sí' if a.get('os') else 'no'}")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--service", action="append", help="repetible; por defecto ecs, evs, vpc, vpn")
    args = parser.parse_args(argv)

    env = EnvCredentialProvider()
    try:
        credentials = env.get_credentials()
    except ValueError:
        raise SystemExit("Define HUAWEI_AK y HUAWEI_SK en el entorno (nunca en el código ni en el chat).")
    region = env.value(ENV_REGION, DEFAULT_REGION)
    clients = ClientFactory(credentials)
    print(f"Región: {region}")
    project_id = resolve_project(env, clients, region)

    ctx = CollectorContext(clients=clients, region=region, project_id=project_id)
    found = {}
    print("\nServicio  Resultado        Recursos por tipo / error")
    for service in args.service or DEFAULT_SERVICES:
        run = run_collector(get_collector(service), ctx)
        if run.error:
            e = run.error
            print(f"  {service:<7} {e.severity}:{e.kind:<14} HTTP {e.http_status} {e.error_code or ''} "
                  f"{('accion=' + e.iam_action) if e.iam_action else ''} ({run.duration_ms} ms)")
            continue
        found[service] = run.resources
        counts = {}
        for r in run.resources:
            counts[r.resource_type] = counts.get(r.resource_type, 0) + 1
        state = "ok" if run.complete else "ok-parcial"
        print(f"  {service:<7} {state:<20} {counts or 'sin recursos'} ({run.duration_ms} ms)")
    coherence(found)


if __name__ == "__main__":
    main()
