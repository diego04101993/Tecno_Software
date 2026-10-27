"""Redact credentials without changing HTTP/WebSocket token transport."""
import logging
import re
import traceback
from urllib.parse import unquote_plus


REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = {
    "token", "access_token", "player_token", "issued_player_token", "secret_key", "seed_password",
    "authorization", "password", "password_hash", "channel_code", "activation_code",
}
_QUERY_VALUE = re.compile(r"([?&])([^=&#\s]+)=([^&#\s\"'<>]*)")
# Unquoted logfmt/header values end at a new field, an existing punctuation
# separator, or a line break, not at an ordinary space. Values containing
# field syntax or separators must be quoted to distinguish them from metadata.
_FIELD_START = r"(?:[\w.-]+|[\"'][\w.-]+[\"'])[^\S\r\n]*[:=]"
_UNQUOTED_VALUE = (
    r"[^\s,;&}\]]+(?:[^\S\r\n]+(?!" + _FIELD_START + r")[^\s,;&}\]]+)*"
)
_NAMED_VALUE = re.compile(
    r"(?i)(?<![\w])(" + "|".join(sorted(_SENSITIVE_KEYS, key=len, reverse=True))
    # Only a quoted value can start on another line (e.g. pretty-printed JSON).
    + r")(\b[\"']?[^\S\r\n]*[:=](?:\s*(?=[\"'])|[^\S\r\n]*))"
    + r"(\[REDACTED\]|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|" + _UNQUOTED_VALUE + r")"
)
_BEARER = re.compile(r"(?i)\bBearer[ \t]+(?!\[REDACTED\])[^\s\"',;<>]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_PLAYER_TOKEN = re.compile(r"\btpw_[A-Za-z0-9_-]+\b")
_ACTIVATION_CODE = re.compile(r"\bTC-[0-9A-F]{4}-[0-9A-F]{4}\b", re.IGNORECASE)


def redact_credentials(value: str) -> str:
    def query(match: re.Match[str]) -> str:
        separator, key, content = match.groups()
        if unquote_plus(key).lower() in _SENSITIVE_KEYS:
            content = REDACTED
        return f"{separator}{key}={content}"

    value = _QUERY_VALUE.sub(query, value)
    value = _NAMED_VALUE.sub(lambda match: match[1] + match[2] + REDACTED, value)
    value = _BEARER.sub("Bearer " + REDACTED, value)
    for pattern in (_JWT, _PLAYER_TOKEN, _ACTIVATION_CODE):
        value = pattern.sub(REDACTED, value)
    return value


def install_security_log_redaction() -> None:
    """Cover existing/future handlers, including Uvicorn's access/error loggers.

    Uvicorn AccessFormatter requires its five original argument positions;
    retain those positions while sanitizing their string values. Other normal
    log messages are rendered first so ``token=%s`` is redacted as well.
    """
    previous_factory = logging.getLogRecordFactory()
    if getattr(previous_factory, "_tecno_security_redaction", False):
        return

    def record_factory(*args, **kwargs):
        record = previous_factory(*args, **kwargs)
        if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5:
            record.args = tuple(redact_credentials(value) if isinstance(value, str) else value for value in record.args)
            record.msg = redact_credentials(str(record.msg))
        else:
            record.msg = redact_credentials(record.getMessage())
            record.args = ()
        if record.exc_info:
            record.exc_text = redact_credentials("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = redact_credentials(record.stack_info)
        return record

    record_factory._tecno_security_redaction = True
    logging.setLogRecordFactory(record_factory)
