# coding: utf-8
"""Dobles de prueba para el motor de escaneo: SDK simulado por región/proyecto."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest import mock

from tests.helpers import FAKE_AK, FAKE_SK

from core.throttling import CallGate, GuardedClient, RetryCounter, RetryPolicy

from tenancy import accounts, clients, projects
from tenancy.catalog_sync import sync_catalog

Handler = Callable[[str, str, Any], Any]  # (region, project_id, request) -> response


class ScanWorld:
    """Estado simulado de Huawei Cloud que los tests modifican entre escaneos."""

    def __init__(self) -> None:
        self.servers: Dict[str, List[dict]] = {}   # huawei_project_id -> servidores ECS
        self.volumes: Dict[str, List[dict]] = {}   # huawei_project_id -> volúmenes EVS
        self.buckets: List[dict] = []              # OBS (cuenta)
        self.failures: Dict[str, Exception] = {}   # método -> excepción
        self.calls: List[Tuple[str, str, str]] = []
        self.extra: Dict[str, Handler] = {}

    def handlers(self) -> Dict[str, Handler]:
        def ecs(region, project, request):
            servers = self.servers.get(project, [])
            page, size = max(request.offset or 1, 1), request.limit
            return SimpleNamespace(servers=servers[(page - 1) * size: page * size], count=len(servers))

        def evs(region, project, request):
            volumes = self.volumes.get(project, [])
            return SimpleNamespace(volumes=volumes[request.offset: request.offset + request.limit],
                                   count=len(volumes))

        def obs(region, project, request):
            return SimpleNamespace(buckets=SimpleNamespace(bucket=list(self.buckets)))

        return {"list_servers_details": ecs, "list_volumes": evs, "list_buckets": obs, **self.extra}


class _BoundClient:
    def __init__(self, world: ScanWorld, region: str, project: str) -> None:
        self._world, self._region, self._project = world, region, project

    def __getattr__(self, method: str):
        handlers = self._world.handlers()
        if method not in handlers:
            raise AttributeError(method)

        def call(request):
            self._world.calls.append((method, self._region, self._project))
            if method in self._world.failures:
                raise self._world.failures[method]
            return handlers[method](self._region, self._project, request)
        return call

    def close(self):
        pass


def _no_sleep(seconds: float) -> None:
    """Los reintentos por throttling no esperan de verdad en los tests."""


class WorldFactory:
    """Sustituye a ``ClientFactory`` dentro de ``scanning.engine``.

    Los clientes simulados pasan por la MISMA protección que los reales
    (``GuardedClient``: gate global + reintentos por throttling).
    """

    def __init__(self, world: ScanWorld, credentials, *, gate: CallGate) -> None:
        self.world = world
        self.secrets = credentials.secrets
        self.retries = RetryCounter()
        self._policy = RetryPolicy(sleep=_no_sleep)
        self._gate = gate

    def _guard(self, client):
        return GuardedClient(client, policy=self._policy, gate=self._gate, counter=self.retries)

    def create(self, client_cls, region_cls, prefix, region_id, project_id):
        return self._guard(_BoundClient(self.world, region_id, project_id))

    def create_obs(self, region_id):
        return self._guard(_BoundClient(self.world, region_id, ""))


@contextmanager
def simulated_huawei(world: ScanWorld, *, gate: Optional[CallGate] = None):
    gate = gate or CallGate(32)
    with mock.patch("scanning.engine.ClientFactory", lambda creds, **kw: WorldFactory(world, creds, gate=gate)):
        yield world


def seed_account(session, keyring, *, client_name="Acme", account_name="prod",
                 projects_spec=(("p-sg", "ap-southeast-3"), ("p-mx", "la-north-2"))):
    """Cliente + cuenta (AK/SK falsas cifradas) + proyectos habilitados."""
    sync_catalog(session)
    client = clients.get_client_by_slug(session, clients.slugify(client_name)) \
        if any(c.name == client_name for c in clients.list_clients(session)) \
        else clients.create_client(session, name=client_name)
    account = accounts.create_account(session, keyring, client_id=client.id, name=account_name,
                                      ak=FAKE_AK, sk=FAKE_SK)
    rows = {hid: projects.add_project(session, client.id, account.id, huawei_project_id=hid, region_id=region)
            for hid, region in projects_spec}
    session.commit()
    return client, account, rows


def server(sid: str, name: str = None, **extra) -> dict:
    return {"id": sid, "name": name or sid, "status": "ACTIVE", "created": "2026-01-15T10:22:00Z",
            "flavor": {"vcpus": "2", "ram": 4096}, **extra}
