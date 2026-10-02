# Costos

## Fuentes (ninguna inventa precios)

| Fuente | Endpoint | Qué devuelve | Estado |
|---|---|---|---|
| Sin fuente (por defecto) | `GET …/costs/estimate` | Inventario "sin precio" + aviso | Hecho |
| Tabla de precios del usuario | `GET …/costs/estimate` | Costo **estimado** mensual por recurso | Hecho |
| BSS `ListCustomerselfResourceRecords` | `GET …/costs/actual?month=YYYY-MM` | Costo **facturado** por recurso del mes | Hecho; **pendiente validar con cuenta real** |
| Comparación de meses facturados | `GET …/costs/compare?month_a=&month_b=` | Diferencias por servicio, región y proyecto | Hecho (reutiliza `services/cost_analysis.py`) |
| BSS `ListOnDemandResourceRatings` (consulta de precios) | — | Precio oficial por especificación | Siguiente proveedor: requiere mapear cada recurso a su `resource_spec_code` |
| Módulo existente `/costos` | `POST /api/costs/compare` | Costos por región y servicio con AK/SK del formulario | Sin cambios |

`…` = `/api/clients/{client_id}/accounts/{account_id}`. Todas aceptan `format=json|xlsx|csv`
(salvo `compare`, que es JSON) y requieren el permiso `costs:read`. Las credenciales
salen de la base de datos (cifradas): el navegador no envía AK/SK.

Totales por moneda: **nunca se mezclan monedas**.

## Tabla de precios (`INVENTORY_PRICE_TABLE`)

Plantilla: [`docs/price_table.example.csv`](price_table.example.csv) (solo cabeceras).
Rellénala con precios **de tu fuente oficial** (calculadora de Huawei Cloud, contrato o
factura) e indica la fuente en la columna `source`.

| Columna | Ejemplo | Significado |
|---|---|---|
| service | `ecs` | servicio del inventario |
| resource_type | `ecs.server` | tipo de recurso |
| region | `ap-southeast-3` o `*` | región exacta o comodín |
| match_attribute / match_value | `flavor_name` / `s6.large.2` | filtro opcional sobre `attributes` |
| quantity_attribute | `size_gb` | multiplica el precio (p. ej. precio por GB-mes); vacío = precio fijo |
| unit_price | `12.34` | importe mensual (punto decimal) |
| currency | `USD` | moneda |
| source | `Calculadora 2026-10` | de dónde sale el precio |

Gana la regla más específica (región exacta y con filtro). Sin regla aplicable el
recurso aparece en "sin precio".

## Pendiente de validar con una cuenta real

- Endpoint BSS para cuentas internacionales (`huaweicloudsdkbss` en `cn-north-1`, como el
  módulo existente, frente a `huaweicloudsdkbssintl`) y permisos IAM de facturación.
- Coincidencia entre `resource_id` facturado y `provider_id` del inventario para cada
  servicio (EVS/EIP suelen coincidir; algunos servicios facturan subrecursos).
- Zona horaria del periodo (`cycle` en hora de China, UTC+8).
