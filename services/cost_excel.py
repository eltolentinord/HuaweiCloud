# coding: utf-8
"""Generación del reporte Excel comparativo de costos."""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.formatting.rule import CellIsRule
from openpyxl.utils import get_column_letter

from exports.excel import neutralize_formulas
from services.cost_analysis import etiqueta_variacion, nombre_region, nombre_servicio  # noqa: F401

def _srv(product) -> str:
    """Servicio legible a partir del código de BSS (``hws.service.type.ec2`` -> ``ECS · Elastic Cloud Server``)."""
    servicio = nombre_servicio(product)
    return servicio["sigla"] if servicio["sigla"] == servicio["nombre"] else f"{servicio['sigla']} · {servicio['nombre']}"

AZUL_OSCURO = "1F3864"


def _estilizar_encabezado(ws, n_cols):
    for c in range(1, n_cols + 1):
        celda = ws.cell(row=1, column=c)
        celda.fill = PatternFill("solid", fgColor=AZUL_OSCURO)
        celda.font = Font(color="FFFFFF", bold=True)
        celda.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def _autoancho(ws):
    for col in ws.columns:
        ancho = max((len(str(c.value)) for c in col if c.value is not None), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max(ancho + 2, 12), 45)


def _tabla(ws, encabezados, filas, formato_moneda_cols, formato_pct_cols):
    ws.append(encabezados)
    for fila in filas:
        ws.append(fila)
    _estilizar_encabezado(ws, len(encabezados))
    for r in range(2, ws.max_row + 1):
        for c in formato_moneda_cols:
            ws.cell(row=r, column=c).number_format = '"$"#,##0.00'
        for c in formato_pct_cols:
            ws.cell(row=r, column=c).number_format = '0.00%'
    _autoancho(ws)


CAB_RECURSO = ["Moneda", "Recurso", "ID", "Servicio", "Región", "Especificación"]


def _fila_recurso(cur, f):
    return [cur, f.get("resource_name"), f.get("resource_id"),
            f"{f.get('servicio_sigla')} · {f.get('servicio_nombre')}" if f.get("servicio_nombre") != f.get("servicio_sigla")
            else f.get("servicio_sigla"), f.get("region_nombre") or f.get("region"), f.get("spec") or f.get("tipo")]


def _hojas_recursos(wb, analisis, before) -> None:
    """Hojas por recurso (solo si la consulta de detalle por recurso tuvo éxito)."""
    monedas = (analisis.get("recursos") or {}).get("monedas")
    if monedas is None:
        return
    top, cambios, altas_bajas = [], [], []
    for m in monedas:
        cur = m["currency"]
        for f in m["top_mes_b"]:
            top.append(_fila_recurso(cur, f) + ["Mes B", f["mes_b"]])
        for f in m["top_mes_a"]:
            top.append(_fila_recurso(cur, f) + ["Mes A", f["mes_a"]])
        for etiqueta, clave in (("Aumento", "mayores_aumentos"), ("Reducción", "mayores_reducciones")):
            for f in m[clave]:
                cambios.append(_fila_recurso(cur, f) + [etiqueta, f["mes_a"], f["mes_b"], f["diferencia"],
                                                        f["variacion_txt"]])
        for etiqueta, clave in (("Nuevo", "nuevos"), ("Sin consumo en Mes B", "eliminados")):
            for f in m[clave]:
                altas_bajas.append(_fila_recurso(cur, f) + [etiqueta, f["mes_a"], f["mes_b"]])
    indice = wb.index(before)
    ws = wb.create_sheet("09_Top_Recursos", index=indice)
    _tabla(ws, CAB_RECURSO + ["Mes", "Costo"], top, [8], [])
    ws = wb.create_sheet("10_Recursos_Aumentos_Reducc", index=indice + 1)
    _tabla(ws, CAB_RECURSO + ["Tipo", "Mes A", "Mes B", "Diferencia", "Variación"], cambios, [8, 9, 10], [])
    ws = wb.create_sheet("11_Recursos_Nuevos_Elimin", index=indice + 2)
    _tabla(ws, CAB_RECURSO + ["Tipo", "Mes A", "Mes B"], altas_bajas, [8, 9], [])


def generar_excel_costos(analisis: Dict[str, Any], mes_a: str, mes_b: str,
                         base_dir: Path, errores=None) -> Path:
    wb = Workbook()

    # 00 Resumen
    ws = wb.active
    ws.title = "00_Resumen_Ejecutivo"
    ws.append(["Métrica", "Valor"])
    ws.append(["Mes A", mes_a])
    ws.append(["Mes B", mes_b])
    ws.append(["Fecha de extracción", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    ws.append([])
    for m in analisis.get("monedas", []):
        ws.append([f"Moneda {m['currency']} - Total Mes A", m["total_mes_a"]])
        ws.append([f"Moneda {m['currency']} - Total Mes B", m["total_mes_b"]])
        ws.append([f"Moneda {m['currency']} - Diferencia", m["diferencia"]])
        ws.append([f"Moneda {m['currency']} - Variación", m["variacion_txt"]])
        ws.append([f"Moneda {m['currency']} - Región con mayor gasto", m["region_mayor_gasto"]])
        for etiqueta, clave in (("Producto más costoso", "producto_mas_costoso"),
                                ("Producto que más aumentó", "producto_mas_aumento"),
                                ("Producto que más disminuyó", "producto_mas_reduccion")):
            ws.append([f"Moneda {m['currency']} - {etiqueta}", _srv(m[clave]) if m.get(clave) else None])
        recursos = {r["currency"]: r for r in (analisis.get("recursos") or {}).get("monedas", [])}.get(m["currency"])
        if recursos and recursos.get("top_mes_b"):
            top = recursos["top_mes_b"][0]
            ws.append([f"Moneda {m['currency']} - Recurso de mayor costo (Mes B)",
                       f"{top['resource_name']} ({top['servicio_sigla']}, {top['region_nombre']})"])
        ws.append([])
    _estilizar_encabezado(ws, 2)
    for r in range(2, ws.max_row + 1):
        if isinstance(ws.cell(row=r, column=2).value, (int, float)):
            ws.cell(row=r, column=2).number_format = '"$"#,##0.00'
    _autoancho(ws)

    def filas_comp(lista, clave_nombre):
        return [[f[clave_nombre], f["mes_a"], f["mes_b"], f["diferencia"], f["variacion_txt"]] for f in lista]

    por_producto = []
    por_region = []
    por_region_producto = []
    aumentos = []
    reducciones = []
    nuevos = []
    eliminados = []
    def fila_rp(cur, f):
        return [cur, f["region"], f["product"], _srv(f["product"]), f["mes_a"], f["mes_b"], f["diferencia"],
                f["variacion_txt"]]

    for m in analisis.get("monedas", []):
        cur = m["currency"]
        for f in m["por_producto"]:
            por_producto.append([cur, f["product"], _srv(f["product"]), f["mes_a"], f["mes_b"], f["diferencia"],
                                 f["variacion_txt"]])
        for f in m["por_region"]:
            por_region.append([cur, f["region"], f.get("region_nombre") or nombre_region(f["region"]),
                               f["mes_a"], f["mes_b"], f["diferencia"], f["variacion_txt"]])
        for f in m["por_region_producto"]:
            por_region_producto.append(fila_rp(cur, f))
        for f in m["mayores_aumentos"]:
            aumentos.append(fila_rp(cur, f))
        for f in m["mayores_reducciones"]:
            reducciones.append(fila_rp(cur, f))
        for n in m["servicios_nuevos"]:
            nuevos.append([cur, n["region"], n["product"], _srv(n["product"]), n["mes_a"], n["mes_b"]])
        for e in m["servicios_eliminados"]:
            eliminados.append([cur, e["region"], e["product"], _srv(e["product"]), e["mes_a"], e["mes_b"]])
    cab_rp = ["Moneda", "Región", "Producto", "Servicio", "Mes A", "Mes B", "Diferencia", "Variación"]

    ws = wb.create_sheet("01_Comparacion_Mensual")
    _tabla(ws, ["Moneda", "Total Mes A", "Total Mes B", "Diferencia", "Variación"],
           [[m["currency"], m["total_mes_a"], m["total_mes_b"], m["diferencia"], m["variacion_txt"]]
            for m in analisis.get("monedas", [])], [2, 3, 4], [])

    ws = wb.create_sheet("02_Costos_por_Producto")
    _tabla(ws, ["Moneda", "Producto", "Servicio", "Mes A", "Mes B", "Diferencia", "Variación"], por_producto,
           [4, 5, 6], [])

    ws = wb.create_sheet("03_Costos_por_Region")
    _tabla(ws, ["Moneda", "Región", "Nombre región", "Mes A", "Mes B", "Diferencia", "Variación"], por_region,
           [4, 5, 6], [])

    ws = wb.create_sheet("04_Region_y_Producto")
    _tabla(ws, cab_rp, por_region_producto, [5, 6, 7], [])
    if ws.max_row > 1:
        ws.conditional_formatting.add(f"G2:G{ws.max_row}",
            CellIsRule(operator="greaterThan", formula=["0"], font=Font(color="FF0000")))
        ws.conditional_formatting.add(f"G2:G{ws.max_row}",
            CellIsRule(operator="lessThan", formula=["0"], font=Font(color="008000")))

    ws = wb.create_sheet("05_Mayores_Aumentos")
    _tabla(ws, cab_rp, aumentos, [5, 6, 7], [])

    ws = wb.create_sheet("06_Mayores_Reducciones")
    _tabla(ws, cab_rp, reducciones, [5, 6, 7], [])

    ws = wb.create_sheet("07_Servicios_Nuevos")
    _tabla(ws, ["Moneda", "Región", "Producto", "Servicio", "Costo Mes A", "Costo Mes B"], nuevos, [5, 6], [])
    if wb.worksheets[-1].max_row > 1:
        for row in wb.worksheets[-1].iter_rows(min_row=2, max_row=wb.worksheets[-1].max_row, min_col=1, max_col=6):
            for c in row:
                c.fill = PatternFill("solid", fgColor="FFF2CC")

    ws = wb.create_sheet("08_Detalle_Costos")
    ws.append(cab_rp)
    for f in aumentos + reducciones:
        ws.append(f)
    _estilizar_encabezado(ws, 8)
    for r in range(2, ws.max_row + 1):
        for c in (5, 6, 7):
            ws.cell(row=r, column=c).number_format = '"$"#,##0.00'
    _autoancho(ws)

    ws = wb.create_sheet("99_Errores")
    ws.append(["Error"])
    for e in (errores or []):
        ws.append([str(e)])
    _estilizar_encabezado(ws, 1)
    _autoancho(ws)

    # Servicios eliminados en hoja propia si hay datos (se agrega antes de 99 en orden visual)
    ws_elim = wb.create_sheet("07b_Servicios_Eliminados", index=wb.index(wb["08_Detalle_Costos"]))
    ws_elim.append(["Moneda", "Región", "Producto", "Servicio", "Costo Mes A", "Costo Mes B"])
    for e in eliminados:
        ws_elim.append(e)
    _estilizar_encabezado(ws_elim, 6)
    for r in range(2, ws_elim.max_row + 1):
        ws_elim.cell(row=r, column=5).number_format = '"$"#,##0.00'
        ws_elim.cell(row=r, column=6).number_format = '"$"#,##0.00'
    _autoancho(ws_elim)

    _hojas_recursos(wb, analisis, before=wb["99_Errores"])

    output_dir = Path(base_dir) / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ruta = output_dir / f"Huawei_Costos_Comparativo_{stamp}.xlsx"
    neutralize_formulas(wb)
    wb.save(ruta)
    return ruta
