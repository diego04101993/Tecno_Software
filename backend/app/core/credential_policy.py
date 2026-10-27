"""Policy for NEW credentials only; existing hashes/login remain unchanged."""
import hashlib

MIN_PASSWORD_LENGTH = 15
MAX_PASSWORD_LENGTH = 128
# Historical project default: deny it without republishing the credential.
_PROJECT_DEFAULT_DIGESTS = {
    "f61d2974715477dfe92bcb8ed5b767abdce80a0d2dae0ca07a99c5e4901a1d30",
}
_COMMON_PASSWORDS = frozenset({
    "password", "passwordpassword", "password123456789",
    "123456789012345", "1234567890123456", "qwertyuiopasdfgh",
    "qwertyuiopasdfghjkl", "letmeinletmeinletmein", "adminadminadmin",
    "changemechangeme", "correct horse battery staple",
})


def validate_new_password(value: str) -> str:
    """Validate exact Unicode code points; never strip, normalize or truncate."""
    if not isinstance(value, str):
        raise ValueError("La contrasena debe ser texto.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("La contrasena debe contener Unicode valido.") from None
    if not MIN_PASSWORD_LENGTH <= len(value) <= MAX_PASSWORD_LENGTH:
        raise ValueError("La contrasena debe tener entre 15 y 128 caracteres.")
    if not value.strip():
        raise ValueError("La contrasena no puede contener solo espacios.")
    # Normalization is ONLY for the blocklist, never storage/authentication.
    candidate = value.strip().casefold()
    if (candidate in _COMMON_PASSWORDS
            or hashlib.sha256(candidate.encode("utf-8")).hexdigest() in _PROJECT_DEFAULT_DIGESTS):
        raise ValueError("Elige una contrasena menos comun.")
    return value
