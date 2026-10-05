# coding: utf-8
"""Router de análisis de costos (módulo separado de ECS)."""

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from core.errors import safe_message
from services.cost_analysis import analizar, analizar_recursos, hallazgos
from services.cost_excel import generar_excel_costos
from services.cost_pdf import generar_pdf_costos
from services.huawei_costs import CostApiError, consultar_mes, consultar_recursos_mes

router = APIRouter()
logger = logging.getLogger(__name__)
BASE = Path(__file__).resolve().parent.parent
TEMPLATES = Jinja2Templates(directory=str(BASE / "templates"))


class CompareRequest(BaseModel):
    ak: str
    sk: str
    month_a: str
    month_b: str
    cost_type: str = "ORIGINAL_COST"
    amount_type: str = "NET_AMOUNT"


class ExportRequest(BaseModel):
    analysis: dict = {}
    month_a: str = ""
    month_b: str = ""
    errores: list = []
    extraction_time: str = ""
    datos_preliminares: bool = False
    recursos_error: Optional[dict] = None


_MES = re.compile(r"^\d{4}-\d{2}$")


def _detalle_recursos(payload: CompareRequest):
    """Consumo por recurso de ambos meses. Si falla, la comparación sigue sin él."""
    try:
        return (consultar_recursos_mes(payload.ak, payload.sk, payload.month_a),
                consultar_recursos_mes(payload.ak, payload.sk, payload.month_b), None)
    except CostApiError as error:
        return [], [], {"mensaje": safe_message(error, (payload.ak, payload.sk)),
                        "http_status": error.http_status, "error_code": error.error_code,
                        "request_id": error.request_id}
    except Exception as error:  # nunca rompe la comparación principal
        logger.warning("Detalle por recurso fallido (%s)", type(error).__name__)
        return [], [], {"mensaje": safe_message(f"{type(error).__name__}: {error}", (payload.ak, payload.sk)),
                        "http_status": None, "error_code": None, "request_id": None}


def _nombres_region(*listas: List[Dict[str, Any]]) -> Dict[str, str]:
    return {r["region"]: r["region_name"] for lista in listas for r in lista
            if r.get("region") and r.get("region_name")}


@router.get("/costos", response_class=HTMLResponse)
async def costos_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "costs.html", {})


@router.get("/costos/calculadora", response_class=HTMLResponse)
async def calculadora_page(request: Request):
    """Calculadora de precios oficiales (usa la API interna: INVENTORY_ADMIN_API=true)."""
    return TEMPLATES.TemplateResponse(request, "calculator.html", {})


@router.get("/costos/comparar-regiones", response_class=HTMLResponse)
async def comparar_regiones_page(request: Request):
    """Comparador de costos por región (usa la API interna: INVENTORY_ADMIN_API=true)."""
    return TEMPLATES.TemplateResponse(request, "costs_compare.html", {})


@router.post("/api/costs/compare")
async def comparar_costos(payload: CompareRequest):
    if not payload.ak or not payload.sk or not payload.month_a or not payload.month_b:
        return JSONResponse(status_code=200, content={
            "error": True,
            "message": "AK, SK y ambos meses son obligatorios.",
        })
    errores = []
    try:
        resp_a = consultar_mes(payload.ak, payload.sk, payload.month_a,
                               payload.cost_type, payload.amount_type)
        resp_b = consultar_mes(payload.ak, payload.sk, payload.month_b,
                               payload.cost_type, payload.amount_type)
        errores.extend(resp_a.get("errors", []))
        errores.extend(resp_b.get("errors", []))
        recursos_a, recursos_b, recursos_error = _detalle_recursos(payload)
        analisis = analizar(resp_a["records"], resp_b["records"], payload.month_a, payload.month_b,
                            nombres_region=_nombres_region(recursos_a, recursos_b))
        datos_preliminares = payload.month_b >= datetime.now().strftime("%Y-%m")
        if recursos_error is None:
            analisis["recursos"] = analizar_recursos(recursos_a, recursos_b)
        analisis["hallazgos"] = hallazgos(analisis, payload.month_a, payload.month_b, datos_preliminares)
        return {
            "error": False,
            "extraction_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "month_a": payload.month_a,
            "month_b": payload.month_b,
            "cost_type": payload.cost_type,
            "amount_type": payload.amount_type,
            "datos_preliminares": datos_preliminares,
            "errores": errores,
            "recursos_error": recursos_error,
            "analysis": analisis,
        }
    except CostApiError as error:
        return {
            "error": True,
            "http_status": error.http_status,
            "request_id": error.request_id,
            "error_code": error.error_code,
            "message": safe_message(error, (payload.ak, payload.sk)),
        }
    except Exception as error:
        logger.warning("Comparación de costos fallida (%s)", type(error).__name__)
        return {"error": True,
                "message": safe_message(f"{type(error).__name__}: {error}", (payload.ak, payload.sk))}


@router.post("/api/costs/export")
async def exportar_costos(payload: ExportRequest):
    try:
        ruta = generar_excel_costos(payload.analysis, payload.month_a, payload.month_b, BASE, errores=payload.errores)
        return FileResponse(ruta, filename=ruta.name)
    except Exception as error:
        logger.exception("Exportación de costos fallida")
        return JSONResponse(status_code=500, content={"error": True, "message": safe_message(error)})


@router.post("/api/costs/export/pdf")
async def exportar_costos_pdf(payload: ExportRequest):
    """Reporte PDF de la comparación ya calculada (nunca recibe AK/SK)."""
    mes_a = payload.month_a if _MES.match(payload.month_a or "") else ""
    mes_b = payload.month_b if _MES.match(payload.month_b or "") else ""
    if not mes_a or not mes_b:
        return JSONResponse(status_code=400, content={"error": True, "message": "Meses inválidos; usa YYYY-MM."})
    try:
        contenido = generar_pdf_costos(payload.analysis, mes_a, mes_b,
                                       extraccion=payload.extraction_time,
                                       preliminar=payload.datos_preliminares,
                                       recursos_error=payload.recursos_error,
                                       errores=payload.errores)
    except Exception as error:
        logger.exception("Exportación PDF de costos fallida")
        return JSONResponse(status_code=500, content={"error": True, "message": safe_message(error)})
    nombre = f"Huawei_Costos_{mes_a}_vs_{mes_b}.pdf"
    return Response(content=contenido, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{nombre}"'})
