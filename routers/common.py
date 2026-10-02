# coding: utf-8
"""Piezas HTTP compartidas por los routers (respuesta de inventario y errores seguros)."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from core.catalog import ALL_SERVICES
from core.errors import mask_project_id, public_error


def inventory_response(servicio: str, region: str, project_id: str,
                       resultado: Dict[str, Any]) -> Dict[str, Any]:
    """Respuesta JSON que consume ``static/app.js`` (502 si un único servicio falla)."""
    avisos = [e for e in resultado["errores"] if e.get("tipo") == "aviso"]
    errores_reales = [e for e in resultado["errores"] if e.get("tipo") != "aviso"]

    if servicio != ALL_SERVICES and errores_reales and not resultado["tablas"]:
        raise HTTPException(status_code=502, detail=public_error(errores_reales[0]))

    return {
        "service": servicio,
        "region": region,
        "project_id_masked": mask_project_id(project_id),
        "total_tablas": len(resultado["tablas"]),
        "resumen": resultado["resumen"],
        "errores": [public_error(e) for e in errores_reales],
        "avisos": [public_error(e) for e in avisos],
        "tables": resultado["tablas"],
    }


async def _safe_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 sin ``input``/``ctx``: FastAPI por defecto devuelve el cuerpo recibido,
    que puede contener AK/SK (p. ej. si falta ``sk`` se devuelve el body con la AK)."""
    errors = [{k: v for k, v in error.items() if k in ("type", "loc", "msg")} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


def install_safe_validation_errors(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _safe_validation_handler)
