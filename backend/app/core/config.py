from functools import lru_cache
from pathlib import Path
import os
import hashlib

from app.core.credential_policy import validate_new_password


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
    APP_ENV = os.getenv("APP_ENV", "production")
    SECRET_KEY = os.getenv("SECRET_KEY")
    ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))
    DATABASE_URL = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@postgres:5432/tecnocontrol",
    )
    DEBUG = os.getenv("DEBUG", "true").lower() == "true"
    CORS_ORIGINS = _build_cors_origins()
    PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip() or None
    MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", str(DEFAULT_MEDIA_ROOT)))
    AUTO_SEED = os.getenv("AUTO_SEED", "false").lower() == "true"
    SEED_PASSWORD = os.getenv("SEED_PASSWORD")


def validate_rate_limit_process_configuration() -> None:
    """Reject an explicit conflicting worker setting; do not infer CLI/processes.

    R2A pins --workers 1. Replica count cannot be discovered safely here and must
    remain one operationally. Multiple workers/replicas require shared storage.
    """
    workers = os.getenv("WEB_CONCURRENCY")
    if workers is not None and workers.strip() != "1":
        raise RuntimeError("In-memory auth rate limiting requires WEB_CONCURRENCY=1 and one backend instance")


# Known repository/example defaults; length alone cannot establish strength.
_SECRET_PLACEHOLDER_DIGESTS = {
    "048af2438891a89a3536ac09cc96ccbd34a1714e88cf8fdb63e6186dcc3ff89d",
    "057ba03d6c44104863dc7361fe4578965d1887360f90a0895882e58a6248fc86",
    "2b957f198720c7359fd7d9452817a17bf513707c9954326a4f06fbcb7cc3005f",
    "8aaf4d6ecedbf63c4f48e12932749edaea82bd1532129506d4d3418b0289bb7c",
    "92809ab604c9377285fbbcf82c4f30db65f107ecfd7217c7e62c17a2b3be7633",
    "c820bae201027851d87900768beedccec11cb7d4166a6a67317b56ca5a827a39",
    "e2186dbdb1bb4193608605e84f33208765b5693b55edd4f730a719a100eeea6f",
}


def validate_security_configuration(settings: Settings) -> None:
    if settings.APP_ENV not in {"production", "development", "test"}:
        raise RuntimeError("APP_ENV must be production, development or test.")
    secret = settings.SECRET_KEY
    if not isinstance(secret, str) or not secret.strip():
        raise RuntimeError("SECRET_KEY must be explicitly configured.")
    try:
        encoded = secret.encode("utf-8")
    except UnicodeEncodeError:
        raise RuntimeError("SECRET_KEY must contain valid Unicode.") from None
    if settings.APP_ENV == "production":
        if len(encoded) < 32:
            raise RuntimeError("SECRET_KEY must contain at least 32 UTF-8 bytes in production.")
        digest = hashlib.sha256(secret.strip().casefold().encode("utf-8")).hexdigest()
        if digest in _SECRET_PLACEHOLDER_DIGESTS:
            raise RuntimeError("SECRET_KEY cannot be a known placeholder in production.")
        if settings.AUTO_SEED:
            raise RuntimeError("AUTO_SEED is prohibited in production.")
    if settings.AUTO_SEED:
        try:
            validate_new_password(settings.SEED_PASSWORD)
        except ValueError:
            raise RuntimeError("AUTO_SEED requires an explicit valid SEED_PASSWORD in development/test.") from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    validate_security_configuration(settings)
    settings.MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    return settings
