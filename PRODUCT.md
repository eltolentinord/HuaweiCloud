# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Equipo técnico interno de una MSP / consultora que administra los entornos Huawei Cloud de varios clientes. Trabajan en escritorio, desde la oficina, revisando a diario el estado de conexión de cada cuenta, el inventario de recursos, las limitaciones de permisos IAM, el historial de escaneos y los costos. El uso en móvil/tablet es secundario (consultas rápidas), pero debe funcionar.

## Product Purpose

Plataforma multi-tenant de inventario y gestión (solo lectura) de Huawei Cloud. Cada cliente de la plataforma (tenant) tiene una o varias cuentas Huawei Cloud con credenciales IAM; la plataforma descubre Projects, Regiones y Enterprise Projects, ejecuta escaneos de inventario con colectores oficiales (ECS, EVS, EIP, VPC, OBS, CBR, ELB, RDS, DCS, NAT, VPN, CES, HSS, WAF, CFW), guarda historial, compara escaneos, estima/compara costos entre regiones y exporta a Excel/CSV. Éxito: saber en segundos el estado de cada cliente y qué hay (y qué no se pudo leer) en su nube.

## Positioning

- Multi-cliente con aislamiento total: un cliente nunca ve datos de otro.
- Solo lectura y seguro: nunca crea ni modifica recursos; AK/SK cifradas, jamás visibles.
- Datos reales, nada inventado: inventario, permisos IAM y precios provienen de APIs oficiales; si falta un dato se dice («Precio no disponible», «Sin datos», «Discovery pendiente»).

## Operating Context

- Conceptos que no deben mezclarse: Cliente/Tenant (de la plataforma) frente a Cuenta, IAM, Project, Enterprise Project y Región (de Huawei Cloud).
- Flujo principal: Cliente → Cuenta Huawei → Conectar (validar IAM) → Descubrir → Escanear inventario → revisar recursos, permisos, historial y costos.
- Estados habituales: credenciales rechazadas, permisos limitados por servicio (403), discovery parcial, escaneo con avisos/errores.

## Capabilities and Constraints

- Stack existente: FastAPI + Jinja2 + JavaScript sin framework + Tailwind CDN + Lucide; PostgreSQL. No se añaden frameworks de frontend.
- Páginas: `/clientes` (inicio/portafolio y espacio de trabajo del cliente), `/dashboard` (inventario, historial, programaciones), `/costos/comparar-regiones`, `/costos` (facturación BSS, heredada), `/` (consulta directa, heredada).
- Operaciones destructivas sobre recursos no existen (solo lectura).
- Idioma de la interfaz: español.

## Brand Commitments

Sin marca propia todavía. «Huawei Cloud Inventory» es el nombre de trabajo; no se usan logos ni marcas registradas de Huawei.

## Evidence on Hand

Datos reales de clientes en la base local (Mi Empresa / Produccion, Itla Mexico 1). No hay testimonios, clientes públicos ni métricas de negocio: no inventarlos.

## Product Principles

1. Claridad de estado primero: cada cliente y cuenta debe comunicar de un vistazo si está conectada, rechazada o limitada.
2. Nunca presentar un dato ausente como cero o como error: distinguir «sin datos», «pendiente» y «error».
3. Seguridad visible: transmitir que es solo lectura y que las credenciales están protegidas, sin mostrar jamás secretos.
4. Densidad operativa: información técnica (IDs, códigos de región) disponible pero secundaria.
