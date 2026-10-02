# coding: utf-8
"""Clientes, cuentas, credenciales en BD, catálogo e aislamiento entre clientes."""

from types import SimpleNamespace
from unittest import mock

from sqlalchemy import select, text

from tests.db_helpers import SqliteTestCase, make_keyring
from tests.helpers import FAKE_AK, FAKE_SK, FakeClient, FakeFactory

from core.catalog import REGIONES, SERVICIOS
from core.crypto import FernetKeyring, SecretDecryptionError
from db.models import CloudAccount, Region, ServiceCatalog
from tenancy import accounts, clients, projects
from tenancy.accounts import DatabaseCredentialProvider, credential_context
from tenancy.catalog_sync import sync_catalog
from tenancy.errors import ConflictError, InvalidStateError, NotFoundError, ValidationFailedError
from tenancy.inventory import run_account_inventory


class TestClients(SqliteTestCase):
    def test_crud(self):
        client = clients.create_client(self.session, name="Compañía Ñandú S.A.")
        self.assertEqual(client.slug, "compania-nandu-s-a")
        clients.update_client(self.session, client.id, name="Ñandú", status="suspended", slug="nandu")
        self.assertEqual([c.slug for c in clients.list_clients(self.session, status="suspended")], ["nandu"])
        clients.delete_client(self.session, client.id)
        with self.assertRaises(NotFoundError):
            clients.get_client(self.session, client.id)

    def test_validation_and_uniqueness(self):
        clients.create_client(self.session, name="Acme", slug="acme")
        with self.assertRaises(ConflictError):
            clients.create_client(self.session, name="Otro", slug="acme")
        for kwargs in ({"name": " "}, {"name": "x", "slug": "Bad Slug"}, {"name": "x", "status": "zzz"}):
            with self.assertRaises(ValidationFailedError):
                clients.create_client(self.session, **kwargs)


class TestAccounts(SqliteTestCase):
    def setUp(self):
        super().setUp()
        self.client = clients.create_client(self.session, name="Acme")
        self.account = accounts.create_account(self.session, self.keyring, client_id=self.client.id,
                                               name="prod", ak=FAKE_AK, sk=FAKE_SK)
        self.session.commit()

    def test_stored_encrypted_never_plaintext(self):
        row = self.session.execute(text("SELECT * FROM cloud_accounts")).mappings().one()
        dump = repr(dict(row))
        self.assertNotIn(FAKE_AK, dump)
        self.assertNotIn(FAKE_SK, dump)
        self.assertNotIn(FAKE_SK.encode(), bytes(row["sk_ciphertext"]))
        self.assertEqual(row["key_version"], 1)
        self.assertEqual(row["status"], "pending")

    def test_table_alone_cannot_recover_secret(self):
        row = self.session.scalar(select(CloudAccount))
        with self.assertRaises(SecretDecryptionError):  # otra clave maestra
            make_keyring().decrypt(row.sk_ciphertext, key_version=row.key_version,
                                   context=credential_context(row.id, "sk"))
        with self.assertRaises(SecretDecryptionError):  # AK y SK no intercambiables
            self.keyring.decrypt(row.sk_ciphertext, key_version=1, context=credential_context(row.id, "ak"))

    def test_database_credential_provider(self):
        provider = DatabaseCredentialProvider(self.session, self.keyring, client_id=self.client.id,
                                              account_id=self.account.id)
        creds = provider.get_credentials()
        self.assertEqual((creds.ak, creds.sk), (FAKE_AK, FAKE_SK))
        self.assertNotIn(FAKE_SK, repr(provider))

    def test_disabled_account_cannot_decrypt(self):
        accounts.update_account(self.session, self.client.id, self.account.id, status="disabled")
        with self.assertRaises(InvalidStateError):
            accounts.decrypt_credentials(self.account, self.keyring)

    def test_replace_credentials_resets_validation(self):
        self.account.status = "active"
        accounts.replace_credentials(self.session, self.keyring, self.client.id, self.account.id,
                                     ak="AKNEW-TEST-ONLY", sk="sk-new-test-only")
        self.assertEqual(self.account.status, "pending")
        self.assertEqual(accounts.decrypt_credentials(self.account, self.keyring).ak, "AKNEW-TEST-ONLY")

    def test_reencrypt_all_rotates_key_version(self):
        # Anillo con la clave v1 actual + una nueva v2 como clave vigente.
        rotated = FernetKeyring(keys={1: self.keyring.keys[1], 2: make_keyring().keys[1]}, current=2)
        self.assertEqual(accounts.reencrypt_all(self.session, rotated), 1)
        self.assertEqual(self.account.key_version, 2)
        self.assertEqual(accounts.decrypt_credentials(self.account, rotated).sk, FAKE_SK)
        self.assertEqual(accounts.reencrypt_all(self.session, rotated), 0)

    def test_name_unique_per_client_and_blank_credentials(self):
        with self.assertRaises(ConflictError):
            accounts.create_account(self.session, self.keyring, client_id=self.client.id, name="prod",
                                    ak=FAKE_AK, sk=FAKE_SK)
        with self.assertRaises(ValidationFailedError):
            accounts.create_account(self.session, self.keyring, client_id=self.client.id, name="x",
                                    ak=" ", sk=FAKE_SK)


class TestIsolation(SqliteTestCase):
    """Un cliente nunca puede ver ni usar cuentas/proyectos de otro."""

    def setUp(self):
        super().setUp()
        sync_catalog(self.session)
        self.acme = clients.create_client(self.session, name="Acme")
        self.globex = clients.create_client(self.session, name="Globex")
        self.acme_account = accounts.create_account(self.session, self.keyring, client_id=self.acme.id,
                                                    name="prod", ak=FAKE_AK, sk=FAKE_SK)
        self.acme_project = projects.add_project(self.session, self.acme.id, self.acme_account.id,
                                                 huawei_project_id="p-acme", region_id="ap-southeast-3")

    def test_accounts_scoped_by_client(self):
        with self.assertRaises(NotFoundError):
            accounts.get_account(self.session, self.globex.id, self.acme_account.id)
        self.assertEqual(accounts.list_accounts(self.session, self.globex.id), [])
        with self.assertRaises(NotFoundError):
            accounts.update_account(self.session, self.globex.id, self.acme_account.id, name="hack")
        with self.assertRaises(NotFoundError):
            accounts.delete_account(self.session, self.globex.id, self.acme_account.id)

    def test_projects_scoped_by_client(self):
        with self.assertRaises(NotFoundError):
            projects.list_projects(self.session, self.globex.id, self.acme_account.id)
        with self.assertRaises(NotFoundError):
            projects.get_project(self.session, self.globex.id, self.acme_account.id, self.acme_project.id)

    def test_credential_provider_scoped_by_client(self):
        provider = DatabaseCredentialProvider(self.session, self.keyring, client_id=self.globex.id,
                                              account_id=self.acme_account.id)
        with self.assertRaises(NotFoundError):
            provider.get_credentials()

    def test_inventory_scoped_by_client(self):
        with self.assertRaises(NotFoundError):
            run_account_inventory(self.session, self.keyring, client_id=self.globex.id,
                                  account_id=self.acme_account.id, project_id=self.acme_project.id,
                                  service="ecs")

    def test_project_of_other_account_rejected(self):
        other = accounts.create_account(self.session, self.keyring, client_id=self.acme.id, name="dev",
                                        ak=FAKE_AK, sk=FAKE_SK)
        with self.assertRaises(NotFoundError):
            projects.get_project(self.session, self.acme.id, other.id, self.acme_project.id)


class TestAccountInventory(SqliteTestCase):
    def setUp(self):
        super().setUp()
        sync_catalog(self.session)
        self.client = clients.create_client(self.session, name="Acme")
        self.account = accounts.create_account(self.session, self.keyring, client_id=self.client.id,
                                               name="prod", ak=FAKE_AK, sk=FAKE_SK)
        self.project = projects.add_project(self.session, self.client.id, self.account.id,
                                            huawei_project_id="proj-123", region_id="ap-southeast-3")

    def run_inventory(self, fake):
        captured = {}

        def factory(creds, **kw):
            captured["creds"], captured["kw"] = creds, kw
            return FakeFactory(fake)
        with mock.patch("core.engine.ClientFactory", factory):
            result = run_account_inventory(self.session, self.keyring, client_id=self.client.id,
                                           account_id=self.account.id, project_id=self.project.id,
                                           service="ecs")
        return result, captured

    def test_uses_decrypted_credentials_and_project_region(self):
        fake = FakeClient(list_servers_details=lambda r: SimpleNamespace(servers=[{"id": "s1"}], count=1))
        result, captured = self.run_inventory(fake)
        self.assertEqual((captured["creds"].ak, captured["creds"].sk), (FAKE_AK, FAKE_SK))
        self.assertEqual(captured["kw"], {"endpoint_domain": "myhuaweicloud.com"})
        self.assertEqual((result.region, result.huawei_project_id), ("ap-southeast-3", "proj-123"))
        self.assertEqual(result.resultado["tablas"][0]["filas"][0]["_detalle"]["ECS ID"], "s1")

    def test_disabled_project_or_invalid_account(self):
        projects.set_project_enabled(self.session, self.client.id, self.account.id, self.project.id, False)
        with self.assertRaises(InvalidStateError):
            self.run_inventory(FakeClient())
        projects.set_project_enabled(self.session, self.client.id, self.account.id, self.project.id, True)
        self.account.status = "invalid"
        with self.assertRaises(InvalidStateError):
            self.run_inventory(FakeClient())


class TestCatalogSync(SqliteTestCase):
    def test_sync_from_core_catalog_is_idempotent(self):
        first = sync_catalog(self.session)
        self.assertEqual(first.regions_created, len(REGIONES))
        self.assertEqual(first.services_created, len(SERVICIOS) - 1)
        second = sync_catalog(self.session)
        self.assertEqual((second.regions_created, second.regions_updated,
                          second.services_created, second.services_updated), (0, 0, 0, 0))
        self.assertEqual(self.session.get(ServiceCatalog, "obs").scope, "global")
        self.assertEqual(self.session.get(ServiceCatalog, "ecs").scope, "regional")
        self.assertEqual(self.session.get(Region, "la-north-2").display_name, "LA-Mexico City2")
        self.assertEqual(len(self.session.scalars(select(ServiceCatalog)).all()), 15)

    def test_sync_updates_changed_names(self):
        sync_catalog(self.session)
        self.session.get(Region, "ap-southeast-3").display_name = "viejo"
        self.assertEqual(sync_catalog(self.session).regions_updated, 1)
        self.assertEqual(self.session.get(Region, "ap-southeast-3").display_name, "AP-Singapore")
