# coding: utf-8
"""Router de análisis de costos (módulo separado de ECS)."""

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from core.errors import safe_message
from services.cost_analysis import analizar
from services.cost_excel import generar_excel_costos
from services.huawei_costs import CostApiError, consultar_mes

router = APIRouter()
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


@router.get("/costos", response_class=HTMLResponse)
async def costos_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "costs.html", {})


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
        analisis = analizar(resp_a["records"], resp_b["records"], payload.month_a, payload.month_b)
        datos_preliminares = payload.month_b >= datetime.now().strftime("%Y-%m")
        return {
            "error": False,
            "extraction_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "month_a": payload.month_a,
            "month_b": payload.month_b,
            "cost_type": payload.cost_type,
            "amount_type": payload.amount_type,
            "datos_preliminares": datos_preliminares,
            "errores": errores,
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
        return {"error": True,
                "message": safe_message(f"{type(error).__name__}: {error}", (payload.ak, payload.sk))}


@router.post("/api/costs/export")
async def exportar_costos(payload: ExportRequest):
    try:
        ruta = generar_excel_costos(payload.analysis, payload.month_a, payload.month_b, BASE, errores=payload.errores)
        return FileResponse(ruta, filename=ruta.name)
    except Exception as error:
        return JSONResponse(status_code=500, content={"error": True, "message": safe_message(error)})
