from functools import lru_cache
from pathlib import Path
import os


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_MEDIA_ROOT = BASE_DIR / "storage" / "uploads"
DEFAULT_CORS_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://192.168.3.83:5173",
)


def _build_cors_origins() -> list[str]:
    raw_origins = os.getenv("CORS_ORIGINS", "")
    configured_origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]

    merged_origins: list[str] = []
    for origin in [*DEFAULT_CORS_ORIGINS, *configured_origins]:
        if origin not in merged_origins:
            merged_origins.append(origin)
    return merged_origins


class Settings:
    # R2B is process-local: one worker and one backend instance only.
    AUTH_RATE_LIMIT_MAX_ENTRIES = 20_000
    AUTH_RATE_LIMIT_IDLE_TTL = 600
    AUTH_RATE_LIMIT_CLEANUP_INTERVAL = 30
    APP_NAME = os.getenv("APP_NAME", "Tecno Control SaaS")
    API_PREFIX = os.getenv("API_PREFIX", "/api")
    SECRET_KEY = os.getenv("SECRET_KEY", "change-this-secret-in-production")
    ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))
    DATABASE_URL = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@postgres:5432/tecnocontrol",
    )
    DEBUG = os.getenv("DEBUG", "true").lower() == "true"
    CORS_ORIGINS = _build_cors_origins()
    PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip() or None
    MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", str(DEFAULT_MEDIA_ROOT)))
    AUTO_SEED = os.getenv("AUTO_SEED", "true").lower() == "true"


def validate_rate_limit_process_configuration() -> None:
    """Reject an explicit conflicting worker setting; do not infer CLI/processes.

    R2A pins --workers 1. Replica count cannot be discovered safely here and must
    remain one operationally. Multiple workers/replicas require shared storage.
    """
    workers = os.getenv("WEB_CONCURRENCY")
    if workers is not None and workers.strip() != "1":
        raise RuntimeError("In-memory auth rate limiting requires WEB_CONCURRENCY=1 and one backend instance")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    return settings
