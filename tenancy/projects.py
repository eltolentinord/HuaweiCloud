# coding: utf-8
"""Proyectos Huawei de una cuenta: descubrimiento (IAM), sincronización y consulta."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional

from sqlalchemy.orm import Session

from core.clients import ClientFactory
from core.crypto import SecretCipher
from core.discovery import DiscoveredProject, IamProjectSource, ProjectSource, resolve_regions, sdk_region_ids
from core.errors import ServiceError, classify_exception
from db.models import CloudAccount, Project
from repositories import catalog as catalog_repo
from repositories import projects as repo
from tenancy.accounts import decrypt_credentials, get_account
from tenancy.catalog_sync import ensure_region
from tenancy.errors import NotFoundError

logger = logging.getLogger(__name__)

ProjectSourceFactory = Callable[[CloudAccount, ClientFactory], ProjectSource]


@dataclass
class ProjectSyncResult:
    created: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    skipped: List[dict] = field(default_factory=list)   # sin región resoluble
    missing: List[str] = field(default_factory=list)    # en BD pero no devueltos por IAM

    def summary(self) -> dict:
        return {
            "created": len(self.created), "updated": len(self.updated),
            "unchanged": len(self.unchanged), "skipped": self.skipped, "missing": self.missing,
        }


class DiscoveryFailedError(Exception):
    """La llamada a IAM falló; ``error`` contiene la clasificación segura."""

    def __init__(self, error: ServiceError) -> None:
        super().__init__(error.message)
        self.error = error


def _now() -> datetime:
    return datetime.now(timezone.utc)


def sync_projects(session: Session, account: CloudAccount, discovered: List[DiscoveredProject],
                  *, now: Optional[datetime] = None) -> ProjectSyncResult:
    """Crea/actualiza proyectos sin duplicados (clave: cuenta + huawei_project_id).

    - No borra ni desactiva proyectos que IAM ya no devuelve: los informa en
      ``missing`` (la detección de cambios llegará con el histórico).
    - ``is_enabled`` es una decisión local del administrador: solo se fija al crear.
    """
    now = now or _now()
    known = catalog_repo.region_ids(session) | sdk_region_ids()
    resolved, skipped = resolve_regions(discovered, known)
    result = ProjectSyncResult(skipped=[{"id": p.id, "name": p.name} for p in skipped])
    existing = repo.by_huawei_id(session, account.id)

    processed = set()
    for item in resolved:
        project = item.project
        if project.id in processed:  # la misma respuesta puede repetir un proyecto
            continue
        processed.add(project.id)
        ensure_region(session, item.region_id, endpoint_domain=account.endpoint_domain)
        values = {"name": project.name, "region_id": item.region_id,
                  "parent_huawei_project_id": project.parent_id, "huawei_enabled": project.enabled}
        row = existing.get(project.id)
        if row is None:
            existing[project.id] = Project(account_id=account.id, huawei_project_id=project.id,
                                           discovered_at=now, is_enabled=project.enabled is not False,
                                           **values)
            session.add(existing[project.id])
            result.created.append(project.id)
            continue
        changed = any(getattr(row, k) != v for k, v in values.items())
        for key, value in values.items():
            setattr(row, key, value)
        row.discovered_at = now
        (result.updated if changed else result.unchanged).append(project.id)

    seen = processed | {p.id for p in skipped}
    result.missing = sorted(set(existing) - seen)
    if not account.huawei_domain_id:
        account.huawei_domain_id = next((p.domain_id for p in discovered if p.domain_id), None)
    session.flush()
    return result


def iam_source(account: CloudAccount, clients: ClientFactory) -> ProjectSource:
    return IamProjectSource(clients, region_id=account.iam_region_id, domain_id=account.huawei_domain_id)


def discover_projects(session: Session, cipher: SecretCipher, client_id: uuid.UUID,
                      account_id: uuid.UUID, *,
                      source_factory: ProjectSourceFactory = iam_source) -> ProjectSyncResult:
    """Descubre y sincroniza proyectos. Sirve también como validación de la cuenta.

    Éxito → ``status='active'`` y ``last_validated_at``. Fallo de autenticación →
    ``status='invalid'``. El mensaje guardado está redactado (nunca AK/SK).
    """
    account = get_account(session, client_id, account_id)
    credentials = decrypt_credentials(account, cipher)
    clients = ClientFactory(credentials, endpoint_domain=account.endpoint_domain)
    try:
        discovered = source_factory(account, clients).list_projects()
    except Exception as exc:
        error = classify_exception(exc, service="iam", region=account.iam_region_id,
                                   secrets=credentials.secrets)
        account.last_validation_error = error.message
        if error.kind == "authentication":
            account.status = "invalid"
        session.flush()
        logger.warning("Descubrimiento IAM fallido %s", error.to_log())
        raise DiscoveryFailedError(error) from None

    result = sync_projects(session, account, discovered)
    account.status = "active"
    account.last_validated_at = _now()
    account.last_validation_error = None
    session.flush()
    logger.info("Descubrimiento IAM cuenta=%s %s", account.id,
                {k: v for k, v in result.summary().items() if k in ("created", "updated", "unchanged")})
    return result


def list_projects(session: Session, client_id: uuid.UUID, account_id: uuid.UUID) -> List[Project]:
    get_account(session, client_id, account_id)
    return repo.list_for_account(session, account_id)


def get_project(session: Session, client_id: uuid.UUID, account_id: uuid.UUID,
                project_id: uuid.UUID) -> Project:
    project = repo.get_for_client(session, client_id, account_id, project_id)
    if project is None:
        raise NotFoundError("Proyecto no encontrado.")
    return project


def set_project_enabled(session: Session, client_id: uuid.UUID, account_id: uuid.UUID,
                        project_id: uuid.UUID, enabled: bool) -> Project:
    project = get_project(session, client_id, account_id, project_id)
    project.is_enabled = enabled
    session.flush()
    return project


def add_project(session: Session, client_id: uuid.UUID, account_id: uuid.UUID, *,
                huawei_project_id: str, region_id: str, name: Optional[str] = None) -> Project:
    """Alta manual (sin IAM) para cuentas cuyo usuario no puede listar proyectos."""
    account = get_account(session, client_id, account_id)
    existing = repo.get_by_huawei_id(session, account.id, huawei_project_id)
    if existing is not None:
        return existing
    ensure_region(session, region_id, endpoint_domain=account.endpoint_domain)
    project = Project(account_id=account.id, huawei_project_id=huawei_project_id,
                      region_id=region_id, name=name or region_id)
    session.add(project)
    session.flush()
    return project
