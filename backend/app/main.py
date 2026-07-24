from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import media
from app.api.router import api_router
from app.core.config import get_settings
from app.core.database import SessionLocal, init_db
from app.services.seed import seed_initial_data


settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    if settings.AUTO_SEED:
        db = SessionLocal()
        try:
            seed_initial_data(db)
        finally:
            db.close()
    yield


app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    description="Plataforma multi-tenant de señalización digital, kiosko y videowall.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(media.router)
app.include_router(api_router, prefix=settings.API_PREFIX)


@app.get("/")
def root() -> dict:
    return {
        "name": settings.APP_NAME,
        "status": "ok",
        "api": settings.API_PREFIX,
        "docs": "/docs",
    }
