"""Temporary diagnostic log for ordinary non-streaming LLM calls.

Disabled unless ``QD_LLM_CALL_DIAGNOSTICS`` is ``1``, ``true``, ``yes``, or ``on``.
A separate exact-hash patch script wires the call sites; importing this module
does not change provider requests, retries, parsing, or timeouts.

Records timings and provider metadata only. It never writes credentials,
message text, reasoning text, strategy source, user or strategy identifiers,
or raw request and response bodies.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from logging.handlers import RotatingFileHandler
from typing import Any
from urllib.parse import urlsplit

_LOGGER_NAME = "quantdinger.llm_call_diagnostics"
_ENABLED = {"1", "true", "yes", "on"}
_PHASES = {"send", "return", "exception", "parsed"}
_TIMEOUT_TYPES = {"read_timeout", "connect_timeout", "timeout"}
_EXCEPTION_TYPES = {
    "ReadTimeout": "read_timeout",
    "ConnectTimeout": "connect_timeout",
    "Timeout": "timeout",
    "SSLError": "ssl_error",
    "ProxyError": "proxy_error",
    "ChunkedEncodingError": "chunked_encoding_error",
    "ContentDecodingError": "content_decoding_error",
    "InvalidURL": "invalid_url",
    "InvalidSchema": "invalid_schema",
    "MissingSchema": "missing_schema",
    "TooManyRedirects": "too_many_redirects",
    "ConnectionError": "connection_error",
    "RequestException": "request_exception",
}
_REQUEST_ID_HEADERS = (
    "x-request-id",
    "request-id",
    "x-correlation-id",
    "cf-ray",
)
_ALLOWED_KEYS = frozenset(
    {
        "phase",
        "attempt_id",
        "pid",
        "tid",
        "elapsed_ms",
        "model",
        "scheme",
        "host",
        "path",
        "max_tokens",
        "temperature",
        "stream",
        "json_mode",
        "timeout_seconds",
        "proxy",
        "proxy_host",
        "message_count",
        "message_chars",
        "http_status",
        "content_type",
        "request_id",
        "finish_reason",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "reasoning_tokens",
        "content_chars",
        "reasoning_chars",
        "exception_type",
    }
)
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]{0,15}$")
_HOST_RE = re.compile(
    r"^(?:[A-Za-z0-9._-]{1,253}|\[[A-Fa-f0-9:]{2,128}\])(?::[0-9]{1,5})?$"
)
_PROXY_HOST_RE = re.compile(r"^(?:[A-Za-z0-9._-]{1,253}|[A-Fa-f0-9:]{2,128})$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9._+/-]{0,255}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CONTENT_TYPE_RE = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")
_FINISH_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_ATTEMPT_RE = re.compile(r"^[0-9a-f]{32}$")
_LEAK_RE = re.compile(
    r"(?i)(authorization\s*:|bearer\s+[a-z0-9._\-]|api[_-]?key\s*=|"
    r"sk-[a-z0-9]|-----BEGIN |password\s*=|secret\s*=|token\s*=)"
)
_MAX_LINE_BYTES = 2000
_MAX_BYTES_CAP = 1024 * 1024
_MAX_BACKUPS_CAP = 5
_LOCK = threading.Lock()
_STATE: dict[str, Any] = {"logger": None, "handler": None}
_attempt = threading.local()


class _OwnerOnlyRotatingFileHandler(RotatingFileHandler):
    """Rotating file handler that keeps the log owner-readable only."""

    def _open(self):
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        fd = os.open(self.baseFilename, flags, 0o600)
        return open(fd, "a", encoding=self.encoding or "utf-8", closefd=True)

    def _restrict(self) -> None:
        if os.path.lexists(self.baseFilename) and not os.path.islink(self.baseFilename):
            os.chmod(self.baseFilename, 0o600)
        directory = os.path.dirname(self.baseFilename)
        if directory and os.path.isdir(directory) and not os.path.islink(directory):
            os.chmod(directory, 0o700)
        for index in range(1, int(self.backupCount) + 1):
            candidate = f"{self.baseFilename}.{index}"
            if os.path.lexists(candidate) and not os.path.islink(candidate):
                os.chmod(candidate, 0o600)

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self._restrict()

    def handleError(self, record: logging.LogRecord) -> None:
        return

    def doRollover(self) -> None:
        super().doRollover()
        self._restrict()


def diagnostics_enabled() -> bool:
    return os.getenv("QD_LLM_CALL_DIAGNOSTICS", "").strip().lower() in _ENABLED


def reset_for_tests() -> None:
    """Close the diagnostic handler. Tests use this to isolate log files."""
    with _LOCK:
        logger = _STATE.get("logger")
        handler = _STATE.get("handler")
        if logger is not None and handler is not None:
            logger.removeHandler(handler)
            handler.close()
        _STATE["logger"] = None
        _STATE["handler"] = None
    _attempt.value = None


def note_send(
    *,
    url: str,
    payload: dict | None,
    timeout: Any,
    stream: bool,
    proxy_url: str | None,
) -> None:
    try:
        if stream or not diagnostics_enabled():
            return
        record = _send_record(url=url, payload=payload, timeout=timeout, proxy_url=proxy_url)
        if record is None:
            return
        _attempt.value = (record["attempt_id"], time.monotonic())
        _emit(record)
    except Exception:
        return


def note_return(response: Any) -> None:
    try:
        if not diagnostics_enabled():
            return
        attempt_id, started = _current_attempt()
        if attempt_id is None:
            return
        record = _base_record("return", attempt_id, started)
        status = _bounded_int(getattr(response, "status_code", None), maximum=999)
        if status is not None:
            record["http_status"] = status
        headers = getattr(response, "headers", None)
        content_type = _content_type(headers)
        if content_type:
            record["content_type"] = content_type
        request_id = _request_id(headers)
        if request_id:
            record["request_id"] = request_id
        _emit(record)
    except Exception:
        return


def note_exception(exc: BaseException, *, timeout: Any) -> None:
    try:
        if not diagnostics_enabled():
            return
        attempt_id, started = _current_attempt()
        if attempt_id is None:
            return
        exception_type = _exception_type(exc)
        record = _base_record("exception", attempt_id, started)
        record["exception_type"] = exception_type
        if exception_type in _TIMEOUT_TYPES:
            timeout_seconds = _bounded_int(_number(timeout), maximum=86400)
            if timeout_seconds is not None:
                record["timeout_seconds"] = timeout_seconds
        _emit(record)
    except Exception:
        return


def note_parsed(result: Any, response: Any) -> None:
    try:
        if not diagnostics_enabled():
            return
        attempt_id, started = _current_attempt()
        if attempt_id is None:
            return
        record = _base_record("parsed", attempt_id, started)
        status = _bounded_int(getattr(response, "status_code", None), maximum=999)
        if status is not None:
            record["http_status"] = status
        headers = getattr(response, "headers", None)
        content_type = _content_type(headers)
        if content_type:
            record["content_type"] = content_type
        request_id = _request_id(headers)
        if request_id:
            record["request_id"] = request_id
        if isinstance(result, dict):
            record.update(_parsed_fields(result))
        _emit(record)
    except Exception:
        return


def _current_attempt() -> tuple[str | None, float]:
    value = getattr(_attempt, "value", None)
    if not isinstance(value, tuple) or len(value) != 2:
        return None, 0.0
    attempt_id, started = value
    if not isinstance(attempt_id, str) or not isinstance(started, float):
        return None, 0.0
    return attempt_id, started


def _send_record(*, url: str, payload: dict | None, timeout: Any, proxy_url: str | None) -> dict[str, Any] | None:
    attempt_id = uuid.uuid4().hex
    if not _ATTEMPT_RE.fullmatch(attempt_id):
        return None
    record = _base_record("send", attempt_id, time.monotonic())
    record["elapsed_ms"] = 0
    record["stream"] = False
    body = payload if isinstance(payload, dict) else {}
    model = _match(body.get("model"), _MODEL_RE)
    if model and not _LEAK_RE.search(model):
        record["model"] = model
    max_tokens = _bounded_int(body.get("max_tokens"), maximum=1_000_000_000)
    if max_tokens is not None:
        record["max_tokens"] = max_tokens
    temperature = _temperature(body.get("temperature"))
    if temperature is not None:
        record["temperature"] = temperature
    record["json_mode"] = _json_mode(body.get("response_format"))
    timeout_seconds = _bounded_int(_number(timeout), maximum=86400)
    if timeout_seconds is not None:
        record["timeout_seconds"] = timeout_seconds
    message_count, message_chars = _message_stats(body.get("messages"))
    record["message_count"] = message_count
    record["message_chars"] = message_chars
    record.update(_endpoint(url))
    proxy, proxy_host = _proxy(proxy_url)
    record["proxy"] = proxy
    if proxy_host:
        record["proxy_host"] = proxy_host
    return record


def _base_record(phase: str, attempt_id: str, started: float) -> dict[str, Any]:
    if phase not in _PHASES or not _ATTEMPT_RE.fullmatch(attempt_id):
        raise ValueError("invalid diagnostic identity")
    elapsed = 0 if phase == "send" else int(max(0.0, time.monotonic() - started) * 1000)
    if elapsed > 172_800_000:
        elapsed = 172_800_000
    return {
        "phase": phase,
        "attempt_id": attempt_id,
        "pid": os.getpid(),
        "tid": int(threading.get_ident()),
        "elapsed_ms": elapsed,
    }


def _parsed_fields(result: dict) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    choices = result.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    finish_reason = _match(str(choice.get("finish_reason") or "").strip().lower(), _FINISH_RE)
    if finish_reason:
        fields["finish_reason"] = finish_reason
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    fields["content_chars"] = _text_length(message.get("content"))
    fields["reasoning_chars"] = _reasoning_length(message)
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    for source, target in (
        ("prompt_tokens", "prompt_tokens"),
        ("completion_tokens", "completion_tokens"),
        ("total_tokens", "total_tokens"),
    ):
        value = _bounded_int(usage.get(source), maximum=1_000_000_000)
        if value is not None:
            fields[target] = value
    reasoning_tokens = _reasoning_tokens(usage)
    if reasoning_tokens is not None:
        fields["reasoning_tokens"] = reasoning_tokens
    return fields


def _reasoning_tokens(usage: dict) -> int | None:
    direct = _bounded_int(usage.get("reasoning_tokens"), maximum=1_000_000_000)
    if direct is not None:
        return direct
    for key in ("completion_tokens_details", "output_tokens_details"):
        details = usage.get(key)
        if isinstance(details, dict):
            value = _bounded_int(details.get("reasoning_tokens"), maximum=1_000_000_000)
            if value is not None:
                return value
    return None


def _reasoning_length(message: dict) -> int:
    for key in ("reasoning_content", "reasoning"):
        value = message.get(key)
        if isinstance(value, str):
            return len(value)
    return 0


def _text_length(content: Any) -> int:
    if isinstance(content, str):
        return min(len(content), 1_000_000_000)
    if not isinstance(content, list):
        return 0
    total = 0
    for part in content:
        if isinstance(part, str):
            total += len(part)
        elif isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                total += len(text)
    return min(total, 1_000_000_000)


def _message_stats(messages: Any) -> tuple[int, int]:
    if not isinstance(messages, list):
        return 0, 0
    count = 0
    chars = 0
    for item in messages:
        if not isinstance(item, dict):
            continue
        count += 1
        chars += _text_length(item.get("content"))
        if count >= 100_000:
            break
    return count, min(chars, 1_000_000_000)


def _json_mode(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") == "json_object"


def _endpoint(url: Any) -> dict[str, Any]:
    if not isinstance(url, str) or not url or any(char.isspace() for char in url):
        return {}
    try:
        parts = urlsplit(url)
    except Exception:
        return {}
    fields: dict[str, Any] = {}
    scheme = parts.scheme.lower()
    if _SCHEME_RE.fullmatch(scheme):
        fields["scheme"] = scheme
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port is not None:
        if not (1 <= parts.port <= 65535):
            return fields
        host = f"{host}:{parts.port}"
    if host and _HOST_RE.fullmatch(host) and "@" not in host:
        fields["host"] = host
    path = parts.path or ""
    if path and _PATH_RE.fullmatch(path) and ".." not in path and "//" not in path and "%" not in path:
        fields["path"] = path
    return fields


def _proxy(proxy_url: Any) -> tuple[bool, str | None]:
    if not isinstance(proxy_url, str) or not proxy_url.strip():
        return False, None
    try:
        host = urlsplit(proxy_url).hostname or ""
    except Exception:
        return True, None
    if _PROXY_HOST_RE.fullmatch(host) and "@" not in host and _LEAK_RE.search(host) is None:
        return True, host
    return True, None


def _header(headers: Any, name: str) -> str | None:
    if headers is None or not hasattr(headers, "get"):
        return None
    try:
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
        if value is None:
            value = headers.get(name.title())
    except Exception:
        return None
    if isinstance(value, str):
        return value.strip()
    return None


def _content_type(headers: Any) -> str | None:
    raw = _header(headers, "Content-Type")
    if not raw:
        return None
    media = raw.split(";", 1)[0].strip().lower()
    if _CONTENT_TYPE_RE.fullmatch(media):
        return media
    return None


def _request_id(headers: Any) -> str | None:
    for name in _REQUEST_ID_HEADERS:
        raw = _header(headers, name)
        if not raw or raw.startswith("eyJ") or raw.lower().startswith("bearer "):
            continue
        if _REQUEST_ID_RE.fullmatch(raw) and _LEAK_RE.search(raw) is None:
            return raw
    return None


def _exception_type(exc: BaseException) -> str:
    for cls in type(exc).__mro__:
        mapped = _EXCEPTION_TYPES.get(cls.__name__)
        if mapped:
            return mapped
    return "unlisted"


def _temperature(value: Any) -> int | float | None:
    number = _number(value)
    if number is None or isinstance(number, bool):
        return None
    if isinstance(number, float) and (number != number or number in (float("inf"), float("-inf"))):
        return None
    if not -2 <= float(number) <= 2:
        return None
    if float(number).is_integer():
        return int(number)
    return round(float(number), 6)


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    return None


def _bounded_int(value: Any, *, maximum: int) -> int | None:
    number = _number(value)
    if number is None:
        return None
    if isinstance(number, float):
        if not number.is_integer():
            return None
        number = int(number)
    if number < 0 or number > maximum:
        return None
    return int(number)


def _match(value: Any, pattern: re.Pattern[str]) -> str | None:
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return None
    if pattern.fullmatch(value):
        return value
    return None


def _emit(record: dict[str, Any]) -> None:
    if set(record) - _ALLOWED_KEYS:
        return
    cleaned: dict[str, Any] = {}
    string_keys = {
        "attempt_id",
        "model",
        "scheme",
        "host",
        "path",
        "content_type",
        "request_id",
        "finish_reason",
        "proxy_host",
    }
    for key, value in record.items():
        if key not in _ALLOWED_KEYS:
            return
        if key == "phase":
            if value not in _PHASES:
                return
        elif key == "exception_type":
            if value not in set(_EXCEPTION_TYPES.values()) | {"unlisted"}:
                return
        elif key in {"stream", "json_mode", "proxy"}:
            if not isinstance(value, bool):
                return
        elif key == "temperature":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return
        elif key in string_keys:
            if not isinstance(value, str) or _LEAK_RE.search(value) or "@" in value or "?" in value:
                return
        elif not isinstance(value, int) or isinstance(value, bool):
            return
        cleaned[key] = value
    line = json.dumps(cleaned, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    if len(line.encode("ascii")) > _MAX_LINE_BYTES or "\n" in line or "\r" in line:
        return
    if _LEAK_RE.search(line) or "@" in line or "?" in line:
        return
    logger = _logger()
    if logger is None:
        return
    logger.info(line)


def _logger() -> logging.Logger | None:
    with _LOCK:
        existing = _STATE.get("logger")
        if existing is not None:
            return existing
        directory = os.getenv("QD_LLM_CALL_DIAG_DIR", "").strip() or os.path.join("logs", "llm-call-diagnostics")
        if os.path.islink(directory):
            return None
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        if not os.path.isdir(directory) or os.path.islink(directory):
            return None
        path = os.path.join(directory, "calls.log")
        if os.path.lexists(path) and os.path.islink(path):
            return None
        handler = _OwnerOnlyRotatingFileHandler(
            path,
            maxBytes=_log_max_bytes(),
            backupCount=_log_backups(),
            encoding="utf-8",
            delay=True,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.setLevel(logging.INFO)
        logger = logging.getLogger(_LOGGER_NAME)
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        _STATE["logger"] = logger
        _STATE["handler"] = handler
        return logger


def _log_max_bytes() -> int:
    return _env_int("QD_LLM_CALL_DIAG_MAX_BYTES", 256 * 1024, minimum=4096, maximum=_MAX_BYTES_CAP)


def _log_backups() -> int:
    return _env_int("QD_LLM_CALL_DIAG_BACKUPS", 3, minimum=1, maximum=_MAX_BACKUPS_CAP)


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return min(maximum, max(minimum, value))
