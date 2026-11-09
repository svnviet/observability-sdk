import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

_SENSITIVE_KEY = re.compile(r"pass(word)?|secret|token|api[_-]?key|authorization|credential", re.I)
_SENSITIVE_INLINE = re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)=\S+")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+")
REDACTED = "[REDACTED]"

_STANDARD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "obskit"}


def redact_text(text: str) -> str:
    return _BEARER.sub(
        f"Bearer {REDACTED}", _SENSITIVE_INLINE.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    )


def redact_value(key: str, value: Any) -> Any:
    if _SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {k: redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, str):
        return redact_text(value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
            **getattr(record, "obskit", {}),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD:
                entry[key] = redact_value(key, value)
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception_type"] = record.exc_info[0].__name__
            entry["stack_trace"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(entry, default=str)
