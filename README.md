# Tecno Control Cloud

Plataforma SaaS de senalizacion digital, kiosko touch y videowall para Mexico y LATAM.

## Stack

- Backend: FastAPI, SQLAlchemy, PostgreSQL, JWT HS256, WebSockets base
- Frontend: React, Vite, Tailwind CSS
- Infraestructura: Docker Compose, almacenamiento local de media, estructura multi-tenant

## Estructura

```text
backend/
  app/
    api/
    core/
    models/
    schemas/
    services/
  storage/
frontend/
  src/
infra/
docker-compose.yml
```

## Modulos incluidos en esta base

- Auth y usuarios con roles `super_admin` y `client`
- Clientes y sucursales multi-tenant
- Canales con modo `normal`, `expanded` y `videowall`
- Campañas, playlist y asignación a canales
- Biblioteca de contenidos con carga de archivos
- Schedules para timeline por dias y horas
- Layouts reutilizables por cliente
- Videowall con nodos, preview y coordenadas
- Kiosko touch con pantallas y hotspots
- WebSockets base para presencia y heartbeats

## Acceso inicial

No hay contrasenas predeterminadas. Crea el primer superadmin mediante el
bootstrap interactivo descrito en SECURITY-1H-R3. Los seeds son opt-in locales.

## Arranque con Docker

```bash
docker compose up --build
```

Servicios:

- API FastAPI: `http://localhost:8000`
- Swagger: `http://localhost:8000/docs`
- Frontend: `http://localhost:5173`

## Variables de entorno

- Backend base: `backend/.env.example`
- Frontend base: `frontend/.env.example`

## Prioridades siguientes recomendadas

1. Migraciones con Alembic y seeds versionados
2. Player Windows/Android con heartbeat real y sincronizacion `play_at_timestamp`
3. Timeline drag and drop persistente
4. Storage S3 compatible y thumbnails
5. Auditoria, billing SaaS y observabilidad



## SECURITY-1H-R3: configuracion y credenciales

APP_ENV admite production (default), development o test; cualquier otro valor
impide arrancar. Configure SECRET_KEY explicitamente en todos los entornos.
Produccion exige al menos 32 bytes UTF-8 y rechaza placeholders conocidos.
Use material generado con al menos 32 bytes aleatorios, por ejemplo 64 caracteres
hexadecimales. El backend valida; nunca genera, recorta ni rota la clave.
Conserve exactamente el secreto productivo vigente. Cambiarlo invalida JWT de
usuarios tras recargar procesos, no los tokens opacos de Players.

AUTO_SEED es false por defecto y esta prohibido en produccion. Para fixtures/demo
locales se requieren APP_ENV=development (o test), AUTO_SEED=true y SEED_PASSWORD
explicita que cumpla la politica. No hay contrasena fija de seed ni se resetean
cuentas existentes. No use credenciales de ejemplos en produccion. El bootstrap
python -m app.scripts.create_superadmin requiere terminal interactiva segura y
confirmacion; no recibe password por argumentos ni variables de entorno.
Este comando escribe en DB, no es un preflight.

Usuarios nuevos y cambios/reset requieren 15-128 caracteres Unicode validos;
se permiten passphrases y espacios, sin strip ni truncamiento. Se rechazan
defaults y una lista local acotada de claves obvias. Omitir password o enviar
null en edicion conserva el hash; enviar cadena vacia explicitamente devuelve
422. Login de cuentas existentes no aplica la nueva politica. No hay reset,
rehash ni cambio de TTL/algoritmo JWT. PBKDF2-HMAC-SHA256 sigue en 210000
iteraciones; aumentar su coste queda como FOLLOW_UP separado con mediciones.

Validacion local: python -B backend/tests/run_screen_rotation_suite.py establece
APP_ENV=test, SQLite en memoria y AUTO_SEED=false antes de importar el backend.
Las suites de mutacion usan copias temporales aisladas. Los tests POSIX deben
correr tambien en Linux. Ningun SKIP constituye PASS.
