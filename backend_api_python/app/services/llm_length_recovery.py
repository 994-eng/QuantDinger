"""One-shot recovery for a code-edit response that hit the output token limit.

This module is used only by the strategy workspace modify path. It does not
change provider timeouts, model, temperature, or max token settings, and it
never writes prompt or completion text.

A normal provider result is returned unchanged. A length failure may be
retried once, asking for a new complete JSON patch of the same change. The
retry is not a continuation of the discarded fragment. A second length
failure, an explicit incomplete patch, or a recovered patch that fails the
existing edit validator becomes a fixed error and applies no code.
"""

from __future__ import annotations

import json
from typing import Any, Callable

PROVIDER_TIMEOUT_SECONDS = 120
GATEWAY_TIMEOUT_SECONDS = 120
CLIENT_TIMEOUT_SECONDS = 180

_INCOMPLETE_COVERAGE = {"incomplete", "partial", "narrowed"}
_KINDS = {"exhausted", "incomplete", "invalid_patch"}
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
}


class CodeEditLengthError(ValueError):
    """Fixed, client-safe terminal error. It carries no provider payload."""

    def __init__(self, kind: str, lang: str = "zh-CN") -> None:
        self.kind = kind if kind in _KINDS else "exhausted"
        self.lang = lang if isinstance(lang, str) else "zh-CN"
        super().__init__(_message(self.kind, self.lang))


def is_output_length_error(exc: BaseException) -> bool:
    """True only for a token-limit stop, not for timeouts or HTTP failures."""
    error_type = getattr(exc, "error_type", "")
    finish_reason = getattr(exc, "finish_reason", "")
    if not isinstance(error_type, str):
        error_type = ""
    if not isinstance(finish_reason, str):
        finish_reason = ""
    return (
        error_type.strip().lower() == "max_tokens_exceeded"
        or finish_reason.strip().lower() == "length"
    )


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
        try:
            recovered = call(**retry_kwargs)
        except Exception as retry_exc:
            if is_output_length_error(retry_exc):
                raise CodeEditLengthError("exhausted", lang) from None
            raise
        if _coverage(recovered) in _INCOMPLETE_COVERAGE:
            raise CodeEditLengthError("incomplete", lang) from None
        return recovered, "complete_patch"


def apply_recovered_code_edit(
    source: str,
    content: Any,
    length_recovery: str,
    apply: Callable[[str, Any], tuple[str, dict[str, Any]]],
    lang: str = "zh-CN",
) -> tuple[str, dict[str, Any]]:
    """Validate a patch. A failed recovery patch does not fall through."""
    try:
        return apply(source, content)
    except Exception as exc:
        if length_recovery == "complete_patch" and type(exc).__name__ == "CodeEditError":
            raise CodeEditLengthError("invalid_patch", lang) from None
        raise


def length_recovery_time_budget(first_elapsed_seconds: float) -> dict[str, Any]:
    """Describe existing limits. This does not change any timeout."""
    elapsed = first_elapsed_seconds if isinstance(first_elapsed_seconds, (int, float)) else 0
    if isinstance(elapsed, float) and elapsed != elapsed:
        elapsed = 0
    elapsed = max(0.0, float(elapsed))
    worst_case = elapsed + PROVIDER_TIMEOUT_SECONDS
    return {
        "provider_timeout_seconds": PROVIDER_TIMEOUT_SECONDS,
        "gateway_timeout_seconds": GATEWAY_TIMEOUT_SECONDS,
        "client_timeout_seconds": CLIENT_TIMEOUT_SECONDS,
        "timeouts_unchanged": True,
        "first_elapsed_seconds": elapsed,
        "worst_case_total_seconds": worst_case,
        "fits_gateway_worst_case": worst_case <= GATEWAY_TIMEOUT_SECONDS,
        "fits_client_worst_case": worst_case <= CLIENT_TIMEOUT_SECONDS,
        "fits_gateway_if_retry_matches_first": elapsed * 2 <= GATEWAY_TIMEOUT_SECONDS,
        "fits_client_if_retry_matches_first": elapsed * 2 <= CLIENT_TIMEOUT_SECONDS,
    }


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
