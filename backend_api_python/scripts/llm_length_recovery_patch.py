#!/usr/bin/env python3
"""Install the code-edit length recovery helper and patch one strategy route.

The supported strategy.py baseline is the running workspace route revision.
The patch rewrites that file in place so its inode stays stable. It refuses
the bind-mounted llm.py revision and the diagnostic helper. Provider
timeouts are not changed.

Run dry-run before apply. Put --state-dir on durable host storage: a helper
or strategy.py written only into a container writable layer is lost when
that container is recreated. This script does not signal or restart processes.
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

MARKER = "qd-llm-length-recovery:v1"
STRATEGY_SHA256 = "ff5652aab4bf2f3c545b1a8bb01dbdda7e0fea0bf271ef889c44d3d25d206307"
STRATEGY_SIZE = 37927
LLM_OVERRIDE_SHA256 = "302b59b88a3bdeaa51395133611abbf8d545c42c6ccdbe5b0b355bfcd3136a3e"
DIAGNOSTIC_HELPER_SHA256 = "b1ad5476c57eb6ac3ede1ea81e3094f9cf6db3cf29548f36d469d671a5dfccc4"
WORKER_UID = 10001
WORKER_GID = 10001
STATE_DIR_NAME = ".llm-length-recovery"
BACKUP_NAME = "strategy.py.before-length-recovery"
MANIFEST_NAME = "manifest.json"
HELPER_NAME = "llm_length_recovery.py"
UNCHANGED_FUNCTION = "run_strategy_workspace_turn"

OLD_IMPORT = 'from app.services.ai_code_edits import (\n    CODE_EDIT_SYSTEM_SUFFIX,\n    CodeEditError,\n    apply_model_code_edits,\n)\n'

NEW_IMPORT = 'from app.services.ai_code_edits import (\n    CODE_EDIT_SYSTEM_SUFFIX,\n    CodeEditError,\n    apply_model_code_edits,\n)\nfrom app.services.llm_length_recovery import (  # qd-llm-length-recovery:v1\n    CodeEditLengthError,\n    apply_recovered_code_edit,\n    call_code_edit_with_length_recovery,\n)\n'

OLD_CALL = '            generated = llm.call_llm_api(\n                messages=messages,\n                model=llm.get_code_generation_model(),\n                temperature=0.2 if use_patch_response else 0.35,\n                use_json_mode=use_patch_response,\n            )\n            if use_patch_response:\n                try:\n                    candidate_code, edit_plan = apply_model_code_edits(existing_code, generated)\n                except CodeEditError as exc:\n                    logger.warning("strategy model patch rejected, retrying full candidate: %s", exc)\n                    full_request = build_strategy_generation_request(\n                        prompt=prompt,\n                        asset_type=asset_type,\n                        existing_code=existing_code,\n                        generation_mode=generation_mode,\n                        context=context,\n                        response_mode="full",\n                    )\n                    full_messages = [dict(item) for item in messages]\n                    full_messages[0] = {"role": "system", "content": full_system_prompt}\n                    full_messages[-1] = {"role": "user", "content": full_request}\n                    fallback = llm.call_llm_api(\n                        messages=full_messages,\n                        model=llm.get_code_generation_model(),\n                        temperature=0.25,\n                        use_json_mode=False,\n                    )\n                    candidate_code = _strip_code_fence(str(fallback or ""))\n                    edit_plan = {\n                        "executor": "model_full_fallback",\n                        "operation": "generate_candidate",\n                        "patch_error": str(exc),\n                    }\n            else:\n                candidate_code = _strip_code_fence(str(generated or ""))\n                edit_plan = {"executor": "model", "operation": "generate_candidate"}\n'

NEW_CALL = '            if use_patch_response:\n                generated, length_recovery = call_code_edit_with_length_recovery(\n                    llm.call_llm_api,\n                    messages=messages,\n                    model=llm.get_code_generation_model(),\n                    temperature=0.2,\n                    use_json_mode=True,\n                    lang=lang,\n                )\n                try:\n                    candidate_code, edit_plan = apply_recovered_code_edit(\n                        existing_code,\n                        generated,\n                        length_recovery,\n                        apply_model_code_edits,\n                        lang=lang,\n                    )\n                except CodeEditError as exc:\n                    logger.warning("strategy model patch rejected, retrying full candidate: %s", exc)\n                    full_request = build_strategy_generation_request(\n                        prompt=prompt,\n                        asset_type=asset_type,\n                        existing_code=existing_code,\n                        generation_mode=generation_mode,\n                        context=context,\n                        response_mode="full",\n                    )\n                    full_messages = [dict(item) for item in messages]\n                    full_messages[0] = {"role": "system", "content": full_system_prompt}\n                    full_messages[-1] = {"role": "user", "content": full_request}\n                    fallback = llm.call_llm_api(\n                        messages=full_messages,\n                        model=llm.get_code_generation_model(),\n                        temperature=0.25,\n                        use_json_mode=False,\n                    )\n                    candidate_code = _strip_code_fence(str(fallback or ""))\n                    edit_plan = {\n                        "executor": "model_full_fallback",\n                        "operation": "generate_candidate",\n                        "patch_error": str(exc),\n                    }\n                else:\n                    if length_recovery == "complete_patch":\n                        edit_plan = {**edit_plan, "length_recovery": "complete_patch"}\n            else:\n                generated = llm.call_llm_api(\n                    messages=messages,\n                    model=llm.get_code_generation_model(),\n                    temperature=0.35,\n                    use_json_mode=False,\n                )\n                candidate_code = _strip_code_fence(str(generated or ""))\n                edit_plan = {"executor": "model", "operation": "generate_candidate"}\n'

OLD_EXC = '    except PermissionError as exc:\n        return _error(str(exc), 403)\n    except ValueError as exc:\n        return _error("strategyV2.generationInvalid", data={"error": str(exc)})\n    except Exception as exc:\n        logger.warning("strategy AI workspace turn failed: %s", exc, exc_info=True)\n        return _error("strategyV2.generationInvalid", data={"error": str(exc)})\n'

NEW_EXC = '    except PermissionError as exc:\n        return _error(str(exc), 403)\n    except CodeEditLengthError as exc:\n        return _error(\n            "strategyV2.generationInvalid",\n            data={"error": str(exc), "lengthRecovery": exc.kind, "codeApplied": False},\n        )\n    except ValueError as exc:\n        return _error("strategyV2.generationInvalid", data={"error": str(exc)})\n    except Exception as exc:\n        logger.warning("strategy AI workspace turn failed: %s", exc, exc_info=True)\n        return _error("strategyV2.generationInvalid", data={"error": str(exc)})\n'


OLD_DET = '        if deterministic_edit:\n            candidate_code, edit_plan = deterministic_edit\n'

NEW_DET = '        length_recovery = "none"\n        if deterministic_edit:\n            candidate_code, edit_plan = deterministic_edit\n'

OLD_COMP = '        candidate_code, program, behavior_validation = _compile_or_repair_generated_strategy(\n            llm,\n            user_prompt,\n            candidate_code,\n            asset_type=asset_type,\n            generation_mode=generation_mode,\n            context=context,\n            system_prompt=full_system_prompt,\n            intent=validation_intent,\n        )\n'

NEW_COMP = '        try:\n            candidate_code, program, behavior_validation = _compile_or_repair_generated_strategy(\n                llm,\n                user_prompt,\n                candidate_code,\n                asset_type=asset_type,\n                generation_mode=generation_mode,\n                context=context,\n                system_prompt=full_system_prompt,\n                intent=validation_intent,\n                max_repair_attempts=0 if length_recovery == "complete_patch" else 2,\n            )\n        except CodeEditLengthError:\n            raise\n        except Exception:\n            if length_recovery != "complete_patch":\n                raise\n            raise CodeEditLengthError("invalid_patch", lang) from None\n'

REPLACEMENTS = (
    ("import_site", OLD_IMPORT, NEW_IMPORT),
    ("call_site", OLD_CALL, NEW_CALL),
    ("handler_site", OLD_EXC, NEW_EXC),
    ("scope_site", OLD_DET, NEW_DET),
    ("repair_site", OLD_COMP, NEW_COMP),
)


class PatchRejected(Exception):
    """The target is not the supported strategy revision or the patch is unsafe."""


def helper_source_path() -> Path:
    return Path(__file__).resolve().parents[1] / "app" / "services" / HELPER_NAME


def build_patched(source: str) -> str:
    if source.count(MARKER) != 0:
        raise PatchRejected("already-patched")
    if 'def run_strategy_workspace_turn(' not in source or 'if intent == "discussion":' not in source:
        raise PatchRejected("unknown-version")
    patched = source
    for name, old, new in REPLACEMENTS:
        count = patched.count(old)
        if count != 1:
            raise PatchRejected(f"anchor {name} count {count}")
        patched = patched.replace(old, new, 1)
    if patched.count(MARKER) != 1:
        raise PatchRejected("patch marker was not unique")
    _assert_preserves_behavior(source, patched)
    return patched


def restore_baseline(source: str) -> str:
    if source.count(MARKER) != 1:
        raise PatchRejected("current file is not the length-recovery patch")
    restored = source
    for name, old, new in reversed(REPLACEMENTS):
        count = restored.count(new)
        if count != 1:
            raise PatchRejected(f"restore anchor {name} count {count}")
        restored = restored.replace(new, old, 1)
    if MARKER in restored:
        raise PatchRejected("restore left the patch marker in place")
    return restored


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("dry-run", "apply", "rollback"))
    parser.add_argument("--strategy-target", required=True, type=Path)
    parser.add_argument("--helper-target", type=Path)
    parser.add_argument("--helper-source", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--worker-uid", type=int, default=WORKER_UID)
    parser.add_argument("--worker-gid", type=int, default=WORKER_GID)
    args = parser.parse_args(argv)
    try:
        if args.command == "dry-run":
            _dry_run(args)
        elif args.command == "apply":
            if args.helper_target is None:
                raise PatchRejected("apply requires --helper-target")
            _apply(args)
        else:
            if args.helper_target is None:
                raise PatchRejected("rollback requires --helper-target")
            _rollback(args)
    except PatchRejected as exc:
        print(f"rejected: {exc}", file=sys.stderr)
        return 2
    return 0


def _dry_run(args: argparse.Namespace) -> None:
    fd = _open_regular(args.strategy_target, os.O_RDONLY)
    try:
        _flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        original, identity = _read_fd(fd)
        _refuse_protected(identity["sha256"], args.strategy_target)
        source = _decode(original)
        if not _is_baseline(identity):
            if MARKER in source:
                print("dry-run: already patched; no change")
                return
            _require_baseline(identity)
        patched = build_patched(source)
        compile(patched, "<llm-length-recovery>", "exec")
        print(
            "dry-run: strategy patch would apply "
            f"inode={identity['inode']} mode={identity['mode']:04o} "
            f"sha256={identity['sha256']}"
        )
        if args.helper_target is not None:
            print(f"dry-run: helper would be installed as {args.helper_target.name} mode=0600")
    finally:
        os.close(fd)


def _apply(args: argparse.Namespace) -> None:
    _refuse_helper_target(args.helper_target)
    helper_bytes = _helper_bytes(args.helper_source)
    fd = _open_regular(args.strategy_target, os.O_RDWR)
    try:
        _flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original, identity = _read_fd(fd)
        _refuse_protected(identity["sha256"], args.strategy_target)
        source = _decode(original)
        if not _is_baseline(identity):
            if MARKER in source:
                raise PatchRejected("already-patched")
            _require_baseline(identity)
        patched = build_patched(source)
        compile(patched, "<llm-length-recovery>", "exec")
        patched_bytes = patched.encode("utf-8")
        created_helper = _install_helper(args.helper_target, helper_bytes, args.worker_uid, args.worker_gid)
        state = _state_dir(args.strategy_target, args.state_dir)
        try:
            _prepare_state(state)
            _write_backup(state, original, identity, _sha256(patched_bytes), _sha256(helper_bytes), created_helper, args.worker_uid, args.worker_gid)
            _write_fd(fd, patched_bytes)
        except Exception as exc:
            _remove_helper_if_created(args.helper_target, helper_bytes, created_helper)
            try:
                _write_fd(fd, original)
            except Exception:
                raise PatchRejected("write failed and in-place restore failed; backup remains") from exc
            raise PatchRejected("write failed; original strategy bytes restored") from exc
        os.fchmod(fd, int(identity["mode"]))
        _, confirmed = _read_fd(fd)
        if confirmed["inode"] != identity["inode"] or confirmed["sha256"] != _sha256(patched_bytes):
            raise PatchRejected("post-write identity check failed")
        if confirmed["uid"] != identity["uid"] or confirmed["gid"] != identity["gid"]:
            _restore_owner(fd, identity)
        print(f"applied: strategy inode preserved inode={confirmed['inode']} sha256={confirmed['sha256']}")
    finally:
        os.close(fd)


def _rollback(args: argparse.Namespace) -> None:
    state = _state_dir(args.strategy_target, args.state_dir)
    manifest = _read_manifest(state)
    backup = _read_private_file(state / BACKUP_NAME)
    if _sha256(backup) != STRATEGY_SHA256 or len(backup) != STRATEGY_SIZE:
        raise PatchRejected("backup is not the supported strategy revision")
    fd = _open_regular(args.strategy_target, os.O_RDWR)
    try:
        _flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current, identity = _read_fd(fd)
        if MARKER.encode("ascii") not in current:
            raise PatchRejected("current strategy file is not the length-recovery patch")
        if identity["inode"] != manifest.get("inode"):
            raise PatchRejected("inode changed; refusing to roll back a different file")
        if identity["sha256"] != manifest.get("patched_sha256"):
            raise PatchRejected("current bytes are not the patched revision recorded for rollback")
        restored = _decode(backup)
        ast.parse(restored)
        compile(restored, "<llm-length-recovery-rollback>", "exec")
        if MARKER in restored:
            raise PatchRejected("backup still contains the patch marker")
        _write_fd(fd, backup)
        os.fchmod(fd, int(manifest.get("mode", identity["mode"])))
        _, confirmed = _read_fd(fd)
        if confirmed["inode"] != identity["inode"] or confirmed["sha256"] != STRATEGY_SHA256:
            raise PatchRejected("post-rollback identity check failed")
        _restore_owner(fd, {"uid": manifest.get("uid"), "gid": manifest.get("gid")})
        if manifest.get("created_helper") is True:
            _remove_helper_if_created(args.helper_target, _helper_bytes(args.helper_source), True)
        print(f"rolled back: strategy inode preserved inode={confirmed['inode']} sha256={confirmed['sha256']}")
    finally:
        os.close(fd)


def _assert_preserves_behavior(source: str, patched: str) -> None:
    if _discussion(source) != _discussion(patched):
        raise PatchRejected("patch changed the discussion path")
    if patched.count("stream_llm_api") != source.count("stream_llm_api"):
        raise PatchRejected("patch changed streaming")
    if patched.count("def generate_strategy(") != 1 or _function_dump(source, "generate_strategy") != _function_dump(patched, "generate_strategy"):
        raise PatchRejected("patch changed strategy generation outside the workspace route")
    if _function_dump(source, "_compile_or_repair_generated_strategy") != _function_dump(patched, "_compile_or_repair_generated_strategy"):
        raise PatchRejected("patch changed strategy repair")
    original_functions = _top_functions(source)
    patched_functions = _top_functions(patched)
    if set(original_functions) != set(patched_functions):
        raise PatchRejected("patch added or removed a route function")
    for name, dump in original_functions.items():
        if name == UNCHANGED_FUNCTION:
            continue
        if patched_functions[name] != dump:
            raise PatchRejected(f"patch changed {name}")
    if _module_prefix(source) != _module_prefix(patched):
        raise PatchRejected("patch changed imports outside the recovery helper")
    if patched.count("call_code_edit_with_length_recovery(") != 1:
        raise PatchRejected("recovery call was not unique")
    if "timeout_seconds" in NEW_CALL or "max_tokens" in NEW_CALL:
        raise PatchRejected("recovery call changes timeout or token budget")
    ast.parse(patched)


def _discussion(source: str) -> str:
    start = source.index('        if intent == "discussion":')
    end = source.index("        system_prompt, generation_intent = build_strategy_system_prompt(")
    return source[start:end]


def _top_functions(source: str) -> dict[str, str]:
    tree = ast.parse(source)
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            found[node.name] = ast.dump(node, include_attributes=False)
    return found


def _function_dump(source: str, name: str) -> str:
    found = _top_functions(source)
    if name not in found:
        raise PatchRejected(f"missing {name}")
    return found[name]


def _module_prefix(source: str) -> list[str]:
    tree = ast.parse(source)
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "app.services.llm_length_recovery":
            continue
        nodes.append(ast.dump(node, include_attributes=False))
    return nodes


def _install_helper(target: Path, helper_bytes: bytes, uid: int, gid: int) -> bool:
    if target.exists() or target.is_symlink():
        if target.is_symlink():
            raise PatchRejected("helper target must not be a symlink")
        existing = target.read_bytes()
        if existing != helper_bytes or _sha256(existing) == DIAGNOSTIC_HELPER_SHA256:
            raise PatchRejected("helper target exists and is not the length-recovery module")
        _restrict_helper(target, uid, gid)
        return False
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(target, flags, 0o600)
    try:
        _write_fd(fd, helper_bytes)
        _ensure_helper_owner(fd, uid)
    except Exception:
        os.close(fd)
        try:
            os.unlink(target)
        except OSError:
            pass
        raise
    os.close(fd)
    return True


def _ensure_helper_owner(fd: int, uid: int) -> None:
    os.fchmod(fd, 0o600)
    info = os.fstat(fd)
    if info.st_uid != uid:
        try:
            os.fchown(fd, uid, -1)
        except OSError as exc:
            raise PatchRejected(
                f"helper would not be readable by worker uid {uid} ({exc.strerror}); strategy was not modified"
            ) from exc
        info = os.fstat(fd)
    if info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o600:
        raise PatchRejected("helper owner or mode does not match the worker requirement")


def _restrict_helper(target: Path, uid: int, gid: int) -> None:
    del gid
    fd = os.open(target, os.O_RDWR | os.O_NOFOLLOW)
    try:
        _ensure_helper_owner(fd, uid)
    finally:
        os.close(fd)


def _remove_helper_if_created(target: Path, helper_bytes: bytes, created: bool) -> None:
    if not created or not target.exists() or target.is_symlink():
        return
    if target.read_bytes() == helper_bytes:
        os.unlink(target)


def _helper_bytes(source: Path | None) -> bytes:
    path = source or helper_source_path()
    if not path.is_file() or path.is_symlink():
        raise PatchRejected("length-recovery helper source is missing")
    data = path.read_bytes()
    if _sha256(data) == DIAGNOSTIC_HELPER_SHA256 or b"RotatingFileHandler" in data:
        raise PatchRejected("refusing to install the diagnostic helper as the recovery module")
    if b"call_code_edit_with_length_recovery" not in data:
        raise PatchRejected("helper source is not the length-recovery module")
    return data


def _refuse_helper_target(target: Path) -> None:
    if target.name in {"llm.py", "llm_call_diagnostics.py"}:
        raise PatchRejected("refusing to replace llm.py or the diagnostic helper")


def _refuse_protected(digest: str, target: Path) -> None:
    if target.name == "llm.py" or digest == LLM_OVERRIDE_SHA256:
        raise PatchRejected("refusing to patch the bind-mounted llm.py revision")
    if digest == DIAGNOSTIC_HELPER_SHA256 or target.name == "llm_call_diagnostics.py":
        raise PatchRejected("refusing to patch the diagnostic helper")


def _is_baseline(identity: dict[str, int | str]) -> bool:
    return identity["sha256"] == STRATEGY_SHA256 and identity["size"] == STRATEGY_SIZE


def _require_baseline(identity: dict[str, int | str]) -> None:
    if not _is_baseline(identity):
        raise PatchRejected(f"unknown-version sha256={identity['sha256']} size={identity['size']}")


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
        raise PatchRejected("another length-recovery patch operation holds the lock") from exc


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
    return data, {
        "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "size": info.st_size,
        "sha256": _sha256(data),
    }


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeError as exc:
        raise PatchRejected("target is not utf-8") from exc


def _state_dir(target: Path, state_dir: Path | None) -> Path:
    return state_dir if state_dir is not None else target.parent / STATE_DIR_NAME


def _prepare_state(state: Path) -> None:
    if state.is_symlink():
        raise PatchRejected("state directory must not be a symlink")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.is_symlink() or not state.is_dir():
        raise PatchRejected("state directory is not a real directory")
    os.chmod(state, 0o700)


def _write_backup(state: Path, original: bytes, identity: dict[str, int | str], patched_sha: str, helper_sha: str, created_helper: bool, uid: int, gid: int) -> None:
    backup_path = state / BACKUP_NAME
    if backup_path.is_symlink() or (state / MANIFEST_NAME).is_symlink():
        raise PatchRejected("backup path must not be a symlink")
    if backup_path.exists() and _read_private_file(backup_path) != original:
        raise PatchRejected("existing backup does not match the supported revision")
    if not backup_path.exists():
        _write_private_file(backup_path, original)
    manifest = {
        "version": 1,
        "marker": MARKER,
        "original_sha256": STRATEGY_SHA256,
        "patched_sha256": patched_sha,
        "helper_sha256": helper_sha,
        "created_helper": created_helper,
        "worker_uid": uid,
        "worker_gid": gid,
        "mode": identity["mode"],
        "uid": identity["uid"],
        "gid": identity["gid"],
        "inode": identity["inode"],
    }
    _write_private_file(state / MANIFEST_NAME, json.dumps(manifest, sort_keys=True).encode("ascii") + b"\n")


def _read_manifest(state: Path) -> dict:
    if state.is_symlink():
        raise PatchRejected("state directory must not be a symlink")
    try:
        manifest = json.loads(_read_private_file(state / MANIFEST_NAME).decode("ascii"))
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
