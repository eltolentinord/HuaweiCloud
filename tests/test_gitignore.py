# coding: utf-8
"""``.env`` y demás archivos con secretos quedan ignorados por Git."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT

MUST_IGNORE = [".env", ".env.local", ".env.production", "config.json", "output/x.xlsx"]
MUST_TRACK = [".env.example", "app.py", "alembic.ini", "migrations/env.py"]


@unittest.skipIf(shutil.which("git") is None, "git no está instalado")
class TestGitIgnore(unittest.TestCase):
    """Usa ``git check-ignore`` real en un repositorio temporal con el .gitignore del proyecto."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        repo = Path(cls.tmp.name)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        shutil.copy(Path(ROOT) / ".gitignore", repo / ".gitignore")
        cls.repo = repo

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def ignored(self, path: str) -> bool:
        result = subprocess.run(["git", "check-ignore", "-q", path], cwd=self.repo)
        return result.returncode == 0

    def test_secret_files_ignored(self):
        for path in MUST_IGNORE:
            self.assertTrue(self.ignored(path), path)

    def test_project_files_tracked(self):
        for path in MUST_TRACK:
            self.assertFalse(self.ignored(path), path)


class TestEnvExample(unittest.TestCase):
    def test_env_example_has_no_values_for_secrets(self):
        values = {}
        for line in (Path(ROOT) / ".env.example").read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
        for key in ("HUAWEI_AK", "HUAWEI_SK", "HUAWEI_PROJECT_ID", "INVENTORY_ENCRYPTION_KEYS"):
            self.assertIn(key, values)
            self.assertEqual(values[key], "", key)
        self.assertIn("USUARIO:CLAVE@", values["DATABASE_URL"])  # solo marcadores, sin clave real


if __name__ == "__main__":
    unittest.main()
