# coding: utf-8
"""Concurrencia ENTRE peticiones/escaneos sobre PostgreSQL REAL (pgserver o TEST_DATABASE_URL).

- Dos peticiones simultáneas para la misma cuenta → exactamente un escaneo (índice único parcial).
- Dos ejecutores del mismo escaneo → solo uno lo toma (claim atómico).
- Dos cuentas de clientes distintos escaneadas a la vez → datos aislados y límite global respetado.
"""

import threading
import uuid

from alembic import command
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from tests.db_helpers import make_keyring
from tests.helpers import FAKE_AK, FAKE_SK
from tests.scan_helpers import ScanWorld, seed_account, server, simulated_huawei
from tests.test_postgres_migrations import PostgresTestCase, alembic_config

from core.throttling import CallGate
from db.models import InventoryResource, ScanRun
from scanning.engine import create_scan, execute_scan, scan_account
from scanning.settings import ScanSettings
from tenancy.errors import ConflictError, InvalidStateError


def run_in_threads(*targets):
    """Arranca las funciones a la vez (barrera) y devuelve resultados o excepciones."""
    barrier = threading.Barrier(len(targets))
    results = [None] * len(targets)

    def runner(index, target):
        barrier.wait()
        try:
            results[index] = target()
        except Exception as exc:  # se devuelve para inspeccionarlo
            results[index] = exc
    threads = [threading.Thread(target=runner, args=(i, t)) for i, t in enumerate(targets)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results


class TestConcurrencyOnPostgres(PostgresTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        command.upgrade(alembic_config(cls.url), "head")
        cls.pooled = create_engine(cls.url, poolclass=QueuePool, pool_size=5, hide_parameters=True)
        cls.factory = sessionmaker(bind=cls.pooled, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        cls.pooled.dispose()
        super().tearDownClass()

    def setUp(self):
        self.keyring = make_keyring()
        name = f"Cliente {uuid.uuid4().hex[:6]}"
        with self.factory() as session:
            client, account, projects = seed_account(
                session, self.keyring, client_name=name,
                projects_spec=(("p-sg", "ap-southeast-3"), ("p-mx", "la-north-2")))
            self.client_id, self.account_id = client.id, account.id

    def new_scan(self):
        with self.factory() as session:
            run = create_scan(session, client_id=self.client_id, account_id=self.account_id, services=["ecs"])
            session.commit()
            return run.id

    def test_simultaneous_requests_create_a_single_scan(self):
        results = run_in_threads(*[self.new_scan for _ in range(4)])
        created = [r for r in results if isinstance(r, uuid.UUID)]
        conflicts = [r for r in results if isinstance(r, ConflictError)]
        self.assertEqual((len(created), len(conflicts)), (1, 3), results)
        with self.factory() as session:
            active = session.scalars(select(ScanRun).where(ScanRun.account_id == self.account_id,
                                                           ScanRun.status.in_(("pending", "running")))).all()
            self.assertEqual(len(active), 1)

    def test_database_rejects_second_active_scan(self):
        self.new_scan()
        with self.factory() as session:
            session.add(ScanRun(account_id=self.account_id, sequence=999, status="pending", trigger="api"))
            with self.assertRaises(IntegrityError):
                session.flush()

    def test_only_one_executor_claims_a_run(self):
        run_id = self.new_scan()
        world = ScanWorld()
        world.servers = {"p-sg": [server("srv-1")], "p-mx": []}
        with simulated_huawei(world):
            results = run_in_threads(*[lambda: execute_scan(self.factory, self.keyring, run_id)] * 3)
        self.assertEqual(sum(isinstance(r, uuid.UUID) for r in results), 1, results)
        self.assertEqual(sum(isinstance(r, InvalidStateError) for r in results), 2, results)
        self.assertEqual(len([c for c in world.calls if c[0] == "list_servers_details"]), 2)  # no duplicadas
        with self.factory() as session:
            self.assertEqual(session.get(ScanRun, run_id).status, "completed")

    def test_two_accounts_scanned_concurrently_stay_isolated(self):
        with self.factory() as session:
            other_client, other_account, _ = seed_account(
                session, self.keyring, client_name=f"Otro {uuid.uuid4().hex[:6]}",
                projects_spec=(("p-sg", "ap-southeast-3"), ("p-mx", "la-north-2")))
            other_ids = (other_client.id, other_account.id)
        world = ScanWorld()  # mismo provider_id en ambas cuentas (y proyectos con el mismo nombre)
        world.servers = {"p-sg": [server("srv-shared", metadata={"note": FAKE_SK})], "p-mx": [server("srv-mx")]}
        gate = CallGate(3)
        settings = ScanSettings(max_workers=4)

        def scan(client_id, account_id):
            return lambda: scan_account(self.factory, self.keyring, client_id=client_id, account_id=account_id,
                                        services=["ecs"], settings=settings)
        with simulated_huawei(world, gate=gate):
            results = run_in_threads(scan(self.client_id, self.account_id), scan(*other_ids))
        self.assertTrue(all(isinstance(r, uuid.UUID) for r in results), results)
        self.assertLessEqual(gate.peak, 3)
        with self.factory() as session:
            runs = [session.get(ScanRun, r) for r in results]
            self.assertEqual([r.status for r in runs], ["completed", "completed"])
            rows = session.scalars(select(InventoryResource).where(
                InventoryResource.provider_id == "srv-shared")).all()
            self.assertEqual({r.account_id for r in rows}, {self.account_id, other_ids[1]})
            self.assertTrue(all(r.last_run_id in results for r in rows))
            leaked = session.execute(text("SELECT count(*) FROM resources WHERE raw::text LIKE :p"),
                                     {"p": f"%{FAKE_SK}%"}).scalar()
            self.assertEqual(leaked, 0)
            self.assertNotIn(FAKE_AK, repr(session.execute(text("SELECT * FROM scan_tasks")).all()))
