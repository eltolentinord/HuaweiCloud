# coding: utf-8
"""Cliente solo lectura para Huawei Cloud BSS Cost Center (Cost Analysis).

El endpoint BSS es el del sitio de la cuenta (``core.bss``; International por defecto).
"""

from typing import Any, Dict, List, Optional

from huaweicloudsdkcore.exceptions import exceptions

from core.bss import bss_models, create_bss_client_with_keys
from core.errors import SITE_MISMATCH_CODE, SITE_MISMATCH_MESSAGE

TAMANO_PAGINA = 500


class CostApiError(Exception):
    def __init__(self, message, http_status=None, request_id=None, error_code=None):
        super().__init__(message)
        self.http_status = http_status
        self.request_id = request_id
        self.error_code = error_code


def _registros_desde_respuesta(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convierte la respuesta oficial (cost_data -> dimensions/costs) a registros planos."""
    registros: List[Dict[str, Any]] = []
    for bloque in data.get("cost_data") or []:
        if not isinstance(bloque, dict):
            continue
        dims: Dict[str, Any] = {}
        for d in bloque.get("dimensions") or []:
            if isinstance(d, dict) and d.get("key") is not None:
                dims[d["key"]] = d.get("value")
        for costo in bloque.get("costs") or []:
            if not isinstance(costo, dict):
                continue
            amount = costo.get("amount")
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                continue
            registros.append({
                "region": dims.get("REGION_CODE") or dims.get("region") or "Sin región",
                "product": dims.get("CLOUD_SERVICE_TYPE") or dims.get("cloud_service_type") or "Sin producto",
                "currency": data.get("currency") or "Sin moneda",
                "amount": amount,
            })
    return registros


def consultar_mes(
    ak: str,
    sk: str,
    mes: str,
    cost_type: str,
    amount_type: str,
    base_url: Optional[str] = None,
    timeout: int = 60,
) -> Dict[str, Any]:
    """Consulta costos del mes (YYYY-MM). AK/SK solo en memoria; nunca se registran."""
    if not mes or len(mes) != 7:
        raise CostApiError("Formato de mes inválido; usa YYYY-MM.")

    models = bss_models()
    GroupBy, ListCostsReq = models.GroupBy, models.ListCostsReq
    ListCostsRequest, TimeCondition = models.ListCostsRequest, models.TimeCondition
    client = create_bss_client_with_keys(ak, sk)

    todos: List[Dict[str, Any]] = []
    errores: List[Dict[str, Any]] = []
    offset = 0
    while True:
        body = ListCostsReq(
            time_condition=TimeCondition(
                time_measure_id=2,
                begin_time=mes,
                end_time=mes,
            ),
            groupby=[
                GroupBy(type="dimension", key="REGION_CODE"),
                GroupBy(type="dimension", key="CLOUD_SERVICE_TYPE"),
            ],
            cost_type=cost_type,
            amount_type=amount_type,
            offset=offset,
            limit=TAMANO_PAGINA,
        )
        request = ListCostsRequest(body=body)
        try:
            response = client.list_costs(request)
        except exceptions.ClientRequestException as error:
            raise CostApiError(
                SITE_MISMATCH_MESSAGE if error.error_code == SITE_MISMATCH_CODE else error.error_msg,
                http_status=error.status_code,
                request_id=error.request_id,
                error_code=error.error_code,
            )
        except Exception as exc:
            raise CostApiError(f"Error de conexión: {type(exc).__name__}: {exc}")

        from huaweicloudsdkcore.utils.http_utils import sanitize_for_serialization
        data = sanitize_for_serialization(response) or {}

        registros = _registros_desde_respuesta(data)
        todos.extend(registros)

        total = data.get("total_count")
        try:
            total = int(total) if total is not None else None
        except (TypeError, ValueError):
            total = None

        if not registros:
            break
        if total is not None and len(todos) >= total:
            break
        if len(registros) < TAMANO_PAGINA:
            break
        offset += len(registros)

    return {"records": todos, "errors": errores}


def consultar_recursos_mes(ak: str, sk: str, mes: str) -> List[Dict[str, Any]]:
    """Consumo por RECURSO del mes (BSS ``ListCustomerselfResourceRecords``).

    Reutiliza ``costs.billing.fetch_resource_bills`` con el BSS del sitio de la cuenta.
    AK/SK solo en memoria; los errores salen clasificados y redactados.
    """
    from costs.billing import fetch_resource_bills
    from core.errors import classify_exception

    if not mes or len(mes) != 7:
        raise CostApiError("Formato de mes inválido; usa YYYY-MM.")
    try:
        client = create_bss_client_with_keys(ak, sk)
        resultado = fetch_resource_bills(None, mes, client_builder=lambda _clients: client)  # type: ignore[arg-type]
    except Exception as exc:
        error = classify_exception(exc, service="bss", secrets=(ak, sk))
        mensaje = SITE_MISMATCH_MESSAGE if error.error_code == SITE_MISMATCH_CODE else error.message
        raise CostApiError(mensaje, http_status=error.http_status, request_id=error.request_id,
                           error_code=error.error_code)
    moneda = resultado.get("currency") or "USD"
    registros: List[Dict[str, Any]] = []
    for r in resultado.get("records") or []:
        if not isinstance(r, dict):
            continue
        try:
            importe = float(r.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        registros.append({
            "resource_id": r.get("resource_id") or "",
            "resource_name": r.get("resource_name") or "",
            "region": r.get("region") or "",
            "region_name": r.get("region_name") or "",
            "service_code": r.get("cloud_service_type") or "",
            "service_name": r.get("cloud_service_type_name") or "",
            "resource_type_name": r.get("resource_type_name") or "",
            "spec": r.get("product_spec_desc") or "",
            "amount": importe,
            "currency": moneda,
        })
    return registros
