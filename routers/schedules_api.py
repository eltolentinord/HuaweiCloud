# coding: utf-8
"""Programaciones de escaneo de una cuenta. Lectura: ``scans:read``; cambios: ``schedules:manage``.

Las programaciones las ejecuta el worker (``python manage.py worker``), no el servidor web.
"""

from __future__ import annotations

import uuid
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.authz import Permission, Principal
from db.session import get_db
from routers.security import get_principal, requires
from tenancy import audit, schedules
from tenancy.schemas import ScheduleIn, ScheduleOut, SchedulePatch

router = APIRouter(prefix="/api/clients/{client_id}/accounts/{account_id}/schedules", tags=["schedules"])
READ = [Depends(requires(Permission.SCANS_READ))]
MANAGE = [Depends(requires(Permission.SCHEDULES_MANAGE))]


@router.get("", response_model=List[ScheduleOut], dependencies=READ)
def list_schedules(client_id: uuid.UUID, account_id: uuid.UUID, db: Session = Depends(get_db)):
    return schedules.list_schedules(db, client_id, account_id)


@router.post("", response_model=ScheduleOut, status_code=201, dependencies=MANAGE)
def create_schedule(client_id: uuid.UUID, account_id: uuid.UUID, body: ScheduleIn, db: Session = Depends(get_db),
                    actor: Principal = Depends(get_principal)):
    item = schedules.create_schedule(db, client_id=client_id, account_id=account_id, **body.model_dump())
    audit.record(db, actor, "schedule.create", client_id=client_id, account_id=account_id,
                 target=("schedule", item.id),
                 details={"name": item.name, "interval_minutes": item.interval_minutes, "services": item.services,
                          "regions": item.regions, "enabled": item.enabled})
    return item


@router.get("/{schedule_id}", response_model=ScheduleOut, dependencies=READ)
def get_schedule(client_id: uuid.UUID, account_id: uuid.UUID, schedule_id: uuid.UUID, db: Session = Depends(get_db)):
    return schedules.get_schedule(db, client_id, account_id, schedule_id)


@router.patch("/{schedule_id}", response_model=ScheduleOut, dependencies=MANAGE)
def update_schedule(client_id: uuid.UUID, account_id: uuid.UUID, schedule_id: uuid.UUID, body: SchedulePatch,
                    db: Session = Depends(get_db), actor: Principal = Depends(get_principal)):
    changes = body.model_dump(exclude_unset=True)
    item = schedules.update_schedule(db, client_id, account_id, schedule_id, **changes)
    audit.record(db, actor, "schedule.update", client_id=client_id, account_id=account_id,
                 target=("schedule", schedule_id), details={**changes, "fields": sorted(changes)})
    return item


@router.delete("/{schedule_id}", status_code=204, dependencies=MANAGE)
def delete_schedule(client_id: uuid.UUID, account_id: uuid.UUID, schedule_id: uuid.UUID,
                    db: Session = Depends(get_db), actor: Principal = Depends(get_principal)):
    schedules.delete_schedule(db, client_id, account_id, schedule_id)
    audit.record(db, actor, "schedule.delete", client_id=client_id, account_id=account_id,
                 target=("schedule", schedule_id))
