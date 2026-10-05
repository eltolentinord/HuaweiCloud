# coding: utf-8
"""Aplicación web de inventario Huawei Cloud (solo lectura).

Servidor: FastAPI + Jinja2.
Interfaz: templates/index.html + static/app.js + static/styles.css.
Escucha únicamente en 127.0.0.1.

Arranque:
    python app.py
    uvicorn app:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.requests import Request

from core.credentials import MissingCredentialsError
from core.observability import configure_logging
from exports.excel import build_inventory_workbook, safe_filename_part, workbook_bytes
from inventory import REGIONES, SERVICIOS, consultar_servicio
from routers.admin import admin_api_enabled, install_admin_api
from routers.common import install_safe_validation_errors, inventory_response
from routers.costs import router as costs_router
from routers.middleware import install_middlewares
from routers.system import router as system_router

BASE_DIR = Path(__file__).resolve().parent
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

configure_logging()  # LOG_LEVEL / LOG_FORMAT (text|json); incluye request/scan/task id

app = FastAPI(title="Huawei Cloud Inventory", docs_url=None, redoc_url=None)
install_middlewares(app)  # request ID, cabeceras de seguridad, métricas, CORS explícito, 500 seguros
install_safe_validation_errors(app)
app.include_router(system_router)
app.include_router(costs_router)
if admin_api_enabled():
    # API multi-cliente (PostgreSQL). Sin login todavía: solo para uso local.
    install_admin_api(app)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class ConsultaRequest(BaseModel):
    ak: str = Field(min_length=1)
    sk: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    region: str = Field(min_length=1)
    service: str = Field(default="ecs")


class ExportTable(BaseModel):
    titulo: str
    columnas: List[str]
    filas: List[Dict[str, Any]]


class ExportRequest(BaseModel):
    region: str
    service: str
    tables: List[ExportTable]
    avisos: List[Dict[str, Any]] = []


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"regiones": REGIONES, "servicios": SERVICIOS},
    )


@app.get("/dashboard")
def dashboard(request: Request):
    """Dashboard del inventario persistido (usa la API interna: INVENTORY_ADMIN_API=true)."""
    return templates.TemplateResponse(request, "dashboard.html", {})


@app.get("/servidores")
def servidores(request: Request):
    """Auditoría de servidores Linux por SSH (usa la API interna: INVENTORY_ADMIN_API=true)."""
    return templates.TemplateResponse(request, "servers.html", {})


@app.get("/clientes")
def clientes(request: Request):
    """Mis clientes (tenants) y su entorno Huawei Cloud (usa la API interna: INVENTORY_ADMIN_API=true)."""
    return templates.TemplateResponse(request, "clients.html", {})


@app.post("/api/inventory")
def inventory(payload: ConsultaRequest):
    servicio = payload.service.strip().lower()
    if servicio not in {s["id"] for s in SERVICIOS}:
        raise HTTPException(status_code=400, detail="Servicio no válido")

    try:
        resultado = consultar_servicio(
            servicio, payload.ak, payload.sk,
            payload.project_id.strip(), payload.region.strip(),
        )
    except MissingCredentialsError:
        raise HTTPException(status_code=400, detail="AK y SK son obligatorios")

    return inventory_response(servicio, payload.region.strip(), payload.project_id, resultado)


@app.post("/api/export")
def export_excel(payload: ExportRequest):
    if not payload.tables:
        raise HTTPException(status_code=400, detail="No hay resultados para exportar")

    workbook = build_inventory_workbook(
        [t.model_dump() for t in payload.tables], payload.avisos
    )
    nombre = (f"inventario_{safe_filename_part(payload.service)}_"
              f"{safe_filename_part(payload.region)}.xlsx")
    return StreamingResponse(
        workbook_bytes(workbook),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
