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

## Credenciales demo

- Super admin: `superadmin@tecnocontrol.com` / `ChangeMe123!`
- Cliente demo: `demo@demo-retail.com` / `ChangeMe123!`

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


