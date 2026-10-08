# coding: utf-8
"""Migraciones Alembic y almacenamiento cifrado sobre PostgreSQL REAL.

Usa ``TEST_DATABASE_URL`` o, si no existe, PostgreSQL embebido (``pgserver``).
Si no hay PostgreSQL disponible, los tests se marcan SKIPPED.
"""

import unittest
from pathlib import Path

from tests.db_helpers import create_temp_database, drop_temp_database, make_keyring
from tests.helpers import FAKE_AK, FAKE_SK, ROOT, api_error

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from db.base import Base
from db.models import CloudAccount
from tenancy import accounts, clients, projects
from tenancy.catalog_sync import sync_catalog

EXPECTED_TABLES = {"alembic_version", "clients", "users", "user_client_roles", "cloud_accounts",
                   "regions", "projects", "service_catalog", "scan_runs", "scan_tasks", "resources",
                   "resource_changes", "scan_schedules",
                   "audit_events", "price_catalog_entries", "flavor_catalog", "enterprise_projects",
                   "volume_type_catalog", "rds_flavor_catalog", "bss_code_catalog",
                   "servers", "server_audit_runs",
                   "ces_alarm_events", "diagnostic_incidents", "diagnostic_commands",
                   "diagnostic_evidence", "diagnostic_audit_logs"}
HEAD = "0013"


def alembic_config(url: str) -> Config:
    config = Config(str(Path(ROOT) / "alembic.ini"))
    config.attributes["database_url"] = url
    return config


class PostgresTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = create_temp_database()
        if cls.url is None:
            raise unittest.SkipTest("PostgreSQL no disponible (define TEST_DATABASE_URL o instala pgserver)")
        cls.engine = create_engine(cls.url, poolclass=NullPool)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        drop_temp_database(cls.url)


class TestMigrations(PostgresTestCase):
    def test_upgrade_head_on_clean_database_matches_models(self):
        command.upgrade(alembic_config(self.url), "head")
        inspector = inspect(self.engine)
        self.assertTrue(EXPECTED_TABLES <= set(inspector.get_table_names()))
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT version_num FROM alembic_version")).scalar(), HEAD)
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        self.assertEqual(diff, [], "los modelos y la migración difieren")

        uniques = {tuple(u["column_names"]) for u in inspector.get_unique_constraints("projects")}
        self.assertIn(("account_id", "huawei_project_id"), uniques)
        columns = {c["name"]: c for c in inspector.get_columns("cloud_accounts")}
        self.assertEqual(columns["sk_ciphertext"]["type"].__class__.__name__, "BYTEA")
        self.assertNotIn("ak", columns)
        self.assertNotIn("sk", columns)

        resource_uniques = {tuple(u["column_names"]) for u in inspector.get_unique_constraints("resources")}
        self.assertIn(("account_id", "resource_type", "scope_key", "provider_id"), resource_uniques)
        raw_type = {c["name"]: c for c in inspector.get_columns("resources")}["raw"]["type"]
        self.assertEqual(raw_type.__class__.__name__, "JSONB")
        gin = {i["name"]: i for i in inspector.get_indexes("resources")}
        self.assertEqual(gin["ix_resources_tags"]["dialect_options"].get("postgresql_using"), "gin")

        command.downgrade(alembic_config(self.url), "0001")  # deshace 0005..0002
        self.assertNotIn("resources", inspect(self.engine).get_table_names())
        self.assertIn("cloud_accounts", inspect(self.engine).get_table_names())
        command.downgrade(alembic_config(self.url), "base")
        self.assertEqual(set(inspect(self.engine).get_table_names()) - {"alembic_version"}, set())
        command.upgrade(alembic_config(self.url), "head")


class TestEncryptedStorageOnPostgres(PostgresTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        command.upgrade(alembic_config(cls.url), "head")

    def setUp(self):
        self.session = sessionmaker(bind=self.engine)()
        self.keyring = make_keyring()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def test_secrets_are_ciphertext_in_postgres(self):
        sync_catalog(self.session)
        client = clients.create_client(self.session, name="Acme PG")
        account = accounts.create_account(self.session, self.keyring, client_id=client.id, name="prod",
                                          ak=FAKE_AK, sk=FAKE_SK)
        projects.add_project(self.session, client.id, account.id, huawei_project_id="p1",
                             region_id="ap-southeast-3")
        self.session.flush()
        row = self.session.execute(text(
            "SELECT encode(ak_ciphertext, 'escape') AS ak, encode(sk_ciphertext, 'escape') AS sk, "
            "key_version FROM cloud_accounts WHERE id = :id"), {"id": account.id}).mappings().one()
        self.assertNotIn(FAKE_AK, row["ak"])
        self.assertNotIn(FAKE_SK, row["sk"])
        found = self.session.execute(text(
            "SELECT count(*) FROM cloud_accounts WHERE position(convert_to(:s, 'UTF8') in sk_ciphertext) > 0"),
            {"s": FAKE_SK}).scalar()
        self.assertEqual(found, 0)
        stored = self.session.get(CloudAccount, account.id)
        self.assertEqual(accounts.decrypt_credentials(stored, self.keyring).sk, FAKE_SK)

    def test_constraints_enforced_by_postgres(self):
        client = clients.create_client(self.session, name="Constraints")
        self.session.flush()
        with self.assertRaises(IntegrityError):
            self.session.execute(text("UPDATE clients SET status = 'bogus' WHERE id = :id"), {"id": client.id})


class TestScanEngineOnPostgres(PostgresTestCase):
    """Escaneo completo sobre PostgreSQL real: JSONB, upsert, deleted_at y ausencia de secretos."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        command.upgrade(alembic_config(cls.url), "head")

    def test_full_scan_cycle(self):
        from tests.scan_helpers import ScanWorld, seed_account, server, simulated_huawei
        from scanning.engine import scan_account
        from db.models import ScanRun

        factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        keyring = make_keyring()
        with factory() as session:
            client, account, _ = seed_account(session, keyring, client_name="PG Scan")
            client_id, account_id = client.id, account.id
        world = ScanWorld()
        world.servers = {"p-sg": [server("srv-1", "web", metadata={"note": FAKE_SK}),
                                  server("srv-2", "db")], "p-mx": [server("srv-1", "mx")]}
        world.buckets = [{"name": "logs", "location": "la-north-2"}]

        def scan():
            with simulated_huawei(world):
                return scan_account(factory, keyring, client_id=client_id, account_id=account_id,
                                    services=["ecs", "obs"])

        first = scan()
        world.servers["p-sg"] = [server("srv-1", "web-v2")]
        world.failures["list_buckets"] = api_error(500, "boom")
        second = scan()
        with factory() as session:
            runs = {r.id: r for r in session.query(ScanRun)}
            self.assertEqual(runs[first].status, "completed")
            self.assertEqual(runs[second].status, "completed_with_errors")
            self.assertEqual((runs[second].total_updated, runs[second].total_deleted), (1, 1))
            rows = session.execute(text(
                "SELECT provider_id, region, deleted_at IS NOT NULL AS deleted, raw->>'name' AS raw_name, "
                "jsonb_typeof(raw) AS kind FROM resources ORDER BY provider_id, region")).all()
            self.assertEqual([tuple(r) for r in rows], [
                ("logs", "la-north-2", False, "logs", "object"),      # OBS falló: no se marca eliminado
                ("srv-1", "ap-southeast-3", False, "web-v2", "object"),
                ("srv-1", "la-north-2", False, "mx", "object"),        # mismo provider_id, otro proyecto
                ("srv-2", "ap-southeast-3", True, "db", "object"),
            ])
            for secret in (FAKE_AK, FAKE_SK):
                found = session.execute(text(
                    "SELECT (SELECT count(*) FROM resources WHERE raw::text LIKE :p OR attributes::text LIKE :p "
                    "OR coalesce(name, '') LIKE :p) + (SELECT count(*) FROM scan_tasks WHERE "
                    "coalesce(error_message_safe, '') LIKE :p OR notices::text LIKE :p)"),
                    {"p": f"%{secret}%"}).scalar()
                self.assertEqual(found, 0)


if __name__ == "__main__":
    unittest.main()
