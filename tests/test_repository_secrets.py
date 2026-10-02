# coding: utf-8
"""Barrido del repositorio: no debe haber credenciales con formato Huawei Cloud."""

import re
import unittest

from tests.helpers import ROOT

# AK de Huawei Cloud: 20 caracteres [A-Z0-9]; SK: 40 caracteres alfanuméricos.
AK_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Z0-9]{20}(?![A-Za-z0-9])")
SK_PATTERN = re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9]*[a-z])(?=[A-Za-z0-9]*[A-Z])(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{40}(?![A-Za-z0-9])")
# Clave maestra Fernet (32 bytes en base64 url-safe = 44 caracteres terminados en "=").
FERNET_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_=-])")
# URL de base de datos con contraseña embebida (usuario:clave@host).
DB_PASSWORD_PATTERN = re.compile(r"postgresql(?:\+\w+)?://[^:/\s@]+:(?!CLAVE@)[^@\s/]+@")
PATTERNS = (AK_PATTERN, SK_PATTERN, FERNET_PATTERN, DB_PASSWORD_PATTERN)
SCANNED = {".py", ".json", ".html", ".js", ".txt", ".md", ".css", ".example", ".ini", ".mako", ".cfg", ".toml"}
EXCLUDED_DIRS = {".venv", "__pycache__", ".git", "output", ".mypy_cache", ".pytest_cache"}


def project_files():
    for path in ROOT.rglob("*"):
        if path.is_file() and path.suffix in SCANNED and not EXCLUDED_DIRS & set(path.parts):
            yield path


class TestNoCredentialsInRepository(unittest.TestCase):
    def test_no_ak_or_sk_like_strings(self):
        offenders = []
        for path in project_files():
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pattern in PATTERNS:
                for match in pattern.finditer(text):
                    offenders.append(f"{path.relative_to(ROOT)}: {match.group()[:4]}…")
        self.assertEqual(offenders, [])

    def test_config_json_has_no_credentials(self):
        import json
        config = ROOT / "config.json"
        if config.exists():
            data = json.loads(config.read_text(encoding="utf-8"))
            for key in ("HUAWEI_AK", "HUAWEI_SK", "HUAWEI_PROJECT_ID"):
                self.assertFalse(data.get(key), key)


if __name__ == "__main__":
    unittest.main()
