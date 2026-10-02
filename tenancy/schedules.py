# coding: utf-8
"""Programaciones de escaneo de una cuenta (CRUD con validación)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

from sqlalchemy.orm import Session

from core.validation import InvalidValueError, validate_region_id
from db.models import MAX_SCHEDULE_MINUTES, MIN_SCHEDULE_MINUTES, ScanSchedule
from repositories import catalog as catalog_repo
from repositories import schedules as repo
from tenancy.accounts import get_account
from tenancy.errors import NotFoundError, ValidationFailedError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validate(session: Session, interval: Optional[int], services: Optional[Sequence[str]],
              regions: Optional[Sequence[str]]):
    if interval is not None and not MIN_SCHEDULE_MINUTES <= interval <= MAX_SCHEDULE_MINUTES:
        raise ValidationFailedError(
            f"La frecuencia debe estar entre {MIN_SCHEDULE_MINUTES} y {MAX_SCHEDULE_MINUTES} minutos.")
    clean_services = None
    if services:
        clean_services = sorted({s.strip().lower() for s in services})
        known = set(catalog_repo.services_by_id(session))
        unknown = [s for s in clean_services if known and s not in known]
        if unknown:
            raise ValidationFailedError(f"Servicios no válidos: {', '.join(unknown)}")
    clean_regions = None
    if regions:
        try:
            clean_regions = sorted({validate_region_id(r) for r in regions})
        except InvalidValueError as exc:
            raise ValidationFailedError(str(exc)) from None
    return clean_services, clean_regions


def create_schedule(session: Session, *, client_id: uuid.UUID, account_id: uuid.UUID, interval_minutes: int,
                    name: Optional[str] = None, services: Optional[Sequence[str]] = None,
                    regions: Optional[Sequence[str]] = None, enabled: bool = True,
                    first_run_at: Optional[datetime] = None) -> ScanSchedule:
    account = get_account(session, client_id, account_id)
    services, regions = _validate(session, interval_minutes, services, regions)
    schedule = ScanSchedule(account_id=account.id, name=(name or "Escaneo programado").strip()[:200],
                            enabled=enabled, interval_minutes=interval_minutes, services=services,
                            regions=regions, next_run_at=first_run_at or _now())
    session.add(schedule)
    session.flush()
    return schedule


def list_schedules(session: Session, client_id: uuid.UUID, account_id: uuid.UUID) -> List[ScanSchedule]:
    return repo.list_for_account(session, get_account(session, client_id, account_id).id)


def get_schedule(session: Session, client_id: uuid.UUID, account_id: uuid.UUID,
                 schedule_id: uuid.UUID) -> ScanSchedule:
    schedule = repo.get_for_client(session, client_id, account_id, schedule_id)
    if schedule is None:
        raise NotFoundError("Programación no encontrada.")
    return schedule


def update_schedule(session: Session, client_id: uuid.UUID, account_id: uuid.UUID, schedule_id: uuid.UUID, *,
                    name: Optional[str] = None, enabled: Optional[bool] = None,
                    interval_minutes: Optional[int] = None, services: Optional[Sequence[str]] = None,
                    regions: Optional[Sequence[str]] = None) -> ScanSchedule:
    schedule = get_schedule(session, client_id, account_id, schedule_id)
    clean_services, clean_regions = _validate(session, interval_minutes, services, regions)
    if name is not None:
        schedule.name = name.strip()[:200] or schedule.name
    if enabled is not None:
        schedule.enabled = enabled
    if interval_minutes is not None:
        schedule.interval_minutes = interval_minutes
    if services is not None:
        schedule.services = clean_services
    if regions is not None:
        schedule.regions = clean_regions
    session.flush()
    return schedule


def delete_schedule(session: Session, client_id: uuid.UUID, account_id: uuid.UUID, schedule_id: uuid.UUID) -> None:
    session.delete(get_schedule(session, client_id, account_id, schedule_id))
    session.flush()


def advance(schedule: ScanSchedule, now: datetime) -> None:
    """Siguiente ejecución sin acumular atrasos (tras una caída no se dispara N veces)."""
    schedule.next_run_at = now + timedelta(minutes=schedule.interval_minutes)
