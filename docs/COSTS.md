# Costos

## Fuentes (ninguna inventa precios)

| Fuente | Endpoint | Qué devuelve | Estado |
|---|---|---|---|
| Sin fuente (por defecto) | `GET …/costs/estimate` | Inventario "sin precio" + aviso | Hecho |
| Tabla de precios del usuario | `GET …/costs/estimate` | Costo **estimado** mensual por recurso | Hecho |
| BSS `ListCustomerselfResourceRecords` | `GET …/costs/actual?month=YYYY-MM` | Costo **facturado** por recurso del mes | Hecho; **pendiente validar con cuenta real** |
| Comparación de meses facturados | `GET …/costs/compare?month_a=&month_b=` | Diferencias por servicio, región y proyecto | Hecho (reutiliza `services/cost_analysis.py`) |
| BSS `ListOnDemandResourceRatings` / `ListRateOnPeriodDetail` (consulta de precios) | `…/cost-compare/prices/refresh` | Precio oficial de lista por especificación | Hecho para ECS, EVS, EIP y ancho de banda; **pendiente validar con cuenta real** (ver abajo) |
| Comparador de costos por región | `/costos/comparar-regiones` | Origen real vs. destino simulado | Hecho (sección siguiente) |
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

- Permisos IAM de facturación. El endpoint ya está resuelto: ver «Endpoint BSS por sitio».
- Coincidencia entre `resource_id` facturado y `provider_id` del inventario para cada
  servicio (EVS/EIP suelen coincidir; algunos servicios facturan subrecursos).
- Zona horaria del periodo (`cycle` en hora de China, UTC+8).

## Comparador de costos por región (`/costos/comparar-regiones`)

Calculadora tipo *Huawei Cloud Price Calculator* que parte de un recurso REAL del
inventario. Es una **simulación**: no crea, migra ni modifica nada en Huawei Cloud
ni en el inventario.

1. **Origen**: cliente → cuenta → inventario actual (último escaneo) → región → recurso
   (ECS, EVS o EIP). Del inventario se extrae: flavor, vCPU, RAM, sistema operativo,
   discos EVS conectados (tipo y tamaño), EIP (tipo, ancho de banda, compartido o no).
   Lo que el inventario NO dice (sistema operativo si falta; si el ancho de banda se
   cobra por tamaño o por tráfico, que la API de EIP v2 no devuelve) se pide al usuario
   y queda anotado como "indicado por el usuario". Nunca se supone.
2. **Destino**: región (global o por recurso), flavor (con la lista oficial de la región
   si se consultó), vCPU/RAM (informativos: el precio lo fija el flavor), sistema
   operativo, discos (tipo, tamaño, cantidad), EIP (tipo, ancho de banda, modo de cobro).
   "Restablecer destino" vuelve a la configuración del origen.
3. **Resultado**: tabla Concepto / Origen / Destino, costo origen, costo destino,
   diferencia absoluta (destino − origen) y porcentual ((destino − origen) / origen × 100),
   moneda, período, modelo de cobro, fuente y fecha del precio más antiguo usado.
   Varios recursos a la vez con total por moneda. Exportación Excel/CSV con el
   sistema de exportación existente (neutraliza fórmulas).

### Fuentes de precio (en este orden; ninguna inventa)

| Orden | Fuente | Dónde se guarda |
|---|---|---|
| 1 | Precio oficial de lista de Huawei Cloud BSS (`official_website_amount`), botón **Consultar precios** | Tabla `price_catalog_entries` con fuente, modo de cobro, período, moneda y `fetched_at` |
| 2 | Tabla de precios oficial del usuario (`INVENTORY_PRICE_TABLE`, mismas reglas que la estimación) | Archivo CSV; para el ancho de banda usa `resource_type = eip.bandwidth` (`bandwidth_type`, `bandwidth_size`) |
| 3 | Ninguna | Se muestra **"precio no disponible"** con el motivo |

- Un total solo se calcula si TODOS los componentes tienen precio y una sola moneda; la
  diferencia, solo si origen y destino son comparables.
- Los precios se buscan por configuración EXACTA (mismo tamaño de disco o ancho de
  banda): no se extrapola de 40 GB a 100 GB.
- Pago por uso: precio por hora × horas de uso al mes (730 por defecto, editable y visible).
  Suscripción mensual: precio de 1 mes. No se mezclan modos.
- El catálogo es información pública de lista: no guarda descuentos ni importes facturados.

### API (`…/cost-compare`)

| Método | Ruta | Permiso | Llama a Huawei |
|---|---|---|---|
| GET | `/options?region=` | costs:read | No |
| GET | `/origin/{resource_id}` | costs:read | No |
| POST | `/compare` | costs:read | No |
| POST | `/compare/export?format=xlsx\|csv` | costs:read | No |
| POST | `/prices/refresh` | scans:run (operador) | Sí: BSS, una consulta por componente distinto (máx. 120), auditada |
| POST | `/flavors/refresh?region=` | scans:run (operador) | Sí: ECS `ListFlavors`, auditada |

Seguridad: rutas siempre bajo cliente y cuenta (un recurso de otra cuenta = 404);
región destino validada (formato y catálogo de regiones), sin endpoints libres (SSRF);
credenciales cifradas de la cuenta, nunca en respuestas; errores de Huawei clasificados
y redactados; el destino se valida con listas blancas (flavor, tipos de disco, rangos).

### Qué precios se pueden obtener realmente y qué queda pendiente

| Componente | API oficial | Estado |
|---|---|---|
| ECS (flavor + Linux/Windows) | BSS `ListOnDemandResourceRatings` / `ListRateOnPeriodDetail`, `resource_spec = <flavor>.linux\|.win` | Implementado; código `hws.service.type.ec2` / `hws.resource.type.vm` **pendiente de validar** |
| EVS (SATA/SAS/GPSSD/SSD/ESSD…, GB) | Igual, `resource_size` + `size_measure_id=17` | Implementado; `hws.service.type.ebs` / `hws.resource.type.volume` **pendiente de validar** |
| EIP (`5_bgp`, `5_sbgp`…) | Igual | Implementado; `hws.service.type.vpc` / `hws.resource.type.ip` **pendiente de validar** |
| Ancho de banda dedicado por tamaño (`19_*`, Mbps) | Igual, `size_measure_id=15` | Implementado; `hws.resource.type.bandwidth` **pendiente de validar** |
| Ancho de banda por tráfico | Precio por GB transferido | **No incluido**: depende del uso; se muestra "precio no disponible" |
| Ancho de banda compartido | Producto aparte | **No incluido** (se indica) |
| Flavors por región | ECS `ListFlavors` | Implementado (requiere Project ID en la región) |
| Imágenes de pago/marketplace, licencias, backups, snapshots, tráfico | — | **Pendiente de fuente oficial** (no se cotizan) |
| RDS, OBS, ELB, NAT y demás servicios | BSS (códigos propios por servicio) | **Pendiente**: el comparador admite hoy ECS, EVS y EIP |

Requisitos para consultar a BSS (pendientes de confirmar con la cuenta real):
- La cuenta necesita un **Project ID en la región cotizada** (`python manage.py account discover`
  o `project add`); sin él, el componente queda "precio no disponible" con ese motivo.
- Permisos IAM de consulta de precios de BSS para el usuario de las AK/SK.
- Si un código de producto no es válido, la consulta falla de forma segura y se muestra
  el motivo.

## Códigos de producto para la consulta de precios

Los códigos que BSS exige (`cloud_service_type`, `resource_type`, `usage_factor`,
`usage_measure_id`) están confirmados con las plantillas oficiales de Huawei Cloud incluidas en
la skill `huawei-cloud-business-tf-support`
(`scripts/bss/list_on_demand_resource_ratings.py` y `list_rate_on_period_detail.py`).

| Componente | cloud_service_type | resource_type | Notas |
|---|---|---|---|
| ECS | `hws.service.type.ec2` | `hws.resource.type.vm` | spec `<flavor>.linux` / `.win` |
| EVS | `hws.service.type.ebs` | `hws.resource.type.volume` | GB (`size_measure_id` 17) |
| EIP (IP) | `hws.service.type.vpc` | `hws.resource.type.ip` | spec `5_bgp` / `5_sbgp` |
| Ancho de banda | `hws.service.type.vpc` | `hws.resource.type.bandwidth` | spec `19_*`, Mbps (15) |
| Tráfico | `hws.service.type.vpc` | `hws.resource.type.bandwidth` | spec `12_*`, `upflow` por GB (10), **sin tamaño** |
| OBS | `hws.service.type.obs` | `hws.resource.type.obs` | spec `obs.standard` / `warm` / `cold`, `Duration` por hora, **sin tamaño**, solo pago por uso |

Periodos de suscripción: `period_type` 2 = mes, 3 = año.

**RDS no se puede cotizar**: no aparece en la matriz de servicios con consulta de precios de
Huawei Cloud. Su catálogo de flavors (`/calculator/rds/flavors`) se conserva como información,
sin precio. Otros servicios con códigos ya confirmados que podrían añadirse más adelante:
NAT Gateway, SFS Turbo, ELB dedicado, VPC Endpoint y BMS.

## Endpoint BSS por sitio

BSS (facturación, precios, costos) es global pero **cada sitio de Huawei Cloud tiene su
endpoint**, y una cuenta solo puede consultar el de su sitio. El de otro sitio responde
HTTP 403 `CBC.0156` («The customer does not belong to the website you are now at»), que la
plataforma clasifica como `site_mismatch` (configuración, no permisos IAM).

| Sitio | Endpoint | SDK (región) |
|---|---|---|
| International (por defecto) | `bss-intl.myhuaweicloud.com` | `huaweicloudsdkbssintl` (`ap-southeast-1`) |
| Europa | `bss.myhuaweicloud.eu` | `huaweicloudsdkbssintl` (`eu-west-101`) |
| China | `bss.myhuaweicloud.com` | `huaweicloudsdkbss` (`cn-north-1`) |

Todo el uso de BSS (`services/huawei_costs.py`, `costs/billing.py`, `costs/huawei_pricing.py`)
obtiene el cliente y los modelos de `core/bss.py` (`create_bss_client`, `bss_models`). Hoy
todas las cuentas usan International; soportar China o Europa consiste en elegir otro sitio
en `core.bss`, sin fijar regiones en los módulos de costos.

