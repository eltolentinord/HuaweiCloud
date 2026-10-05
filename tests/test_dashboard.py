# coding: utf-8
"""Dashboard: página, recursos estáticos, CSP compatible y enlace desde la interfaz actual."""

import re
import unittest

from fastapi.testclient import TestClient

from app import app


class TestDashboardPage(unittest.TestCase):
    def setUp(self):
        self.http = TestClient(app)

    def test_page_and_script_are_served(self):
        page = self.http.get("/dashboard")
        self.assertEqual(page.status_code, 200)
        self.assertIn('src="/static/dashboard.js?v=', page.text)
        script = self.http.get("/static/dashboard.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("function esc(", script.text)  # todo dato de la nube se escapa

    def test_existing_interface_links_to_dashboard(self):
        self.assertIn('href="/dashboard"', self.http.get("/").text)

    def test_csp_allows_every_external_asset_used(self):
        page = self.http.get("/dashboard")
        csp = page.headers["Content-Security-Policy"]
        for url in re.findall(r'(?:src|href)="(https://[^/"]+)', page.text):
            host = url
            self.assertIn(host, csp, f"{host} no está permitido por la CSP")

    def test_dashboard_only_uses_internal_api_routes(self):
        script = self.http.get("/static/dashboard.js").text
        self.assertNotIn("innerHTML = r.name", script)
        for path in re.findall(r"`(/api/[^`$?]*)", script):
            self.assertTrue(path.startswith(("/api/admin/clients", "/api/clients/", "/api/scans/")), path)


if __name__ == "__main__":
    unittest.main()
