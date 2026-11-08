"""One-shot recovery for a code-edit response that hit the output token limit.

This module is used only by the strategy workspace modify path. It does not
change provider timeouts, model, temperature, or max token settings, and it
never writes prompt or completion text.

The configured Gunicorn timeout is a worker-silence interval. With
``worker_class = gthread`` and ``preload_app = False``, that number is not a
proven hard limit for one request. The proxy read timeout is not known here.

A normal provider result is returned unchanged. A length failure may be
retried once, asking for a new complete JSON patch of the same change. The
retry is not a continuation of the discarded fragment. A second length
failure, an explicit incomplete patch, or a recovered patch that fails the
existing edit validator becomes a fixed error and applies no code.
"""

from __future__ import annotations

import json
from typing import Any, Callable

PROVIDER_CALL_TIMEOUT_SECONDS = 120
GUNICORN_SILENCE_TIMEOUT_SECONDS = 120
CLIENT_WAIT_SECONDS = 180

_INCOMPLETE_COVERAGE = {"incomplete", "partial", "narrowed"}
_LENGTH_ERROR_TYPE = "max_tokens_exceeded"
_LENGTH_FINISH_REASON = "length"
_KINDS = {"exhausted", "incomplete", "invalid_patch", "retry_failed"}
_RECOVERY_INSTRUCTION = (
    "The previous answer was discarded because it reached the output limit "
    "before it was a complete JSON patch. Return one new complete, "
    "self-contained JSON patch for the same requested change, using the "
    "source edit contract. Keep that patch concise. Do not continue a "
    "fragment and do not return the full source file. If the entire change "
    'cannot fit in one valid patch, return {"coverage":"incomplete",'
    '"operations":[]} and do not include a partial edit.'
)
_MESSAGES = {
    "exhausted": {
        "zh": "模型连续两次达到输出长度上限，没有应用任何代码。请缩小这次修改的范围后再试。",
        "en": "The model hit the output limit twice, so no code was applied. Narrow this change and try again.",
    },
    "incomplete": {
        "zh": "重新生成的补丁没有覆盖全部修改目标，没有应用任何代码。请缩小这次修改的范围后再试。",
        "en": "The regenerated patch did not cover the whole change, so no code was applied. Narrow this change and try again.",
    },
    "invalid_patch": {
        "zh": "重新生成的补丁无法通过完整性校验，没有应用任何代码。请缩小这次修改的范围后再试。",
        "en": "The regenerated patch failed validation, so no code was applied. Narrow this change and try again.",
    },
    "retry_failed": {
        "zh": "重新请求完整补丁时失败，没有应用任何代码。请缩小这次修改的范围后再试。",
        "en": "The request for a complete patch failed, so no code was applied. Narrow this change and try again.",
    },
}


class CodeEditLengthError(ValueError):
    """Fixed, client-safe terminal error. It carries no provider payload."""

    def __init__(self, kind: str, lang: str = "zh-CN") -> None:
        self.kind = kind if kind in _KINDS else "exhausted"
        self.lang = lang if isinstance(lang, str) else "zh-CN"
        super().__init__(_message(self.kind, self.lang))


def is_output_length_error(exc: BaseException) -> bool:
    """True only for structured provider fields, never for exception text.

    The running provider error exposes ``error_type`` and ``finish_reason``.
    A length stop sets ``max_tokens_exceeded`` and ``finish_reason`` ``length``.
    Message text is ignored, including text that merely contains those words.
    """
    error_type = _structured_token(getattr(exc, "error_type", None))
    finish_reason = _structured_token(getattr(exc, "finish_reason", None))
    if error_type == _LENGTH_ERROR_TYPE and finish_reason in {"", _LENGTH_FINISH_REASON}:
        return True
    return finish_reason == _LENGTH_FINISH_REASON and error_type in {"", _LENGTH_ERROR_TYPE}


def call_code_edit_with_length_recovery(call: Callable[..., Any], /, **kwargs: Any) -> tuple[Any, str]:
    """Call the provider once, or twice when the first result stopped on length.

    ``lang`` is consumed here and is not forwarded. Every other keyword,
    including timeout, model, and temperature, is passed through unchanged.
    The returned content object is the provider object, not a rewritten copy.
    """
    lang = kwargs.get("lang", "zh-CN")
    call_kwargs = {key: value for key, value in kwargs.items() if key != "lang"}
    try:
        return call(**call_kwargs), "none"
    except Exception as exc:
        if not is_output_length_error(exc):
            raise
    retry_kwargs = dict(call_kwargs)
    retry_kwargs["messages"] = _recovery_messages(call_kwargs.get("messages"))
    recovered = None
    failure = ""
    try:
        recovered = call(**retry_kwargs)
    except Exception as retry_exc:
        failure = "exhausted" if is_output_length_error(retry_exc) else "retry_failed"
    if failure:
        raise CodeEditLengthError(failure, lang)
    if _coverage(recovered) in _INCOMPLETE_COVERAGE:
        raise CodeEditLengthError("incomplete", lang)
    return recovered, "complete_patch"


def apply_recovered_code_edit(
    source: str,
    content: Any,
    length_recovery: str,
    apply: Callable[[str, Any], tuple[str, dict[str, Any]]],
    lang: str = "zh-CN",
) -> tuple[str, dict[str, Any]]:
    """Validate a patch. A failed or self-reported partial recovery does not fall through."""
    if length_recovery == "complete_patch" and _coverage(content) in _INCOMPLETE_COVERAGE:
        raise CodeEditLengthError("incomplete", lang)
    rejected = False
    try:
        return apply(source, content)
    except Exception as exc:
        rejected = length_recovery == "complete_patch" and type(exc).__name__ == "CodeEditError"
        if not rejected:
            raise
    raise CodeEditLengthError("invalid_patch", lang)


def length_recovery_time_budget(first_elapsed_seconds: float) -> dict[str, Any]:
    """Report configured timers without inferring that a total duration kills the worker.

    Each provider call still uses the existing 120s request timeout. Gunicorn's
    120s timeout, with the gthread worker and preload disabled, measures worker
    silence. It is not treated here as a hard abort at 124s. The frontend wait
    and the proxy read timeout are separate, and the proxy value is not known.
    """
    elapsed = first_elapsed_seconds if isinstance(first_elapsed_seconds, (int, float)) else 0
    if isinstance(elapsed, float) and elapsed != elapsed:
        elapsed = 0
    elapsed = max(0.0, float(elapsed))
    return {
        "timeouts_unchanged": True,
        "provider_call_timeout_seconds": PROVIDER_CALL_TIMEOUT_SECONDS,
        "gunicorn_silence_timeout_seconds": GUNICORN_SILENCE_TIMEOUT_SECONDS,
        "gunicorn_worker_class": "gthread",
        "gunicorn_preload_app": False,
        "gunicorn_timeout_is_per_request_hard_kill": False,
        "client_wait_seconds": CLIENT_WAIT_SECONDS,
        "proxy_read_timeout_known": False,
        "first_elapsed_seconds": elapsed,
    }


def _structured_token(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _recovery_messages(messages: Any) -> list[dict[str, str]]:
    if not isinstance(messages, list):
        raise CodeEditLengthError("exhausted")
    copied = list(messages)
    copied.append({"role": "user", "content": _RECOVERY_INSTRUCTION})
    return copied


def _coverage(content: Any) -> str | None:
    payload = content if isinstance(content, dict) else _json_object(content)
    if not isinstance(payload, dict):
        return None
    value = payload.get("coverage")
    if not isinstance(value, str):
        return None
    return value.strip().lower()


def _json_object(content: Any) -> dict[str, Any] | None:
    if not isinstance(content, str):
        return None
    raw = content.strip()
    if raw.startswith("```json"):
        raw = raw[7:]
    elif raw.startswith("```"):
        raw = raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    try:
        payload = json.loads(raw.strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _message(kind: str, lang: str) -> str:
    texts = _MESSAGES[kind]
    if str(lang or "").strip().lower().startswith("zh"):
        return texts["zh"]
    return texts["en"]
