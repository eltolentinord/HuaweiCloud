# coding: utf-8
"""Cálculos de comparación de costos (puros, sin llamadas de red)."""

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from core.catalog import REGIONES

# Códigos de servicio de BSS (``hws.service.type.<x>``) -> (sigla, nombre). Si un código
# no está aquí se usa el nombre que devuelve BSS o la sigla derivada del código.
SERVICIOS_BSS: Dict[str, Tuple[str, str]] = {
    "ec2": ("ECS", "Elastic Cloud Server"),
    "ebs": ("EVS", "Elastic Volume Service"),
    "evs": ("EVS", "Elastic Volume Service"),
    "vpc": ("VPC", "Virtual Private Cloud"),
    "eip": ("EIP", "Elastic IP"),
    "obs": ("OBS", "Object Storage Service"),
    "cbr": ("CBR", "Cloud Backup and Recovery"),
    "vpn": ("VPN", "Virtual Private Network"),
    "hss": ("HSS", "Host Security Service"),
    "elb": ("ELB", "Elastic Load Balance"),
    "rds": ("RDS", "Relational Database Service"),
    "dcs": ("DCS", "Distributed Cache Service"),
    "nat": ("NAT", "NAT Gateway"),
    "natgateway": ("NAT", "NAT Gateway"),
    "waf": ("WAF", "Web Application Firewall"),
    "cfw": ("CFW", "Cloud Firewall"),
    "ces": ("CES", "Cloud Eye"),
    "smn": ("SMN", "Simple Message Notification"),
    "lts": ("LTS", "Log Tank Service"),
    "ims": ("IMS", "Image Management Service"),
    "kms": ("DEW", "Data Encryption Workshop"),
    "dew": ("DEW", "Data Encryption Workshop"),
    "modelarts": ("ModelArts", "ModelArts"),
    "cce": ("CCE", "Cloud Container Engine"),
    "swr": ("SWR", "SoftWare Repository for Container"),
    "dns": ("DNS", "Domain Name Service"),
    "cdn": ("CDN", "Content Delivery Network"),
    "sms": ("SMS", "Server Migration Service"),
    "oms": ("OMS", "Object Storage Migration Service"),
    "dds": ("DDS", "Document Database Service"),
    "gaussdb": ("GaussDB", "GaussDB"),
    "functiongraph": ("FunctionGraph", "FunctionGraph"),
    "apig": ("APIG", "API Gateway"),
}
_PREFIJO_SERVICIO = "hws.service.type."
_NOMBRES_REGION = {r["id"]: r["nombre"] for r in REGIONES}
_MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre")
TOP_RECURSOS = 15
TOP_CAMBIOS = 10


def nombre_servicio(codigo: Optional[str], nombre_bss: Optional[str] = None) -> Dict[str, str]:
    """``{codigo, sigla, nombre}`` legibles para un código de servicio de BSS."""
    codigo = (codigo or "").strip()
    clave = codigo[len(_PREFIJO_SERVICIO):] if codigo.startswith(_PREFIJO_SERVICIO) else codigo
    if clave.lower() in SERVICIOS_BSS:
        sigla, nombre = SERVICIOS_BSS[clave.lower()]
    else:
        sigla = clave.upper() if clave else "Sin servicio"
        nombre = (nombre_bss or "").strip() or sigla
    return {"codigo": codigo or "Sin servicio", "sigla": sigla, "nombre": nombre}


def _latino(texto: str) -> bool:
    try:
        texto.encode("latin-1")
        return True
    except UnicodeEncodeError:
        return False


def nombre_region(codigo: Optional[str], nombres: Optional[Dict[str, str]] = None) -> str:
    """Nombre comercial de la región: el del catálogo; si no está, el que devuelve BSS (solo si
    está en alfabeto latino: BSS puede devolverlo en chino, p. ej. "中国-香港"); si no, el código."""
    codigo = (codigo or "").strip()
    de_bss = ((nombres or {}).get(codigo) or "").strip()
    return _NOMBRES_REGION.get(codigo) or (de_bss if de_bss and _latino(de_bss) else "") or codigo or "Sin región"


def nombre_mes(mes: str) -> str:
    """``2026-07`` -> ``julio 2026`` (si el formato no cuadra, se devuelve tal cual)."""
    try:
        anio, numero = mes.split("-")
        return f"{_MESES[int(numero) - 1]} {anio}"
    except (ValueError, IndexError, AttributeError):
        return str(mes)


def dinero_txt(valor: float, moneda: str, signo: bool = False) -> str:
    texto = f"{moneda} {abs(valor):,.2f}"
    if signo:
        return ("+ " if valor >= 0 else "- ") + texto
    return ("- " + texto) if valor < 0 else texto


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


def _nombrar(filas: List[Dict[str, Any]], nombres_region: Optional[Dict[str, str]] = None) -> None:
    """Añade nombres legibles (aditivo: las claves originales no cambian)."""
    for fila in filas:
        if "product" in fila:
            servicio = nombre_servicio(fila["product"])
            fila["servicio_sigla"], fila["servicio_nombre"] = servicio["sigla"], servicio["nombre"]
        if "region" in fila:
            fila["region_nombre"] = nombre_region(fila["region"], nombres_region)


def _por_region_detalle(por_region: List[Dict[str, Any]], por_region_producto: List[Dict[str, Any]]):
    """Cada región con sus servicios (mayor gasto en el mes B primero)."""
    detalle = []
    for region in sorted(por_region, key=lambda f: (-f["mes_b"], -f["mes_a"], str(f["region"]))):
        servicios = [f for f in por_region_producto if f["region"] == region["region"]]
        servicios.sort(key=lambda f: (-f["mes_b"], -f["mes_a"], str(f["product"])))
        detalle.append({**region, "servicios": servicios})
    return detalle


def analizar(registros_a: List[Dict[str, Any]], registros_b: List[Dict[str, Any]],
             mes_a: str, mes_b: str, nombres_region: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
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
        _nombrar(por_producto + por_region_producto + por_region, nombres_region)

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
            "por_region_detalle": _por_region_detalle(por_region, por_region_producto),
        })

    return {"monedas": por_moneda, "total_registros_a": len(registros_a), "total_registros_b": len(registros_b)}


# ------------------------------------------------------------------ por recurso
def _clave_recurso(r: Dict[str, Any]) -> str:
    return r.get("resource_id") or f"{r.get('service_code')}|{r.get('region')}|{r.get('resource_name')}"


def _agregar_recursos(registros: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Suma importes por recurso conservando sus datos descriptivos."""
    recursos: Dict[str, Dict[str, Any]] = {}
    for r in registros:
        clave = _clave_recurso(r)
        if clave not in recursos:
            servicio = nombre_servicio(r.get("service_code"), r.get("service_name"))
            region = r.get("region") or ""
            recursos[clave] = {
                "resource_id": r.get("resource_id") or "",
                "resource_name": r.get("resource_name") or r.get("resource_id") or "Sin nombre",
                "servicio_sigla": servicio["sigla"],
                "servicio_nombre": servicio["nombre"],
                "tipo": r.get("resource_type_name") or "",
                "spec": r.get("spec") or "",
                "region": region,
                "region_nombre": nombre_region(region, {region: r.get("region_name") or ""}),
                "importe": 0.0,
            }
        recursos[clave]["importe"] += float(r.get("amount") or 0.0)
    return recursos


def _sin_privados(lista: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{k: v for k, v in f.items() if not k.startswith("_")} for f in lista]


def analizar_recursos(recursos_a: List[Dict[str, Any]], recursos_b: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Comparación por recurso (registros de consumo de BSS), separada por moneda."""
    por_moneda_a: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    por_moneda_b: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in recursos_a:
        por_moneda_a[r.get("currency") or "Sin moneda"].append(r)
    for r in recursos_b:
        por_moneda_b[r.get("currency") or "Sin moneda"].append(r)

    monedas = []
    for moneda in sorted(set(por_moneda_a) | set(por_moneda_b)):
        a = _agregar_recursos(por_moneda_a.get(moneda, []))
        b = _agregar_recursos(por_moneda_b.get(moneda, []))
        filas = []
        for clave in sorted(set(a) | set(b)):
            base = b.get(clave) or a[clave]
            va = a[clave]["importe"] if clave in a else 0.0
            vb = b[clave]["importe"] if clave in b else 0.0
            fila = {k: v for k, v in base.items() if k != "importe"}
            fila.update(mes_a=round(va, 2), mes_b=round(vb, 2), diferencia=round(vb - va, 2),
                        variacion_txt=etiqueta_variacion(va, vb), _a=va, _b=vb)
            filas.append(fila)

        orden_b = sorted((f for f in filas if f["_b"] > 0), key=lambda f: (-f["_b"], f["resource_name"]))
        orden_a = sorted((f for f in filas if f["_a"] > 0), key=lambda f: (-f["_a"], f["resource_name"]))
        aumentos = sorted((f for f in filas if f["_b"] - f["_a"] > 0.005),
                          key=lambda f: (-(f["_b"] - f["_a"]), f["resource_name"]))
        reducciones = sorted((f for f in filas if f["_a"] - f["_b"] > 0.005),
                             key=lambda f: (-(f["_a"] - f["_b"]), f["resource_name"]))
        nuevos = sorted((f for f in filas if f["_a"] == 0 and f["_b"] > 0),
                        key=lambda f: (-f["_b"], f["resource_name"]))
        eliminados = sorted((f for f in filas if f["_a"] > 0 and f["_b"] == 0),
                            key=lambda f: (-f["_a"], f["resource_name"]))
        monedas.append({
            "currency": moneda,
            "recursos_mes_a": len(orden_a),
            "recursos_mes_b": len(orden_b),
            "total_mes_a": round(sum(f["_a"] for f in filas), 2),
            "total_mes_b": round(sum(f["_b"] for f in filas), 2),
            "top_mes_a": _sin_privados(orden_a[:TOP_RECURSOS]),
            "top_mes_b": _sin_privados(orden_b[:TOP_RECURSOS]),
            "mayores_aumentos": _sin_privados(aumentos[:TOP_CAMBIOS]),
            "mayores_reducciones": _sin_privados(reducciones[:TOP_CAMBIOS]),
            "nuevos": _sin_privados(nuevos),
            "eliminados": _sin_privados(eliminados),
        })
    return {"monedas": monedas}


# ------------------------------------------------------------------ hallazgos
def hallazgos(analisis: Dict[str, Any], mes_a: str, mes_b: str, preliminar: bool = False) -> Dict[str, List[str]]:
    """Frases generadas SOLO a partir de los importes (nada inventado).

    ``hallazgos``: lo que dicen los datos. ``revisar``: puntos objetivos a revisar.
    """
    nombre_a, nombre_b = nombre_mes(mes_a), nombre_mes(mes_b)
    salida: List[str] = []
    revisar: List[str] = []
    recursos = {m["currency"]: m for m in (analisis.get("recursos") or {}).get("monedas", [])}
    for m in analisis.get("monedas", []):
        cur, diff = m["currency"], m["diferencia"]
        if m["total_mes_a"] == 0 and m["total_mes_b"] == 0:
            continue
        if diff == 0:
            salida.append(f"El consumo total se mantuvo en {dinero_txt(m['total_mes_b'], cur)} "
                          f"entre {nombre_a} y {nombre_b}.")
        else:
            verbo = "aumentó" if diff > 0 else "disminuyó"
            salida.append(f"El consumo total {verbo} de {dinero_txt(m['total_mes_a'], cur)} en {nombre_a} a "
                          f"{dinero_txt(m['total_mes_b'], cur)} en {nombre_b} "
                          f"({dinero_txt(diff, cur, signo=True)}, {m['variacion_txt']}).")
        for region in sorted(m.get("por_region", []), key=lambda f: -abs(f["diferencia"])):
            nombre = region.get("region_nombre") or region["region"]
            if region["diferencia"] > 0:
                salida.append(f"{nombre} aumentó {dinero_txt(region['diferencia'], cur)}.")
            elif region["diferencia"] < 0:
                salida.append(f"{nombre} disminuyó {dinero_txt(-region['diferencia'], cur)}.")
        servicios = [f for f in m.get("por_producto", []) if f["diferencia"] != 0]
        subida = max(servicios, key=lambda f: f["diferencia"], default=None)
        bajada = min(servicios, key=lambda f: f["diferencia"], default=None)
        if subida and subida["diferencia"] > 0:
            salida.append(f"{subida.get('servicio_sigla') or subida['product']} fue el servicio con mayor aumento: "
                          f"{dinero_txt(subida['diferencia'], cur, signo=True)}.")
        if bajada and bajada["diferencia"] < 0:
            salida.append(f"{bajada.get('servicio_sigla') or bajada['product']} fue el servicio con mayor "
                          f"reducción: {dinero_txt(bajada['diferencia'], cur, signo=True)}.")
        detalle = recursos.get(cur)
        if detalle:
            if detalle["top_mes_b"]:
                top = detalle["top_mes_b"][0]
                salida.append(f"El recurso de mayor costo en {nombre_b} fue {top['resource_name']} "
                              f"({top['servicio_sigla']}, {top['region_nombre']}): {dinero_txt(top['mes_b'], cur)}.")
            if detalle["nuevos"]:
                salida.append(f"{len(detalle['nuevos'])} recurso(s) con consumo en {nombre_b} "
                              f"no tenían consumo en {nombre_a}.")
            if detalle["eliminados"]:
                salida.append(f"{len(detalle['eliminados'])} recurso(s) con consumo en {nombre_a} "
                              f"no tienen consumo en {nombre_b}.")
            for f in [f for f in detalle["mayores_aumentos"] if f["mes_a"] > 0][:3]:
                revisar.append(f"Revisar {f['resource_name']} ({f['servicio_sigla']}, {f['region_nombre']}): "
                               f"pasó de {dinero_txt(f['mes_a'], cur)} a {dinero_txt(f['mes_b'], cur)}.")
            for f in detalle["nuevos"][:3]:
                revisar.append(f"Confirmar el recurso nuevo {f['resource_name']} ({f['servicio_sigla']}, "
                               f"{f['region_nombre']}): {dinero_txt(f['mes_b'], cur)} en {nombre_b}.")
    if preliminar and salida:
        revisar.append(f"{nombre_b.capitalize()} está en curso: sus importes son preliminares y pueden aumentar.")
    return {"hallazgos": salida, "revisar": revisar}
