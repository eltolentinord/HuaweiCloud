# coding: utf-8
"""Modelos, relaciones y constraints (SQLite en memoria)."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tests.db_helpers import SqliteTestCase
from tests.helpers import FAKE_AK, FAKE_SK

from db.models import Client, CloudAccount, Project, Region, User, UserClientRole
from tenancy import accounts, clients


class TestModels(SqliteTestCase):
    def setUp(self):
        super().setUp()
        self.session.add(Region(id="ap-southeast-3", display_name="AP-Singapore"))
        self.client = clients.create_client(self.session, name="Acme")
        self.account = accounts.create_account(self.session, self.keyring, client_id=self.client.id,
                                               name="prod", ak=FAKE_AK, sk=FAKE_SK)
        self.project = Project(account_id=self.account.id, huawei_project_id="p1", region_id="ap-southeast-3")
        self.session.add(self.project)
        self.session.flush()

    def test_relationships(self):
        self.session.expire_all()
        client = self.session.get(Client, self.client.id)
        self.assertEqual([a.name for a in client.accounts], ["prod"])
        self.assertEqual([p.huawei_project_id for p in client.accounts[0].projects], ["p1"])
        self.assertEqual(client.accounts[0].projects[0].region.display_name, "AP-Singapore")

    def test_timestamps_are_set(self):
        self.session.refresh(self.client)
        self.assertIsNotNone(self.client.created_at)
        self.assertIsNotNone(self.client.updated_at)

    def test_delete_client_cascades_accounts_and_projects(self):
        self.session.delete(self.client)
        self.session.flush()
        self.assertEqual(self.session.scalars(select(CloudAccount)).all(), [])
        self.assertEqual(self.session.scalars(select(Project)).all(), [])

    def test_region_in_use_cannot_be_deleted(self):
        self.session.delete(self.session.get(Region, "ap-southeast-3"))
        with self.assertRaises(IntegrityError):
            self.session.flush()

    def test_duplicate_project_rejected(self):
        self.session.add(Project(account_id=self.account.id, huawei_project_id="p1", region_id="ap-southeast-3"))
        with self.assertRaises(IntegrityError):
            self.session.flush()

    def test_same_project_id_allowed_in_other_account(self):
        other = accounts.create_account(self.session, self.keyring, client_id=self.client.id,
                                        name="dev", ak=FAKE_AK, sk=FAKE_SK)
        self.session.add(Project(account_id=other.id, huawei_project_id="p1", region_id="ap-southeast-3"))
        self.session.flush()

    def test_status_check_constraint(self):
        self.client.status = "bogus"
        with self.assertRaises(IntegrityError):
            self.session.flush()

    def test_user_access_to_multiple_clients(self):
        other = clients.create_client(self.session, name="Globex")
        user = User(email="ops@example.com")
        self.session.add(user)
        self.session.flush()
        clients.grant_access(self.session, user_id=user.id, client_id=self.client.id, role="admin")
        clients.grant_access(self.session, user_id=user.id, client_id=other.id)
        self.assertEqual([c.name for c in clients.clients_for_user(self.session, user.id)], ["Acme", "Globex"])
        self.assertEqual(self.session.get(UserClientRole, (user.id, self.client.id)).role, "admin")
        user.is_active = False
        self.session.flush()
        self.assertEqual(clients.clients_for_user(self.session, user.id), [])

    def test_repr_never_contains_secrets_or_ciphertext(self):
        text = repr(self.account)
        self.assertNotIn(FAKE_AK, text)
        self.assertNotIn("ciphertext", text)
