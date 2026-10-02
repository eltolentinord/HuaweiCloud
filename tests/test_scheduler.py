# coding: utf-8
"""Scheduler: programaciones, worker, cola en BD, recuperación tras reinicio y API."""

import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

from alembic import command
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from tests.db_helpers import make_keyring
from tests.scan_helpers import ScanWorld, seed_account, server, simulated_huawei
from tests.test_postgres_concurrency import run_in_threads
from tests.test_postgres_migrations import PostgresTestCase, alembic_config
from tests.test_scan_api import ScanApiTestCase
from tests.test_scanning import ScanTestCase

from core.authz import Principal
from db.models import ScanRun, ScanSchedule
from routers.security import get_principal
from scanning import worker
from scanning.engine import create_scan
from tenancy import accounts, schedules
from tenancy.errors import ValidationFailedError

# Hora real: la detección de escaneos abandonados compara contra el reloj del sistema.
NOW = datetime.now(timezone.utc).replace(microsecond=0)


class SchedulerTestCase(ScanTestCase):
    services = ("ecs",)

    def schedule(self, **kw):
        kw.setdefault("interval_minutes", 60)
        kw.setdefault("services", ["ecs"])
        kw.setdefault("first_run_at", NOW - timedelta(minutes=1))
        item = schedules.create_schedule(self.session, client_id=self.client.id, account_id=self.account.id, **kw)
        self.session.commit()
        return item

    def cycle(self, now=NOW):
        with simulated_huawei(self.world):
            report = worker.run_once(self.factory, self.keyring, now=now)
        self.session.expire_all()
        return report

    def runs(self):
        return list(self.session.scalars(select(ScanRun).order_by(ScanRun.sequence)))


class TestSchedules(SchedulerTestCase):
    def test_validation(self):
        for kwargs in ({"interval_minutes": 5}, {"interval_minutes": 20000}, {"services": ["nope"]},
                       {"regions": ["x.evil.example/"]}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValidationFailedError):
                    self.schedule(**kwargs)

    def test_due_schedule_is_queued_and_executed(self):
        item = self.schedule()
        report = self.cycle()
        self.assertEqual((report.queued, len(report.executed)), (1, 1))
        [run] = self.runs()
        self.assertEqual((run.trigger, run.status), ("schedule", "completed"))
        self.session.refresh(item)
        self.assertEqual(item.last_status, "queued")
        self.assertEqual(item.last_run_id, run.id)
        self.assertEqual(item.next_run_at.replace(tzinfo=timezone.utc), NOW + timedelta(minutes=60))

    def test_not_due_or_disabled_does_nothing(self):
        self.schedule(first_run_at=NOW + timedelta(minutes=5))
        disabled = self.schedule(enabled=False)
        self.assertEqual(self.cycle().queued, 0)
        self.assertEqual(self.runs(), [])
        schedules.update_schedule(self.session, self.client.id, self.account.id, disabled.id, enabled=True)
        self.session.commit()
        self.assertEqual(self.cycle().queued, 1)

    def test_no_backlog_after_downtime(self):
        self.schedule(first_run_at=NOW - timedelta(days=3))  # 72 ejecuciones "perdidas"
        self.assertEqual(self.cycle().queued, 1)
        self.assertEqual(self.cycle().queued, 0)  # no recupera atrasos en bucle

    def test_active_scan_prevents_duplicate(self):
        create_scan(self.session, client_id=self.client.id, account_id=self.account.id, services=["ecs"])
        self.session.commit()
        item = self.schedule()
        with mock.patch.object(worker, "execute_pending"):  # el escaneo manual sigue "pendiente"
            report = self.cycle()
        self.assertEqual((report.queued, report.skipped_active), (0, 1))
        self.session.refresh(item)
        self.assertEqual(item.last_status, "skipped_active")
        self.assertEqual(len(self.runs()), 1)

    def test_error_in_one_schedule_does_not_stop_others(self):
        broken = self.schedule(regions=["eu-west-101"])  # la cuenta no tiene proyectos ahí
        ok = self.schedule(interval_minutes=30)
        report = self.cycle()
        self.assertEqual((report.schedule_errors, report.queued), (1, 1))
        self.session.refresh(broken)
        self.assertEqual(broken.last_status, "error")
        self.assertIn("eu-west-101", broken.last_error_safe)
        self.session.refresh(ok)
        self.assertEqual(ok.last_status, "queued")

    def test_disabled_account_reports_error(self):
        accounts.update_account(self.session, self.client.id, self.account.id, status="disabled")
        self.session.commit()
        item = self.schedule()
        self.assertEqual(self.cycle().schedule_errors, 1)
        self.session.refresh(item)
        self.assertIn("disabled", item.last_error_safe)


class TestRecoveryAfterRestart(SchedulerTestCase):
    def test_pending_runs_survive_restart_and_are_executed(self):
        run = create_scan(self.session, client_id=self.client.id, account_id=self.account.id, services=["ecs"])
        self.session.commit()
        report = self.cycle()  # "nuevo proceso": encuentra el pendiente en la base de datos
        self.assertEqual(report.executed, [run.id])
        self.assertEqual(self.session.get(ScanRun, run.id).status, "completed")

    def test_stale_running_scan_is_released(self):
        run = create_scan(self.session, client_id=self.client.id, account_id=self.account.id, services=["ecs"])
        run.status = "running"
        run.updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
        self.session.commit()
        report = self.cycle(now=datetime.now(timezone.utc))
        self.assertEqual(report.recovered, 1)
        self.assertEqual(self.session.get(ScanRun, run.id).status, "failed")
        create_scan(self.session, client_id=self.client.id, account_id=self.account.id, services=["ecs"])  # libre

    def test_run_forever_stops_and_survives_errors(self):
        stop = threading.Event()
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("ciclo roto")
            stop.set()
        with mock.patch.object(worker, "run_once", side_effect=flaky):
            worker.run_forever(self.factory, self.keyring, poll_seconds=0, stop=stop)
        self.assertEqual(len(calls), 2)  # el error del primer ciclo no detuvo el bucle


class TestScheduleApiAndQueueMode(ScanApiTestCase):
    def test_crud_and_permissions(self):
        base = f"/api/clients/{self.cid}/accounts/{self.aid}/schedules"
        created = self.call("POST", base, json={"interval_minutes": 120, "services": ["ecs"], "name": "Cada 2h"})
        self.assertEqual(created.status_code, 201, created.text)
        sid = created.json()["id"]
        self.assertEqual(len(self.call("GET", base).json()), 1)
        self.assertFalse(self.call("PATCH", f"{base}/{sid}", json={"enabled": False}).json()["enabled"])
        self.assertEqual(self.call("POST", base, json={"interval_minutes": 1}).status_code, 422)
        self.assertEqual(self.call("POST", base, json={"interval_minutes": 60, "next_run_at": "x",
                                                       "last_status": "hack"}).status_code, 201)  # campos extra ignorados
        self.app.dependency_overrides[get_principal] = lambda: Principal(
            subject="v", kind="user", client_roles={str(self.cid): "viewer"})
        self.assertEqual(self.call("GET", base).status_code, 200)
        self.assertEqual(self.call("DELETE", f"{base}/{sid}").status_code, 403)
        self.app.dependency_overrides.pop(get_principal)
        self.assertEqual(self.call("GET", f"/api/clients/{self.other_cid}/accounts/{self.aid}/schedules").status_code, 404)
        self.assertEqual(self.call("DELETE", f"{base}/{sid}").status_code, 204)

    def test_worker_executor_mode_only_queues(self):
        with mock.patch.dict(os.environ, {"INVENTORY_SCAN_EXECUTOR": "worker"}):
            response = self.start()
        scan_id = response.json()["id"]
        detail = self.call("GET", f"/api/scans/{scan_id}", params={"client_id": str(self.cid)}).json()
        self.assertEqual(detail["status"], "pending")  # nadie lo ejecutó en el servidor web
        with simulated_huawei(self.world):
            report = worker.run_once(self.factory, self.keyring)
        self.assertEqual([str(r) for r in report.executed], [scan_id])


class TestWorkersOnPostgres(PostgresTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        command.upgrade(alembic_config(cls.url), "head")
        cls.pooled = create_engine(cls.url, poolclass=QueuePool, pool_size=6, hide_parameters=True)
        cls.factory = sessionmaker(bind=cls.pooled, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        cls.pooled.dispose()
        super().tearDownClass()

    def test_concurrent_workers_never_duplicate_a_schedule(self):
        keyring = make_keyring()
        with self.factory() as session:
            client, account, _ = seed_account(session, keyring, client_name=f"W {uuid.uuid4().hex[:6]}",
                                              projects_spec=(("p-sg", "ap-southeast-3"),))
            schedules.create_schedule(session, client_id=client.id, account_id=account.id, interval_minutes=60,
                                      services=["ecs"], first_run_at=NOW - timedelta(minutes=1))
            session.commit()
            account_id = account.id
        world = ScanWorld()
        world.servers = {"p-sg": [server("srv-1")]}
        with simulated_huawei(world):
            reports = run_in_threads(*[lambda: worker.run_once(self.factory, keyring, now=NOW)] * 4)
        self.assertTrue(all(isinstance(r, worker.WorkerReport) for r in reports), reports)
        self.assertEqual(sum(r.queued for r in reports), 1)
        self.assertEqual(sum(len(r.executed) for r in reports), 1)
        with self.factory() as session:
            runs = session.scalars(select(ScanRun).where(ScanRun.account_id == account_id)).all()
            self.assertEqual([(r.trigger, r.status) for r in runs], [("schedule", "completed")])
            item = session.scalars(select(ScanSchedule).where(ScanSchedule.account_id == account_id)).one()
            self.assertEqual(item.next_run_at, NOW + timedelta(minutes=60))
