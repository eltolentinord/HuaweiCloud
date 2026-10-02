# coding: utf-8
"""Cliente solo lectura para Huawei Cloud BSS Cost Center (Cost Analysis)."""

from typing import Any, Dict, List, Optional

from huaweicloudsdkcore.auth.credentials import GlobalCredentials
from huaweicloudsdkcore.exceptions import exceptions

from huaweicloudsdkbss.v2 import (
    BssClient,
    GroupBy,
    ListCostsReq,
    ListCostsRequest,
    TimeCondition,
)
from huaweicloudsdkbss.v2.region.bss_region import BssRegion

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

    credentials = GlobalCredentials(ak=ak, sk=sk)
    client = (
        BssClient.new_builder()
        .with_credentials(credentials)
        .with_region(BssRegion.value_of("cn-north-1"))
        .build()
    )

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
                error.error_msg,
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
