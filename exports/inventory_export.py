# coding: utf-8
"""Exportaciones del inventario PERSISTIDO: inventario (completo o filtrado), resumen y
diferencias entre escaneos, en Excel o CSV.

- Excel: reutiliza ``exports.excel.build_inventory_workbook`` (formato, filtros,
  paneles fijos y neutralización de fórmulas).
- CSV: UTF-8 con BOM (Excel lo abre bien) y protección contra CSV/Formula Injection
  según OWASP: cualquier celda que empiece por ``= + - @``, tabulador o retorno de
  carro se prefija con un apóstrofo. Aquí sí se altera el texto, porque en CSV no
  existe una marca de "texto literal" como el ``quotePrefix`` de Excel.
- Nunca se exporta ``raw``: solo campos normalizados, tags y atributos (ya
  redactados al guardarse). Nada de credenciales.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, Iterable, List, Optional, Sequence

from core.serialization import canonical_json
from db.models import InventoryResource, Project
from exports.excel import FORMULA_PREFIXES, build_inventory_workbook, workbook_bytes
from scanning.history import ScanComparison

MAX_EXPORT_ROWS = 100_000
MAX_CELL_JSON = 2_000

RESOURCE_COLUMNS = ["Servicio", "Tipo", "ID proveedor", "Nombre", "Estado", "Región", "Project ID",
                    "Enterprise Project", "Creado en Huawei", "Primera vez visto", "Última vez visto",
                    "Eliminado", "Tags", "Atributos"]


def csv_safe(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(FORMULA_PREFIXES) else text


def _iso(value) -> str:
    return value.isoformat() if value is not None else ""


def _tags(tags: Dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" if v not in ("", None) else str(k) for k, v in sorted((tags or {}).items()))


def _attributes(attributes: Dict[str, Any]) -> str:
    text = canonical_json(attributes or {})
    return text if len(text) <= MAX_CELL_JSON else text[:MAX_CELL_JSON] + "…"


def resource_rows(rows: Iterable[InventoryResource], projects: Dict[Any, Project]) -> List[Dict[str, Any]]:
    result = []
    for r in rows:
        project = projects.get(r.project_id)
        result.append({
            "Servicio": r.service, "Tipo": r.resource_type, "ID proveedor": r.provider_id,
            "Nombre": r.name or "", "Estado": r.status or "", "Región": r.region,
            "Project ID": project.huawei_project_id if project else "(cuenta)",
            "Enterprise Project": r.enterprise_project_id or "",
            "Creado en Huawei": _iso(r.provider_created_at), "Primera vez visto": _iso(r.first_seen),
            "Última vez visto": _iso(r.last_seen), "Eliminado": _iso(r.deleted_at),
            "Tags": _tags(r.tags), "Atributos": _attributes(r.attributes),
        })
    return result


def summary_rows(stats: Dict[str, Any], last_scan: Optional[Any]) -> List[Dict[str, Any]]:
    rows = [{"Grupo": "Total", "Clave": "Recursos activos", "Valor": stats["total_active"]},
            {"Grupo": "Total", "Clave": "Recursos eliminados", "Valor": stats["total_deleted"]}]
    for group, label in (("by_service", "Servicio"), ("by_region", "Región"), ("by_resource_type", "Tipo"),
                         ("by_status", "Estado")):
        rows += [{"Grupo": label, "Clave": key or "(vacío)", "Valor": value} for key, value in stats[group].items()]
    if last_scan is not None:
        rows += [{"Grupo": "Último escaneo", "Clave": key, "Valor": value} for key, value in (
            ("Número", last_scan.sequence), ("Estado", last_scan.status), ("Inicio", _iso(last_scan.started_at)),
            ("Fin", _iso(last_scan.finished_at)), ("Recursos", last_scan.total_resources),
            ("Errores", last_scan.total_errors), ("Avisos", last_scan.total_warnings))]
    return rows


def diff_rows(comparison: ScanComparison) -> Dict[str, List[Dict[str, Any]]]:
    sheets: Dict[str, List[Dict[str, Any]]] = {"Agregados": [], "Eliminados": [], "Modificados": [],
                                               "Transitorios": []}
    names = {"added": "Agregados", "removed": "Eliminados", "modified": "Modificados", "transient": "Transitorios"}
    for item in comparison.items:
        base = {"Servicio": item.service, "Tipo": item.resource_type, "ID proveedor": item.provider_id,
                "Nombre": item.name or "", "Región": item.region, "Eventos": " → ".join(item.events)}
        if item.category == "modified" and item.changed_fields:
            for change in item.changed_fields:
                sheets["Modificados"].append({**base, "Campo": change["field"],
                                              "Antes": _cell(change.get("before")), "Después": _cell(change.get("after"))})
        else:
            sheets[names[item.category]].append(base)
    return sheets


def _cell(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)[:MAX_CELL_JSON]
    return "" if value is None else value


# --------------------------------------------------------------------- formatos
def to_xlsx(tables: Sequence[Dict[str, Any]]) -> io.BytesIO:
    return workbook_bytes(build_inventory_workbook(list(tables)))


def to_csv(columns: Sequence[str], rows: Iterable[Dict[str, Any]]) -> io.BytesIO:
    text = io.StringIO()
    writer = csv.writer(text, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow([csv_safe(c) for c in columns])
    for row in rows:
        writer.writerow([csv_safe(row.get(c)) for c in columns])
    return io.BytesIO(("﻿" + text.getvalue()).encode("utf-8"))


def table(title: str, rows: List[Dict[str, Any]], columns: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    columns = list(columns or (rows[0].keys() if rows else ["(sin datos)"]))
    return {"titulo": title, "columnas": columns, "filas": rows}
