# coding: utf-8
"""Motor de escaneo persistente con paralelismo controlado (Fase 3B).

    CloudAccount → Projects habilitados (filtrables por región/proyecto)
      → plan de ScanTasks:
          · servicio regional: una tarea por proyecto (cada Project ID = 1 región)
          · servicio global (OBS): UNA tarea por cuenta
      → planificador: hasta ``max_workers`` tareas a la vez, con límites por
        servicio y por región, y un límite global de llamadas por proceso
      → workers: SOLO llaman a Huawei y normalizan (sin base de datos)
      → hilo principal (escritor único): upsert/deleted_at, estado y commit por tarea

Garantías:
- La sesión SQLAlchemy no se comparte entre hilos: los workers nunca la tocan.
- Un servicio que falla no aborta el ScanRun; se registra en su ScanTask.
- ``deleted_at`` solo tras una consulta exitosa y COMPLETA del servicio en ese
  ámbito; denied/unavailable/partial/failed/skipped nunca borran.
- Dos tareas nunca escriben el mismo recurso: el ámbito (proyecto o cuenta) ×
  servicio es único dentro de un escaneo y hay como mucho un escaneo activo por
  cuenta (índice único parcial en ``scan_runs``).
- Ante 401 ``APIGW.*`` (credenciales rechazadas) no se lanzan más tareas.
- AK/SK solo existen en memoria durante la ejecución.
"""

from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from collectors import get_collector
from collectors.base import CollectorContext
from core.clients import ClientFactory
from core.crypto import SecretCipher
from core.engine import ServiceRun, run_collector
from core.errors import AUTHENTICATION, DENIED_KINDS, UNAVAILABLE, classify_exception, safe_message
from db.models import CloudAccount, Project, ScanRun, ScanTask, ServiceCatalog
from repositories import catalog as catalog_repo
from repositories import projects as projects_repo
from repositories import resources as resources_repo
from repositories import scans as scans_repo
from scanning.settings import ScanSettings
from tenancy.accounts import decrypt_credentials, get_account
from tenancy.catalog_sync import sync_catalog
from tenancy.errors import ConflictError, InvalidStateError, NotFoundError, ValidationFailedError

logger = logging.getLogger(__name__)

# Sin latido durante este tiempo, un escaneo activo se considera abandonado (proceso caído).
STALE_RUN_AFTER = timedelta(minutes=60)
BLOCKING_ACCOUNT_STATUSES = ("disabled", "invalid")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _elapsed_ms(start: datetime, end: datetime) -> int:
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return max(int((end - start).total_seconds() * 1000), 0)


# ---------------------------------------------------------------- planificación
@dataclass(frozen=True)
class PlannedTask:
    sequence: int
    service: str
    scope: str
    region: str
    project: Optional[Project]  # None en servicios globales


def plan_tasks(projects: Sequence[Project], services: Sequence[ServiceCatalog]) -> List[PlannedTask]:
    """Globales: una vez por cuenta (región del primer proyecto). Regionales: proyecto × servicio.

    El orden intercala regiones (round-robin por proyecto) para que el planificador
    reparta la carga entre endpoints desde el principio.
    """
    plan: List[PlannedTask] = []
    if not projects:
        return plan
    for service in (s for s in services if s.scope == "global"):
        plan.append(PlannedTask(len(plan), service.id, "global", projects[0].region_id, None))
    for service in (s for s in services if s.scope == "regional"):
        for project in projects:
            plan.append(PlannedTask(len(plan), service.id, "regional", project.region_id, project))
    return plan


def _catalog(session: Session) -> List[ServiceCatalog]:
    services = catalog_repo.enabled_services(session)
    if not services and not catalog_repo.services_by_id(session):
        sync_catalog(session)  # base recién migrada: el catálogo sale de core/catalog.py
        services = catalog_repo.enabled_services(session)
    return services


def _select_services(catalog: List[ServiceCatalog], services: Optional[Sequence[str]]) -> List[ServiceCatalog]:
    if not services:
        return catalog
    requested = {s.strip().lower() for s in services}
    unknown = requested - {s.id for s in catalog}
    if unknown:
        raise ValidationFailedError(f"Servicios no válidos o deshabilitados: {', '.join(sorted(unknown))}")
    return [s for s in catalog if s.id in requested]


def _select_projects(projects: List[Project], regions: Optional[Sequence[str]],
                     project_ids: Optional[Sequence[uuid.UUID]]) -> List[Project]:
    if regions:
        wanted = {r.strip() for r in regions}
        unknown = wanted - {p.region_id for p in projects}
        if unknown:
            raise ValidationFailedError(
                f"Regiones sin proyectos habilitados en la cuenta: {', '.join(sorted(unknown))}")
        projects = [p for p in projects if p.region_id in wanted]
    if project_ids:
        wanted_ids = {uuid.UUID(str(p)) for p in project_ids}
        unknown_ids = wanted_ids - {p.id for p in projects}
        if unknown_ids:
            raise ValidationFailedError("Algún proyecto no pertenece a la cuenta, está deshabilitado "
                                        "o no está en las regiones indicadas.")
        projects = [p for p in projects if p.id in wanted_ids]
    return projects


def _release_stale_run(session: Session, run: ScanRun, now: datetime) -> None:
    run.status = "failed"
    run.error_message_safe = "Escaneo abandonado (sin actividad); se liberó para permitir uno nuevo."
    run.finished_at = now
    for task in scans_repo.tasks(session, run.id):
        if task.status in ("pending", "running"):
            task.status, task.error_kind = "skipped", "stale"
            task.error_message_safe = "Tarea no completada: el escaneo quedó abandonado."
    session.flush()
    logger.warning("ScanRun %s abandonado; se marca como failed", run.id)


def create_scan(session: Session, *, client_id: uuid.UUID, account_id: uuid.UUID,
                services: Optional[Sequence[str]] = None, regions: Optional[Sequence[str]] = None,
                project_ids: Optional[Sequence[uuid.UUID]] = None, trigger: str = "manual") -> ScanRun:
    """Crea el ScanRun (``pending``) y todas sus ScanTasks. No llama a Huawei Cloud."""
    account = get_account(session, client_id, account_id)
    if account.status in BLOCKING_ACCOUNT_STATUSES:
        raise InvalidStateError(f"La cuenta está en estado '{account.status}'.")
    now = _now()
    active = scans_repo.active_for_account(session, account.id)
    if active is not None:
        if not scans_repo.is_stale(active, now=now, max_silence=STALE_RUN_AFTER):
            raise ConflictError(f"Ya hay un escaneo en curso para esta cuenta ({active.id}).")
        _release_stale_run(session, active, now)

    catalog = _select_services(_catalog(session), services)
    projects = projects_repo.list_for_account(session, account.id, enabled_only=True)
    if not projects:
        raise InvalidStateError("La cuenta no tiene proyectos habilitados; ejecuta el descubrimiento IAM.")
    projects = _select_projects(projects, regions, project_ids)

    plan = plan_tasks(projects, catalog)
    run = ScanRun(id=uuid.uuid4(), account_id=account.id, status="pending", trigger=trigger,
                  total_tasks=len(plan),
                  stats={"services": [s.id for s in catalog], "projects": len(projects),
                         "regions": sorted({p.region_id for p in projects}),
                         "filters": {"regions": sorted(regions or []),
                                     "project_ids": sorted(str(p) for p in (project_ids or []))}})
    session.add(run)
    for item in plan:
        session.add(ScanTask(scan_run_id=run.id, sequence=item.sequence, service=item.service,
                             scope=item.scope, region=item.region,
                             project_id=item.project.id if item.project else None))
    try:
        session.flush()
    except IntegrityError:  # otra petición creó un escaneo activo en paralelo
        session.rollback()
        raise ConflictError("Ya hay un escaneo en curso para esta cuenta.") from None
    logger.info("ScanRun creado id=%s cuenta=%s tareas=%d regiones=%s", run.id, account.id, len(plan),
                run.stats["regions"])
    return run


# ------------------------------------------------------------------- ejecución
@dataclass(frozen=True)
class TaskJob:
    """Datos planos de una tarea para el worker (sin objetos ORM)."""

    task_id: uuid.UUID
    service: str
    scope: str
    region: str
    huawei_project_id: str
    project_id: Optional[uuid.UUID]


def _collect(job: TaskJob, clients: ClientFactory) -> ServiceRun:
    """Worker: SOLO Huawei Cloud + normalización. Nunca toca la base de datos."""
    ctx = CollectorContext(clients=clients, region=job.region, project_id=job.huawei_project_id)
    return run_collector(get_collector(job.service), ctx)


def _job_for(session: Session, account: CloudAccount, task: ScanTask) -> Optional[TaskJob]:
    if task.project_id:
        project = session.get(Project, task.project_id)
    else:  # global: cualquier proyecto habilitado de la región elegida (OBS no usa Project ID)
        candidates = projects_repo.list_for_account(session, account.id, enabled_only=True)
        project = next((p for p in candidates if p.region_id == task.region), candidates[0] if candidates else None)
    if project is None or not project.is_enabled:
        return None
    return TaskJob(task.id, task.service, task.scope, task.region, project.huawei_project_id, task.project_id)


def _record_failure(task: ScanTask, result: ServiceRun) -> None:
    """Registra el error sin tocar recursos. 403/401 de autorización/API inexistente = aviso."""
    error = result.error
    if error.kind in DENIED_KINDS:
        task.status = "denied"
    elif error.kind == UNAVAILABLE:
        task.status = "unavailable"
    else:
        task.status = "failed"
        task.error_count = 1
    task.error_kind = error.kind
    task.iam_action = (error.iam_action or None) and error.iam_action[:128]
    task.http_status = error.http_status
    task.error_code = (error.error_code or None) and str(error.error_code)[:128]
    task.request_id = (error.request_id or None) and str(error.request_id)[:128]
    task.error_message_safe = error.message
    if error.safe_explanation:
        task.notices = task.notices + [error.safe_explanation]


def _persist(session: Session, account: CloudAccount, run: ScanRun, task: ScanTask,
             result: ServiceRun, secrets: Sequence[str]) -> None:
    """Escritor único: aplica el resultado de un worker a la base de datos."""
    task.notices = [n.message for n in result.notices]
    if result.error:
        _record_failure(task, result)  # los recursos existentes NO se tocan
        return
    stats = resources_repo.apply_snapshot(
        session, account_id=account.id, project_id=task.project_id, service=task.service,
        resources=result.resources, run_id=run.id, now=_now(), complete=result.complete,
        secrets=secrets,
    )
    task.status = "succeeded" if result.complete else "partial"
    task.resource_count = stats.seen
    task.created_count = stats.created + stats.restored
    task.updated_count = stats.updated
    task.deleted_count = stats.deleted
    extra = []
    if stats.skipped_without_id:
        extra.append(f"{stats.skipped_without_id} recurso(s) sin identificador no se guardaron.")
    if stats.restored:
        extra.append(f"{stats.restored} recurso(s) reaparecieron y se restauraron.")
    task.notices = task.notices + extra


def _finalize(run: ScanRun, tasks: Sequence[ScanTask], now: datetime, extra_stats: Optional[dict] = None) -> None:
    """Estado final del ScanRun.

    - completed:               todas las tareas correctas.
    - completed_with_warnings: solo avisos (denied/unavailable/partial), con o sin datos.
    - completed_with_errors:   hay datos y algún error real (failed/skipped).
    - failed:                  ningún dato y algún error real, o credenciales no disponibles.
    """
    by_status: Dict[str, int] = {}
    for task in tasks:
        by_status[task.status] = by_status.get(task.status, 0) + 1
    ok = by_status.get("succeeded", 0) + by_status.get("partial", 0)
    errors = by_status.get("failed", 0) + by_status.get("skipped", 0)
    warnings = by_status.get("denied", 0) + by_status.get("unavailable", 0) + by_status.get("partial", 0)
    if run.status != "failed":
        if errors:
            run.status = "completed_with_errors" if ok else "failed"
        else:
            run.status = "completed_with_warnings" if warnings else "completed"
    run.total_resources = sum(t.resource_count for t in tasks)
    run.total_created = sum(t.created_count for t in tasks)
    run.total_updated = sum(t.updated_count for t in tasks)
    run.total_deleted = sum(t.deleted_count for t in tasks)
    run.total_errors = errors
    run.total_warnings = warnings
    run.stats = {**(run.stats or {}), **(extra_stats or {}), "tasks_by_status": by_status}
    run.finished_at = now
    run.duration_ms = _elapsed_ms(run.started_at or now, now)


class _Limits:
    """Contadores de tareas en curso por servicio y por región (solo hilo principal)."""

    def __init__(self, settings: ScanSettings) -> None:
        self.settings = settings
        self.by_service: Dict[str, int] = {}
        self.by_region: Dict[str, int] = {}
        self.running = 0
        self.peak = {"workers": 0, "per_service": 0, "per_region": 0}

    def allows(self, job: TaskJob) -> bool:
        return (self.running < self.settings.max_workers
                and self.by_service.get(job.service, 0) < self.settings.per_service
                and self.by_region.get(job.region, 0) < self.settings.per_region)

    def acquire(self, job: TaskJob) -> None:
        self.running += 1
        self.by_service[job.service] = self.by_service.get(job.service, 0) + 1
        self.by_region[job.region] = self.by_region.get(job.region, 0) + 1
        self.peak["workers"] = max(self.peak["workers"], self.running)
        self.peak["per_service"] = max(self.peak["per_service"], self.by_service[job.service])
        self.peak["per_region"] = max(self.peak["per_region"], self.by_region[job.region])

    def release(self, job: TaskJob) -> None:
        self.running -= 1
        self.by_service[job.service] -= 1
        self.by_region[job.region] -= 1


def _fail_run_without_credentials(session: Session, run: ScanRun, exc: Exception) -> None:
    message = getattr(exc, "message", None) or "No se pudieron descifrar las credenciales de la cuenta."
    for task in scans_repo.tasks(session, run.id):
        task.status, task.error_kind = "skipped", "credentials"
    run.status, run.error_message_safe = "failed", safe_message(message)
    _finalize(run, scans_repo.tasks(session, run.id), _now())
    session.commit()
    logger.warning("ScanRun %s fallido: credenciales no disponibles (%s)", run.id, type(exc).__name__)


def execute_scan(session_factory: sessionmaker, cipher: SecretCipher, run_id: uuid.UUID,
                 *, settings: Optional[ScanSettings] = None) -> uuid.UUID:
    """Ejecuta un ScanRun ``pending``. Nunca lanza por fallos de Huawei Cloud."""
    settings = settings or ScanSettings.from_env()
    with session_factory() as session:
        run = scans_repo.get(session, run_id)
        if run is None:
            raise NotFoundError("Escaneo no encontrado.")
        if not scans_repo.claim_pending(session, run.id, now=_now()):
            session.rollback()
            session.refresh(run)
            raise InvalidStateError(f"El escaneo está en estado '{run.status}'.")
        session.commit()
        session.refresh(run)
        account = session.get(CloudAccount, run.account_id)
        started = time.perf_counter()

        try:
            credentials = decrypt_credentials(account, cipher)
            clients = ClientFactory(credentials, endpoint_domain=account.endpoint_domain)
        except Exception as exc:  # cuenta deshabilitada, clave maestra incorrecta...
            _fail_run_without_credentials(session, run, exc)
            return run.id
        secrets = credentials.secrets

        limits = _Limits(settings)
        running: Dict[Future, TaskJob] = {}
        stop_dispatch = False

        def skip(task: ScanTask, kind: str, message: str) -> None:
            task.status, task.error_kind, task.error_message_safe = "skipped", kind, message

        # Trabajos calculados una sola vez (datos planos para los workers).
        pending: List[ScanTask] = []
        jobs: Dict[uuid.UUID, TaskJob] = {}
        for task in scans_repo.tasks(session, run.id):
            if task.status != "pending":
                continue
            job = _job_for(session, account, task)
            if job is None:
                skip(task, "configuration", "El proyecto de la tarea ya no existe o está deshabilitado.")
                continue
            jobs[task.id] = job
            pending.append(task)
        session.commit()

        with ThreadPoolExecutor(max_workers=settings.max_workers, thread_name_prefix=f"scan-{str(run.id)[:8]}") as pool:
            while pending or running:
                # 1) Despacho: llena huecos respetando límites global/servicio/región.
                if not stop_dispatch:
                    for task in list(pending):
                        job = jobs[task.id]
                        if not limits.allows(job):
                            continue
                        task.status, task.started_at = "running", _now()
                        limits.acquire(job)
                        running[pool.submit(_collect, job, clients)] = job
                        pending.remove(task)
                    session.commit()
                if not running:
                    break

                # 2) Recogida y persistencia (escritor único, hilo principal).
                done, _ = wait(list(running), return_when=FIRST_COMPLETED)
                for future in done:
                    job = running.pop(future)
                    limits.release(job)
                    task = session.get(ScanTask, job.task_id)
                    try:
                        result = future.result()
                    except Exception as exc:  # no debería ocurrir: run_collector no lanza
                        result = ServiceRun(service=job.service, scope=job.scope, region=job.region,
                                            error=classify_exception(exc, service=job.service, secrets=secrets))
                    try:
                        _persist(session, account, run, task, result, secrets)
                    except Exception as exc:  # error de persistencia: solo invalida esta tarea
                        session.rollback()
                        task = session.get(ScanTask, job.task_id)
                        task.status, task.error_count, task.error_kind = "failed", 1, "internal"
                        task.error_message_safe = f"Error interno al guardar el resultado ({type(exc).__name__})."
                        logger.error("Tarea %s (%s) fallida al persistir: %s", task.id, task.service,
                                     safe_message(exc, secrets))
                    task.finished_at = _now()
                    task.duration_ms = _elapsed_ms(task.started_at, task.finished_at)
                    if task.error_kind == AUTHENTICATION and not stop_dispatch:
                        # Credenciales rechazadas (APIGW.*): no se lanzan más tareas contra Huawei.
                        stop_dispatch = True
                        for waiting in pending:
                            skip(waiting, AUTHENTICATION,
                                 "Omitida: las credenciales fueron rechazadas en otra tarea.")
                        pending.clear()
                    scans_repo.heartbeat(session, run.id, now=_now())
                    session.commit()
                    logger.info("ScanTask run=%s service=%s region=%s status=%s recursos=%d +%d ~%d -%d",
                                run.id, task.service, task.region, task.status, task.resource_count,
                                task.created_count, task.updated_count, task.deleted_count)

        session.refresh(run)
        _finalize(run, scans_repo.tasks(session, run.id), _now(),
                  {"concurrency": {**settings.as_dict(), "peak": limits.peak},
                   "throttle_retries": clients.retries.count})
        session.commit()
        logger.info("ScanRun %s %s en %.1fs (recursos=%d errores=%d avisos=%d)", run.id, run.status,
                    time.perf_counter() - started, run.total_resources, run.total_errors, run.total_warnings)
        return run.id


def scan_account(session_factory: sessionmaker, cipher: SecretCipher, *, client_id: uuid.UUID,
                 account_id: uuid.UUID, services: Optional[Sequence[str]] = None,
                 regions: Optional[Sequence[str]] = None, project_ids: Optional[Sequence[uuid.UUID]] = None,
                 trigger: str = "cli", settings: Optional[ScanSettings] = None) -> uuid.UUID:
    """Crea y ejecuta un escaneo de la cuenta (uso síncrono: CLI/tests)."""
    with session_factory() as session:
        run = create_scan(session, client_id=client_id, account_id=account_id, services=services,
                          regions=regions, project_ids=project_ids, trigger=trigger)
        session.commit()
        run_id = run.id
    return execute_scan(session_factory, cipher, run_id, settings=settings)
