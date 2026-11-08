"""Offline checks for the temporary non-streaming LLM diagnostic log."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import logging
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = BACKEND_ROOT / "app" / "services" / "llm_call_diagnostics.py"
SCRIPT_PATH = BACKEND_ROOT / "scripts" / "llm_call_diagnostics_patch.py"
PACKAGED_LLM = BACKEND_ROOT / "app" / "services" / "llm.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("llm_call_diagnostics", HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


diag = _load_helper()

CHAT_BODY = "CHAT_BODY_SENTINEL_do_not_log"
REASONING = "REASONING_SENTINEL_do_not_log"
STRATEGY = "STRATEGY_CODE_SENTINEL_def_on_bar"
USER_STRATEGY = "USER-STRATEGY-ID-SENTINEL"
SECRET_KEY = "sk-live-SECRETKEY12345678"
BEARER = "Bearer SUPERSECRETTOKEN"
QUERY_SECRET = "querykey-SENTINEL"
URL_USER = "urluser-SENTINEL"
URL_PASS = "urlpass-SENTINEL"
PROXY_USER = "proxyuser-SENTINEL"
PROXY_PASS = "proxypass-SENTINEL"
GENERATION_ID = "generation-id-SENTINEL"
TIMEOUT_TEXT = "timeout-SENTINEL"
COOKIE = "COOKIE_SENTINEL"
IMAGE = "SECRETBASE64SENTINEL"
SENTINELS = (
    CHAT_BODY,
    REASONING,
    STRATEGY,
    USER_STRATEGY,
    SECRET_KEY,
    "SUPERSECRETTOKEN",
    QUERY_SECRET,
    URL_USER,
    URL_PASS,
    PROXY_USER,
    PROXY_PASS,
    GENERATION_ID,
    TIMEOUT_TEXT,
    COOKIE,
    IMAGE,
    "Authorization",
    "Bearer",
    "api_key",
    "image_url",
    "Set-Cookie",
    "password",
)
ENDPOINT = (
    f"https://{URL_USER}:{URL_PASS}@llm.example.test:8443"
    f"/v1/chat/completions?api_key={QUERY_SECRET}&token=tok-SENTINEL"
)
PROXY = f"http://{PROXY_USER}:{PROXY_PASS}@proxy.sentinel.test:9/secret?x=1"
LOCK_SCRIPT = """
import fcntl
import os
import sys
import time
fd = os.open(sys.argv[1], os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX)
print("locked", flush=True)
time.sleep(30)
"""


class ReadTimeout(Exception):
    def __str__(self) -> str:
        return f"{TIMEOUT_TEXT} {SECRET_KEY} {BEARER}"

    def __repr__(self) -> str:
        return self.__str__()


class AuthorizationError(Exception):
    def __str__(self) -> str:
        return BEARER

    def __repr__(self) -> str:
        return self.__str__()


class ConnectionError(Exception):
    def __str__(self) -> str:
        return f"proxy password={PROXY_PASS}"


class _Response:
    def __init__(self, request_id: str = "req-7f3a1b2c") -> None:
        self.status_code = 200
        self.headers = {
            "Content-Type": "application/json; charset=utf-8",
            "x-request-id": request_id,
            "Authorization": BEARER,
            "Set-Cookie": f"session={COOKIE}",
        }
        self.calls: list[str] = []

    def json(self):
        self.calls.append("json")
        raise AssertionError("response.json was called")

    @property
    def text(self):
        self.calls.append("text")
        raise AssertionError("response.text was read")

    @property
    def content(self):
        self.calls.append("content")
        raise AssertionError("response.content was read")

    def iter_lines(self, *args, **kwargs):
        self.calls.append("iter_lines")
        raise AssertionError("stream was consumed")

    def iter_content(self, *args, **kwargs):
        self.calls.append("iter_content")
        raise AssertionError("stream was consumed")


def _load_patcher():
    spec = importlib.util.spec_from_file_location("llm_call_diagnostics_patch", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def patcher():
    return _load_patcher()


@pytest.fixture(autouse=True)
def _isolate_diagnostics(monkeypatch, tmp_path):
    monkeypatch.setenv("QD_LLM_CALL_DIAG_DIR", str(tmp_path / "diag"))
    monkeypatch.delenv("QD_LLM_CALL_DIAGNOSTICS", raising=False)
    diag.reset_for_tests()
    yield
    diag.reset_for_tests()


def _override_bytes() -> bytes:
    raw = os.environ.get("QD_LLM_OVERRIDE_SOURCE", "").strip()
    if not raw:
        pytest.skip("QD_LLM_OVERRIDE_SOURCE is not set")
    path = Path(raw)
    if not path.is_file():
        pytest.skip("QD_LLM_OVERRIDE_SOURCE is not a file")
    return path.read_bytes()


def _log_text(directory: Path) -> str:
    chunks = []
    if not directory.exists():
        return ""
    for path in sorted(directory.rglob("*")):
        if path.is_file() and not path.is_symlink():
            chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def _records(directory: Path) -> list[dict]:
    text = _log_text(directory)
    if not text.strip():
        return []
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_diagnostics_are_disabled_by_default(tmp_path):
    directory = tmp_path / "diag"
    diag.note_send(
        url=ENDPOINT,
        payload={"model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": CHAT_BODY}]},
        timeout=120,
        stream=False,
        proxy_url=PROXY,
    )
    assert diag.diagnostics_enabled() is False
    assert not directory.exists()


def test_import_does_not_create_a_log(tmp_path):
    assert diag.diagnostics_enabled() is False
    assert not (tmp_path / "diag").exists()


def test_secret_sentinels_never_reach_the_log(monkeypatch, tmp_path):
    monkeypatch.setenv("QD_LLM_CALL_DIAGNOSTICS", "true")
    directory = tmp_path / "diag"
    system = f"{STRATEGY} {USER_STRATEGY}"
    payload = {
        "model": "deepseek-v4.1-flash",
        "temperature": 0.2,
        "max_tokens": 8192,
        "response_format": {"type": "json_object"},
        "Authorization": BEARER,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": CHAT_BODY},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{IMAGE}"}},
                ],
            },
        ],
    }
    result = {
        "id": GENERATION_ID,
        "api_key": QUERY_SECRET,
        "choices": [
            {
                "finish_reason": "length",
                "message": {
                    "role": "assistant",
                    "content": CHAT_BODY,
                    "reasoning_content": REASONING,
                },
            }
        ],
        "usage": {
            "prompt_tokens": 5600,
            "completion_tokens": 8192,
            "total_tokens": 13792,
            "completion_tokens_details": {"reasoning_tokens": 8000},
        },
    }
    response = _Response()
    snapshot = copy.deepcopy(result)
    old_umask = os.umask(0)
    root = logging.getLogger()
    root_handlers = list(root.handlers)
    try:
        diag.note_send(url=ENDPOINT, payload=payload, timeout=120, stream=True, proxy_url=PROXY)
        assert not directory.exists()
        diag.note_send(url=ENDPOINT, payload=payload, timeout=120, stream=False, proxy_url=PROXY)
        diag.note_return(response)
        diag.note_parsed(result, response)
        diag.note_send(
            url=ENDPOINT,
            payload={"model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": CHAT_BODY}]},
            timeout=55,
            stream=False,
            proxy_url="",
        )
        diag.note_exception(ReadTimeout(TIMEOUT_TEXT), timeout=55)
        diag.note_send(
            url=ENDPOINT,
            payload={"model": "deepseek-v4.1-flash", "messages": []},
            timeout=10,
            stream=False,
            proxy_url=None,
        )
        diag.note_exception(AuthorizationError(BEARER), timeout=10)
        diag.note_send(
            url="https://llm.example.test/v1/chat/completions",
            payload={"model": "deepseek-v4.1-flash", "messages": []},
            timeout=10,
            stream=False,
            proxy_url="",
        )
        diag.note_exception(ConnectionError(PROXY_PASS), timeout=10)
    finally:
        os.umask(old_umask)

    assert result == snapshot
    assert response.calls == []
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    log_path = directory / "calls.log"
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    raw = _log_text(directory)
    for sentinel in SENTINELS:
        assert sentinel not in raw
    rows = _records(directory)
    assert rows
    for row in rows:
        assert set(row) <= diag._ALLOWED_KEYS
    sends = [row for row in rows if row["phase"] == "send"]
    assert len(sends) == 4
    first, second, third, fourth = sends
    assert first["model"] == "deepseek-v4.1-flash"
    assert first["scheme"] == "https"
    assert first["host"] == "llm.example.test:8443"
    assert first["path"] == "/v1/chat/completions"
    assert first["max_tokens"] == 8192
    assert first["temperature"] == 0.2
    assert first["stream"] is False
    assert first["json_mode"] is True
    assert first["timeout_seconds"] == 120
    assert first["proxy"] is True
    assert first["proxy_host"] == "proxy.sentinel.test"
    assert first["message_count"] == 2
    assert first["message_chars"] == len(system) + len(CHAT_BODY)
    assert first["elapsed_ms"] == 0
    assert first["pid"] == os.getpid()
    assert first["tid"] == threading.get_ident()
    returned = [row for row in rows if row["phase"] == "return"]
    parsed = [row for row in rows if row["phase"] == "parsed"]
    assert len(returned) == 1
    assert len(parsed) == 1
    assert returned[0]["attempt_id"] == first["attempt_id"] == parsed[0]["attempt_id"]
    assert returned[0]["http_status"] == 200
    assert returned[0]["content_type"] == "application/json"
    assert returned[0]["request_id"] == "req-7f3a1b2c"
    assert parsed[0]["finish_reason"] == "length"
    assert parsed[0]["prompt_tokens"] == 5600
    assert parsed[0]["completion_tokens"] == 8192
    assert parsed[0]["total_tokens"] == 13792
    assert parsed[0]["reasoning_tokens"] == 8000
    assert parsed[0]["content_chars"] == len(CHAT_BODY)
    assert parsed[0]["reasoning_chars"] == len(REASONING)
    assert second["json_mode"] is False
    assert second["proxy"] is False
    assert "proxy_host" not in second
    assert second["message_count"] == 1
    assert second["message_chars"] == len(CHAT_BODY)
    exceptions = [row for row in rows if row["phase"] == "exception"]
    assert [row["exception_type"] for row in exceptions] == [
        "read_timeout",
        "unlisted",
        "connection_error",
    ]
    assert exceptions[0]["attempt_id"] == second["attempt_id"]
    assert exceptions[0]["timeout_seconds"] == 55
    assert "timeout_seconds" not in exceptions[1]
    assert "timeout_seconds" not in exceptions[2]
    assert third["attempt_id"] == exceptions[1]["attempt_id"]
    assert fourth["attempt_id"] == exceptions[2]["attempt_id"]
    assert root.handlers == root_handlers
    assert logging.getLogger(diag._LOGGER_NAME).propagate is False


def test_unsafe_request_id_and_finish_reason_are_dropped(monkeypatch, tmp_path):
    monkeypatch.setenv("QD_LLM_CALL_DIAGNOSTICS", "1")
    diag.note_send(
        url="https://llm.example.test/v1/chat/completions",
        payload={"model": SECRET_KEY, "max_tokens": True, "temperature": 9},
        timeout=120,
        stream=False,
        proxy_url="",
    )
    response = _Response(request_id=SECRET_KEY)
    diag.note_return(response)
    diag.note_parsed(
        {
            "choices": [
                {
                    "finish_reason": f"length {CHAT_BODY}",
                    "message": {"content": "x", "reasoning": REASONING},
                }
            ]
        },
        response,
    )
    raw = _log_text(tmp_path / "diag")
    assert SECRET_KEY not in raw
    assert CHAT_BODY not in raw
    assert REASONING not in raw
    parsed = [row for row in _records(tmp_path / "diag") if row["phase"] == "parsed"]
    assert parsed
    assert "finish_reason" not in parsed[0]
    assert "request_id" not in parsed[0]
    assert parsed[0]["content_chars"] == 1
    assert parsed[0]["reasoning_chars"] == len(REASONING)
    send = [row for row in _records(tmp_path / "diag") if row["phase"] == "send"][0]
    assert "model" not in send
    assert "max_tokens" not in send
    assert "temperature" not in send


def test_log_rotation_stays_owner_only(monkeypatch, tmp_path):
    monkeypatch.setenv("QD_LLM_CALL_DIAGNOSTICS", "on")
    monkeypatch.setenv("QD_LLM_CALL_DIAG_MAX_BYTES", "4096")
    monkeypatch.setenv("QD_LLM_CALL_DIAG_BACKUPS", "2")
    old_umask = os.umask(0)
    try:
        for _ in range(40):
            diag.note_send(
                url="https://llm.example.test/v1/chat/completions",
                payload={"model": "deepseek-v4.1-flash", "max_tokens": 8192, "messages": []},
                timeout=120,
                stream=False,
                proxy_url="",
            )
    finally:
        os.umask(old_umask)
    directory = tmp_path / "diag"
    files = [path for path in directory.iterdir() if path.is_file()]
    assert len(files) >= 2
    for path in files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert not path.is_symlink()


def test_symlink_log_is_not_followed(monkeypatch, tmp_path):
    monkeypatch.setenv("QD_LLM_CALL_DIAGNOSTICS", "yes")
    directory = tmp_path / "diag"
    directory.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("CANARY", encoding="utf-8")
    (directory / "calls.log").symlink_to(outside)
    diag.note_send(
        url=ENDPOINT,
        payload={"messages": [{"role": "user", "content": CHAT_BODY}]},
        timeout=30,
        stream=False,
        proxy_url=PROXY,
    )
    assert outside.read_text(encoding="utf-8") == "CANARY"


def test_public_sources_do_not_embed_deployment_paths_or_attack_tools():
    helper = (BACKEND_ROOT / "app" / "services" / "llm_call_diagnostics.py").read_text(encoding="utf-8")
    script = SCRIPT_PATH.read_text(encoding="utf-8")
    for text in (helper, script):
        assert "/root/" not in text
        assert "quantdinger/overrides" not in text
        assert "SIGHUP" not in text
        assert "os.kill" not in text
        assert "settrace" not in text
        assert "breakpoint(" not in text
        assert "mitm" not in text
        assert "HTTPAdapter" not in text
        assert "sitecustomize" not in text
    assert "import requests" not in helper
    assert "d59e6f10cc87229177f296e9a21465b3f4190430f6d8c3d67314a1bd57551805" in script


def test_packaged_llm_is_rejected(tmp_path, patcher, capsys):
    packaged = PACKAGED_LLM.read_bytes()
    assert hashlib.sha256(packaged).hexdigest() != patcher.EXPECTED_SHA256
    target = tmp_path / "llm.py"
    target.write_bytes(packaged)
    inode = target.stat().st_ino
    state = tmp_path / "state"
    code = patcher.main(["apply", "--target", str(target), "--state-dir", str(state)])
    captured = capsys.readouterr()
    assert code == 2
    assert "unknown-version" in captured.err
    assert target.read_bytes() == packaged
    assert target.stat().st_ino == inode
    assert not state.exists()
    with pytest.raises(patcher.PatchRejected, match="unknown-version"):
        patcher.build_patched(packaged.decode("utf-8"))


def test_inplace_rewrite_preserves_inode_and_hardlink(tmp_path, patcher):
    target = tmp_path / "data.txt"
    target.write_bytes(b"old-bytes")
    os.chmod(target, 0o640)
    link = tmp_path / "link.txt"
    os.link(target, link)
    inode = target.stat().st_ino
    fd = os.open(target, os.O_RDWR)
    try:
        patcher._write_fd(fd, b"new-bytes-longer")
        os.fchmod(fd, 0o640)
    finally:
        os.close(fd)
    assert target.stat().st_ino == inode == link.stat().st_ino
    assert link.read_bytes() == b"new-bytes-longer"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    fd = os.open(target, os.O_RDWR)
    try:
        patcher._write_fd(fd, b"short")
    finally:
        os.close(fd)
    assert target.read_bytes() == link.read_bytes() == b"short"
    assert target.stat().st_ino == inode


def test_state_files_are_owner_only_even_with_open_umask(tmp_path, patcher):
    state = tmp_path / "state"
    old_umask = os.umask(0)
    try:
        patcher._prepare_state(state)
        patcher._write_private_file(state / "backup", b"abc")
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "backup").stat().st_mode) == 0o600


def test_symlink_backup_is_refused(tmp_path, patcher):
    state = tmp_path / "state"
    state.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"CANARY")
    (state / patcher.BACKUP_NAME).symlink_to(outside)
    with pytest.raises(patcher.PatchRejected):
        patcher._read_private_file(state / patcher.BACKUP_NAME)
    assert outside.read_bytes() == b"CANARY"


def test_concurrent_apply_does_not_write(tmp_path, patcher, capsys):
    target = tmp_path / "llm.py"
    original = b"not-the-supported-revision"
    target.write_bytes(original)
    proc = subprocess.Popen(
        [sys.executable, "-c", LOCK_SCRIPT, str(target)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "locked"
        code = patcher.main(["apply", "--target", str(target), "--state-dir", str(tmp_path / "state")])
        captured = capsys.readouterr()
    finally:
        proc.kill()
        proc.wait(timeout=5)
    assert code == 2
    assert "holds the lock" in captured.err
    assert target.read_bytes() == original
    assert not (tmp_path / "state").exists()


def test_supported_override_apply_and_rollback(tmp_path, patcher, capsys):
    original = _override_bytes()
    assert hashlib.sha256(original).hexdigest() == patcher.EXPECTED_SHA256
    assert len(original) == patcher.EXPECTED_SIZE
    source = original.decode("utf-8")
    patched = patcher.build_patched(source)
    compile(patched, "<patched>", "exec")
    assert patcher._note_sites(patched) == patcher.NOTE_FUNCTIONS
    for name in patcher.UNCHANGED_FUNCTIONS:
        assert patcher._function_dump(source, name) == patcher._function_dump(patched, name)
    assert source.count("response.json()") == patched.count("response.json()")
    broken = source + "\n" + patcher.IMPORT_OLD
    with pytest.raises(patcher.PatchRejected, match="import_site"):
        patcher.build_patched(broken)

    target = tmp_path / "llm.py"
    target.write_bytes(original)
    os.chmod(target, 0o640)
    link = tmp_path / "llm.py.hardlink"
    os.link(target, link)
    inode = target.stat().st_ino
    mode = stat.S_IMODE(target.stat().st_mode)
    mtime = target.stat().st_mtime_ns
    state = tmp_path / "state"
    assert patcher.main(["dry-run", "--target", str(target), "--state-dir", str(state)]) == 0
    assert "patch would apply" in capsys.readouterr().out
    assert target.read_bytes() == original
    assert target.stat().st_ino == inode
    assert target.stat().st_mtime_ns == mtime
    assert not state.exists()

    assert patcher.main(["apply", "--target", str(target), "--state-dir", str(state)]) == 0
    assert "inode preserved" in capsys.readouterr().out
    assert target.stat().st_ino == inode == link.stat().st_ino
    assert stat.S_IMODE(target.stat().st_mode) == mode == 0o640
    assert link.read_bytes() == target.read_bytes()
    assert target.read_bytes() != original
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    backup = state / patcher.BACKUP_NAME
    manifest_path = state / patcher.MANIFEST_NAME
    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    assert manifest["original_sha256"] == patcher.EXPECTED_SHA256
    assert manifest["inode"] == inode
    assert manifest["mode"] == 0o640
    assert manifest["uid"] == target.stat().st_uid
    assert manifest["gid"] == target.stat().st_gid
    applied = target.read_bytes()

    aliased = tmp_path / "alias-state"
    aliased.mkdir()
    outside = tmp_path / "outside-state"
    outside.mkdir()
    linked_state = tmp_path / "linked-state"
    linked_state.symlink_to(outside, target_is_directory=True)
    alias_target = tmp_path / "llm-alias.py"
    alias_target.write_bytes(original)
    assert patcher.main(["apply", "--target", str(alias_target), "--state-dir", str(linked_state)]) == 2
    assert alias_target.read_bytes() == original
    assert list(outside.iterdir()) == []

    assert patcher.main(["apply", "--target", str(target), "--state-dir", str(state)]) == 2
    assert target.read_bytes() == applied
    assert target.stat().st_ino == inode
    assert patcher.main(["rollback", "--target", str(target), "--state-dir", str(state)]) == 0
    assert "inode preserved" in capsys.readouterr().out
    assert target.read_bytes() == link.read_bytes() == original
    assert target.stat().st_ino == inode
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert patcher.main(["rollback", "--target", str(target), "--state-dir", str(state)]) == 2
    assert target.read_bytes() == original
    assert target.stat().st_ino == inode
