# coding: utf-8
"""Pruebas unitarias de cálculo de costos (sin llamadas reales)."""

import unittest

from services.cost_analysis import (
    analizar,
    diferencia_absoluta,
    etiqueta_variacion,
    servicios_eliminados,
    servicios_nuevos,
    variacion_porcentual,
)


def _r(region, product, currency, amount):
    return {"region": region, "product": product, "currency": currency, "amount": amount}


class TestVariacion(unittest.TestCase):
    def test_diferencia_absoluta(self):
        self.assertEqual(diferencia_absoluta(100, 150), 50)
        self.assertEqual(diferencia_absoluta(200, 150), -50)

    def test_variacion_normal(self):
        self.assertAlmostEqual(variacion_porcentual(100, 150), 50.0)
        self.assertAlmostEqual(variacion_porcentual(200, 100), -50.0)

    def test_division_entre_cero_nuevo(self):
        self.assertIsNone(variacion_porcentual(0, 100))  # "Nuevo"
        self.assertEqual(etiqueta_variacion(0, 100), "Nuevo")

    def test_ambos_cero(self):
        self.assertEqual(variacion_porcentual(0, 0), 0.0)
        self.assertEqual(etiqueta_variacion(0, 0), "0.00%")

    def test_reduccion_total(self):
        self.assertAlmostEqual(variacion_porcentual(100, 0), -100.0)


class TestServicios(unittest.TestCase):
    def setUp(self):
        self.a = [_r("sg1", "ECS", "USD", 100), _r("sg1", "EVS", "USD", 50)]
        self.b = [_r("sg1", "ECS", "USD", 150), _r("sg1", "OBS", "USD", 80)]

    def test_servicios_nuevos(self):
        nuevos = servicios_nuevos(self.a, self.b)
        self.assertEqual(len(nuevos), 1)
        self.assertEqual(nuevos[0]["product"], "OBS")
        self.assertEqual(nuevos[0]["mes_a"], 0.0)
        self.assertEqual(nuevos[0]["mes_b"], 80)

    def test_servicios_eliminados(self):
        eliminados = servicios_eliminados(self.a, self.b)
        self.assertEqual(len(eliminados), 1)
        self.assertEqual(eliminados[0]["product"], "EVS")


class TestAnalizar(unittest.TestCase):
    def test_monedas_distintas_no_se_mezclan(self):
        a = [_r("sg1", "ECS", "USD", 100), _r("sg1", "ECS", "CNY", 200)]
        b = [_r("sg1", "ECS", "USD", 120), _r("sg1", "ECS", "CNY", 180)]
        resultado = analizar(a, b, "2026-08", "2026-09")
        self.assertEqual(len(resultado["monedas"]), 2)
        usd = next(m for m in resultado["monedas"] if m["currency"] == "USD")
        cny = next(m for m in resultado["monedas"] if m["currency"] == "CNY")
        self.assertEqual(usd["total_mes_a"], 100)
        self.assertEqual(usd["total_mes_b"], 120)
        self.assertEqual(cny["total_mes_a"], 200)
        self.assertEqual(cny["total_mes_b"], 180)

    def test_totales_y_top(self):
        a = [_r("sg1", "ECS", "USD", 100)]
        b = [_r("sg1", "ECS", "USD", 300), _r("sg1", "OBS", "USD", 50)]
        resultado = analizar(a, b, "2026-08", "2026-09")
        m = resultado["monedas"][0]
        self.assertEqual(m["total_mes_a"], 100)
        self.assertEqual(m["total_mes_b"], 350)
        self.assertEqual(m["producto_mas_costoso"], "ECS")
        self.assertEqual(m["producto_mas_aumento"], "ECS")
        self.assertEqual(len(m["servicios_nuevos"]), 1)

    def test_vacio(self):
        resultado = analizar([], [], "2026-08", "2026-09")
        self.assertEqual(resultado["monedas"], [])


if __name__ == "__main__":
    unittest.main()
