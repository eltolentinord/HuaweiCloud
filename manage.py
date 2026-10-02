# coding: utf-8
"""Administración por línea de comandos (requiere DATABASE_URL e INVENTORY_ENCRYPTION_KEYS).

    python manage.py keys generate                  # nueva clave maestra (guárdala fuera del repo)
    python manage.py keys reencrypt                 # recifra cuentas con la clave actual
    python manage.py catalog sync                   # regiones y servicios desde core/catalog.py
    python manage.py client create "Acme S.A." [--slug acme]
    python manage.py client list
    python manage.py account create acme "Producción" [--domain-id ...] [--from-env]
    python manage.py account list acme
    python manage.py account discover acme <ACCOUNT_ID>
    python manage.py project list acme <ACCOUNT_ID>
    python manage.py project add acme <ACCOUNT_ID> <HUAWEI_PROJECT_ID> <REGION>
    python manage.py scan run acme <ACCOUNT_ID> [--service ecs] [--region la-north-2] [--project <ID>] [--workers 4]
    python manage.py scan show acme <SCAN_ID>
    python manage.py schedule create acme <ACCOUNT_ID> --every 360 [--service ecs] [--region ...]
    python manage.py schedule list acme <ACCOUNT_ID>
    python manage.py schedule disable acme <ACCOUNT_ID> <SCHEDULE_ID>
    python manage.py worker [--once] [--poll 30]     # ejecuta programaciones y escaneos en cola

AK/SK se piden con getpass (no quedan en el historial de la consola). Con
``--from-env`` se leen de HUAWEI_AK / HUAWEI_SK. Nunca se imprimen.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import uuid

from core.credentials import ENV_AK, ENV_SK, EnvCredentialProvider
from core.crypto import FernetKeyring, generate_key
from db.session import get_session_factory, session_scope
from repositories import scans as scans_repo
from scanning.engine import scan_account
from scanning.settings import ScanSettings
from tenancy import accounts, clients, projects, schedules
from tenancy.catalog_sync import sync_catalog
from tenancy.errors import TenancyError
from tenancy.projects import DiscoveryFailedError


def _client_id(session, slug: str) -> uuid.UUID:
    return clients.get_client_by_slug(session, slug).id


def _read_secrets(from_env: bool):
    if from_env:
        env = EnvCredentialProvider()
        return env.value(ENV_AK) or "", env.value(ENV_SK) or ""
    return getpass.getpass("Access Key (AK): "), getpass.getpass("Secret Key (SK): ")


def cmd_keys(args) -> None:
    if args.action == "generate":
        print(generate_key())
        print("Guárdala como INVENTORY_ENCRYPTION_KEYS=\"<version>:<clave>\" fuera del repositorio.",
              file=sys.stderr)
        return
    with session_scope() as session:
        print(f"Cuentas recifradas: {accounts.reencrypt_all(session, FernetKeyring.from_env())}")


def cmd_catalog(args) -> None:
    with session_scope() as session:
        print(sync_catalog(session))


def cmd_client(args) -> None:
    with session_scope() as session:
        if args.action == "create":
            client = clients.create_client(session, name=args.name, slug=args.slug)
            print(f"{client.id}  {client.slug}  {client.name}")
        else:
            for client in clients.list_clients(session):
                print(f"{client.id}  {client.slug:<24} {client.status:<10} {client.name}")


def cmd_account(args) -> None:
    with session_scope() as session:
        client_id = _client_id(session, args.client)
        if args.action == "create":
            ak, sk = _read_secrets(args.from_env)
            account = accounts.create_account(
                session, FernetKeyring.from_env(), client_id=client_id, name=args.name, ak=ak, sk=sk,
                huawei_domain_id=args.domain_id, endpoint_domain=args.endpoint_domain,
                iam_region_id=args.iam_region)
            print(f"{account.id}  {account.name}  status={account.status}")
        elif args.action == "list":
            for account in accounts.list_accounts(session, client_id):
                print(f"{account.id}  {account.name:<24} {account.status:<9} key_v{account.key_version}")
        else:
            try:
                result = projects.discover_projects(session, FernetKeyring.from_env(), client_id,
                                                    uuid.UUID(args.account_id))
            except DiscoveryFailedError as exc:
                session.commit()
                raise SystemExit(f"Descubrimiento fallido: {exc.error.message}")
            print(result.summary())


def cmd_project(args) -> None:
    with session_scope() as session:
        client_id = _client_id(session, args.client)
        if args.action == "add":
            p = projects.add_project(session, client_id, uuid.UUID(args.account_id),
                                     huawei_project_id=args.huawei_project_id, region_id=args.region)
            print(f"{p.id}  {p.region_id}  {p.huawei_project_id}")
            return
        for p in projects.list_projects(session, client_id, uuid.UUID(args.account_id)):
            print(f"{p.id}  {p.region_id:<16} {p.huawei_project_id}  {p.name or ''}"
                  f"{'' if p.is_enabled else '  (deshabilitado)'}")


def cmd_scan(args) -> None:
    with session_scope() as session:
        client_id = _client_id(session, args.client)
    if args.action == "run":
        scan_id = scan_account(get_session_factory(), FernetKeyring.from_env(), client_id=client_id,
                               account_id=uuid.UUID(args.target_id), services=args.service or None,
                               regions=args.region or None,
                               project_ids=[uuid.UUID(p) for p in args.project] if args.project else None,
                               trigger="cli", settings=ScanSettings.from_env(max_workers=args.workers))
        args.target_id = str(scan_id)
    with session_scope() as session:
        run = scans_repo.get_for_client(session, client_id, uuid.UUID(args.target_id))
        if run is None:
            raise SystemExit("Error: escaneo no encontrado.")
        print(f"{run.id}  {run.status}  recursos={run.total_resources} +{run.total_created} "
              f"~{run.total_updated} -{run.total_deleted} errores={run.total_errors} ({run.duration_ms} ms)")
        for task in scans_repo.tasks(session, run.id):
            detail = f"  {task.error_kind}: {task.error_message_safe}" if task.error_message_safe else ""
            print(f"  {task.service:<5} {task.region:<16} {task.status:<10} {task.resource_count:>5}{detail}")


def cmd_schedule(args) -> None:
    with session_scope() as session:
        client_id = _client_id(session, args.client)
        account_id = uuid.UUID(args.account_id)
        if args.action == "create":
            item = schedules.create_schedule(session, client_id=client_id, account_id=account_id,
                                             interval_minutes=args.every, name=args.name,
                                             services=args.service, regions=args.region)
            print(f"{item.id}  cada {item.interval_minutes} min  próxima: {item.next_run_at:%Y-%m-%d %H:%M} UTC")
        elif args.action == "disable":
            schedules.update_schedule(session, client_id, account_id, uuid.UUID(args.schedule_id), enabled=False)
            print("Programación deshabilitada")
        else:
            for item in schedules.list_schedules(session, client_id, account_id):
                print(f"{item.id}  {'on ' if item.enabled else 'off'}  cada {item.interval_minutes:>5} min  "
                      f"próxima {item.next_run_at:%Y-%m-%d %H:%M}  último={item.last_status or '-'}  {item.name}")


def cmd_worker(args) -> None:
    from scanning import worker

    factory, cipher = get_session_factory(), FernetKeyring.from_env()
    settings = ScanSettings.from_env(max_workers=args.workers)
    if args.once:
        report = worker.run_once(factory, cipher, settings=settings)
        print(f"recuperados={report.recovered} encolados={report.queued} omitidos={report.skipped_active} "
              f"errores={report.schedule_errors} ejecutados={len(report.executed)}")
        return
    try:
        worker.run_forever(factory, cipher, poll_seconds=args.poll, settings=settings)
    except KeyboardInterrupt:
        print("Worker detenido")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Administración de Huawei Cloud Inventory")
    sub = parser.add_subparsers(dest="command", required=True)

    keys = sub.add_parser("keys")
    keys.add_argument("action", choices=["generate", "reencrypt"])
    keys.set_defaults(func=cmd_keys)

    catalog = sub.add_parser("catalog")
    catalog.add_argument("action", choices=["sync"])
    catalog.set_defaults(func=cmd_catalog)

    client = sub.add_parser("client")
    client_sub = client.add_subparsers(dest="action", required=True)
    create = client_sub.add_parser("create")
    create.add_argument("name")
    create.add_argument("--slug")
    client_sub.add_parser("list")
    client.set_defaults(func=cmd_client)

    account = sub.add_parser("account")
    account_sub = account.add_subparsers(dest="action", required=True)
    create = account_sub.add_parser("create")
    create.add_argument("client", help="slug del cliente")
    create.add_argument("name")
    create.add_argument("--domain-id")
    create.add_argument("--endpoint-domain", default="myhuaweicloud.com")
    create.add_argument("--iam-region")
    create.add_argument("--from-env", action="store_true", help="leer AK/SK de HUAWEI_AK/HUAWEI_SK")
    listing = account_sub.add_parser("list")
    listing.add_argument("client")
    discover = account_sub.add_parser("discover")
    discover.add_argument("client")
    discover.add_argument("account_id")
    account.set_defaults(func=cmd_account)

    project = sub.add_parser("project")
    project_sub = project.add_subparsers(dest="action", required=True)
    listing = project_sub.add_parser("list")
    listing.add_argument("client")
    listing.add_argument("account_id")
    add = project_sub.add_parser("add", help="alta manual si el usuario IAM no puede listar proyectos")
    add.add_argument("client")
    add.add_argument("account_id")
    add.add_argument("huawei_project_id")
    add.add_argument("region")
    project.set_defaults(func=cmd_project)

    scan = sub.add_parser("scan")
    scan_sub = scan.add_subparsers(dest="action", required=True)
    run = scan_sub.add_parser("run")
    run.add_argument("client")
    run.add_argument("target_id", metavar="account_id")
    run.add_argument("--service", action="append", help="repetible; por defecto todos")
    run.add_argument("--region", action="append", help="repetible; por defecto todas las de la cuenta")
    run.add_argument("--project", action="append", help="ID interno del proyecto (ver 'project list'); repetible")
    run.add_argument("--workers", type=int, help="tareas simultáneas (1 = secuencial; máx. 16)")
    show = scan_sub.add_parser("show")
    show.add_argument("client")
    show.add_argument("target_id", metavar="scan_id")
    scan.set_defaults(func=cmd_scan)

    schedule = sub.add_parser("schedule")
    schedule_sub = schedule.add_subparsers(dest="action", required=True)
    create = schedule_sub.add_parser("create")
    create.add_argument("client")
    create.add_argument("account_id")
    create.add_argument("--every", type=int, required=True, help="minutos entre escaneos (15 – 10080)")
    create.add_argument("--name")
    create.add_argument("--service", action="append")
    create.add_argument("--region", action="append")
    listing = schedule_sub.add_parser("list")
    listing.add_argument("client")
    listing.add_argument("account_id")
    disable = schedule_sub.add_parser("disable")
    disable.add_argument("client")
    disable.add_argument("account_id")
    disable.add_argument("schedule_id")
    schedule.set_defaults(func=cmd_schedule)

    worker = sub.add_parser("worker", help="procesa programaciones y escaneos en cola (proceso aparte)")
    worker.add_argument("--once", action="store_true", help="un solo ciclo y termina")
    worker.add_argument("--poll", type=int, default=30, help="segundos entre ciclos")
    worker.add_argument("--workers", type=int, help="tareas simultáneas por escaneo")
    worker.set_defaults(func=cmd_worker)
    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except TenancyError as exc:
        raise SystemExit(f"Error: {exc.message}")


if __name__ == "__main__":
    main()
