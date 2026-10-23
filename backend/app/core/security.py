from base64 import b64decode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import re
import secrets

from fastapi import HTTPException, status

from app.core.config import get_settings


settings = get_settings()
MAX_ACCESS_TOKEN_LENGTH = 16_384


def _b64_encode(data: bytes) -> str:
    return urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def _b64_decode(data: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", data):
        raise ValueError("Invalid base64url")
    padding = "=" * (-len(data) % 4)
    decoded = b64decode(data + padding, altchars=b"-_", validate=True)
    if _b64_encode(decoded) != data:
        raise ValueError("Noncanonical base64url")
    return decoded


def hash_password(password: str) -> str:
    iterations = 210_000
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, raw_iterations, salt, digest = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        calculated = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            int(raw_iterations),
        ).hex()
        return hmac.compare_digest(calculated, digest)
    except ValueError:
        return False


def create_access_token(subject: str, extra_claims: dict[str, object] | None = None) -> str:
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": subject,
        "exp": int(expires_at.timestamp()),
        **(extra_claims or {}),
    }
    encoded_header = _b64_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    encoded_payload = _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{encoded_header}.{encoded_payload}".encode("utf-8")
    signature = hmac.new(settings.SECRET_KEY.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{encoded_header}.{encoded_payload}.{_b64_encode(signature)}"


def decode_access_token(token: str) -> dict[str, object]:
    try:
        if not isinstance(token, str) or not 0 < len(token) <= MAX_ACCESS_TOKEN_LENGTH:
            raise ValueError("Invalid token length")
        encoded_header, encoded_payload, encoded_signature = token.split(".")
        header = json.loads(_b64_decode(encoded_header).decode("utf-8"))
        if not isinstance(header, dict) or header.get("alg") != "HS256":
            raise ValueError("Unsupported algorithm")
        signing_input = f"{encoded_header}.{encoded_payload}".encode("utf-8")
        expected_signature = hmac.new(settings.SECRET_KEY.encode("utf-8"), signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(expected_signature, _b64_decode(encoded_signature)):
            raise ValueError("Invalid signature")
        payload = json.loads(_b64_decode(encoded_payload).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Invalid claims")
        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject or not subject.isprintable() or subject.strip() != subject:
            raise ValueError("Invalid subject")
        if type(payload.get("exp")) is not int:
            raise ValueError("Invalid expiration")
    except (ValueError, RecursionError):
        # ValueError includes base64, Unicode and JSON decoding failures. Do not
        # catch application/DB errors or include untrusted token data in errors.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from None

    if payload["exp"] <= int(datetime.now(UTC).timestamp()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")

    return payload

