# coding: utf-8
"""Worker de escaneos: proceso SEPARADO del servidor web (``python manage.py worker``).

Cada ciclo (``run_once``):
  1. Recupera escaneos abandonados (sin latido) → ``failed``, liberando la cuenta.
  2. Encola las programaciones vencidas: las reclama con ``FOR UPDATE SKIP LOCKED``,
     avanza ``next_run_at`` y crea el ScanRun ``pending`` (el índice único parcial
     impide duplicados: si ya hay uno activo se registra ``skipped_active``).
  3. Ejecuta los escaneos ``pending`` (de programaciones o encolados por la API con
     ``INVENTORY_SCAN_EXECUTOR=worker``). El "claim" atómico garantiza que dos
     workers nunca ejecutan el mismo escaneo.

La cola es la propia base de datos: no hace falta Redis/Celery para empezar, y un
reinicio no pierde nada (lo pendiente sigue en ``scan_runs``). Se pueden ejecutar
varios workers en paralelo.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import sessionmaker

from core.crypto import SecretCipher
from db.models import CloudAccount
from repositories import scans as scans_repo
from repositories import schedules as schedules_repo
from scanning.engine import STALE_RUN_AFTER, create_scan, execute_scan, release_stale_run
from scanning.settings import ScanSettings
from tenancy.errors import ConflictError, InvalidStateError, TenancyError
from tenancy.schedules import advance

logger = logging.getLogger(__name__)
DEFAULT_POLL_SECONDS = 30


@dataclass
class WorkerReport:
    recovered: int = 0
    queued: int = 0
    skipped_active: int = 0
    schedule_errors: int = 0
    executed: List[uuid.UUID] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def recover_stale(factory: sessionmaker, now: datetime) -> int:
    recovered = 0
    with factory() as session:
        for run in schedules_repo.stale_candidates(session):
            if scans_repo.is_stale(run, now=now, max_silence=STALE_RUN_AFTER):
                release_stale_run(session, run, now)
                recovered += 1
        session.commit()
    return recovered


def queue_due_schedules(factory: sessionmaker, now: datetime, report: WorkerReport, *, limit: int = 20) -> None:
    with factory() as session:
        for schedule in schedules_repo.claim_due(session, now=now, limit=limit):
            advance(schedule, now)  # antes de nada: un fallo no provoca reintentos en bucle
            schedule.last_triggered_at = now
            account = session.get(CloudAccount, schedule.account_id)
            try:
                with session.begin_nested():
                    run = create_scan(session, client_id=account.client_id, account_id=account.id,
                                      services=schedule.services, regions=schedule.regions, trigger="schedule")
                schedule.last_run_id, schedule.last_status, schedule.last_error_safe = run.id, "queued", None
                report.queued += 1
            except ConflictError:
                schedule.last_status, schedule.last_error_safe = "skipped_active", None
                report.skipped_active += 1
            except TenancyError as exc:  # cuenta deshabilitada, sin proyectos, filtros inválidos...
                schedule.last_status, schedule.last_error_safe = "error", exc.message
                report.schedule_errors += 1
                logger.warning("Programación %s no encolada: %s", schedule.id, exc.message)
        session.commit()


def execute_pending(factory: sessionmaker, cipher: SecretCipher, report: WorkerReport, *,
                    settings: Optional[ScanSettings] = None, limit: int = 5) -> None:
    with factory() as session:
        pending = [run.id for run in schedules_repo.pending_runs(session, limit=limit)]
    for run_id in pending:
        try:
            report.executed.append(execute_scan(factory, cipher, run_id, settings=settings))
        except InvalidStateError:
            continue  # otro worker lo reclamó primero


def run_once(factory: sessionmaker, cipher: SecretCipher, *, now: Optional[datetime] = None,
             settings: Optional[ScanSettings] = None, max_runs: int = 5) -> WorkerReport:
    now = now or _now()
    report = WorkerReport()
    report.recovered = recover_stale(factory, now)
    queue_due_schedules(factory, now, report)
    execute_pending(factory, cipher, report, settings=settings, limit=max_runs)
    if report.recovered or report.queued or report.executed or report.schedule_errors:
        logger.info("Worker: recuperados=%d encolados=%d omitidos=%d errores=%d ejecutados=%d",
                    report.recovered, report.queued, report.skipped_active, report.schedule_errors,
                    len(report.executed), extra={"event": "worker_cycle"})
    return report


def run_forever(factory: sessionmaker, cipher: SecretCipher, *, poll_seconds: int = DEFAULT_POLL_SECONDS,
                stop: Optional[threading.Event] = None, settings: Optional[ScanSettings] = None) -> None:
    """Bucle del worker. Un error en un ciclo se registra y el bucle continúa."""
    stop = stop or threading.Event()
    logger.info("Worker iniciado (cada %ds)", poll_seconds)
    while not stop.is_set():
        try:
            run_once(factory, cipher, settings=settings)
        except Exception:  # nunca silencioso: se registra con traza y se reintenta en el siguiente ciclo
            logger.exception("Ciclo del worker fallido")
        stop.wait(poll_seconds)
    logger.info("Worker detenido")
