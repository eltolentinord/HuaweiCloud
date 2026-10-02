# Autenticación y autorización

## Estado actual (Fase 4)

| Pieza | Estado |
|---|---|
| Modelo de usuarios (`users`, `user_client_roles`) | Hecho (Fase 2) |
| Roles por cliente `viewer` ⊂ `operator` ⊂ `admin` + admin de plataforma | Hecho (`core/authz.py`) |
| Matriz de permisos y comprobación por ruta (`requires(Permission.X)`) | Hecho en todas las rutas internas |
| Aislamiento: sin rol → 404; rol insuficiente → 403 | Hecho y probado |
| Identidad del llamante (`routers.security.get_principal`) | Token de servicio o solo local |
| Login de usuarios (OIDC/JWT) | **Pendiente** (diseño abajo) |

Hoy solo existen dos principales, ambos administradores de plataforma:

- `INVENTORY_ADMIN_TOKEN` definido → `Authorization: Bearer <token>` (servicio).
- Sin token → solo peticiones desde la propia máquina (loopback).

No hay un login "casero": las contraseñas de usuarios no se almacenan en esta aplicación.

## Permisos

| Permiso | viewer | operator | admin (cliente) | admin plataforma |
|---|:-:|:-:|:-:|:-:|
| clients:read, accounts:read, scans:read, inventory:read, exports:read, costs:read | ✔ | ✔ | ✔ | ✔ |
| scans:run (llama a Huawei), projects:manage, schedules:manage | | ✔ | ✔ | ✔ |
| accounts:manage, credentials:manage (AK/SK), audit:read | | | ✔ | ✔ |
| clients:manage (crear/modificar/borrar clientes) | | | | ✔ |

## Integración del login (fase posterior)

1. **Proveedor de identidad externo (OIDC)**: Keycloak, Microsoft Entra ID, Auth0, Okta…
   La aplicación no guarda contraseñas ni gestiona MFA.
2. **Validación del token** en `get_principal` (único punto a cambiar):
   - JWT firmado; verificar `iss`, `aud`, `exp`, `nbf` y la firma con las claves JWKS
     del proveedor (cacheadas, con rotación). Librería recomendada: `PyJWT[crypto]`.
   - Algoritmos permitidos explícitos (p. ej. `RS256`); nunca `none`.
3. **Mapeo de identidad**: `sub` (o email verificado) → `users` (añadir columnas
   `external_subject` única e `identity_provider`, migración nueva). Alta de usuarios
   por invitación de un admin del cliente; sin auto-registro abierto.
4. **Construir el principal**: `core.authz.principal_for_user(session, user.id)` ya carga
   los roles por cliente. El rol de plataforma puede venir de un grupo/claim del proveedor.
5. **Interfaz web**: flujo Authorization Code + PKCE. Sesión en cookie `HttpOnly`,
   `Secure`, `SameSite=Lax` y protección CSRF para métodos que modifican; o token en
   memoria si la UI pasa a SPA.
6. **Flujo heredado** (`/api/inventory` con AK/SK en el formulario): en modo SaaS debe
   desactivarse por configuración; en modo local se mantiene.
7. **Auditoría**: ya implementada (`audit_events`, `GET /api/clients/{id}/audit`). Con
   login real, `actor_subject` pasará a ser el usuario autenticado sin cambiar nada más.

Las rutas no cambian: ya declaran el permiso que necesitan.
