# coding: utf-8
"""Cálculos de comparación de costos (puros, sin llamadas de red)."""

from collections import defaultdict
from typing import Any, Dict, List, Optional


def diferencia_absoluta(mes_a: float, mes_b: float) -> float:
    return mes_b - mes_a


def variacion_porcentual(mes_a: float, mes_b: float) -> Optional[float]:
    """Devuelve None cuando mes_a es 0 y mes_b > 0 (servicio "Nuevo")."""
    if mes_a == 0:
        return None if mes_b > 0 else 0.0
    return ((mes_b - mes_a) / mes_a) * 100


def etiqueta_variacion(mes_a: float, mes_b: float) -> str:
    if mes_a == 0 and mes_b > 0:
        return "Nuevo"
    pct = variacion_porcentual(mes_a, mes_b)
    return f"{pct:.2f}%" if pct is not None else "Nuevo"


def _agregar(registros: List[Dict[str, Any]], clave) -> Dict[Any, float]:
    totales: Dict[Any, float] = defaultdict(float)
    for r in registros:
        totales[clave(r)] += r["amount"]
    return totales


def _comparar(a_map: Dict[Any, float], b_map: Dict[Any, float], nombre_clave) -> List[Dict[str, Any]]:
    filas = []
    for clave in sorted(set(a_map) | set(b_map), key=lambda k: str(k)):
        a = a_map.get(clave, 0.0)
        b = b_map.get(clave, 0.0)
        filas.append({
            nombre_clave: clave,
            "mes_a": round(a, 2),
            "mes_b": round(b, 2),
            "diferencia": round(b - a, 2),
            "variacion_pct": variacion_porcentual(a, b),
            "variacion_txt": etiqueta_variacion(a, b),
        })
    return filas


def servicios_nuevos(registros_a, registros_b, clave=lambda r: (r["region"], r["product"])):
    a = _agregar(registros_a, clave)
    b = _agregar(registros_b, clave)
    nuevos = []
    for k in sorted(set(a) | set(b), key=lambda x: str(x)):
        va, vb = a.get(k, 0.0), b.get(k, 0.0)
        if va == 0 and vb > 0:
            region, product = k if isinstance(k, tuple) else (k, k)
            nuevos.append({"region": region, "product": product, "mes_a": 0.0, "mes_b": round(vb, 2)})
    return nuevos


def servicios_eliminados(registros_a, registros_b, clave=lambda r: (r["region"], r["product"])):
    a = _agregar(registros_a, clave)
    b = _agregar(registros_b, clave)
    eliminados = []
    for k in sorted(set(a) | set(b), key=lambda x: str(x)):
        va, vb = a.get(k, 0.0), b.get(k, 0.0)
        if va > 0 and vb == 0:
            region, product = k if isinstance(k, tuple) else (k, k)
            eliminados.append({"region": region, "product": product, "mes_a": round(va, 2), "mes_b": 0.0})
    return eliminados


def tendencia_mensual(mes_a: str, total_a: float, mes_b: str, total_b: float):
    return [
        {"mes": mes_a, "total": round(total_a, 2)},
        {"mes": mes_b, "total": round(total_b, 2)},
    ]


def analizar(registros_a: List[Dict[str, Any]], registros_b: List[Dict[str, Any]],
             mes_a: str, mes_b: str) -> Dict[str, Any]:
    """Agrupa por moneda (nunca mezcla monedas distintas)."""
    monedas_a: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    monedas_b: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in registros_a:
        monedas_a[r["currency"]].append(r)
    for r in registros_b:
        monedas_b[r["currency"]].append(r)

    monedas = sorted(set(monedas_a) | set(monedas_b))
    por_moneda = []
    for moneda in monedas:
        a = monedas_a.get(moneda, [])
        b = monedas_b.get(moneda, [])

        total_a = sum(r["amount"] for r in a)
        total_b = sum(r["amount"] for r in b)
        diff = total_b - total_a
        pct = variacion_porcentual(total_a, total_b)

        por_producto = _comparar(_agregar(a, lambda r: r["product"]), _agregar(b, lambda r: r["product"]), "product")
        por_region = _comparar(_agregar(a, lambda r: r["region"]), _agregar(b, lambda r: r["region"]), "region")
        por_region_producto = _comparar(
            _agregar(a, lambda r: (r["region"], r["product"])),
            _agregar(b, lambda r: (r["region"], r["product"])),
            "clave",
        )
        for fila in por_region_producto:
            region, product = fila.pop("clave")
            fila["region"] = region
            fila["product"] = product

        nuevos = servicios_nuevos(a, b)
        eliminados = servicios_eliminados(a, b)

        aumentos = sorted(por_region_producto, key=lambda f: f["diferencia"], reverse=True)
        reducciones = sorted(por_region_producto, key=lambda f: f["diferencia"])

        region_mayor = max(por_region, key=lambda f: f["mes_b"], default=None)
        producto_top = max(por_producto, key=lambda f: f["mes_b"], default=None)

        por_moneda.append({
            "currency": moneda,
            "total_mes_a": round(total_a, 2),
            "total_mes_b": round(total_b, 2),
            "diferencia": round(diff, 2),
            "variacion_pct": pct,
            "variacion_txt": etiqueta_variacion(total_a, total_b),
            "region_mayor_gasto": region_mayor["region"] if region_mayor else None,
            "producto_mas_costoso": producto_top["product"] if producto_top else None,
            "producto_mas_aumento": aumentos[0]["product"] if aumentos and aumentos[0]["diferencia"] > 0 else None,
            "producto_mas_reduccion": reducciones[0]["product"] if reducciones and reducciones[0]["diferencia"] < 0 else None,
            "por_producto": por_producto,
            "por_region": por_region,
            "por_region_producto": por_region_producto,
            "servicios_nuevos": nuevos,
            "servicios_eliminados": eliminados,
            "mayores_aumentos": [f for f in aumentos if f["diferencia"] > 0][:10],
            "mayores_reducciones": [f for f in reducciones if f["diferencia"] < 0][:10],
            "tendencia_mensual": tendencia_mensual(mes_a, total_a, mes_b, total_b),
        })

    return {"monedas": por_moneda, "total_registros_a": len(registros_a), "total_registros_b": len(registros_b)}
