"""Offline checks for one-shot code-edit length recovery."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
STRATEGY_PATH = BACKEND_ROOT / "app" / "routes" / "strategy.py"
HELPER_PATH = BACKEND_ROOT / "app" / "services" / "llm_length_recovery.py"
EDITS_PATH = BACKEND_ROOT / "app" / "services" / "ai_code_edits.py"
SCRIPT_PATH = BACKEND_ROOT / "scripts" / "llm_length_recovery_patch.py"

TRUNCATED = "TRUNCATED_BODY_SENTINEL_do_not_keep"
SECRET = "sk-live-SECRETKEY12345678"
STRATEGY = "STRATEGY_CODE_SENTINEL_def_on_bar"
CHAT = "CHAT_BODY_SENTINEL_do_not_log"
PROMPT = "USER_PROMPT_SENTINEL_keep_goal"

LOCK_SCRIPT = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX)
print("locked", flush=True)
time.sleep(30)
"""


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


recovery = _load(HELPER_PATH, "llm_length_recovery")
edits = _load(EDITS_PATH, "ai_code_edits")
patcher = _load(SCRIPT_PATH, "llm_length_recovery_patch")


class LengthStop(Exception):
    def __init__(self, finish_reason: str = "length", error_type: str = "max_tokens_exceeded") -> None:
        super().__init__(f"{TRUNCATED} {SECRET} {STRATEGY} {CHAT}")
        self.finish_reason = finish_reason
        self.error_type = error_type
        self.body = TRUNCATED

    def __repr__(self) -> str:
        return self.__str__()


class ProviderTimeout(Exception):
    error_type = "timeout"
    finish_reason = ""

    def __str__(self) -> str:
        return f"timeout {SECRET}"


class ProviderHTTPError(Exception):
    error_type = "invalid_request"
    finish_reason = ""
    status_code = 403

    def __str__(self) -> str:
        return f"HTTP {SECRET}"


def _messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "edit contract"},
        {"role": "user", "content": PROMPT},
    ]


def _record_call(responses):
    calls = []

    def call(**kwargs):
        calls.append(kwargs)
        item = responses[len(calls) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    return call, calls


def test_success_returns_the_same_object_and_calls_once():
    payload = json.dumps({"operations": [{"old_text": "alpha = 1\n", "new_text": "alpha = 2\n"}]})
    messages = _messages()
    call, calls = _record_call([payload])
    content, status = recovery.call_code_edit_with_length_recovery(
        call,
        messages=messages,
        model="deepseek-v4.1-flash",
        temperature=0.2,
        use_json_mode=True,
        lang="zh-CN",
    )
    assert content is payload
    assert status == "none"
    assert len(calls) == 1
    assert calls[0]["messages"] is messages
    assert "lang" not in calls[0]
    assert "timeout_seconds" not in calls[0]
    assert calls[0]["model"] == "deepseek-v4.1-flash"
    assert calls[0]["temperature"] == 0.2
    assert calls[0]["use_json_mode"] is True


def test_length_then_valid_patch_is_applied_once():
    source = "alpha = 1\n"
    recovered = json.dumps({"operations": [{"old_text": "alpha = 1\n", "new_text": "alpha = 2\n"}]})
    messages = _messages()
    call, calls = _record_call([LengthStop(), recovered])
    content, status = recovery.call_code_edit_with_length_recovery(
        call,
        messages=messages,
        model="deepseek-v4.1-flash",
        temperature=0.2,
        use_json_mode=True,
        timeout_seconds=120,
    )
    assert status == "complete_patch"
    assert content is recovered
    candidate, plan = recovery.apply_recovered_code_edit(
        source,
        content,
        status,
        edits.apply_model_code_edits,
    )
    assert candidate == "alpha = 2\n"
    assert plan["operation_count"] == 1
    assert len(calls) == 2
    assert calls[0]["timeout_seconds"] == calls[1]["timeout_seconds"] == 120
    assert calls[0]["model"] == calls[1]["model"]
    assert calls[0]["temperature"] == calls[1]["temperature"]
    assert calls[0]["use_json_mode"] is calls[1]["use_json_mode"] is True
    assert calls[0]["messages"] is messages
    assert messages == _messages()
    assert calls[1]["messages"][:-1] == messages
    instruction = calls[1]["messages"][-1]["content"]
    assert "same requested change" in instruction
    for secret in (TRUNCATED, SECRET, STRATEGY, CHAT):
        assert secret not in instruction


def test_second_length_is_a_safe_error_without_partial_code():
    messages = _messages()
    call, calls = _record_call([LengthStop(), LengthStop(finish_reason="Length")])
    with pytest.raises(recovery.CodeEditLengthError) as caught:
        recovery.call_code_edit_with_length_recovery(call, messages=messages, model="m", temperature=0.2, use_json_mode=True)
    assert caught.value.kind == "exhausted"
    assert "没有应用任何代码" in str(caught.value)
    assert "缩小" in str(caught.value)
    for secret in (TRUNCATED, SECRET, STRATEGY, CHAT):
        assert secret not in str(caught.value)
    assert len(calls) == 2


def test_explicit_incomplete_patch_is_not_applied():
    partial = json.dumps({
        "coverage": "incomplete",
        "operations": [{"old_text": "alpha = 1\n", "new_text": f"alpha = {STRATEGY}\n"}],
    })
    call, calls = _record_call([LengthStop(error_type="max_tokens_exceeded", finish_reason=""), partial])
    with pytest.raises(recovery.CodeEditLengthError) as caught:
        recovery.call_code_edit_with_length_recovery(
            call,
            messages=_messages(),
            model="m",
            temperature=0.2,
            use_json_mode=True,
            lang="en",
        )
    assert caught.value.kind == "incomplete"
    assert "no code was applied" in str(caught.value).lower()
    assert STRATEGY not in str(caught.value)
    assert len(calls) == 2


def test_recovered_invalid_patch_is_not_applied_and_does_not_fall_through():
    truncated_json = '{"operations":[{"old_text":"alpha'
    call, _calls = _record_call([LengthStop(), truncated_json])
    content, status = recovery.call_code_edit_with_length_recovery(
        call,
        messages=_messages(),
        model="m",
        temperature=0.2,
        use_json_mode=True,
    )
    with pytest.raises(recovery.CodeEditLengthError) as caught:
        recovery.apply_recovered_code_edit("alpha = 1\n", content, status, edits.apply_model_code_edits)
    assert caught.value.kind == "invalid_patch"
    assert truncated_json not in str(caught.value)
    assert "没有应用任何代码" in str(caught.value)


def test_non_length_failures_do_not_retry_and_invalid_json_stays_a_code_edit_error():
    messages = _messages()
    for failure in (ProviderTimeout(), ProviderHTTPError(), ValueError(f"length {SECRET}")):
        call, calls = _record_call([failure])
        with pytest.raises(type(failure)):
            recovery.call_code_edit_with_length_recovery(
                call,
                messages=messages,
                model="m",
                temperature=0.2,
                use_json_mode=True,
            )
        assert len(calls) == 1
    call, calls = _record_call(['{"not":"a patch"}'])
    content, status = recovery.call_code_edit_with_length_recovery(
        call,
        messages=messages,
        model="m",
        temperature=0.2,
        use_json_mode=True,
    )
    assert status == "none"
    assert content == '{"not":"a patch"}'
    with pytest.raises(edits.CodeEditError):
        recovery.apply_recovered_code_edit("alpha = 1\n", content, status, edits.apply_model_code_edits)
    assert len(calls) == 1


def test_time_budget_reports_existing_limits_without_changing_them():
    observed = recovery.length_recovery_time_budget(62)
    assert observed["timeouts_unchanged"] is True
    assert observed["provider_timeout_seconds"] == 120
    assert observed["gateway_timeout_seconds"] == 120
    assert observed["client_timeout_seconds"] == 180
    assert observed["fits_gateway_worst_case"] is False
    assert observed["fits_client_worst_case"] is False
    assert observed["fits_gateway_if_retry_matches_first"] is False
    assert observed["fits_client_if_retry_matches_first"] is True
    quick = recovery.length_recovery_time_budget(3)
    assert quick["fits_gateway_if_retry_matches_first"] is True
    assert quick["fits_client_if_retry_matches_first"] is True


def test_public_files_do_not_embed_runtime_paths_or_attack_tools():
    helper = HELPER_PATH.read_text(encoding="utf-8")
    script = SCRIPT_PATH.read_text(encoding="utf-8")
    for text in (helper, script):
        assert "/root/" not in text
        assert "quantdinger/overrides" not in text
        assert "SIGHUP" not in text
        assert "os.kill" not in text
        assert "settrace" not in text
        assert "mitm" not in text
    assert "import logging" not in helper
    assert "RotatingFileHandler" not in helper
    assert patcher.STRATEGY_SHA256 in script
    assert patcher.LLM_OVERRIDE_SHA256 in script
    assert patcher.WORKER_UID == 10001


def _baseline_text() -> str:
    current = STRATEGY_PATH.read_text(encoding="utf-8")
    if hashlib.sha256(current.encode("utf-8")).hexdigest() == patcher.STRATEGY_SHA256:
        return current
    restored = patcher.restore_baseline(current)
    assert hashlib.sha256(restored.encode("utf-8")).hexdigest() == patcher.STRATEGY_SHA256
    assert len(restored.encode("utf-8")) == patcher.STRATEGY_SIZE
    return restored


def test_repo_strategy_is_exactly_the_supported_patch():
    baseline = _baseline_text()
    patched = patcher.build_patched(baseline)
    assert patched == STRATEGY_PATH.read_text(encoding="utf-8")
    assert patcher._discussion(baseline) == patcher._discussion(patched)
    assert patcher._function_dump(baseline, "generate_strategy") == patcher._function_dump(patched, "generate_strategy")
    assert "stream_llm_api" not in patched
    assert patched.count("call_code_edit_with_length_recovery(") == 1
    assert "timeout_seconds" not in patcher.NEW_CALL


def test_strategy_apply_and_rollback_preserve_inode(tmp_path, capsys):
    baseline = _baseline_text().encode("utf-8")
    target = tmp_path / "strategy.py"
    target.write_bytes(baseline)
    os.chmod(target, 0o640)
    link = tmp_path / "strategy.py.link"
    os.link(target, link)
    inode = target.stat().st_ino
    mtime = target.stat().st_mtime_ns
    helper = tmp_path / "llm_length_recovery.py"
    state = tmp_path / "state"
    uid = os.geteuid()
    gid = os.getegid()
    common = [
        "--strategy-target", str(target),
        "--helper-target", str(helper),
        "--state-dir", str(state),
        "--worker-uid", str(uid),
        "--worker-gid", str(gid),
    ]
    assert patcher.main(["dry-run", *common]) == 0
    assert "would apply" in capsys.readouterr().out
    assert target.read_bytes() == baseline
    assert target.stat().st_mtime_ns == mtime
    assert not helper.exists()
    assert not state.exists()
    assert patcher.main(["apply", *common]) == 0
    assert "inode preserved" in capsys.readouterr().out
    assert target.stat().st_ino == inode == link.stat().st_ino
    assert link.read_bytes() == target.read_bytes() == STRATEGY_PATH.read_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert stat.S_IMODE(helper.stat().st_mode) == 0o600
    assert helper.stat().st_uid == uid
    assert helper.read_bytes() == HELPER_PATH.read_bytes()
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / patcher.BACKUP_NAME).stat().st_mode) == 0o600
    assert patcher.main(["apply", *common]) == 2
    assert target.stat().st_ino == inode
    assert patcher.main(["rollback", *common]) == 0
    assert target.read_bytes() == link.read_bytes() == baseline
    assert target.stat().st_ino == inode
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert not helper.exists()


def test_wrong_worker_uid_does_not_patch_strategy(tmp_path, capsys):
    baseline = _baseline_text().encode("utf-8")
    target = tmp_path / "strategy.py"
    target.write_bytes(baseline)
    helper = tmp_path / "llm_length_recovery.py"
    code = patcher.main([
        "apply",
        "--strategy-target", str(target),
        "--helper-target", str(helper),
        "--state-dir", str(tmp_path / "state"),
        "--worker-uid", "10001",
        "--worker-gid", "10001",
    ])
    captured = capsys.readouterr()
    assert code == 2
    assert "10001" in captured.err
    assert target.read_bytes() == baseline
    assert not helper.exists()
    assert not (tmp_path / "state").exists()


def test_refuses_llm_and_diagnostic_targets(tmp_path, capsys):
    baseline = _baseline_text().encode("utf-8")
    llm_target = tmp_path / "llm.py"
    llm_target.write_bytes(baseline)
    helper = tmp_path / "llm_length_recovery.py"
    assert patcher.main([
        "apply",
        "--strategy-target", str(llm_target),
        "--helper-target", str(helper),
        "--state-dir", str(tmp_path / "state-llm"),
        "--worker-uid", str(os.geteuid()),
    ]) == 2
    assert "llm.py" in capsys.readouterr().err
    assert llm_target.read_bytes() == baseline
    assert not helper.exists()
    strategy = tmp_path / "strategy.py"
    strategy.write_bytes(baseline)
    diag = tmp_path / "llm_call_diagnostics.py"
    assert patcher.main([
        "apply",
        "--strategy-target", str(strategy),
        "--helper-target", str(diag),
        "--state-dir", str(tmp_path / "state-diag"),
        "--worker-uid", str(os.geteuid()),
    ]) == 2
    assert strategy.read_bytes() == baseline
    assert not diag.exists()


def test_unknown_strategy_and_lock_conflict_do_not_write(tmp_path, capsys):
    target = tmp_path / "strategy.py"
    original = b"not-the-strategy-route"
    target.write_bytes(original)
    helper = tmp_path / "llm_length_recovery.py"
    assert patcher.main([
        "apply",
        "--strategy-target", str(target),
        "--helper-target", str(helper),
        "--state-dir", str(tmp_path / "state"),
        "--worker-uid", str(os.geteuid()),
    ]) == 2
    assert "unknown-version" in capsys.readouterr().err
    assert target.read_bytes() == original
    assert not helper.exists()
    target.write_bytes(_baseline_text().encode("utf-8"))
    proc = subprocess.Popen(
        [sys.executable, "-c", LOCK_SCRIPT, str(target)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "locked"
        code = patcher.main([
            "apply",
            "--strategy-target", str(target),
            "--helper-target", str(helper),
            "--state-dir", str(tmp_path / "locked-state"),
            "--worker-uid", str(os.geteuid()),
        ])
        captured = capsys.readouterr()
    finally:
        proc.kill()
        proc.wait(timeout=5)
    assert code == 2
    assert "holds the lock" in captured.err
    assert not helper.exists()
    assert hashlib.sha256(target.read_bytes()).hexdigest() == patcher.STRATEGY_SHA256
