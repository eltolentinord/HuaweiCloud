# coding: utf-8
"""IAM Discovery: validar la identidad IAM de una cuenta Huawei y descubrir su alcance.

Conceptos (no mezclar): el *cliente/tenant* es de ESTA plataforma; la *cuenta*,
la identidad *IAM*, los *Projects*, las *Regions* y los *Enterprise Projects* son de
HUAWEI CLOUD.

- ``validate_credentials``: comprueba AK/SK autenticando contra IAM (lista los
  proyectos visibles) SIN guardar nada salvo el estado de la cuenta.
- ``discover``: Projects (IAM ``KeystoneListAuthProjects``) → Regions (las de esos
  proyectos) → Enterprise Projects (EPS ``ListEnterpriseProject``), sincronizados en
  PostgreSQL. Cada paso es independiente: un 403 se registra como
  "Permiso insuficiente" y el descubrimiento continúa. Solo un fallo de
  AUTENTICACIÓN (AK/SK inválidas) detiene el proceso.
- ``account_permissions``: qué puede y qué no puede consultar la identidad IAM,
  según el último descubrimiento y las últimas tareas de escaneo de cada servicio.

No crea un inventario paralelo: los recursos los sigue obteniendo el motor de
escaneo existente con los Projects descubiertos. Solo lectura sobre Huawei Cloud.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.catalog import ALL_SERVICES, SERVICIOS
from core.clients import ClientFactory
from core.crypto import SecretCipher
from core.discovery import DiscoveredEnterpriseProject, EnterpriseProjectSource, EpsEnterpriseProjectSource
from core.errors import AUTHENTICATION, AUTHORIZATION, PERMISSION, UNAVAILABLE, ServiceError, classify_exception
from db.models import CloudAccount, EnterpriseProject
from repositories import projects as projects_repo
from repositories import scans as scans_repo
from scanning.coverage import service_coverage
from tenancy.accounts import decrypt_credentials, get_account
from tenancy.projects import (
    DiscoveryFailedError,
    ProjectSourceFactory,
    ProjectSyncResult,
    iam_source,
    sync_projects,
)

logger = logging.getLogger(__name__)

PERMISSION_DENIED = "Permiso insuficiente"
STEP_OK, STEP_DENIED, STEP_UNAVAILABLE, STEP_FAILED, STEP_SKIPPED = "ok", "denied", "unavailable", "failed", "skipped"
EnterpriseSourceFactory = Callable[[CloudAccount, ClientFactory], EnterpriseProjectSource]


def eps_source(account: CloudAccount, clients: ClientFactory) -> EnterpriseProjectSource:
    return EpsEnterpriseProjectSource(clients, endpoint_domain=account.endpoint_domain,
                                      domain_id=account.huawei_domain_id)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- resultados
@dataclass
class StepResult:
    """Resultado de un paso del descubrimiento (mensajes ya redactados: nunca AK/SK)."""

    status: str
    count: int = 0
    message: Optional[str] = None
    iam_action: Optional[str] = None
    http_status: Optional[int] = None
    error_code: Optional[str] = None

    @classmethod
    def from_error(cls, error: ServiceError) -> "StepResult":
        if error.kind in (AUTHORIZATION, PERMISSION):
            status = STEP_DENIED
            message = PERMISSION_DENIED + (f" (acción IAM {error.iam_action})" if error.iam_action else "")
        elif error.kind == UNAVAILABLE:
            status, message = STEP_UNAVAILABLE, "Servicio no disponible para esta cuenta o región."
        else:
            status, message = STEP_FAILED, error.message
        return cls(status=status, message=message, iam_action=error.iam_action,
                   http_status=error.http_status, error_code=error.error_code)

    def as_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "count": self.count, "message": self.message, "iam_action": self.iam_action,
                "http_status": self.http_status, "error_code": self.error_code}


@dataclass
class EnterpriseSyncResult:
    created: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)   # ya no visibles: present = false (no se borran)

    def summary(self) -> Dict[str, Any]:
        return {"created": len(self.created), "updated": len(self.updated), "unchanged": len(self.unchanged),
                "missing": self.missing}


@dataclass
class IamValidation:
    authenticated: bool
    account_status: str
    projects_visible: Optional[int] = None
    message: Optional[str] = None
    step: Optional[StepResult] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"authenticated": self.authenticated, "account_status": self.account_status,
                "projects_visible": self.projects_visible, "message": self.message,
                "step": self.step.as_dict() if self.step else None}


@dataclass
class DiscoveryReport:
    steps: Dict[str, StepResult]
    regions: List[str]
    finished_at: datetime
    projects: Optional[ProjectSyncResult] = None
    enterprise_projects: Optional[EnterpriseSyncResult] = None

    @property
    def complete(self) -> bool:
        return all(s.status == STEP_OK for s in self.steps.values())

    def as_dict(self) -> Dict[str, Any]:
        """Forma persistida en ``cloud_accounts.discovery_report`` y devuelta por la API."""
        return {"finished_at": self.finished_at.isoformat(), "complete": self.complete,
                "steps": {name: step.as_dict() for name, step in self.steps.items()},
                "regions": self.regions,
                "projects": self.projects.summary() if self.projects else None,
                "enterprise_projects": self.enterprise_projects.summary() if self.enterprise_projects else None}


# ---------------------------------------------------------------- Enterprise Projects
def sync_enterprise_projects(session: Session, account: CloudAccount,
                             discovered: List[DiscoveredEnterpriseProject], *,
                             now: Optional[datetime] = None) -> EnterpriseSyncResult:
    """Crea/actualiza sin duplicados (clave: cuenta + id de EPS). Los que ya no aparecen
    quedan ``present = false``; nunca se borran (los recursos pueden referenciarlos)."""
    now = now or _now()
    result = EnterpriseSyncResult()
    existing = {row.huawei_ep_id: row for row in session.scalars(
        select(EnterpriseProject).where(EnterpriseProject.account_id == account.id))}
    seen = set()
    for ep in discovered:
        if ep.id in seen:
            continue
        seen.add(ep.id)
        values = {"name": ep.name[:200], "description": ep.description, "status": ep.status,
                  "ep_type": (ep.type or None) and ep.type[:16], "present": True}
        row = existing.get(ep.id)
        if row is None:
            session.add(EnterpriseProject(account_id=account.id, huawei_ep_id=ep.id[:64], discovered_at=now, **values))
            result.created.append(ep.id)
            continue
        changed = any(getattr(row, k) != v for k, v in values.items())
        for key, value in values.items():
            setattr(row, key, value)
        row.discovered_at = now
        (result.updated if changed else result.unchanged).append(ep.id)
    for ep_id, row in existing.items():
        if ep_id not in seen and row.present:
            row.present = False
            result.missing.append(ep_id)
    session.flush()
    return result


# ---------------------------------------------------------------- validación y descubrimiento
def _clients(account: CloudAccount, cipher: SecretCipher):
    credentials = decrypt_credentials(account, cipher)
    return credentials, ClientFactory(credentials, endpoint_domain=account.endpoint_domain)


def validate_credentials(session: Session, cipher: SecretCipher, client_id: uuid.UUID, account_id: uuid.UUID, *,
                         source_factory: ProjectSourceFactory = iam_source) -> IamValidation:
    """Autentica la identidad IAM sin sincronizar nada.

    - Éxito → cuenta ``active``.
    - 403 al listar proyectos → las credenciales SÍ autentican (cuenta ``active``) pero
      la identidad no puede listar proyectos: "Permiso insuficiente".
    - Fallo de autenticación → cuenta ``invalid``.
    - Otro fallo (red, servicio) → el estado de la cuenta no cambia.
    """
    account = get_account(session, client_id, account_id)
    credentials, clients = _clients(account, cipher)
    try:
        projects = source_factory(account, clients).list_projects()
    except Exception as exc:
        error = classify_exception(exc, service="iam", region=account.iam_region_id, secrets=credentials.secrets)
        step = StepResult.from_error(error)
        if error.kind == AUTHENTICATION:
            account.status, account.last_validation_error = "invalid", error.message
            session.flush()
            return IamValidation(False, account.status, message=error.message, step=step)
        if step.status == STEP_DENIED:
            account.status, account.last_validated_at, account.last_validation_error = "active", _now(), None
            session.flush()
            return IamValidation(True, account.status, message=step.message, step=step)
        account.last_validation_error = error.message
        session.flush()
        return IamValidation(False, account.status, message=error.message, step=step)
    account.status, account.last_validated_at, account.last_validation_error = "active", _now(), None
    if not account.huawei_domain_id:
        account.huawei_domain_id = next((p.domain_id for p in projects if p.domain_id), None)
    session.flush()
    return IamValidation(True, account.status, projects_visible=len(projects),
                         step=StepResult(status=STEP_OK, count=len(projects)))


def discover(session: Session, cipher: SecretCipher, client_id: uuid.UUID, account_id: uuid.UUID, *,
             project_source_factory: ProjectSourceFactory = iam_source,
             enterprise_source_factory: EnterpriseSourceFactory = eps_source) -> DiscoveryReport:
    """Descubre y sincroniza Projects, Regions y Enterprise Projects de la cuenta.

    Lanza ``DiscoveryFailedError`` solo si la autenticación falla o si ningún paso pudo
    completarse ni denegarse (p. ej. Huawei inalcanzable). El informe queda guardado en
    la cuenta en todos los casos.
    """
    account = get_account(session, client_id, account_id)
    credentials, clients = _clients(account, cipher)
    now = _now()
    steps: Dict[str, StepResult] = {}
    project_result: Optional[ProjectSyncResult] = None
    enterprise_result: Optional[EnterpriseSyncResult] = None
    first_error: Optional[ServiceError] = None

    try:
        discovered = project_source_factory(account, clients).list_projects()
        project_result = sync_projects(session, account, discovered, now=now)
        steps["projects"] = StepResult(status=STEP_OK, count=len(project_result.created) + len(project_result.updated)
                                       + len(project_result.unchanged))
    except Exception as exc:
        error = classify_exception(exc, service="iam", region=account.iam_region_id, secrets=credentials.secrets)
        first_error = error
        steps["projects"] = StepResult.from_error(error)
        if error.kind == AUTHENTICATION:
            account.status, account.last_validation_error = "invalid", error.message
            steps["enterprise_projects"] = StepResult(status=STEP_SKIPPED, message="No se intentó: credenciales inválidas.")
            _save(session, account, DiscoveryReport(steps=steps, regions=_regions(session, account), finished_at=now))
            logger.warning("IAM discovery: autenticación fallida %s", error.to_log())
            raise DiscoveryFailedError(error) from None

    try:
        eps = enterprise_source_factory(account, clients).list_enterprise_projects()
        enterprise_result = sync_enterprise_projects(session, account, eps, now=now)
        steps["enterprise_projects"] = StepResult(status=STEP_OK, count=len(eps))
    except Exception as exc:
        error = classify_exception(exc, service="eps", secrets=credentials.secrets)
        first_error = first_error or error
        steps["enterprise_projects"] = StepResult.from_error(error)
        if error.kind == AUTHENTICATION:  # p. ej. sin acceso al endpoint global: no invalida la cuenta
            steps["enterprise_projects"].status = STEP_FAILED

    report = DiscoveryReport(steps=steps, regions=_regions(session, account), finished_at=now,
                             projects=project_result, enterprise_projects=enterprise_result)
    reached = any(s.status in (STEP_OK, STEP_DENIED) for s in steps.values())
    if reached:  # Huawei respondió con las credenciales: autenticación correcta
        account.status, account.last_validated_at, account.last_validation_error = "active", now, None
    _save(session, account, report)
    logger.info("IAM discovery cuenta=%s %s", account.id, {k: (v.status, v.count) for k, v in steps.items()})
    if not reached and first_error is not None:
        raise DiscoveryFailedError(first_error)
    return report


def _regions(session: Session, account: CloudAccount) -> List[str]:
    return sorted({p.region_id for p in projects_repo.list_for_account(session, account.id)})


def _save(session: Session, account: CloudAccount, report: DiscoveryReport) -> None:
    account.last_discovery_at = report.finished_at
    account.discovery_report = report.as_dict()
    session.flush()


# ---------------------------------------------------------------- permisos efectivos
SERVICE_STATUS = {"succeeded": "read", "denied": "denied", "partial": "partial", "unavailable": "unavailable",
                  "failed": "error", "running": "pending", "pending": "pending", "skipped": "not_checked"}


def account_permissions(session: Session, account: CloudAccount, *, recent_runs: int = 20) -> Dict[str, Any]:
    """Qué puede consultar la identidad IAM de la cuenta.

    Por servicio se usa el escaneo MÁS RECIENTE que lo incluyó (un escaneo parcial no
    borra lo sabido de otros servicios). ``not_checked`` = aún no se escaneó.
    Un servicio denegado NO es un fallo global de la cuenta.
    """
    services: Dict[str, Dict[str, Any]] = {}
    for run in scans_repo.list_for_account(session, account.id, limit=recent_runs):
        tasks = [t for t in scans_repo.tasks(session, run.id) if t.service not in services]
        if not tasks:
            continue
        for item in service_coverage(tasks):
            services[item.service] = {
                "service": item.service, "status": SERVICE_STATUS.get(item.status, item.status),
                "regions_affected": item.regions_affected, "iam_actions": item.iam_actions,
                "message": item.message, "scan_sequence": run.sequence,
                "checked_at": (run.finished_at or run.created_at).isoformat() if (run.finished_at or run.created_at) else None,
            }
    for entry in SERVICIOS:
        if entry["id"] != ALL_SERVICES and entry["id"] not in services:
            services[entry["id"]] = {"service": entry["id"], "status": "not_checked", "regions_affected": [],
                                     "iam_actions": [], "message": None, "scan_sequence": None, "checked_at": None}
    report = account.discovery_report or {}
    return {"account_id": str(account.id), "account_status": account.status,
            "discovery": {"finished_at": report.get("finished_at"), "steps": report.get("steps") or {}},
            "services": [services[k] for k in sorted(services)]}
