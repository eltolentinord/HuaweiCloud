# coding: utf-8
"""Reporte PDF comparativo de consumo (reportlab).

Estructura: portada, resumen ejecutivo, comparación por región y por servicio,
servicios que más subieron/bajaron, detalle de cada región, recursos que más
consumieron, recursos con mayor aumento/reducción, nuevos/eliminados, hallazgos y
control del documento. Todo sale del análisis ya calculado (``services.cost_analysis``):
nada se inventa y el PDF nunca recibe credenciales.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from functools import lru_cache
from io import BytesIO
from typing import Any, Dict, Iterable, List, Optional, Sequence
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from services.cost_analysis import dinero_txt, nombre_mes

TITULO = "Reporte comparativo de consumo"
PROVEEDOR = "Huawei Cloud"
AZUL = colors.HexColor("#1F3864")
AZUL_MEDIO = colors.HexColor("#2F5597")
BANDA = colors.HexColor("#DCE6F2")
GRIS_FILA = colors.HexColor("#F2F4F7")
GRIS_TEXTO = colors.HexColor("#5B6573")
LINEA = colors.HexColor("#C9D1DC")
VERDE = colors.HexColor("#1E7B34")
ROJO = colors.HexColor("#B42318")
MAX_FILAS_REGION = 40

# Huawei devuelve algunos textos en chino (p. ej. especificaciones "输入Tokens" o nombres
# de región). Helvetica solo cubre Windows-1252, así que esos tramos se escriben con una
# fuente CJK incrustada (subconjunto): sin ella saldrían como cuadros negros al imprimir.
FUENTE_CJK = "CJK"
CANDIDATAS_CJK = (
    (os.environ.get("INVENTORY_PDF_CJK_FONT", ""), 0),
    (r"C:\Windows\Fonts\msyh.ttc", 0),        # Microsoft YaHei
    (r"C:\Windows\Fonts\simsun.ttc", 0),      # SimSun
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
    ("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc", 0),
    ("/System/Library/Fonts/PingFang.ttc", 0),
)
_FUERA_DE_HELVETICA = re.compile(r"[^\x00-\xff\u2013\u2014\u2018\u2019\u201c\u201d\u2022\u2026\u20ac]+")


@lru_cache(maxsize=1)
def _registrar_fuente_cjk() -> str:
    """Registra una fuente CJK la primera vez (TTF/TTC del sistema o, si no hay, la CID de Adobe)."""
    for ruta, indice in CANDIDATAS_CJK:
        if ruta and os.path.isfile(ruta):
            try:
                pdfmetrics.registerFont(TTFont(FUENTE_CJK, ruta, subfontIndex=indice))
                return FUENTE_CJK
            except Exception:  # fuente ilegible: se prueba la siguiente
                continue
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light"


def _texto(valor: Any) -> str:
    """Escapa para ``Paragraph`` y envuelve en la fuente CJK lo que Helvetica no puede dibujar."""
    texto = escape(str(valor))
    if not _FUERA_DE_HELVETICA.search(texto):
        return texto
    fuente = _registrar_fuente_cjk()
    return _FUERA_DE_HELVETICA.sub(lambda m: f'<font name="{fuente}">{m.group(0)}</font>', texto)

_base = getSampleStyleSheet()
ESTILOS = {
    "portada_titulo": ParagraphStyle("portada_titulo", parent=_base["Title"], fontName="Helvetica-Bold",
                                     fontSize=24, leading=30, textColor=AZUL, alignment=TA_CENTER),
    "portada_periodo": ParagraphStyle("portada_periodo", parent=_base["Title"], fontName="Helvetica-Bold",
                                      fontSize=18, leading=24, textColor=AZUL_MEDIO, alignment=TA_CENTER),
    "portada_texto": ParagraphStyle("portada_texto", parent=_base["Normal"], fontSize=12, leading=16,
                                    textColor=GRIS_TEXTO, alignment=TA_CENTER),
    "h1": ParagraphStyle("h1", parent=_base["Heading1"], fontName="Helvetica-Bold", fontSize=16, leading=20,
                         textColor=AZUL, spaceBefore=10, spaceAfter=6, keepWithNext=1),
    "h2": ParagraphStyle("h2", parent=_base["Heading2"], fontName="Helvetica-Bold", fontSize=12.5, leading=16,
                         textColor=AZUL_MEDIO, spaceBefore=10, spaceAfter=4, keepWithNext=1),
    "texto": ParagraphStyle("texto", parent=_base["Normal"], fontName="Helvetica", fontSize=9.5, leading=13),
    # Texto que introduce una tabla: nunca queda solo al final de una página.
    "intro": ParagraphStyle("intro", parent=_base["Normal"], fontName="Helvetica", fontSize=9.5, leading=13,
                            spaceAfter=4, keepWithNext=1),
    "nota": ParagraphStyle("nota", parent=_base["Normal"], fontName="Helvetica-Oblique", fontSize=8.5,
                           leading=11, textColor=GRIS_TEXTO),
    "vineta": ParagraphStyle("vineta", parent=_base["Normal"], fontName="Helvetica", fontSize=9.5, leading=13,
                             leftIndent=12, bulletIndent=2),
    "celda": ParagraphStyle("celda", parent=_base["Normal"], fontName="Helvetica", fontSize=8, leading=10),
    "celda_der": ParagraphStyle("celda_der", parent=_base["Normal"], fontName="Helvetica", fontSize=8,
                                leading=10, alignment=2),
    "celda_cab": ParagraphStyle("celda_cab", parent=_base["Normal"], fontName="Helvetica-Bold", fontSize=8,
                                leading=10, textColor=colors.white, alignment=TA_CENTER),
    "celda_id": ParagraphStyle("celda_id", parent=_base["Normal"], fontName="Helvetica", fontSize=6.5,
                               leading=8, textColor=GRIS_TEXTO),
    "kpi_label": ParagraphStyle("kpi_label", parent=_base["Normal"], fontName="Helvetica-Bold", fontSize=9,
                                leading=11, textColor=AZUL, alignment=TA_CENTER),
    "kpi_valor": ParagraphStyle("kpi_valor", parent=_base["Normal"], fontName="Helvetica-Bold", fontSize=11,
                                leading=14, alignment=TA_CENTER),
}


def _p(texto: Any, estilo: str = "celda") -> Paragraph:
    return Paragraph(_texto(str(texto if texto is not None else "—")), ESTILOS[estilo])


def _dif(valor: float, moneda: str) -> Paragraph:
    color = "#B42318" if valor > 0 else "#1E7B34" if valor < 0 else "#000000"
    return Paragraph(f'<font color="{color}">{_texto(dinero_txt(valor, moneda, signo=True))}</font>',
                     ESTILOS["celda_der"])


def _tabla(cabeceras: Sequence[str], filas: List[List[Any]], anchos: Sequence[float],
           total: bool = False) -> Table:
    datos = [[_p(c, "celda_cab") for c in cabeceras]] + filas
    tabla = Table(datos, colWidths=list(anchos), repeatRows=1)
    estilo = [
        ("BACKGROUND", (0, 0), (-1, 0), AZUL),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, LINEA),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for fila in range(2, len(datos), 2):
        estilo.append(("BACKGROUND", (0, fila), (-1, fila), GRIS_FILA))
    if total and len(datos) > 1:
        estilo += [("BACKGROUND", (0, -1), (-1, -1), BANDA), ("LINEABOVE", (0, -1), (-1, -1), 0.8, AZUL)]
    tabla.setStyle(TableStyle(estilo))
    return tabla


def _vinetas(frases: Iterable[str]) -> List[Paragraph]:
    return [Paragraph(_texto(f), ESTILOS["vineta"], bulletText="•") for f in frases]


def _kpis(pares: Sequence[tuple], ancho: float) -> Table:
    fila_label = [_p(label, "kpi_label") for label, _ in pares]
    fila_valor = [Paragraph(valor, ESTILOS["kpi_valor"]) for _, valor in pares]
    tabla = Table([fila_label, fila_valor], colWidths=[ancho / len(pares)] * len(pares))
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BANDA),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return tabla


def _nombre_recurso(f: Dict[str, Any]) -> Paragraph:
    nombre = _texto(str(f.get("resource_name") or "Sin nombre"))
    rid = str(f.get("resource_id") or "")
    if rid and rid != f.get("resource_name"):
        nombre += f'<br/><font size="6.5" color="#5B6573">{_texto(rid)}</font>'
    return Paragraph(nombre, ESTILOS["celda"])


def _servicio(f: Dict[str, Any]) -> str:
    sigla, nombre = f.get("servicio_sigla"), f.get("servicio_nombre")
    if sigla and nombre and nombre != sigla:
        return f"{sigla} · {nombre}"
    return sigla or nombre or f.get("product") or "—"


class _Pagina:
    """Encabezado y pie de página (no en la portada)."""

    def __init__(self, periodo: str):
        self.periodo = periodo

    def __call__(self, canvas, doc):
        canvas.saveState()
        ancho, alto = doc.pagesize
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(AZUL_MEDIO)
        canvas.drawRightString(ancho - doc.rightMargin, alto - 12 * mm, f"{TITULO} · {PROVEEDOR}")
        canvas.setStrokeColor(LINEA)
        canvas.line(doc.leftMargin, alto - 13.5 * mm, ancho - doc.rightMargin, alto - 13.5 * mm)
        canvas.setFillColor(GRIS_TEXTO)
        canvas.drawString(doc.leftMargin, 10 * mm, self.periodo)
        canvas.drawRightString(ancho - doc.rightMargin, 10 * mm, f"Página {doc.page}")
        canvas.restoreState()


def _portada(historia: List[Any], periodo: str, extraccion: str, preliminar: bool) -> None:
    historia += [Spacer(1, 70 * mm), _p(TITULO.upper(), "portada_titulo"), Spacer(1, 6 * mm),
                 _p(periodo, "portada_periodo"), Spacer(1, 4 * mm), _p(PROVEEDOR, "portada_texto"),
                 Spacer(1, 30 * mm), _p(f"Fecha de extracción: {extraccion}", "portada_texto")]
    if preliminar:
        historia += [Spacer(1, 3 * mm),
                     _p("El segundo mes está en curso: sus importes son preliminares.", "portada_texto")]
    historia.append(PageBreak())


def _seccion_moneda(historia: List[Any], m: Dict[str, Any], recursos: Optional[Dict[str, Any]],
                    nombre_a: str, nombre_b: str, ancho: float,
                    recursos_error: Optional[Dict[str, Any]]) -> None:
    cur = m["currency"]
    cap_a, cap_b = nombre_a.capitalize(), nombre_b.capitalize()

    # Resumen ejecutivo
    historia.append(_p(f"Resumen ejecutivo ({cur})", "h1"))
    diff = m["diferencia"]
    if diff == 0:
        frase = f"El consumo total se mantuvo en {dinero_txt(m['total_mes_b'], cur)} entre {nombre_a} y {nombre_b}."
    else:
        verbo = "aumentó" if diff > 0 else "disminuyó"
        frase = (f"El consumo total {verbo} de {dinero_txt(m['total_mes_a'], cur)} en {nombre_a} a "
                 f"{dinero_txt(m['total_mes_b'], cur)} en {nombre_b}. La diferencia fue de "
                 f"{dinero_txt(diff, cur, signo=True)} ({m['variacion_txt']}).")
    region_top = max(m.get("por_region", []), key=lambda f: abs(f["diferencia"]), default=None)
    if region_top and region_top["diferencia"] != 0:
        frase += (f" El cambio principal ocurrió en {region_top.get('region_nombre') or region_top['region']}, "
                  f"donde el costo pasó de {dinero_txt(region_top['mes_a'], cur)} a "
                  f"{dinero_txt(region_top['mes_b'], cur)}.")
    historia += [_p(frase, "texto"), Spacer(1, 4 * mm)]
    historia.append(_kpis([(cap_a, _texto(dinero_txt(m["total_mes_a"], cur))),
                           (cap_b, _texto(dinero_txt(m["total_mes_b"], cur))),
                           ("Diferencia", _texto(dinero_txt(diff, cur, signo=True))),
                           ("Variación", _texto(str(m["variacion_txt"])))], ancho))

    # Por región
    historia.append(_p("Comparación total por región", "h2"))
    filas = [[_p(f.get("region_nombre") or f["region"]), _p(f["region"], "celda_id"),
              _p(dinero_txt(f["mes_a"], cur), "celda_der"), _p(dinero_txt(f["mes_b"], cur), "celda_der"),
              _dif(f["diferencia"], cur)]
             for f in sorted(m.get("por_region", []), key=lambda f: (-f["mes_b"], -f["mes_a"]))]
    filas.append([_p("Total comparado"), _p(""), _p(dinero_txt(m["total_mes_a"], cur), "celda_der"),
                  _p(dinero_txt(m["total_mes_b"], cur), "celda_der"), _dif(diff, cur)])
    historia.append(_tabla(["Región", "Código", cap_a, cap_b, "Diferencia"], filas,
                           [ancho * 0.30, ancho * 0.16, ancho * 0.18, ancho * 0.18, ancho * 0.18], total=True))

    # Por servicio
    historia.append(_p("Comparación consolidada por servicio", "h2"))
    servicios = sorted(m.get("por_producto", []), key=lambda f: (-f["mes_b"], -f["mes_a"]))
    filas = [[_p(f.get("servicio_sigla") or f["product"]), _p(f.get("servicio_nombre") or f["product"]),
              _p(dinero_txt(f["mes_a"], cur), "celda_der"), _p(dinero_txt(f["mes_b"], cur), "celda_der"),
              _dif(f["diferencia"], cur)] for f in servicios]
    historia.append(_tabla(["Producto", "Servicio", cap_a, cap_b, "Diferencia"],
                           filas or [[_p("Sin datos"), _p(""), _p(""), _p(""), _p("")]],
                           [ancho * 0.14, ancho * 0.32, ancho * 0.18, ancho * 0.18, ancho * 0.18]))

    subieron = [f for f in servicios if f["diferencia"] > 0]
    bajaron = [f for f in servicios if f["diferencia"] < 0]
    if subieron:
        historia.append(_p("Servicios que más aumentaron", "h2"))
        historia += _vinetas(
            f"{f.get('servicio_sigla') or f['product']}: pasó de {dinero_txt(f['mes_a'], cur)} a "
            f"{dinero_txt(f['mes_b'], cur)}; aumento de {dinero_txt(f['diferencia'], cur)}."
            for f in sorted(subieron, key=lambda f: -f["diferencia"])[:8])
    if bajaron:
        historia.append(_p("Servicios que disminuyeron", "h2"))
        historia += _vinetas(
            f"{f.get('servicio_sigla') or f['product']}: pasó de {dinero_txt(f['mes_a'], cur)} a "
            f"{dinero_txt(f['mes_b'], cur)}; reducción de {dinero_txt(-f['diferencia'], cur)}."
            for f in sorted(bajaron, key=lambda f: f["diferencia"])[:8])

    # Cada región
    for region in m.get("por_region_detalle", []):
        if region["mes_a"] == 0 and region["mes_b"] == 0:
            continue
        nombre = region.get("region_nombre") or region["region"]
        historia.append(_p(f"Comparación de {nombre}", "h1"))
        d = region["diferencia"]
        verbo = "aumentó" if d > 0 else "disminuyó" if d < 0 else "se mantuvo"
        texto = (f"El costo de {nombre} {verbo}: de {dinero_txt(region['mes_a'], cur)} en {nombre_a} a "
                 f"{dinero_txt(region['mes_b'], cur)} en {nombre_b}")
        texto += f" ({dinero_txt(d, cur, signo=True)})." if d else "."
        historia.append(_p(texto, "intro"))
        lista = region.get("servicios", [])
        filas = [[_p(f.get("servicio_sigla") or f["product"]), _p(f.get("servicio_nombre") or f["product"]),
                  _p(dinero_txt(f["mes_a"], cur), "celda_der"), _p(dinero_txt(f["mes_b"], cur), "celda_der"),
                  _dif(f["diferencia"], cur)] for f in lista[:MAX_FILAS_REGION]]
        historia.append(_tabla(["Producto", "Servicio", cap_a, cap_b, "Diferencia"], filas,
                               [ancho * 0.14, ancho * 0.32, ancho * 0.18, ancho * 0.18, ancho * 0.18]))
        if len(lista) > MAX_FILAS_REGION:
            historia.append(_p(f"Se muestran {MAX_FILAS_REGION} de {len(lista)} servicios; el resto "
                               "está en el Excel.", "nota"))
        mayores = [f for f in lista if f["mes_b"] > 0][:7]
        if mayores:
            historia.append(_p(f"Mayores costos de {nombre} en {nombre_b}", "h2"))
            historia += _vinetas(f"{_servicio(f)}: {dinero_txt(f['mes_b'], cur)}." for f in mayores)

    # Recursos
    historia.append(_p("Recursos que más consumieron", "h1"))
    if recursos_error:
        motivo = recursos_error.get("mensaje") or "Error desconocido"
        meta = " · ".join(x for x in (f"HTTP {recursos_error['http_status']}" if recursos_error.get("http_status")
                                      else "", f"Código {recursos_error['error_code']}"
                                      if recursos_error.get("error_code") else "",
                                      f"Request ID {recursos_error['request_id']}"
                                      if recursos_error.get("request_id") else "") if x)
        historia.append(_p(f"Detalle por recurso no disponible: {motivo}" + (f" ({meta})" if meta else ""),
                           "texto"))
        return
    if not recursos:
        historia.append(_p("Sin datos de consumo por recurso para estos meses.", "texto"))
        return
    historia.append(_p(f"Recursos facturados: {recursos['recursos_mes_a']} en {nombre_a} y "
                       f"{recursos['recursos_mes_b']} en {nombre_b} (registros de consumo por recurso de BSS).",
                       "intro"))
    anchos_rec = [ancho * 0.33, ancho * 0.17, ancho * 0.16, ancho * 0.17, ancho * 0.17]

    def filas_top(lista, campo):
        return [[_nombre_recurso(f), _p(f.get("servicio_sigla")), _p(f.get("region_nombre")),
                 _p(f.get("spec") or f.get("tipo") or "—"), _p(dinero_txt(f[campo], cur), "celda_der")]
                for f in lista]

    historia.append(_p(f"TOP {len(recursos['top_mes_b'])} recursos en {nombre_b}", "h2"))
    historia.append(_tabla(["Recurso", "Servicio", "Región", "Especificación", "Costo"],
                           filas_top(recursos["top_mes_b"], "mes_b") or [[_p("Sin consumo"), _p(""), _p(""),
                                                                          _p(""), _p("")]], anchos_rec))
    if recursos["top_mes_a"]:
        historia.append(_p(f"TOP {len(recursos['top_mes_a'])} recursos en {nombre_a}", "h2"))
        historia.append(_tabla(["Recurso", "Servicio", "Región", "Especificación", "Costo"],
                               filas_top(recursos["top_mes_a"], "mes_a"), anchos_rec))

    anchos_cmp = [ancho * 0.31, ancho * 0.12, ancho * 0.15, ancho * 0.14, ancho * 0.14, ancho * 0.14]

    def filas_cmp(lista):
        return [[_nombre_recurso(f), _p(f.get("servicio_sigla")), _p(f.get("region_nombre")),
                 _p(dinero_txt(f["mes_a"], cur), "celda_der"), _p(dinero_txt(f["mes_b"], cur), "celda_der"),
                 _dif(f["diferencia"], cur)] for f in lista]

    for titulo, clave in (("Recursos con mayor aumento", "mayores_aumentos"),
                          ("Recursos con mayor reducción", "mayores_reducciones"),
                          (f"Recursos nuevos en {nombre_b}", "nuevos"),
                          (f"Recursos sin consumo en {nombre_b}", "eliminados")):
        lista = recursos.get(clave) or []
        historia.append(_p(titulo, "h2"))
        if lista:
            historia.append(_tabla(["Recurso", "Servicio", "Región", cap_a, cap_b, "Diferencia"],
                                   filas_cmp(lista[:25]), anchos_cmp))
            if len(lista) > 25:
                historia.append(_p(f"Se muestran 25 de {len(lista)}; el resto está en el Excel.", "nota"))
        else:
            historia.append(_p("Ninguno.", "texto"))


def generar_pdf_costos(analisis: Dict[str, Any], mes_a: str, mes_b: str, *, extraccion: str = "",
                       preliminar: bool = False, recursos_error: Optional[Dict[str, Any]] = None,
                       errores: Optional[List[Any]] = None) -> bytes:
    """Devuelve el PDF como bytes."""
    nombre_a, nombre_b = nombre_mes(mes_a), nombre_mes(mes_b)
    periodo = f"{nombre_a.capitalize()} vs. {nombre_b}"
    extraccion = extraccion or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    salida = BytesIO()
    # Tamaño carta y márgenes de impresión cómodos (≥ 15 mm: ninguna impresora recorta texto).
    doc = SimpleDocTemplate(salida, pagesize=LETTER, leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=22 * mm, bottomMargin=20 * mm,
                            title=f"{TITULO} - {periodo}", author=PROVEEDOR, subject=TITULO)
    ancho = doc.width
    historia: List[Any] = []
    _portada(historia, periodo, extraccion, preliminar)

    monedas = analisis.get("monedas") or []
    recursos = {m["currency"]: m for m in (analisis.get("recursos") or {}).get("monedas", [])}
    if not monedas:
        historia += [_p("Resumen ejecutivo", "h1"), _p("Sin datos de consumo para los meses consultados.", "texto")]
    for indice, m in enumerate(monedas):
        if indice:
            historia.append(PageBreak())
        _seccion_moneda(historia, m, recursos.get(m["currency"]), nombre_a, nombre_b, ancho, recursos_error)

    textos = analisis.get("hallazgos") or {}
    historia.append(_p("Hallazgos principales", "h1"))
    historia += _vinetas(textos.get("hallazgos") or ["Sin hallazgos: no hay consumo en los meses consultados."])
    if textos.get("revisar"):
        historia.append(_p("Puntos a revisar", "h1"))
        historia += _vinetas(textos["revisar"])
    if errores:
        historia.append(_p("Avisos de la consulta", "h2"))
        historia += _vinetas(str(e) for e in errores[:20])

    control = [[_p("Documento"), _p(TITULO)], [_p("Período"), _p(periodo)], [_p("Proveedor"), _p(PROVEEDOR)],
               [_p("Fecha de extracción"), _p(extraccion)],
               [_p("Fuente"), _p("Huawei Cloud BSS: ListCosts (totales) y ListCustomerselfResourceRecords "
                                  "(detalle por recurso)")]]
    historia.append(KeepTogether([_p("Control del documento", "h1"),
                                  _tabla(["Campo", "Detalle"], control, [ancho * 0.3, ancho * 0.7])]))
    if preliminar:
        historia.append(_p(f"{nombre_b.capitalize()} está en curso: sus importes son preliminares.", "nota"))

    pagina = _Pagina(f"Comparación {periodo}")
    doc.build(historia, onLaterPages=pagina)
    return salida.getvalue()
