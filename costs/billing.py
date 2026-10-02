# coding: utf-8
"""Costo REAL facturado por recurso: BSS ``ListCustomerselfResourceRecords``.

Confirmado en el SDK ``huaweicloudsdkbss`` 3.1.216:
- ``GET /v2/bills/customer-bills/res-fee-records``; parámetros ``cycle`` (YYYY-MM,
  hora de China), ``statistic_type`` (1 = por periodo de facturación), ``method``
  (``oneself`` = solo el propio cliente), ``offset``/``limit``.
- Respuesta: ``fee_records`` (``ResFeeRecordV2``: ``resource_id``, ``region``,
  ``cloud_service_type``, ``amount``, ``official_amount``, ``enterprise_project_id``…),
  ``total_count`` y ``currency``.

PENDIENTE DE VALIDAR con una cuenta real: el endpoint BSS a usar para cuentas
internacionales (``huaweicloudsdkbss`` en cn-north-1, como el módulo de costos
existente, frente a ``huaweicloudsdkbssintl``) y los permisos IAM necesarios. La
obtención del cliente es inyectable (``client_builder``) para poder cambiarla.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional, Sequence

from core.clients import ClientFactory
from core.pagination import OffsetPagination, Page, paginate
from core.serialization import serialize_items
from costs.report import CostLine, CostReport
from db.models import InventoryResource

PAGE_SIZE = 100
BSS_REGION = "cn-north-1"  # el mismo que usa services/huawei_costs.py


def default_client_builder(clients: ClientFactory) -> Any:
    from huaweicloudsdkbss.v2 import BssClient
    from huaweicloudsdkbss.v2.region.bss_region import BssRegion

    return clients.create_global(BssClient, BssRegion, "bss", BSS_REGION)


def fetch_resource_bills(clients: ClientFactory, month: str, *,
                         client_builder: Callable[[ClientFactory], Any] = default_client_builder) -> Dict[str, Any]:
    """Todos los registros de consumo del mes (paginados). Lanza las excepciones del SDK."""
    from huaweicloudsdkbss.v2 import ListCustomerselfResourceRecordsRequest

    client = client_builder(clients)
    currency: Dict[str, Optional[str]] = {"value": None}

    def fetch(params: dict) -> Page:
        response = client.list_customerself_resource_records(ListCustomerselfResourceRecordsRequest(
            cycle=month, statistic_type=1, method="oneself", include_zero_record=False, **params))
        currency["value"] = currency["value"] or getattr(response, "currency", None)
        return Page(items=serialize_items(getattr(response, "fee_records", None)),
                    total=getattr(response, "total_count", None))

    records = paginate(fetch, OffsetPagination(limit=PAGE_SIZE), id_key=None, label="bss.res-fee-records")
    return {"records": records, "currency": currency["value"] or "USD"}


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def build_actual_report(month: str, bills: Dict[str, Any], resources: Sequence[InventoryResource],
                        project_names: dict) -> CostReport:
    """Agrupa la facturación por ``resource_id`` y la cruza con el inventario (``provider_id``)."""
    by_provider = {r.provider_id: r for r in resources}
    grouped: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"amount": Decimal("0"), "official": Decimal("0")})
    for record in bills["records"]:
        key = record.get("resource_id") or f"(sin id) {record.get('cloud_service_type', '')}"
        item = grouped[key]
        item["amount"] += _decimal(record.get("amount"))
        item["official"] += _decimal(record.get("official_amount"))
        item.setdefault("region", record.get("region") or "")
        item.setdefault("service_code", record.get("cloud_service_type") or "")
        item.setdefault("name", record.get("resource_name"))
    report = CostReport(kind="actual", period=month,
                        source=f"Huawei Cloud BSS: consumo por recurso ({month}, facturado)")
    for provider_id, item in sorted(grouped.items()):
        resource = by_provider.get(provider_id)
        line = CostLine(provider_id=provider_id, service=resource.service if resource else item["service_code"],
                        region=resource.region if resource else item["region"], amount=item["amount"],
                        currency=bills["currency"], resource_id=resource.id if resource else None,
                        project=project_names.get(resource.project_id, "(cuenta)") if resource else None,
                        name=(resource.name if resource else item["name"]), basis="facturado",
                        official_amount=item["official"])
        (report.lines if resource else report.unmatched).append(line)
    return report


def compare_months(report_a: CostReport, report_b: CostReport) -> Dict[str, Any]:
    """Comparación entre meses con el análisis existente (``services.cost_analysis.analizar``)."""
    from services.cost_analysis import analizar

    def records(report: CostReport) -> List[Dict[str, Any]]:
        return [{"region": l.region or "Sin región", "product": l.service, "currency": l.currency,
                 "amount": float(l.amount)} for l in report.lines + report.unmatched]

    return analizar(records(report_a), records(report_b), report_a.period or "", report_b.period or "")
