#!/usr/bin/env python3
"""Apply or roll back a temporary non-streaming LLM diagnostic patch.

The patch is inert until ``QD_LLM_CALL_DIAGNOSTICS`` is enabled in the process
environment. This script never signals, reloads, or restarts a process, and it
never opens a network connection.

``apply`` rewrites only a regular file whose bytes hash to the supported
revision. The rewrite keeps that file's inode so a file bind mount keeps
seeing the update. Unknown bytes, missing anchors, a business-logic AST
change, or a lock held by another patch process are refused before writing.
"""

from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import json
import os
import stat
import sys
from pathlib import Path


MARKER = "qd-llm-call-diagnostics:v1"
EXPECTED_SHA256 = "d59e6f10cc87229177f296e9a21465b3f4190430f6d8c3d67314a1bd57551805"
EXPECTED_SIZE = 72016
STATE_DIR_NAME = ".llm-call-diagnostics"
BACKUP_NAME = "llm.py.before-llm-call-diagnostics"
MANIFEST_NAME = "manifest.json"
UNCHANGED_FUNCTIONS = (
    "_stream_openai_compatible",
    "_iter_sse_data",
    "_call_google_gemini",
    "_call_litellm",
    "stream_llm_api",
    "_custom_capacity_key",
    "_custom_inline_system_payload",
    "_capacity_error",
)
FINGERPRINTS = (
    "def _custom_capacity_key(",
    "def _custom_inline_system_payload(",
    'error_type="upstream_capacity"',
)
NOTE_FUNCTIONS = {
    ("_llm_post", "note_send"),
    ("_llm_post", "note_exception"),
    ("_llm_post", "note_return"),
    ("_call_openai_compatible", "note_parsed"),
}

IMPORT_OLD = "from app.utils.redaction import redact_secrets\n"
IMPORT_NEW = (
    "from app.utils.redaction import redact_secrets\n"
    "try:\n"
    "    from app.services import llm_call_diagnostics as _llm_diag  # "
    + MARKER
    + "\n"
    "except ImportError:\n"
    "    _llm_diag = None\n"
)
POST_OLD = (
    "        try:\n"
    "            if not stream and not proxy_url:\n"
    "                session.close()\n"
    "                post_kwargs = dict(kwargs)\n"
    '                post_kwargs.pop("stream", None)\n'
    "                response = requests.post(url, **post_kwargs)\n"
    "            else:\n"
    "                response = session.post(url, **kwargs)\n"
    "        except requests.exceptions.RequestException as exc:\n"
)
POST_NEW = (
    "        try:\n"
    "            if not stream and _llm_diag is not None:\n"
    "                try:\n"
    "                    _llm_diag.note_send(\n"
    "                        url=url,\n"
    "                        payload=json_payload,\n"
    "                        timeout=timeout,\n"
    "                        stream=False,\n"
    "                        proxy_url=proxy_url,\n"
    "                    )\n"
    "                except Exception:\n"
    "                    pass\n"
    "            if not stream and not proxy_url:\n"
    "                session.close()\n"
    "                post_kwargs = dict(kwargs)\n"
    '                post_kwargs.pop("stream", None)\n'
    "                response = requests.post(url, **post_kwargs)\n"
    "            else:\n"
    "                response = session.post(url, **kwargs)\n"
    "        except requests.exceptions.RequestException as exc:\n"
    "            if not stream and _llm_diag is not None:\n"
    "                try:\n"
    "                    _llm_diag.note_exception(exc, timeout=timeout)\n"
    "                except Exception:\n"
    "                    pass\n"
)
RETURN_OLD = (
    "        if stream:\n"
    "            response._quantdinger_llm_session = session\n"
    "        else:\n"
    "            session.close()\n"
    "        return response\n"
)
RETURN_NEW = (
    "        if stream:\n"
    "            response._quantdinger_llm_session = session\n"
    "        else:\n"
    "            session.close()\n"
    "            if _llm_diag is not None:\n"
    "                try:\n"
    "                    _llm_diag.note_return(response)\n"
    "                except Exception:\n"
    "                    pass\n"
    "        return response\n"
)
PARSED_OLD = (
    "        result = response.json()\n"
    '        if "choices" in result and len(result["choices"]) > 0:\n'
)
PARSED_NEW = (
    "        result = response.json()\n"
    "        if _llm_diag is not None:\n"
    "            try:\n"
    "                _llm_diag.note_parsed(result, response)\n"
    "            except Exception:\n"
    "                pass\n"
    '        if "choices" in result and len(result["choices"]) > 0:\n'
)
REPLACEMENTS = (
    ("import_site", IMPORT_OLD, IMPORT_NEW),
    ("post_site", POST_OLD, POST_NEW),
    ("return_site", RETURN_OLD, RETURN_NEW),
    ("parsed_site", PARSED_OLD, PARSED_NEW),
)


class PatchRejected(Exception):
    """The target is not the supported revision or the patch is unsafe."""


def build_patched(source: str) -> str:
    """Return the patched source, or reject an unknown or ambiguous revision."""
    if source.count(MARKER) != 0:
        raise PatchRejected("already-patched")
    for fingerprint in FINGERPRINTS:
        if source.count(fingerprint) < 1:
            raise PatchRejected("unknown-version")
    patched = source
    for name, old, new in REPLACEMENTS:
        count = patched.count(old)
        if count != 1:
            raise PatchRejected(f"anchor {name} count {count}")
        patched = patched.replace(old, new, 1)
    if patched == source or patched.count(MARKER) != 1:
        raise PatchRejected("patch marker was not unique")
    _assert_preserves_behavior(source, patched)
    return patched


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("dry-run", "apply", "rollback"))
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--state-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "dry-run":
            _dry_run(args.target, args.state_dir)
        elif args.command == "apply":
            _apply(args.target, args.state_dir)
        else:
            _rollback(args.target, args.state_dir)
    except PatchRejected as exc:
        print(f"rejected: {exc}", file=sys.stderr)
        return 2
    return 0


def _dry_run(target: Path, state_dir: Path | None) -> None:
    fd = _open_regular(target, os.O_RDONLY)
    try:
        _flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        original, identity = _read_fd(fd)
        source = _decode(original)
        if not _is_base_revision(identity):
            if MARKER in source:
                print("dry-run: already patched; no change")
                return
            _require_base_revision(identity)
        patched = build_patched(source)
        compile(patched, "<llm-call-diagnostics>", "exec")
        print(
            "dry-run: patch would apply "
            f"inode={identity['inode']} mode={identity['mode']:04o} "
            f"sha256={identity['sha256']}"
        )
        if state_dir is not None and not state_dir.exists():
            print("dry-run: state directory would be created on apply")
    finally:
        os.close(fd)


def _apply(target: Path, state_dir: Path | None) -> None:
    fd = _open_regular(target, os.O_RDWR)
    try:
        _flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original, identity = _read_fd(fd)
        source = _decode(original)
        if not _is_base_revision(identity):
            if MARKER in source:
                raise PatchRejected("already-patched")
            _require_base_revision(identity)
        patched = build_patched(source)
        compile(patched, "<llm-call-diagnostics>", "exec")
        patched_bytes = patched.encode("utf-8")
        patched_sha = _sha256(patched_bytes)
        state = _state_dir(target, state_dir)
        _prepare_state(state)
        _write_backup(state, original, identity, patched_sha)
        try:
            _write_fd(fd, patched_bytes)
        except Exception as exc:
            try:
                _write_fd(fd, original)
            except Exception:
                raise PatchRejected("write failed and in-place restore failed; backup remains") from exc
            raise PatchRejected("write failed; original bytes restored") from exc
        os.fchmod(fd, identity["mode"])
        _, confirmed = _read_fd(fd)
        if confirmed["inode"] != identity["inode"] or confirmed["sha256"] != patched_sha:
            raise PatchRejected("post-write identity check failed")
        if confirmed["uid"] != identity["uid"] or confirmed["gid"] != identity["gid"]:
            _restore_owner(fd, identity)
        print(f"applied: inode preserved inode={confirmed['inode']} sha256={patched_sha}")
    finally:
        os.close(fd)


def _rollback(target: Path, state_dir: Path | None) -> None:
    state = _state_dir(target, state_dir)
    manifest = _read_manifest(state)
    backup = _read_private_file(state / BACKUP_NAME)
    if _sha256(backup) != EXPECTED_SHA256 or len(backup) != EXPECTED_SIZE:
        raise PatchRejected("backup is not the supported revision")
    if manifest.get("original_sha256") != EXPECTED_SHA256:
        raise PatchRejected("manifest does not match the supported revision")
    fd = _open_regular(target, os.O_RDWR)
    try:
        _flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current, identity = _read_fd(fd)
        if MARKER.encode("ascii") not in current:
            raise PatchRejected("current file is not the diagnostic patch")
        if identity["inode"] != manifest.get("inode"):
            raise PatchRejected("inode changed; refusing to roll back a different file")
        if identity["sha256"] != manifest.get("patched_sha256"):
            raise PatchRejected("current bytes are not the patched revision recorded for rollback")
        if identity["sha256"] == EXPECTED_SHA256:
            raise PatchRejected("already rolled back")
        restored = _decode(backup)
        ast.parse(restored)
        compile(restored, "<llm-call-diagnostics-rollback>", "exec")
        if MARKER in restored:
            raise PatchRejected("backup still contains the diagnostic marker")
        try:
            _write_fd(fd, backup)
        except Exception as exc:
            raise PatchRejected("rollback write failed; backup remains") from exc
        mode = int(manifest.get("mode", identity["mode"]))
        os.fchmod(fd, mode)
        _, confirmed = _read_fd(fd)
        if confirmed["inode"] != identity["inode"] or confirmed["sha256"] != EXPECTED_SHA256:
            raise PatchRejected("post-rollback identity check failed")
        _restore_owner(fd, {"uid": manifest.get("uid"), "gid": manifest.get("gid")})
        print(f"rolled back: inode preserved inode={confirmed['inode']} sha256={confirmed['sha256']}")
    finally:
        os.close(fd)


def _assert_preserves_behavior(source: str, patched: str) -> None:
    if patched.count("response.json()") != source.count("response.json()"):
        raise PatchRejected("patch changed response.json() call count")
    if patched.count(".iter_lines(") != source.count(".iter_lines("):
        raise PatchRejected("patch changed stream iteration")
    if patched.count("iter_content") != source.count("iter_content"):
        raise PatchRejected("patch changed stream consumption")
    for name in UNCHANGED_FUNCTIONS:
        if _function_dump(source, name) != _function_dump(patched, name):
            raise PatchRejected(f"patch changed {name}")
    notes = _note_sites(patched)
    if notes != NOTE_FUNCTIONS:
        raise PatchRejected("diagnostic calls are not limited to the non-streaming sites")
    original_tree = ast.parse(source)
    patched_tree = ast.parse(patched)
    _strip_diagnostics(patched_tree)
    if ast.dump(original_tree, include_attributes=False) != ast.dump(patched_tree, include_attributes=False):
        raise PatchRejected("patched AST changes business logic")


def _function_dump(source: str, name: str) -> str:
    tree = ast.parse(source)
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    if len(matches) != 1:
        raise PatchRejected(f"anchor function {name} count {len(matches)}")
    return ast.dump(matches[0], include_attributes=False)


def _note_sites(source: str) -> set[tuple[str, str]]:
    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []
            self.found: set[tuple[str, str]] = set()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Attribute(self, node: ast.Attribute) -> None:
            if isinstance(node.value, ast.Name) and node.value.id == "_llm_diag" and node.attr.startswith("note_"):
                owner = self.stack[-1] if self.stack else "<module>"
                self.found.add((owner, node.attr))
            self.generic_visit(node)

    visitor = Visitor()
    visitor.visit(ast.parse(source))
    return visitor.found


def _strip_diagnostics(node: ast.AST) -> None:
    for field, value in list(ast.iter_fields(node)):
        if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
            kept = []
            for stmt in value:
                if _is_diag_stmt(stmt):
                    continue
                _strip_diagnostics(stmt)
                kept.append(stmt)
            setattr(node, field, kept)
        elif isinstance(value, ast.AST):
            _strip_diagnostics(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, ast.AST):
                    _strip_diagnostics(item)


def _is_diag_stmt(stmt: ast.stmt) -> bool:
    return _is_diag_import(stmt) or _is_diag_if(stmt)


def _is_diag_if(stmt: ast.stmt) -> bool:
    if not isinstance(stmt, ast.If) or stmt.orelse:
        return False
    if not any(isinstance(node, ast.Name) and node.id == "_llm_diag" for node in ast.walk(stmt.test)):
        return False
    return bool(stmt.body) and all(_is_diag_try(item) for item in stmt.body)


def _is_diag_try(stmt: ast.stmt) -> bool:
    if not isinstance(stmt, ast.Try) or stmt.orelse or stmt.finalbody:
        return False
    if len(stmt.body) != 1 or len(stmt.handlers) != 1:
        return False
    handler = stmt.handlers[0]
    if not isinstance(handler.type, ast.Name) or handler.type.id != "Exception":
        return False
    if len(handler.body) != 1 or not isinstance(handler.body[0], ast.Pass):
        return False
    call = stmt.body[0]
    if not isinstance(call, ast.Expr) or not isinstance(call.value, ast.Call):
        return False
    func = call.value.func
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "_llm_diag"
        and func.attr in {"note_send", "note_return", "note_exception", "note_parsed"}
    )


def _is_diag_import(stmt: ast.stmt) -> bool:
    if not isinstance(stmt, ast.Try) or stmt.orelse or stmt.finalbody:
        return False
    if len(stmt.body) != 1 or len(stmt.handlers) != 1:
        return False
    imported = stmt.body[0]
    if not isinstance(imported, ast.ImportFrom) or imported.module != "app.services" or imported.level != 0:
        return False
    if len(imported.names) != 1:
        return False
    alias = imported.names[0]
    if alias.name != "llm_call_diagnostics" or alias.asname != "_llm_diag":
        return False
    handler = stmt.handlers[0]
    if not isinstance(handler.type, ast.Name) or handler.type.id != "ImportError":
        return False
    if len(handler.body) != 1 or not isinstance(handler.body[0], ast.Assign):
        return False
    assign = handler.body[0]
    if len(assign.targets) != 1 or not isinstance(assign.targets[0], ast.Name):
        return False
    if assign.targets[0].id != "_llm_diag" or not isinstance(assign.value, ast.Constant):
        return False
    return assign.value.value is None


def _open_regular(target: Path, flags: int) -> int:
    try:
        fd = os.open(target, flags)
    except OSError as exc:
        raise PatchRejected(f"cannot access target ({exc.strerror})") from exc
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise PatchRejected("target is not a regular file")
    return fd


def _flock(fd: int, operation: int) -> None:
    try:
        fcntl.flock(fd, operation)
    except BlockingIOError as exc:
        raise PatchRejected("another diagnostics patch operation holds the lock") from exc


def _read_fd(fd: int) -> tuple[bytes, dict[str, int | str]]:
    info = os.fstat(fd)
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    remaining = info.st_size
    while remaining > 0:
        chunk = os.read(fd, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) != info.st_size:
        raise PatchRejected("short read while checking the target")
    identity: dict[str, int | str] = {
        "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "size": info.st_size,
        "sha256": _sha256(data),
    }
    return data, identity


def _is_base_revision(identity: dict[str, int | str]) -> bool:
    return identity["sha256"] == EXPECTED_SHA256 and identity["size"] == EXPECTED_SIZE


def _require_base_revision(identity: dict[str, int | str]) -> None:
    if not _is_base_revision(identity):
        raise PatchRejected(
            "unknown-version "
            f"sha256={identity['sha256']} size={identity['size']}"
        )


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeError as exc:
        raise PatchRejected("target is not utf-8") from exc


def _state_dir(target: Path, state_dir: Path | None) -> Path:
    if state_dir is not None:
        return state_dir
    return target.parent / STATE_DIR_NAME


def _prepare_state(state: Path) -> None:
    if state.is_symlink():
        raise PatchRejected("state directory must not be a symlink")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.is_symlink() or not state.is_dir():
        raise PatchRejected("state directory is not a real directory")
    os.chmod(state, 0o700)


def _write_backup(state: Path, original: bytes, identity: dict[str, int | str], patched_sha: str) -> None:
    backup_path = state / BACKUP_NAME
    manifest_path = state / MANIFEST_NAME
    if backup_path.is_symlink() or manifest_path.is_symlink():
        raise PatchRejected("backup path must not be a symlink")
    if backup_path.exists():
        existing = _read_private_file(backup_path)
        if existing != original or _sha256(existing) != EXPECTED_SHA256:
            raise PatchRejected("existing backup does not match the supported revision")
    else:
        _write_private_file(backup_path, original)
    manifest = {
        "version": 1,
        "marker": MARKER,
        "original_sha256": EXPECTED_SHA256,
        "patched_sha256": patched_sha,
        "original_size": EXPECTED_SIZE,
        "mode": identity["mode"],
        "uid": identity["uid"],
        "gid": identity["gid"],
        "inode": identity["inode"],
    }
    _write_private_file(manifest_path, json.dumps(manifest, sort_keys=True).encode("ascii") + b"\n")


def _read_manifest(state: Path) -> dict:
    if state.is_symlink():
        raise PatchRejected("state directory must not be a symlink")
    payload = _read_private_file(state / MANIFEST_NAME)
    try:
        manifest = json.loads(payload.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PatchRejected("manifest is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise PatchRejected("manifest version is not supported")
    return manifest


def _write_private_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    try:
        _write_fd(fd, data)
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def _read_private_file(path: Path) -> bytes:
    if path.is_symlink():
        raise PatchRejected("refusing to read a symlink backup")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PatchRejected("backup is not a regular file")
        os.lseek(fd, 0, os.SEEK_SET)
        chunks = []
        remaining = info.st_size
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _write_fd(fd: int, data: bytes) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    view = memoryview(data)
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:
            raise OSError("short write")
        written += count
    os.ftruncate(fd, len(data))
    os.fsync(fd)


def _restore_owner(fd: int, identity: dict) -> None:
    uid = identity.get("uid")
    gid = identity.get("gid")
    if not isinstance(uid, int) or not isinstance(gid, int):
        return
    info = os.fstat(fd)
    if info.st_uid == uid and info.st_gid == gid:
        return
    if os.geteuid() != 0:
        raise PatchRejected("owner changed and cannot be restored without privileges")
    os.fchown(fd, uid, gid)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


if __name__ == "__main__":
    sys.exit(main())
