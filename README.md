# Huawei Cloud Inventory

Aplicación web de **solo lectura** para consultar y exportar el inventario de 15 servicios
de Huawei Cloud (ECS, EVS, EIP, OBS, CBR, ELB, RDS, DCS, VPC, NAT, VPN, Cloud Eye, HSS,
WAF, CFW) y comparar costos mensuales. No crea, modifica, apaga ni elimina recursos.

## Instalación (Windows PowerShell)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt        # aplicación
python -m pip install -r requirements-dev.txt    # + httpx para los tests de la API
```

## Ejecución

```powershell
python app.py                                    # http://127.0.0.1:8000
# o bien
uvicorn app:app --host 127.0.0.1 --port 8000
```

Las credenciales (AK/SK, Project ID, región) se escriben en el formulario: viajan solo en
la petición, no se guardan en el servidor, el navegador (localStorage) ni los logs, y nunca
aparecen en respuestas o mensajes de error.

### Scripts de línea de comandos

`test_ecs.py` (inventario ECS detallado) y `test.py` (flavors con endpoints explícitos,
p. ej. Huawei Cloud Europa) leen las credenciales de variables de entorno. Consulta
`.env.example`:

```powershell
$env:HUAWEI_AK = "TU_ACCESS_KEY"
$env:HUAWEI_SK = "TU_SECRET_KEY"
$env:HUAWEI_PROJECT_ID = "TU_PROJECT_ID"
$env:HUAWEI_REGION = "ap-southeast-3"
python test_ecs.py [--raw-json] [--server-id ID]
```

`config.json` sigue funcionando como respaldo local para `test_ecs.py`, pero está en
`.gitignore` y no debe contener credenciales en el repositorio.

## Tests

```powershell
python -m unittest discover -s tests -t .
```

No hacen llamadas reales: todos los clientes del SDK son dobles de prueba (`tests/helpers.py`).

## Arquitectura

```
Huawei Cloud SDK
  → core/credentials.py   CredentialProvider (Static / Env) · tenancy/accounts.py: DatabaseCredentialProvider
  → core/clients.py       ClientFactory (credenciales, región/endpoint, HttpConfig)
  → collectors/*.py       un collector por servicio: SDK + paginación + normalización
  → core/pagination.py    Page / Offset / Marker / Token / SinglePage + protecciones
  → core/models.py        Resource normalizado (attributes + raw + raw_hash)
  → core/engine.py        ejecuta collectors, mide duración y clasifica errores
  → presentation/tables.py  Resource → tablas/KPIs del frontend (etiquetas en español)
  → inventory.py          fachada compatible (consultar_servicio)
  → app.py                FastAPI: /, /api/inventory, /api/export (+ routers/costs.py)
  → exports/excel.py      Excel con protección contra Formula Injection
```

### Paginación por servicio (confirmada en los docstrings del SDK 3.1.216)

| Servicio | Mecanismo | Total en respuesta |
|---|---|---|
| ECS | `offset` = **número de página** (base 1) + `limit` | `count` |
| WAF | `page` (base 1) + `pagesize` | `total` |
| EVS, CBR, RDS, DCS, CES, HSS | `offset` = registros + `limit` | `count` / `total_count` / `instance_num` / `total_num` |
| CFW | `offset` + `limit` **en el body** | `data.total` |
| EIP v2, NAT | `marker` = ID del último elemento | — |
| VPC v3 (3 listas), ELB v3, VPN conexiones | `page_info.next_marker` | VPN: `total_count` |
| OBS, VPN gateways | sin paginación en la API | — |

### Decisiones documentadas

- **OBS es de ámbito de cuenta**: `ListBuckets` devuelve buckets de todas las regiones.
  El collector es `scope="global"` y asigna a cada bucket su región real (`location`); la
  vista por región muestra solo los de la región consultada y el KPI
  "Buckets en otras regiones" cuenta el resto.
- **Proyectos de empresa**: HSS y WAF filtran por defecto al proyecto "default"; se envía
  `all_granted_eps` (documentado) y, si la cuenta lo rechaza (400/403), se reintenta sin el
  parámetro y se muestra un aviso. **CFW** no documenta `all_granted_eps`, así que no se
  envía (pendiente de validar con una cuenta real con EPS). ELB ya devuelve todos los
  proyectos sin el parámetro.
- **CES**: la API limita `offset` a 10 000 (máx. ~10 100 alarmas por consulta).
- **`raw`** conserva la respuesta serializada del SDK con `user_data`/contraseñas redactadas.
- **Excel**: las celdas que empiezan por `= + - @ \t \r` se escriben como texto con
  `quotePrefix` (no se altera el valor visible).
- **Paginación**: un cursor repetido, una página completa de duplicados, una respuesta con
  formato inválido o más de 10 000 páginas generan un error explícito (no datos truncados).

## Fase 2: base multi-cliente (PostgreSQL)

La base de datos es **opcional**: sin `DATABASE_URL` todo sigue funcionando con el
formulario. Con ella, las AK/SK se guardan cifradas y el inventario se ejecuta por
`cloud_account_id` (el navegador ya no envía credenciales).

### Configuración (PowerShell)

```powershell
python -m pip install -r requirements.txt
$env:DATABASE_URL = "postgresql+psycopg://USUARIO:CLAVE@localhost:5432/huawei_inventory"
python manage.py keys generate          # imprime una clave maestra nueva
$env:INVENTORY_ENCRYPTION_KEYS = "1:<clave_generada>"
alembic upgrade head                    # crea el esquema en una base vacía
python manage.py catalog sync           # regiones y 15 servicios desde core/catalog.py
```

Guarda la clave maestra fuera del repositorio (gestor de secretos / variable de
sistema). **Si se pierde, las AK/SK guardadas no se pueden recuperar.**

### Alta de un cliente y su cuenta

```powershell
python manage.py client create "Acme S.A." --slug acme
python manage.py account create acme "Producción"     # pide AK y SK sin mostrarlas
python manage.py account discover acme <ACCOUNT_ID>    # IAM: descubre Project IDs/regiones
python manage.py project list acme <ACCOUNT_ID>
```

### API interna (sin login todavía: solo uso local)

Se activa con `$env:INVENTORY_ADMIN_API = "true"`:

| Método | Ruta |
|---|---|
| GET/POST | `/api/admin/clients` |
| GET/PATCH/DELETE | `/api/admin/clients/{client_id}` |
| GET/POST | `/api/admin/clients/{client_id}/accounts` |
| GET/PATCH/DELETE | `/api/admin/clients/{client_id}/accounts/{account_id}` |
| PUT | `.../accounts/{account_id}/credentials` (reemplazar AK/SK) |
| POST | `.../accounts/{account_id}/discover-projects` |
| GET/POST, PATCH | `.../accounts/{account_id}/projects`, `.../projects/{project_id}` |
| POST | `/api/clients/{client_id}/accounts/{account_id}/inventory` `{"project_id", "service"}` |

La última devuelve exactamente el mismo JSON que `/api/inventory`.

### Rotación de la clave maestra

```powershell
$env:INVENTORY_ENCRYPTION_KEYS = "1:<clave_antigua>,2:<clave_nueva>"
$env:INVENTORY_ENCRYPTION_KEY_VERSION = "2"
python manage.py keys reencrypt
# después se puede retirar la versión 1
```

### Tests y PostgreSQL

`python -m unittest discover -s tests -t .` ejecuta:
- modelos/servicios/API sobre SQLite en memoria (siempre);
- migraciones y almacenamiento cifrado sobre **PostgreSQL real**: `TEST_DATABASE_URL`
  o PostgreSQL embebido (`pgserver`, en `requirements-dev.txt`). Sin ninguno de los
  dos, esos tests aparecen como *skipped*.

### Comportamiento IAM pendiente de confirmar con una cuenta real

Ver la cabecera de `core/discovery.py`: formato de nombres de subproyectos, proyectos
internos (p. ej. `MOS`, que se omite por no tener región) y cuentas con EPS.

## Fase 3A: motor de escaneo persistente

```
CloudAccount → Projects habilitados → ScanRun → ScanTasks → Collectors → resources (PostgreSQL)
```

```powershell
alembic upgrade head                                   # crea scan_runs, scan_tasks, resources
python manage.py scan run acme <ACCOUNT_ID>            # escaneo completo (secuencial)
python manage.py scan run acme <ACCOUNT_ID> --service ecs --service obs
python manage.py scan show acme <SCAN_ID>
```

API interna (con `INVENTORY_ADMIN_API=true`):

| Método | Ruta | |
|---|---|---|
| POST | `/api/clients/{client_id}/accounts/{account_id}/scans` | inicia un escaneo (202, se ejecuta en segundo plano) |
| GET | `/api/clients/{client_id}/accounts/{account_id}/scans` | últimos escaneos |
| GET | `/api/scans/{scan_id}?client_id=...` | estado, progreso, tareas y errores seguros |
| GET | `/api/clients/{client_id}/accounts/{account_id}/resources` | inventario (`service`, `region`, `include_deleted`, `include_raw`, `limit`, `offset`) |

Reglas del motor:
- **Regionales**: una tarea por proyecto × servicio. **Globales (OBS)**: una tarea por cuenta.
- **Upsert** por `(account_id, resource_type, scope_key, provider_id)`; `scope_key` es
  `project:<id>` o `account` (OBS). Nuevo → created; mismo contenido → solo `last_seen`;
  contenido distinto (`raw_hash` o campos normalizados) → updated.
- **deleted_at** solo tras una consulta **exitosa y completa** del servicio en ese ámbito.
  Un error (401/403/500/red) o una cobertura parcial (HSS/WAF sin `all_granted_eps`)
  nunca marca recursos como eliminados. Nunca hay borrado físico.
- Un servicio fallido no aborta el escaneo: `completed_with_errors`.
- `raw` se guarda con secretos redactados (user_data, contraseñas, tokens, Authorization,
  y cualquier texto que contenga la AK/SK de la cuenta).
- Un escaneo `pending/running` de más de 2 h se considera abandonado y no bloquea otro.

## Fase 3B: multi-proyecto, multi-región y paralelismo controlado

- En Huawei Cloud cada **Project ID pertenece a una sola región**; una cuenta cubre varias
  regiones (y subproyectos) mediante varios proyectos. Cada proyecto es un ámbito
  independiente de recursos, aunque compartan región.
- **Planificador**: hasta `INVENTORY_SCAN_MAX_WORKERS` tareas a la vez (defecto 4), con
  límites por servicio (2) y por región (3). Los workers solo llaman a Huawei y
  normalizan; **un único escritor** (hilo principal) guarda en PostgreSQL.
- **Límite global** de llamadas simultáneas por proceso (`INVENTORY_MAX_CONCURRENT_CALLS`, 8).
- **Throttling** (HTTP 429 `APIGW.0308`): reintento con backoff exponencial y jitter
  (4 intentos). Ningún otro error se reintenta.
- **Un escaneo activo por cuenta**, garantizado por un índice único parcial; un escaneo
  sin latido durante 60 min se considera abandonado y se libera.
- Filtros: `--region` / `--project` (CLI) o `regions` / `project_ids` (API).

## Validación con Huawei Cloud real

### Clasificación de errores (`core/errors.py`)

| Categoría | Señal | Gravedad | Escaneo |
|---|---|---|---|
| `authentication` | 401 con código `APIGW.*` (p. ej. APIGW.0301: AK/SK o firma inválidas) | error | `failed`; se omiten las tareas restantes |
| `authorization` | 401 con código del servicio / acción IAM en el mensaje (p. ej. VPN.0003 `vpn:vpnGateways:list`) | aviso | tarea `denied` |
| `permission` | 403, AccessDenied, Forbidden | aviso | tarea `denied` |
| `unavailable` | 404 `APIGW.0101` (API no publicada en la región) | aviso | tarea `unavailable` |
| `api` / `network` / `pagination` / `internal` | resto de errores HTTP, red, paginación, fallos del collector | error | tarea `failed` |

Ningún error ni aviso marca recursos como eliminados. Un escaneo con datos y solo
avisos termina en `completed_with_warnings`.

### Comprobación real controlada (solo lectura)

```powershell
$env:HUAWEI_AK = "..." ; $env:HUAWEI_SK = "..."   # nunca en el código ni en chats
$env:HUAWEI_PROJECT_ID = "..."                     # opcional (si falta, se usa IAM)
python scripts/live_check.py                       # ECS, EVS, VPC y VPN por defecto
```

Imprime solo conteos, categorías de error y comprobaciones de coherencia (sí/no).

## Advertencias

- Nunca publiques ni compartas AK/SK. Si una AK/SK se compartió (chat, ticket, repositorio),
  debe revocarse aunque haya sido temporal.
- Las variables `$env:` solo duran la sesión actual de PowerShell.
