#!/usr/bin/env python3
"""Bounded, reversible organization of direct files in one explicit directory."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any


TOOL = "safe-tidy"
SCHEMA_VERSION = 1
FILE_PROVIDER_XATTR = "com.apple.file-provider-domain-id"
DEFAULT_MAX_FILES = 10_000
DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024


class SafeTidyError(Exception):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _seal(value: dict[str, Any], field: str) -> dict[str, Any]:
    result = dict(value)
    result.pop(field, None)
    result[field] = hashlib.sha256(_canonical_json(result)).hexdigest()
    return result


def _verify_seal(value: dict[str, Any], field: str) -> bool:
    expected = value.get(field)
    if not isinstance(expected, str) or len(expected) != 64:
        return False
    return _seal(value, field)[field] == expected


def _envelope(
    status: str,
    summary: str,
    *,
    data: dict[str, Any] | None = None,
    artifacts: list[dict[str, str]] | None = None,
    warnings: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "status": status,
        "summary": summary,
        "data": data or {},
        "artifacts": artifacts or [],
        "warnings": warnings or [],
    }


def _emit(envelope: dict[str, Any]) -> None:
    print(json.dumps(envelope, ensure_ascii=False, sort_keys=True, allow_nan=False))


def _identity(info: os.stat_result) -> dict[str, int]:
    return {
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "mode": int(stat.S_IMODE(info.st_mode)),
    }


def _same_identity(info: os.stat_result, expected: dict[str, Any]) -> bool:
    return (
        int(info.st_dev) == expected.get("device")
        and int(info.st_ino) == expected.get("inode")
        and int(stat.S_IMODE(info.st_mode)) == expected.get("mode")
    )


def _is_missing_xattr(exc: OSError) -> bool:
    missing = {getattr(errno, "ENODATA", -1), getattr(errno, "ENOATTR", -1)}
    return exc.errno in missing


def _fileprovider_marker(path_or_fd: str | int) -> bool:
    if sys.platform == "darwin" and hasattr(os, "getxattr"):
        try:
            if isinstance(path_or_fd, int):
                return bool(os.getxattr(path_or_fd, FILE_PROVIDER_XATTR))
            return bool(os.getxattr(path_or_fd, FILE_PROVIDER_XATTR, follow_symlinks=False))
        except OSError as exc:
            if _is_missing_xattr(exc):
                return False
            # On macOS, an unavailable check means the tool cannot promise it will
            # avoid touching a provider-managed placeholder. Fail closed.
            raise SafeTidyError(
                "provider_check_unavailable",
                "Could not verify File Provider status safely.",
                details={"reason": exc.strerror or exc.__class__.__name__},
            ) from exc
    return False


def _windows_placeholder(info: os.stat_result) -> bool:
    attrs = getattr(info, "st_file_attributes", 0)
    recall_on_open = 0x00040000
    recall_on_data_access = 0x00400000
    return bool(attrs & (recall_on_open | recall_on_data_access))


def _check_provider_path(path: Path) -> None:
    if sys.platform == "darwin":
        current = path
        for ancestor in (current, *current.parents):
            if _fileprovider_marker(str(ancestor)):
                raise SafeTidyError(
                    "provider_managed_root",
                    "The selected directory is inside an Apple File Provider location; Safe Tidy will not read or move it.",
                    details={"path": str(ancestor)},
                )


def _has_git_marker(path: Path) -> bool:
    for ancestor in (path, *path.parents):
        marker = ancestor / ".git"
        try:
            os.lstat(marker)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise SafeTidyError(
                "unsafe_root",
                "Could not inspect a possible Git marker in the selected path.",
                details={"reason": exc.strerror or exc.__class__.__name__},
            ) from exc
        else:
            return True
    # Recognize a bare repository without opening or traversing its object store.
    try:
        head = os.lstat(path / "HEAD")
        objects = os.lstat(path / "objects")
        refs = os.lstat(path / "refs")
    except OSError:
        return False
    return stat.S_ISREG(head.st_mode) and stat.S_ISDIR(objects.st_mode) and stat.S_ISDIR(refs.st_mode)


def _resolve_root(raw: str) -> Path:
    if not raw:
        raise SafeTidyError("unsafe_root", "Choose one explicit directory to inspect.")
    candidate = Path(raw).expanduser()
    try:
        given_info = os.lstat(candidate)
    except OSError as exc:
        raise SafeTidyError(
            "root_unavailable",
            "The selected directory does not exist or cannot be inspected.",
            details={"reason": exc.strerror or exc.__class__.__name__},
        ) from exc
    if stat.S_ISLNK(given_info.st_mode):
        raise SafeTidyError("unsafe_root", "The selected directory is a symbolic link.")
    if not stat.S_ISDIR(given_info.st_mode):
        raise SafeTidyError("unsafe_root", "The selected path is not a directory.")
    try:
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise SafeTidyError("root_unavailable", "Could not resolve the selected directory safely.") from exc
    if root == Path(root.anchor):
        raise SafeTidyError("unsafe_root", "A filesystem root is too broad for Safe Tidy.")
    try:
        home = Path.home().resolve(strict=True)
    except OSError:
        home = None
    if home is not None and root == home:
        raise SafeTidyError("unsafe_root", "A home directory is too broad; choose one specific subdirectory.")
    _check_provider_path(root)
    if _has_git_marker(root):
        raise SafeTidyError("unsafe_root", "The selected directory is inside a Git repository.")
    return root


def _stat_path(path: Path) -> os.stat_result:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise SafeTidyError(
            "filesystem_error",
            "Could not inspect a planned path safely.",
            details={"path": str(path), "reason": exc.strerror or exc.__class__.__name__},
        ) from exc


def _snapshot(path: Path, before: os.stat_result) -> dict[str, Any]:
    if not stat.S_ISREG(before.st_mode):
        raise SafeTidyError("not_regular_file", "Only direct regular files can be hashed or moved.")
    if sys.platform == "win32" and _windows_placeholder(before):
        raise SafeTidyError(
            "cloud_placeholder",
            "This file is marked for cloud recall; Safe Tidy will not read or move it.",
            details={"path": path.name},
        )

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SafeTidyError(
            "file_unavailable",
            "Could not open the file without following links.",
            details={"path": path.name, "reason": exc.strerror or exc.__class__.__name__},
        ) from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
            raise SafeTidyError("file_changed", "The file changed while inventory was starting.", details={"path": path.name})
        if _fileprovider_marker(fd):
            raise SafeTidyError(
                "provider_placeholder",
                "The file is marked as Apple File Provider content; Safe Tidy will not read it.",
                details={"path": path.name},
            )
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
        stable = (
            _identity(opened) == _identity(after)
            and opened.st_size == after.st_size
            and opened.st_mtime_ns == after.st_mtime_ns
            and opened.st_ctime_ns == after.st_ctime_ns
        )
        if not stable:
            raise SafeTidyError("file_changed", "The file changed while it was being hashed.", details={"path": path.name})
        path_after = os.lstat(path)
        if _identity(after) != _identity(path_after):
            raise SafeTidyError("file_changed", "The file path changed while it was being hashed.", details={"path": path.name})
        return {
            "path": path.name,
            "size_bytes": int(after.st_size),
            "sha256": digest.hexdigest(),
            "file_id": _identity(after),
        }
    finally:
        os.close(fd)


def _scan(root: Path, *, max_files: int, max_total_bytes: int) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = [
        {
            "code": "direct_files_only",
            "message": "Only direct regular files were inspected; subdirectories were not traversed.",
        },
        {
            "code": "external_references_not_checked",
            "message": "Moves do not update playlists, project files, or other path references.",
        },
    ]
    try:
        names = []
        entry_count = 0
        with os.scandir(root) as entries:
            for entry in entries:
                entry_count += 1
                if entry_count <= max_files:
                    names.append(entry.name)
    except OSError as exc:
        raise SafeTidyError(
            "directory_unreadable",
            "Could not list the selected directory.",
            details={"reason": exc.strerror or exc.__class__.__name__},
        ) from exc

    if entry_count > max_files:
        raise SafeTidyError(
            "scan_limit_exceeded",
            "The directory has more direct entries than this scan limit allows; choose a smaller directory.",
            details={"entry_count": entry_count, "max_files": max_files},
        )
    names.sort()

    total_bytes = 0
    root_device = os.lstat(root).st_dev
    for name in names:
        path = root / name
        if name.startswith("."):
            skipped.append({"path": name, "reason": "hidden_file_or_entry"})
            continue
        try:
            before = os.lstat(path)
        except OSError as exc:
            skipped.append({"path": name, "reason": "stat_failed"})
            warnings.append({"code": "entry_unavailable", "message": f"Skipped {name}: {exc.strerror or 'stat failed'}."})
            continue
        if stat.S_ISLNK(before.st_mode):
            skipped.append({"path": name, "reason": "symlink"})
            continue
        if not stat.S_ISREG(before.st_mode):
            skipped.append({"path": name, "reason": "not_regular_file"})
            continue
        if before.st_dev != root_device:
            skipped.append({"path": name, "reason": "filesystem_boundary"})
            continue
        if sys.platform == "win32" and _windows_placeholder(before):
            skipped.append({"path": name, "reason": "cloud_placeholder"})
            warnings.append({"code": "cloud_placeholder_skipped", "message": f"Skipped cloud-recall file {name}."})
            continue
        if total_bytes + before.st_size > max_total_bytes:
            skipped.append({"path": name, "reason": "byte_limit_reached"})
            warnings.append({"code": "byte_limit_reached", "message": f"Skipped {name}; the configured hash-byte limit was reached."})
            continue
        try:
            record = _snapshot(path, before)
        except SafeTidyError as exc:
            skipped.append({"path": name, "reason": exc.code})
            warnings.append({"code": exc.code, "message": f"Skipped {name}: {exc.message}"})
            continue
        total_bytes += record["size_bytes"]
        files.append(record)

    groups: dict[tuple[str, int], list[str]] = {}
    for record in files:
        key = (record["sha256"], record["size_bytes"])
        groups.setdefault(key, []).append(record["path"])
    duplicate_groups = [
        {"sha256": digest, "size_bytes": size, "paths": sorted(paths)}
        for (digest, size), paths in groups.items()
        if len(paths) > 1
    ]
    duplicate_groups.sort(key=lambda group: (group["sha256"], group["size_bytes"], group["paths"]))
    files.sort(key=lambda record: record["path"])
    skipped.sort(key=lambda record: (record["path"], record["reason"]))
    warnings.sort(key=lambda warning: warning["code"])
    return {
        "files": files,
        "files_scanned": len(files),
        "bytes_hashed": total_bytes,
        "duplicate_groups": duplicate_groups,
        "skipped": skipped,
        "warnings": warnings,
    }


def _extension_folder(name: str) -> str:
    suffix = Path(name).suffix
    extension = suffix[1:].lower() if suffix.startswith(".") else ""
    return extension or "no-extension"


def _validate_leaf(name: str) -> bool:
    windows_base = name.split(".", 1)[0].upper() if isinstance(name, str) else ""
    reserved_windows_names = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    return (
        isinstance(name, str)
        and bool(name)
        and name not in {".", ".."}
        and "/" not in name
        and "\\" not in name
        and "\x00" not in name
        and ":" not in name
        and not name.endswith((".", " "))
        and windows_base not in reserved_windows_names
        and PurePosixPath(name).name == name
    )


def _artifact_path(raw: str, root: Path, *, must_exist: bool) -> Path:
    candidate = Path(raw).expanduser()
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise SafeTidyError(
            "artifact_parent_unavailable",
            "The output directory must already exist.",
            details={"reason": exc.strerror or exc.__class__.__name__},
        ) from exc
    final = parent / candidate.name
    try:
        final.relative_to(root)
    except ValueError:
        pass
    else:
        raise SafeTidyError("artifact_inside_root", "Plan and receipt files must be outside the directory being organized.")
    try:
        info = os.lstat(final)
    except FileNotFoundError:
        if must_exist:
            raise SafeTidyError("artifact_missing", "The selected plan or receipt file does not exist.")
        return final
    except OSError as exc:
        raise SafeTidyError("artifact_unavailable", "Could not inspect the plan or receipt path.") from exc
    if stat.S_ISLNK(info.st_mode):
        raise SafeTidyError("artifact_symlink", "Plan and receipt paths cannot be symbolic links.")
    if not must_exist:
        raise SafeTidyError("output_exists", "The output file already exists; Safe Tidy will not overwrite it.")
    if not stat.S_ISREG(info.st_mode):
        raise SafeTidyError("artifact_invalid", "The selected plan or receipt is not a regular file.")
    return final


def _write_new_json(path: Path, document: dict[str, Any]) -> None:
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise SafeTidyError("output_exists", "The output file appeared before it could be created; nothing was overwritten.") from exc
    except OSError as exc:
        raise SafeTidyError(
            "output_unavailable",
            "Could not create the output file exclusively.",
            details={"reason": exc.strerror or exc.__class__.__name__},
        ) from exc
    owned = os.fstat(fd)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        try:
            current = os.lstat(path)
            if _identity(current) == _identity(owned):
                os.unlink(path)
        except OSError:
            pass
        raise SafeTidyError("output_write_failed", "Could not finish writing the output artifact.") from exc


def _read_json(path: Path, *, root: Path) -> dict[str, Any]:
    info = _stat_path(path)
    if not stat.S_ISREG(info.st_mode):
        raise SafeTidyError("artifact_invalid", "The selected plan or receipt is not a regular file.")
    if info.st_size > 10 * 1024 * 1024:
        raise SafeTidyError("artifact_too_large", "Plan and receipt files must be smaller than 10 MiB.")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(info):
            os.close(fd)
            raise SafeTidyError("artifact_changed", "The plan or receipt changed while it was opened.")
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(10 * 1024 * 1024 + 1)
        if len(raw) > 10 * 1024 * 1024:
            raise SafeTidyError("artifact_too_large", "Plan and receipt files must be smaller than 10 MiB.")
        value = json.loads(raw.decode("utf-8"))
    except SafeTidyError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SafeTidyError("artifact_invalid", "Could not read valid JSON from the selected plan or receipt.") from exc
    after = os.lstat(path)
    if _identity(info) != _identity(after):
        raise SafeTidyError("artifact_changed", "The plan or receipt path changed while it was read.")
    if not isinstance(value, dict):
        raise SafeTidyError("artifact_invalid", "The selected plan or receipt must contain one JSON object.")
    # Confirm that the path was not swapped for a symlink or different file while read.
    after = os.lstat(path)
    if _identity(info) != _identity(after):
        raise SafeTidyError("artifact_changed", "The plan or receipt path changed while it was read.")
    return value


def _check_relative_path(raw: Any, *, source: bool) -> tuple[str, ...]:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw or ":" in raw:
        raise SafeTidyError("invalid_plan", "A planned path is not a safe relative path.")
    parts = tuple(raw.split("/"))
    if any(part in {"", ".", ".."} or not _validate_leaf(part) for part in parts):
        raise SafeTidyError("invalid_plan", "A planned path is not a safe relative path.")
    if source and len(parts) != 1:
        raise SafeTidyError("invalid_plan", "Planned sources must be direct files in the selected directory.")
    if not source and len(parts) != 2:
        raise SafeTidyError("invalid_plan", "Planned destinations must be one extension folder below the selected directory.")
    return parts


def _validate_moves(moves: Any) -> list[dict[str, Any]]:
    if not isinstance(moves, list):
        raise SafeTidyError("invalid_plan", "The plan does not contain a valid move list.")
    result: list[dict[str, Any]] = []
    sources: set[str] = set()
    destinations: set[str] = set()
    for item in moves:
        if not isinstance(item, dict):
            raise SafeTidyError("invalid_plan", "A planned move is malformed.")
        source_parts = _check_relative_path(item.get("source"), source=True)
        destination_parts = _check_relative_path(item.get("destination"), source=False)
        source = source_parts[0]
        destination = "/".join(destination_parts)
        if destination_parts[1] != source or destination_parts[0] != _extension_folder(source):
            raise SafeTidyError("invalid_plan", "A destination does not match the source extension folder.")
        if source in sources or destination in destinations:
            raise SafeTidyError("invalid_plan", "The plan contains a duplicate source or destination.")
        sources.add(source)
        destinations.add(destination)
        size = item.get("size_bytes")
        digest = item.get("sha256")
        file_id = item.get("file_id")
        if not isinstance(size, int) or size < 0 or not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise SafeTidyError("invalid_plan", "A planned move is missing valid size or content-hash evidence.")
        if any(item.get(field, digest) != digest for field in ("source_sha256", "destination_sha256")):
            raise SafeTidyError("invalid_plan", "Source and destination receipt hashes do not match the verified content hash.")
        if not isinstance(file_id, dict) or any(not isinstance(file_id.get(key), int) for key in ("device", "inode", "mode")):
            raise SafeTidyError("invalid_plan", "A planned move is missing filesystem identity evidence.")
        result.append({
            "source": source,
            "destination": destination,
            "size_bytes": size,
            "sha256": digest,
            "file_id": {key: int(file_id[key]) for key in ("device", "inode", "mode")},
        })
    return result


def _check_file_matches(path: Path, move: dict[str, Any], *, code: str) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise SafeTidyError(code, "A planned file is missing.", details={"path": path.name}) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or not _same_identity(info, move["file_id"]):
        raise SafeTidyError(code, "A planned file was replaced or changed identity.", details={"path": path.name})
    try:
        record = _snapshot(path, info)
    except SafeTidyError as exc:
        raise SafeTidyError(code, exc.message, details={"path": path.name, "reason": exc.code}) from exc
    if record["sha256"] != move["sha256"] or record["size_bytes"] != move["size_bytes"]:
        raise SafeTidyError(code, "A planned file's contents changed after the plan was made.", details={"path": path.name})


def _safe_destination_dir(root: Path, folder: str, *, create: bool) -> Path:
    path = root / folder
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if not create:
            raise SafeTidyError("destination_directory_missing", "A planned destination folder is missing.", details={"path": folder})
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise SafeTidyError("destination_directory_unavailable", "Could not create a destination folder safely.", details={"path": folder}) from exc
        info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SafeTidyError("destination_directory_unsafe", "A destination folder is a symlink or is not a directory.", details={"path": folder})
    return path


def _preflight_apply(root: Path, plan: dict[str, Any], moves: list[dict[str, Any]]) -> None:
    if plan.get("conflicts"):
        raise SafeTidyError("plan_has_conflicts", "Resolve the listed destination conflicts and create a fresh plan before applying.")
    if not moves:
        raise SafeTidyError("empty_plan", "The plan has no moves to apply.")
    for move in moves:
        source = root / move["source"]
        destination = root / move["destination"]
        _check_file_matches(source, move, code="source_changed")
        try:
            folder = _safe_destination_dir(root, destination.parent.name, create=False)
        except SafeTidyError as exc:
            if exc.code != "destination_directory_missing":
                raise
            folder = None
        if folder is not None and os.path.lexists(destination):
            raise SafeTidyError("destination_exists", "A destination appeared after planning; nothing was overwritten.", details={"path": move["destination"]})
        if folder is not None:
            folder_info = os.lstat(folder)
            source_info = os.lstat(source)
            if stat.S_ISLNK(folder_info.st_mode) or not stat.S_ISDIR(folder_info.st_mode):
                raise SafeTidyError("destination_directory_unsafe", "A destination folder changed during preflight.", details={"path": destination.parent.name})
            if not stat.S_ISREG(source_info.st_mode) or not _same_identity(source_info, move["file_id"]):
                raise SafeTidyError("source_changed", "A source changed during preflight.", details={"path": move["source"]})
            if folder_info.st_dev != source_info.st_dev:
                raise SafeTidyError("filesystem_not_supported", "The destination is on a different filesystem; safe no-overwrite moves are unavailable.", details={"path": move["destination"]})


def _load_plan(root: Path, raw_path: str) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    path = _artifact_path(raw_path, root, must_exist=True)
    plan = _read_json(path, root=root)
    if plan.get("schema_version") != SCHEMA_VERSION or plan.get("tool") != TOOL or plan.get("kind") != "organization-plan":
        raise SafeTidyError("invalid_plan", "The selected file is not a Safe Tidy organization plan.")
    if plan.get("root") != str(root) or plan.get("scope") != "direct-files-only":
        raise SafeTidyError("invalid_plan", "The plan targets a different directory or unsupported scope.")
    if not _verify_seal(plan, "plan_sha256"):
        raise SafeTidyError("invalid_plan", "The plan checksum is invalid; create a fresh plan before applying.")
    moves = _validate_moves(plan.get("moves"))
    conflicts = plan.get("conflicts")
    if not isinstance(conflicts, list):
        raise SafeTidyError("invalid_plan", "The plan conflict list is malformed.")
    return path, plan, moves


def _make_plan(root: Path, scan: dict[str, Any], *, max_total_bytes: int) -> dict[str, Any]:
    moves: list[dict[str, Any]] = []
    conflicts: list[dict[str, str]] = []
    for record in scan["files"]:
        source = record["path"]
        if not _validate_leaf(source):
            scan["skipped"].append({"path": source, "reason": "unsupported_filename"})
            continue
        folder = _extension_folder(source)
        destination = f"{folder}/{source}"
        if not _validate_leaf(folder):
            scan["skipped"].append({"path": source, "reason": "unsupported_extension"})
            continue
        source_path = root / source
        destination_path = root / folder / source
        try:
            parent_info = os.lstat(root / folder)
        except FileNotFoundError:
            parent_info = None
        if parent_info is not None and (stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode)):
            conflicts.append({"source": source, "destination": destination, "reason": "destination_folder_unsafe"})
            continue
        if os.path.lexists(destination_path):
            conflicts.append({"source": source, "destination": destination, "reason": "destination_exists"})
            continue
        if parent_info is not None:
            current_parent = os.lstat(root / folder)
            current_source = os.lstat(source_path)
            if stat.S_ISLNK(current_parent.st_mode) or not stat.S_ISDIR(current_parent.st_mode):
                conflicts.append({"source": source, "destination": destination, "reason": "destination_folder_unsafe"})
                continue
            if current_parent.st_dev != current_source.st_dev:
                conflicts.append({"source": source, "destination": destination, "reason": "destination_filesystem_differs"})
                continue
        moves.append({
            "source": source,
            "destination": destination,
            "size_bytes": record["size_bytes"],
            "sha256": record["sha256"],
            "file_id": record["file_id"],
        })
    moves.sort(key=lambda item: item["source"])
    conflicts.sort(key=lambda item: (item["source"], item["destination"]))
    scan["skipped"].sort(key=lambda item: (item["path"], item["reason"]))
    document = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "kind": "organization-plan",
        "scope": "direct-files-only",
        "root": str(root),
        "organization": "extension-folders",
        "moves": moves,
        "conflicts": conflicts,
        "skipped": scan["skipped"],
        "limits": {"max_files": DEFAULT_MAX_FILES, "max_total_bytes": max_total_bytes},
    }
    return _seal(document, "plan_sha256")


def _artifact(path: Path, label: str) -> dict[str, str]:
    return {"path": str(path), "label": label, "media_type": "application/json"}


def command_inventory(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    root = _resolve_root(args.root)
    scan = _scan(root, max_files=DEFAULT_MAX_FILES, max_total_bytes=args.max_total_bytes)
    status = "needs_attention" if scan["skipped"] else "ok"
    summary = f"Inspected {scan['files_scanned']} direct files and found {len(scan['duplicate_groups'])} exact-content duplicate groups."
    return 0, _envelope(status, summary, data={"root": str(root), **scan}, warnings=scan["warnings"])


def command_plan(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    root = _resolve_root(args.root)
    output = _artifact_path(args.output, root, must_exist=False)
    scan = _scan(root, max_files=DEFAULT_MAX_FILES, max_total_bytes=args.max_total_bytes)
    plan = _make_plan(root, scan, max_total_bytes=args.max_total_bytes)
    _write_new_json(output, plan)
    warnings = list(scan["warnings"])
    if plan["conflicts"]:
        warnings.append({"code": "destination_conflicts", "message": "Some files have occupied or unsafe destinations and were excluded from moves."})
    status = "needs_attention" if scan["skipped"] or plan["conflicts"] else "ok"
    summary = f"Prepared {len(plan['moves'])} proposed moves and {len(plan['conflicts'])} destination conflicts for review."
    data = {"root": str(root), "moves": plan["moves"], "conflicts": plan["conflicts"], "skipped": plan["skipped"], "plan_sha256": plan["plan_sha256"]}
    return 0, _envelope(status, summary, data=data, artifacts=[_artifact(output, "Organization plan")], warnings=warnings)


def _move_link_then_unlink(source: Path, destination: Path, move: dict[str, Any]) -> None:
    if os.path.lexists(destination):
        raise SafeTidyError("destination_exists", "A destination appeared immediately before apply; nothing was overwritten.", details={"path": str(destination)})
    _check_file_matches(source, move, code="source_changed")
    try:
        os.link(source, destination, follow_symlinks=False)
    except FileExistsError as exc:
        raise SafeTidyError("destination_exists", "A destination appeared immediately before apply; nothing was overwritten.", details={"path": str(destination)}) from exc
    except OSError as exc:
        raise SafeTidyError(
            "filesystem_not_supported",
            "This filesystem could not make an exclusive no-overwrite file link; the source was left in place.",
            details={"reason": exc.strerror or exc.__class__.__name__, "path": str(destination)},
        ) from exc
    try:
        linked = os.lstat(destination)
        if not _same_identity(linked, move["file_id"]):
            raise SafeTidyError("source_changed", "The source changed during apply; the unexpected destination was left for review.")
        _check_file_matches(destination, move, code="source_changed")
        _check_file_matches(source, move, code="source_changed")
        os.unlink(source)
    except Exception:
        # If our exclusive link still points to the planned inode and the source
        # remains, dropping this extra link restores the pre-apply state. If it was
        # replaced, leave it untouched for human review.
        try:
            current = os.lstat(destination)
            if _same_identity(current, move["file_id"]) and os.path.lexists(source):
                os.unlink(destination)
        except OSError:
            pass
        raise


def command_apply(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    root = _resolve_root(args.root)
    plan_path, plan, moves = _load_plan(root, args.plan)
    receipt_path = _artifact_path(args.receipt, root, must_exist=False)
    _preflight_apply(root, plan, moves)
    receipt_moves = [
        {
            **move,
            "source_sha256": move["sha256"],
            "destination_sha256": move["sha256"],
        }
        for move in moves
    ]
    receipt = _seal({
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "kind": "undo-receipt",
        "root": str(root),
        "plan_path": str(plan_path),
        "plan_sha256": plan["plan_sha256"],
        "moves": receipt_moves,
        "undo_model": "content-hash-and-filesystem-identity",
    }, "receipt_sha256")
    _write_new_json(receipt_path, receipt)
    completed = 0
    try:
        for move in moves:
            source = root / move["source"]
            destination = root / move["destination"]
            _safe_destination_dir(root, destination.parent.name, create=True)
            _move_link_then_unlink(source, destination, move)
            completed += 1
    except KeyboardInterrupt:
        return 1, _envelope(
            "needs_attention",
            f"Apply stopped after {completed} moves; use the receipt to undo completed or interrupted moves.",
            data={"error_code": "apply_interrupted", "moves_completed": completed, "receipt": str(receipt_path)},
            artifacts=[_artifact(receipt_path, "Undo receipt")],
            warnings=[{"code": "partial_apply", "message": "Review the receipt and run undo before retrying."}],
        )
    except SafeTidyError as exc:
        return 1, _envelope(
            "needs_attention" if completed else "error",
            f"Apply stopped after {completed} moves: {exc.message}",
            data={"error_code": exc.code, "moves_completed": completed, **exc.details, "receipt": str(receipt_path)},
            artifacts=[_artifact(receipt_path, "Undo receipt")],
            warnings=[{"code": "partial_apply" if completed else "apply_failed", "message": "Use the receipt to inspect or undo any completed move."}],
        )
    except OSError as exc:
        return 1, _envelope(
            "needs_attention" if completed else "error",
            f"Apply stopped after {completed} moves because the filesystem refused an operation.",
            data={"error_code": "filesystem_error", "moves_completed": completed, "reason": exc.strerror or exc.__class__.__name__, "receipt": str(receipt_path)},
            artifacts=[_artifact(receipt_path, "Undo receipt")],
            warnings=[{"code": "partial_apply" if completed else "apply_failed", "message": "Use the receipt to inspect or undo any completed move."}],
        )
    return 0, _envelope(
        "ok",
        f"Applied {completed} reviewed file moves; the receipt can restore their original paths.",
        data={"moves_applied": completed, "plan_sha256": plan["plan_sha256"], "receipt": str(receipt_path)},
        artifacts=[_artifact(receipt_path, "Undo receipt")],
        warnings=[{"code": "empty_folders_remain_after_undo", "message": "Undo restores files and leaves any empty extension folders in place."}],
    )


def _classify_for_undo(root: Path, move: dict[str, Any]) -> str:
    source = root / move["source"]
    destination = root / move["destination"]
    source_exists = os.path.lexists(source)
    destination_exists = os.path.lexists(destination)
    if not source_exists and not destination_exists:
        raise SafeTidyError("both_paths_missing", "Neither the original nor planned destination exists.", details={"source": move["source"], "destination": move["destination"]})

    source_matches = False
    destination_matches = False
    if source_exists:
        _check_file_matches(source, move, code="source_changed")
        source_matches = True
    if destination_exists:
        parent = _safe_destination_dir(root, destination.parent.name, create=False)
        if os.path.islink(destination):
            raise SafeTidyError("target_changed", "The destination is now a symbolic link; undo left it untouched.", details={"path": move["destination"]})
        _check_file_matches(destination, move, code="target_changed")
        destination_matches = True
        parent_info = os.lstat(parent)
        destination_info = os.lstat(destination)
        if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(destination_info.st_mode):
            raise SafeTidyError("target_changed", "The destination changed during undo; it was left untouched.", details={"path": move["destination"]})
        if parent_info.st_dev != destination_info.st_dev:
            raise SafeTidyError("target_changed", "The destination is no longer on its planned filesystem.", details={"path": move["destination"]})

    if source_matches and destination_matches:
        source_info = os.lstat(source)
        destination_info = os.lstat(destination)
        if _identity(source_info) != _identity(destination_info):
            raise SafeTidyError("target_changed", "The original path and destination now contain different files; undo left both untouched.")
        return "duplicate_link"
    if source_matches:
        return "already_original"
    if destination_matches:
        return "moved"
    raise SafeTidyError("target_changed", "Could not verify the planned source or destination.")


def _restore_move(root: Path, move: dict[str, Any]) -> str:
    state = _classify_for_undo(root, move)
    source = root / move["source"]
    destination = root / move["destination"]
    if state == "already_original":
        return state
    if state == "duplicate_link":
        # The source still exists with original bytes and identity. Removing only
        # the verified extra hard-link directory entry reverses an interrupted
        # link-before-unlink step without touching file contents.
        current = os.lstat(destination)
        if not _same_identity(current, move["file_id"]):
            raise SafeTidyError("target_changed", "The destination changed during undo; it was left untouched.")
        os.unlink(destination)
        return "restored"

    if os.path.lexists(source):
        raise SafeTidyError("source_exists", "The original path is occupied; undo will not overwrite it.", details={"path": move["source"]})
    try:
        os.link(destination, source, follow_symlinks=False)
    except FileExistsError as exc:
        raise SafeTidyError("source_exists", "The original path appeared during undo; it was not overwritten.", details={"path": move["source"]}) from exc
    except OSError as exc:
        raise SafeTidyError("filesystem_not_supported", "This filesystem could not restore the file with an exclusive no-overwrite link.", details={"reason": exc.strerror or exc.__class__.__name__}) from exc
    # Keep the exclusive source link if a concurrent change makes the destination
    # unsafe to remove; the receipt can recover the interrupted move later.
    _check_file_matches(source, move, code="target_changed")
    _check_file_matches(destination, move, code="target_changed")
    os.unlink(destination)
    return "restored"


def command_undo(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    root = _resolve_root(args.root)
    receipt_path = _artifact_path(args.receipt, root, must_exist=True)
    receipt = _read_json(receipt_path, root=root)
    if receipt.get("schema_version") != SCHEMA_VERSION or receipt.get("tool") != TOOL or receipt.get("kind") != "undo-receipt":
        raise SafeTidyError("invalid_receipt", "The selected file is not a Safe Tidy undo receipt.")
    if receipt.get("root") != str(root) or receipt.get("undo_model") != "content-hash-and-filesystem-identity":
        raise SafeTidyError("invalid_receipt", "The receipt targets a different directory or unsupported undo model.")
    if not _verify_seal(receipt, "receipt_sha256"):
        raise SafeTidyError("invalid_receipt", "The receipt checksum is invalid; no files were changed.")
    moves = _validate_moves(receipt.get("moves"))
    if len(moves) != len(receipt.get("moves", [])):
        raise SafeTidyError("invalid_receipt", "The receipt move list is malformed.")

    restored = 0
    already_original = 0
    try:
        for move in reversed(moves):
            result = _restore_move(root, move)
            if result == "already_original":
                already_original += 1
            else:
                restored += 1
    except KeyboardInterrupt:
        return 1, _envelope(
            "needs_attention",
            f"Undo stopped after restoring {restored} files; rerun undo with the same receipt to continue.",
            data={"error_code": "undo_interrupted", "files_restored": restored, "files_already_original": already_original},
            artifacts=[_artifact(receipt_path, "Undo receipt")],
        )
    except SafeTidyError as exc:
        return 1, _envelope(
            "needs_attention",
            f"Undo stopped after restoring {restored} files: {exc.message}",
            data={"error_code": exc.code, "files_restored": restored, "files_already_original": already_original, **exc.details},
            artifacts=[_artifact(receipt_path, "Undo receipt")],
            warnings=[{"code": "undo_incomplete", "message": "The receipt remains available; review the reported paths before retrying."}],
        )
    except OSError as exc:
        return 1, _envelope(
            "needs_attention",
            f"Undo stopped after restoring {restored} files because the filesystem refused an operation.",
            data={"error_code": "filesystem_error", "files_restored": restored, "files_already_original": already_original, "reason": exc.strerror or exc.__class__.__name__},
            artifacts=[_artifact(receipt_path, "Undo receipt")],
            warnings=[{"code": "undo_incomplete", "message": "The receipt remains available; review the reported paths before retrying."}],
        )
    return 0, _envelope(
        "ok",
        f"Restored {restored} files to their original paths; {already_original} were already there.",
        data={"files_restored": restored, "files_already_original": already_original, "receipt": str(receipt_path)},
        artifacts=[_artifact(receipt_path, "Undo receipt")],
        warnings=[{"code": "empty_folders_remain", "message": "Empty extension folders remain after undo."}],
    )


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        _emit(_envelope("error", "Invalid command arguments.", data={"error_code": "invalid_arguments", "message": message}))
        self.exit(2)


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description="Inventory, preview, and reverse direct-file organization in one explicit directory.")
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=JsonArgumentParser)

    inventory = subparsers.add_parser("inventory", help="Hash direct regular files and report exact-content duplicates.")
    inventory.add_argument("--root", required=True, help="One existing directory; recursive scans and home-directory roots are refused.")
    inventory.add_argument("--max-total-bytes", type=_positive_int, default=DEFAULT_MAX_TOTAL_BYTES, help=f"Maximum bytes to hash (default: {DEFAULT_MAX_TOTAL_BYTES}).")
    inventory.set_defaults(handler=command_inventory)

    plan = subparsers.add_parser("plan", help="Write a reviewable extension-folder move plan without moving files.")
    plan.add_argument("--root", required=True, help="One existing directory to inspect.")
    plan.add_argument("--output", required=True, help="New JSON plan path outside the selected directory.")
    plan.add_argument("--max-total-bytes", type=_positive_int, default=DEFAULT_MAX_TOTAL_BYTES, help=f"Maximum bytes to hash (default: {DEFAULT_MAX_TOTAL_BYTES}).")
    plan.set_defaults(handler=command_plan)

    apply = subparsers.add_parser("apply", help="Apply one reviewed, unchanged plan and create an undo receipt first.")
    apply.add_argument("--root", required=True, help="The exact directory recorded in the plan.")
    apply.add_argument("--plan", required=True, help="The reviewed JSON plan produced by the plan command.")
    apply.add_argument("--receipt", required=True, help="New JSON receipt path outside the selected directory.")
    apply.set_defaults(handler=command_apply)

    undo = subparsers.add_parser("undo", help="Restore files using a Safe Tidy receipt; never overwrite changed targets.")
    undo.add_argument("--root", required=True, help="The exact directory recorded in the receipt.")
    undo.add_argument("--receipt", required=True, help="The JSON undo receipt produced by apply.")
    undo.set_defaults(handler=command_undo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code, envelope = args.handler(args)
    except SafeTidyError as exc:
        envelope = _envelope(
            "error",
            exc.message,
            data={"error_code": exc.code, **exc.details},
        )
        code = 1
    except (OSError, ValueError) as exc:
        envelope = _envelope(
            "error",
            "The operation could not be completed safely.",
            data={"error_code": "filesystem_error", "message": getattr(exc, "strerror", None) or exc.__class__.__name__},
        )
        code = 1
    _emit(envelope)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
