# coding: utf-8
"""Cifrado autenticado de secretos y configuración de claves maestras."""

import unittest

from tests.db_helpers import make_keyring
from tests.helpers import FAKE_SK

from core.crypto import (
    CryptoConfigurationError,
    FernetKeyring,
    SecretDecryptionError,
    generate_key,
)

CTX = "cloud_account:1234:sk"


class TestFernetKeyring(unittest.TestCase):
    def test_roundtrip(self):
        keyring = make_keyring()
        token, version = keyring.encrypt(FAKE_SK, context=CTX)
        self.assertEqual(version, 1)
        self.assertNotIn(FAKE_SK.encode(), token)
        self.assertEqual(keyring.decrypt(token, key_version=1, context=CTX), FAKE_SK)

    def test_ciphertext_is_randomized(self):
        keyring = make_keyring()
        self.assertNotEqual(keyring.encrypt(FAKE_SK, context=CTX)[0], keyring.encrypt(FAKE_SK, context=CTX)[0])

    def test_context_binding_prevents_swapping(self):
        keyring = make_keyring()
        token, version = keyring.encrypt(FAKE_SK, context=CTX)
        with self.assertRaises(SecretDecryptionError):
            keyring.decrypt(token, key_version=version, context="cloud_account:9999:sk")

    def test_tampered_token_fails(self):
        keyring = make_keyring()
        token, version = keyring.encrypt(FAKE_SK, context=CTX)
        tampered = token[:-5] + (b"A" if token[-5:-4] != b"A" else b"B") + token[-4:]
        with self.assertRaises(SecretDecryptionError):
            keyring.decrypt(tampered, key_version=version, context=CTX)

    def test_wrong_master_key_fails_without_leaking(self):
        token, _ = make_keyring().encrypt(FAKE_SK, context=CTX)
        with self.assertRaises(SecretDecryptionError) as ctx:
            make_keyring().decrypt(token, key_version=1, context=CTX)
        self.assertNotIn(FAKE_SK, str(ctx.exception))

    def test_unknown_version(self):
        token, _ = make_keyring().encrypt(FAKE_SK, context=CTX)
        with self.assertRaises(SecretDecryptionError):
            make_keyring().decrypt(token, key_version=7, context=CTX)

    def test_rotation(self):
        keys = {1: generate_key(), 2: generate_key()}
        old = FernetKeyring.from_keys(keys, current=1)
        new = FernetKeyring.from_keys(keys)  # actual = mayor versión
        token, version = old.encrypt(FAKE_SK, context=CTX)
        new_token, new_version = new.reencrypt(token, key_version=version, context=CTX)
        self.assertEqual((version, new_version), (1, 2))
        self.assertEqual(new.decrypt(new_token, key_version=2, context=CTX), FAKE_SK)

    def test_empty_secret_rejected(self):
        with self.assertRaises(ValueError):
            make_keyring().encrypt("", context=CTX)

    def test_repr_hides_keys(self):
        key = generate_key()
        self.assertNotIn(key, repr(FernetKeyring.from_keys({1: key})))


class TestKeyConfiguration(unittest.TestCase):
    def test_from_env(self):
        k1, k2 = generate_key(), generate_key()
        keyring = FernetKeyring.from_env({"INVENTORY_ENCRYPTION_KEYS": f"1:{k1}, 2:{k2}"})
        self.assertEqual(keyring.current_version, 2)
        keyring = FernetKeyring.from_env({"INVENTORY_ENCRYPTION_KEYS": f"1:{k1},2:{k2}",
                                          "INVENTORY_ENCRYPTION_KEY_VERSION": "1"})
        self.assertEqual(keyring.current_version, 1)

    def test_missing_or_invalid_configuration(self):
        for env in ({}, {"INVENTORY_ENCRYPTION_KEYS": ""}, {"INVENTORY_ENCRYPTION_KEYS": "nokey"},
                    {"INVENTORY_ENCRYPTION_KEYS": "1:not-a-fernet-key"},
                    {"INVENTORY_ENCRYPTION_KEYS": f"0:{generate_key()}"},
                    {"INVENTORY_ENCRYPTION_KEYS": f"1:{generate_key()}", "INVENTORY_ENCRYPTION_KEY_VERSION": "3"}):
            with self.subTest(env=list(env)):
                with self.assertRaises(CryptoConfigurationError):
                    FernetKeyring.from_env(env)

    def test_configuration_errors_do_not_echo_keys(self):
        bad = "1:" + "x" * 44
        with self.assertRaises(CryptoConfigurationError) as ctx:
            FernetKeyring.from_env({"INVENTORY_ENCRYPTION_KEYS": bad})
        self.assertNotIn("x" * 44, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
