# coding: utf-8
"""Nombres legibles para el catálogo por región (funciones puras, sin red).

Los códigos vienen de las APIs oficiales: ECS ``ListFlavors`` (``ecs:performancetype``,
``cond:operation:status``, ``cond:operation:az``) y EVS ``CinderListVolumeTypes``.
Un código desconocido se muestra tal cual: nunca se inventa una descripción.
"""

from __future__ import annotations

import re
from typing import Dict, Optional

# ecs:performancetype (documentación del SDK ECS v2, FlavorExtraSpec).
FAMILIAS: Dict[str, str] = {
    "normal": "Uso general",
    "entry": "Uso general de entrada",
    "cpuv1": "Cómputo I",
    "cpuv2": "Cómputo II",
    "computingv3": "Uso general mejorado",
    "kunpeng_computing": "Kunpeng uso general mejorado",
    "kunpeng_highmem": "Kunpeng memoria optimizada",
    "highmem": "Memoria optimizada",
    "saphana": "Memoria grande (SAP HANA)",
    "diskintensive": "Disco intensivo",
    "highio": "Ultra alto I/O",
    "ultracpu": "Cómputo de ultra alto rendimiento",
    "gpu": "GPU",
    "fpga": "FPGA",
    "ascend": "Ascend (IA)",
    "savedhpc": "HPC",
}

# cond:operation:status / estado por zona.
ESTADOS: Dict[str, str] = {
    "normal": "Disponible",
    "promotion": "Disponible (recomendado)",
    "sellout": "Agotado",
    "obt": "Beta pública",
    "abandon": "Retirado",
}
VENDIBLES = frozenset({"normal", "promotion", "obt"})

# Tipos de disco EVS (nombres comerciales de Huawei Cloud).
DISCOS: Dict[str, str] = {
    "SATA": "Común (SATA)",
    "SAS": "Alto I/O (SAS)",
    "GPSSD": "SSD de uso general",
    "SSD": "Ultra alto I/O (SSD)",
    "ESSD": "SSD extremo",
    "GPSSD2": "SSD de uso general V2",
    "ESSD2": "SSD extremo V2",
}

_AZ_ESTADO = re.compile(r"([A-Za-z0-9_.\-]+)\(([A-Za-z_]+)\)")


def familia(codigo: Optional[str]) -> str:
    codigo = (codigo or "").strip()
    return FAMILIAS.get(codigo.lower(), codigo) if codigo else "Sin familia"


def estado(codigo: Optional[str]) -> str:
    """Sin dato = ``normal`` (lo indica la documentación de ``cond:operation:status``)."""
    codigo = (codigo or "normal").strip().lower()
    return ESTADOS.get(codigo, codigo)


def disco(nombre: Optional[str]) -> str:
    nombre = (nombre or "").strip()
    return DISCOS.get(nombre.upper(), nombre) if nombre else "Sin tipo"


def zonas_flavor(texto: Optional[str]) -> Dict[str, str]:
    """``cond:operation:az`` -> ``{zona: estado}``. Formato oficial: ``az1(normal),az2(sellout)``."""
    return {zona: est.lower() for zona, est in _AZ_ESTADO.findall(texto or "")}


def lista_zonas(texto: Optional[str]) -> list:
    """``RESKEY:availability_zones`` / ``sold_out_availability_zones``: lista separada por comas."""
    return sorted({z.strip() for z in (texto or "").split(",") if z.strip()})


def descripcion_flavor(vcpus: int, ram_mb: int, performance_type: Optional[str]) -> str:
    ram_gb = round(ram_mb / 1024, 2)
    return f"{vcpus} vCPU · {ram_gb:g} GB · {familia(performance_type)}"
