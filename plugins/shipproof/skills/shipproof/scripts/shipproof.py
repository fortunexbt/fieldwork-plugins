#!/usr/bin/env python3
"""Verify selected release responses and create metadata-only repo handoffs."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any


TOOL = "shipproof"
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
MAX_ALLOWED_BYTES = 10 * 1024 * 1024
MAX_HASHED_FILES = 5000
MAX_HASHED_BYTES = 100 * 1024 * 1024
MAX_HANDOFF_FILE_BYTES = 20 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_REDIRECTS = 5

SAFE_RESPONSE_HEADERS = {
    "age",
    "cache-control",
    "content-length",
    "content-encoding",
    "content-type",
    "date",
    "etag",
    "expires",
    "last-modified",
    "location",
    "server",
    "vary",
    "x-cache",
    "x-content-type-options",
}

SOURCE_SUFFIXES = {
    ".bash", ".c", ".cc", ".cfg", ".cjs", ".cmake", ".cpp", ".cs", ".css",
    ".go", ".h", ".hpp", ".htm", ".html", ".ini", ".java", ".js", ".json",
    ".jsonc", ".jsx", ".kt", ".lua", ".md", ".mjs", ".mts", ".php", ".py",
    ".pyi", ".rb", ".rs", ".rst", ".sass", ".scala", ".scss", ".sh", ".sql",
    ".swift", ".svelte", ".toml", ".ts", ".tsx", ".txt", ".vue", ".xml", ".yaml",
    ".yml", ".zsh",
}
SOURCE_BASENAMES = {
    "cargo.toml", "cmakelists.txt", "dockerfile", "go.mod", "makefile", "meson.build",
    "package.json", "pyproject.toml", "readme", "readme.md", "readme.rst",
}
SENSITIVE_BASENAMES = {
    ".env", ".npmrc", ".pypirc", ".netrc", ".git-credentials", "credentials",
    "secrets", "secret", "id_rsa", "id_ed25519", "known_hosts",
}
SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".tfstate", ".tfvars"}
GENERATED_PARTS = {
    ".git", ".venv", "venv", "node_modules", "vendor", "dist", "build", "coverage",
    "__pycache__", "target", ".next", ".turbo",
}
SAFE_COMMAND_BASES = {"build", "test", "lint", "check", "typecheck", "format", "dev", "preview"}


class InputError(Exception):
    """A supplied argument or manifest is invalid."""


class ExecutionError(Exception):
    """An operation could not be completed."""


def envelope(status: str, summary: str, data: dict[str, Any] | None = None,
             artifacts: list[dict[str, str]] | None = None,
             warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "tool": TOOL,
        "status": status,
        "summary": summary,
        "data": data or {},
        "artifacts": artifacts or [],
        "warnings": warnings or [],
    }


def safe_url(value: str) -> str:
    """Keep the route useful while omitting query strings and fragments."""
    parsed = urllib.parse.urlsplit(value)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        host = f"{host}:{port}"
    return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path or "/", "", ""))


def validate_url(value: Any, label: str = "url") -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{label} must be a non-empty HTTP or HTTPS URL.")
    if len(value) > 4096:
        raise InputError(f"{label} is longer than the 4096-character limit.")
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise InputError(f"{label} is malformed.") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise InputError(f"{label} must use HTTP or HTTPS and include a host.")
    if parsed.username is not None or parsed.password is not None:
        raise InputError(f"{label} must not contain username or password credentials.")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise InputError(f"{label} has an invalid port.") from exc
    if any(ord(char) < 32 for char in value):
        raise InputError(f"{label} contains a control character.")
    return value


def parse_expected_headers(value: Any, label: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InputError(f"{label} must be an object of safe response header names and values.")
    result: dict[str, str] = {}
    for key, expected in value.items():
        name = str(key).strip().lower()
        if name not in SAFE_RESPONSE_HEADERS or name == "location":
            raise InputError(f"{label} contains an unsupported or sensitive header name.")
        if not isinstance(expected, str) or "\r" in expected or "\n" in expected:
            raise InputError(f"{label}.{key} must be a single-line string.")
        result[name] = expected.strip()
    return result


def safe_headers(headers: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in SAFE_RESPONSE_HEADERS:
        value = headers.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if name == "location":
            try:
                text = safe_url(text)
            except Exception:
                text = "[unavailable]"
        result[name] = text[:500]
    return dict(sorted(result.items()))


def normalize_resource(raw: Any, index: int, base_dir: Path) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise InputError(f"resources[{index}] must be an object.")
    name = raw.get("name", f"resource {index + 1}")
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise InputError(f"resources[{index}].name must be a short non-empty string.")
    url = validate_url(raw.get("url"), f"resources[{index}].url")
    asset_value = raw.get("asset")
    if not isinstance(asset_value, str) or not asset_value.strip():
        raise InputError(f"resources[{index}].asset must be a path to an expected local asset.")
    asset_path = Path(asset_value).expanduser()
    if not asset_path.is_absolute():
        asset_path = base_dir / asset_path
    expected_sha = raw.get("sha256")
    if expected_sha is not None:
        if not isinstance(expected_sha, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha) is None:
            raise InputError(f"resources[{index}].sha256 must contain 64 hexadecimal characters.")
        expected_sha = expected_sha.lower()
    markers = raw.get("markers", [])
    if not isinstance(markers, list) or len(markers) > 100 or any(not isinstance(marker, str) or not marker for marker in markers):
        raise InputError(f"resources[{index}].markers must be a list of non-empty UTF-8 strings.")
    if any("\x00" in marker for marker in markers):
        raise InputError(f"resources[{index}].markers cannot contain NUL characters.")
    expected_status = raw.get("expected_status", 200)
    if not isinstance(expected_status, int) or not 100 <= expected_status <= 599:
        raise InputError(f"resources[{index}].expected_status must be an HTTP status from 100 to 599.")
    headers = parse_expected_headers(raw.get("headers"), f"resources[{index}].headers")
    return {
        "name": name.strip(),
        "url": url,
        "asset_path": asset_path,
        "expected_sha256": expected_sha,
        "markers": markers,
        "expected_status": expected_status,
        "expected_headers": headers,
    }


def load_manifest(path: Path) -> list[dict[str, Any]]:
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_METADATA_BYTES:
            raise InputError("Manifest must be a regular UTF-8 JSON file no larger than 1 MiB.")
        with path.open("rb") as source:
            raw_payload = source.read(MAX_METADATA_BYTES + 1)
        if len(raw_payload) > MAX_METADATA_BYTES:
            raise InputError("Manifest must be a regular UTF-8 JSON file no larger than 1 MiB.")
        payload = json.loads(raw_payload.decode("utf-8"))
    except FileNotFoundError as exc:
        raise InputError(f"Manifest file does not exist: {path.name}.") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputError(f"Manifest cannot be read as UTF-8 JSON: {path.name}.") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise InputError("Manifest must be a JSON object with schema_version 1.")
    raw_resources = payload.get("resources")
    if not isinstance(raw_resources, list) or not raw_resources:
        raise InputError("Manifest resources must be a non-empty list.")
    if len(raw_resources) > 200:
        raise InputError("Manifest may contain at most 200 resources per run.")
    return [normalize_resource(raw, index, path.expanduser().resolve().parent)
            for index, raw in enumerate(raw_resources)]


def read_expected_asset(path: Path, max_bytes: int) -> tuple[bytes | None, str | None]:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None, "expected_asset_missing"
    except OSError:
        return None, "expected_asset_unreadable"
    if not stat.S_ISREG(info.st_mode):
        return None, "expected_asset_not_regular_file"
    if info.st_size > max_bytes:
        return None, "expected_asset_exceeds_limit"
    try:
        with path.open("rb") as source:
            data = source.read(max_bytes + 1)
    except OSError:
        return None, "expected_asset_unreadable"
    if len(data) > max_bytes:
        return None, "expected_asset_exceeds_limit"
    return data, None


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def fetch_bounded(url: str, timeout: float, max_bytes: int) -> dict[str, Any]:
    opener = urllib.request.build_opener(NoRedirectHandler())
    current = url
    redirects: list[dict[str, Any]] = []
    seen = {current}
    for hop in range(MAX_REDIRECTS + 1):
        request = urllib.request.Request(
            current,
            headers={"Accept": "*/*", "Accept-Encoding": "identity", "User-Agent": "ShipProof/1.0"},
            method="GET",
        )
        response = None
        status: int | None = None
        response_headers: Any = {}
        body: bytes | None = None

        def response_result(payload: bytes | None = None, reason: str | None = None) -> dict[str, Any]:
            result = {
                "final_url": safe_url(current), "status_code": status, "headers": safe_headers(response_headers),
                "redirects": redirects, "body": payload,
            }
            if reason is not None:
                result["unverifiable_reason"] = reason
            return result

        try:
            response = opener.open(request, timeout=timeout)
            status = int(response.status)
            response_headers = response.headers
            if 200 <= status < 300:
                body = response.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            response_headers = exc.headers or {}
            exc.close()
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            reason = "network_error"
            if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError):
                reason = "timeout"
            return response_result(reason=reason)
        finally:
            if response is not None:
                response.close()

        location = response_headers.get("location") if response_headers else None
        if status in {301, 302, 303, 307, 308} and location:
            if hop >= MAX_REDIRECTS:
                return response_result(reason="redirect_limit_exceeded")
            destination = urllib.parse.urljoin(current, str(location))
            try:
                validate_url(destination, "redirect URL")
            except InputError:
                redirects.append({"status_code": status, "from": safe_url(current), "to": "[blocked]"})
                return response_result(reason="unsafe_redirect_target")
            if urllib.parse.urlsplit(current).scheme == "https" and urllib.parse.urlsplit(destination).scheme == "http":
                redirects.append({"status_code": status, "from": safe_url(current), "to": safe_url(destination)})
                return response_result(reason="https_to_http_redirect_blocked")
            redirects.append({"status_code": status, "from": safe_url(current), "to": safe_url(destination)})
            if destination in seen:
                return response_result(reason="redirect_loop")
            seen.add(destination)
            current = destination
            continue

        if status is None:
            return response_result(reason="no_http_response")
        if not (200 <= status < 300):
            return response_result()
        if body is None:
            return response_result(reason="response_body_unavailable")
        if len(body) > max_bytes:
            return response_result(reason="response_exceeds_limit")
        content_encoding = str(response_headers.get("content-encoding", "identity")).strip().lower()
        if content_encoding not in {"", "identity"}:
            return response_result(reason="unsupported_content_encoding")
        return response_result(body)
    return {
        "final_url": safe_url(current), "status_code": None, "headers": {}, "redirects": redirects,
        "body": None, "unverifiable_reason": "redirect_limit_exceeded",
    }


def evaluate_resource(resource: dict[str, Any], timeout: float, max_bytes: int) -> dict[str, Any]:
    expected_body, local_issue = read_expected_asset(resource["asset_path"], max_bytes)
    fetched = fetch_bounded(resource["url"], timeout, max_bytes)
    checks: list[dict[str, Any]] = []
    issues: list[str] = []
    remote_body = fetched["body"]

    actual_status = fetched["status_code"]
    if actual_status is None:
        checks.append({"check": "http_status", "result": "unverifiable", "expected": resource["expected_status"], "actual": None})
    else:
        status_match = actual_status == resource["expected_status"]
        checks.append({
            "check": "http_status", "result": "match" if status_match else "mismatch",
            "expected": resource["expected_status"], "actual": actual_status,
        })

    if expected_body is None:
        checks.append({"check": "expected_asset", "result": "unverifiable", "reason": local_issue})
        issues.append(local_issue or "expected_asset_unavailable")
    else:
        local_hash = hashlib.sha256(expected_body).hexdigest()
        pinned_hash = resource["expected_sha256"]
        if pinned_hash is not None:
            pinned_match = local_hash == pinned_hash
            checks.append({
                "check": "manifest_sha256", "result": "match" if pinned_match else "mismatch",
                "expected": pinned_hash, "actual": local_hash,
            })
        if remote_body is None:
            checks.append({"check": "sha256", "result": "unverifiable", "expected": local_hash, "actual": None})
            issues.append(fetched.get("unverifiable_reason", "remote_body_unavailable"))
        else:
            actual_hash = hashlib.sha256(remote_body).hexdigest()
            hash_match = actual_hash == local_hash
            checks.append({
                "check": "sha256", "result": "match" if hash_match else "mismatch",
                "expected": local_hash, "actual": actual_hash,
            })

    for marker in resource["markers"]:
        marker_bytes = marker.encode("utf-8")
        if expected_body is None:
            local_result = "unverifiable"
        else:
            local_result = "match" if marker_bytes in expected_body else "mismatch"
        if remote_body is None:
            remote_result = "unverifiable"
        else:
            remote_result = "match" if marker_bytes in remote_body else "mismatch"
        result = "mismatch" if "mismatch" in {local_result, remote_result} else (
            "unverifiable" if "unverifiable" in {local_result, remote_result} else "match"
        )
        checks.append({
            "check": "marker", "marker": marker, "result": result,
            "expected_asset": local_result, "served_response": remote_result,
        })

    for name, expected in resource["expected_headers"].items():
        actual = fetched["headers"].get(name)
        if actual is None or actual.lower() != expected.lower():
            checks.append({"check": "header", "name": name, "result": "mismatch", "expected": expected, "actual": actual})
        else:
            checks.append({"check": "header", "name": name, "result": "match", "expected": expected, "actual": actual})

    if fetched.get("unverifiable_reason"):
        issues.append(fetched["unverifiable_reason"])
        checks.append({"check": "response_body", "result": "unverifiable", "reason": fetched["unverifiable_reason"]})

    results = {check["result"] for check in checks}
    if "mismatch" in results:
        status = "mismatch"
    elif "unverifiable" in results:
        status = "unverifiable"
    else:
        status = "match"
    return {
        "name": resource["name"],
        "result": status,
        "request_url": safe_url(resource["url"]),
        "final_url": fetched["final_url"],
        "status_code": actual_status,
        "headers": fetched["headers"],
        "redirects": fetched["redirects"],
        "checks": checks,
        "reasons": sorted(set(issues)),
    }


def run_verification(resources: list[dict[str, Any]], timeout: float, max_bytes: int) -> dict[str, Any]:
    results = [evaluate_resource(resource, timeout, max_bytes) for resource in resources]
    all_match = all(item["result"] == "match" for item in results)
    if all_match:
        status = "ok"
        summary = f"All {len(results)} release resource(s) matched the selected local assets."
    else:
        status = "needs_attention"
        mismatch = sum(item["result"] == "mismatch" for item in results)
        unavailable = sum(item["result"] == "unverifiable" for item in results)
        summary = f"{mismatch} resource(s) mismatched and {unavailable} could not be fully verified out of {len(results)}."
    return envelope(status, summary, {"resources": results})


def validate_include_patterns(patterns: list[str]) -> list[str]:
    result: list[str] = []
    for pattern in patterns:
        pattern = pattern.strip().replace("\\", "/")
        if not pattern or pattern.startswith("/") or ".." in PurePosixPath(pattern).parts:
            raise InputError("--include patterns must be non-empty repository-relative globs without '..'.")
        result.append(pattern)
    return result


def is_sensitive_repo_path(value: str) -> bool:
    path = PurePosixPath(value)
    parts = [part.lower() for part in path.parts]
    if any(part in GENERATED_PARTS for part in parts):
        return True
    for part in parts:
        lower = part.lower()
        if lower in SENSITIVE_BASENAMES or lower.startswith(".env"):
            return True
        if Path(lower).suffix in SENSITIVE_SUFFIXES:
            return True
        if lower.endswith((".lock", ".pem", ".key", ".tfvars", ".tfstate")):
            return True
        if any(term in lower for term in ("credential", "secret", "password", "token")):
            return True
    return False


def is_source_candidate(value: str) -> bool:
    path = PurePosixPath(value)
    return path.name.lower() in SOURCE_BASENAMES or path.suffix.lower() in SOURCE_SUFFIXES


def matches_patterns(value: str, patterns: list[str]) -> bool:
    path = PurePosixPath(value)
    for pattern in patterns:
        candidates = [pattern]
        if "**/" in pattern:
            candidates.append(pattern.replace("**/", ""))
        target = value if "/" in pattern else path.name
        if any(fnmatch.fnmatchcase(target, candidate) for candidate in candidates):
            return True
    return False


def git_command(repo: Path, *args: str) -> bytes:
    command = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args]
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        completed = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=environment, timeout=15)
    except FileNotFoundError as exc:
        raise ExecutionError("Git is required for Repo Handoff but was not found on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ExecutionError("Git inspection timed out.") from exc
    except subprocess.CalledProcessError as exc:
        raise ExecutionError("Git could not inspect the supplied checkout.") from exc
    return completed.stdout


def decode_git_path(raw: bytes) -> str:
    return os.fsdecode(raw).replace("\\", "\\\\").replace("\x00", "")


def git_dirty_paths(repo: Path) -> dict[str, str]:
    raw = git_command(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = raw.split(b"\0")
    dirty: dict[str, str] = {}
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        status = entry[:2].decode("ascii", "replace")
        path = decode_git_path(entry[3:])
        dirty[path] = status.strip() or "??"
        if "R" in status or "C" in status:
            if index < len(entries):
                old_path = decode_git_path(entries[index])
                dirty[old_path] = "renamed_from"
                index += 1
    return dirty


def git_build_commands(repo: Path, tracked_paths: set[str]) -> list[str]:
    commands: set[str] = set()
    if "package.json" in tracked_paths:
        path = repo / "package.json"
        try:
            metadata = path.lstat()
            if stat.S_ISREG(metadata.st_mode) and metadata.st_size <= MAX_METADATA_BYTES:
                with path.open("rb") as source:
                    raw_package = source.read(MAX_METADATA_BYTES + 1)
                package = json.loads(raw_package.decode("utf-8")) if len(raw_package) <= MAX_METADATA_BYTES else {}
                scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
                manager = package.get("packageManager", "npm") if isinstance(package, dict) else "npm"
                manager = str(manager).split("@", 1)[0]
                if manager not in {"npm", "pnpm", "yarn", "bun"}:
                    manager = "npm"
                if isinstance(scripts, dict):
                    for name in scripts:
                        if (isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9:_-]{0,40}", name)
                                and name.split(":", 1)[0].split("-", 1)[0] in SAFE_COMMAND_BASES):
                            commands.add(f"{manager} run {name}")
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    if "Makefile" in tracked_paths or "makefile" in tracked_paths:
        make_path = repo / ("Makefile" if "Makefile" in tracked_paths else "makefile")
        try:
            metadata = make_path.lstat()
            if stat.S_ISREG(metadata.st_mode) and metadata.st_size <= MAX_METADATA_BYTES:
                with make_path.open("rb") as source:
                    raw_makefile = source.read(MAX_METADATA_BYTES + 1)
                content = raw_makefile.decode("utf-8", "replace") if len(raw_makefile) <= MAX_METADATA_BYTES else ""
                for line in content.splitlines():
                    match = re.match(r"^([A-Za-z0-9_.-]{1,60}):(?:[^=]|$)", line)
                    target = match.group(1).lower() if match else ""
                    if (match and target.split("-", 1)[0] in SAFE_COMMAND_BASES
                            and target not in {"all", ".phony"}):
                        commands.add(f"make {match.group(1)}")
        except OSError:
            pass
    if "pyproject.toml" in tracked_paths:
        project_file = repo / "pyproject.toml"
        try:
            metadata = project_file.lstat()
            if stat.S_ISREG(metadata.st_mode) and metadata.st_size <= MAX_METADATA_BYTES:
                with project_file.open("rb") as source:
                    raw_pyproject = source.read(MAX_METADATA_BYTES + 1)
                config = tomllib.loads(raw_pyproject.decode("utf-8")) if len(raw_pyproject) <= MAX_METADATA_BYTES else {}
                if isinstance(config.get("build-system"), dict):
                    commands.add("python -m build")
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            pass
        if any(PurePosixPath(path).name.startswith(("test_", "tests")) and path.endswith(".py")
               for path in tracked_paths):
            commands.add("python -m pytest")
    if "go.mod" in tracked_paths:
        commands.update({"go test ./...", "go build ./..."})
    if "Cargo.toml" in tracked_paths:
        commands.update({"cargo test", "cargo build"})
    return sorted(commands)


def markdown_cell(value: str) -> str:
    clean = "".join(char if char >= " " and char != "\x7f" else " " for char in value)
    return clean.replace("|", "\\|").replace("`", "'").strip()


def collect_handoff(repo_arg: Path, includes: list[str]) -> tuple[dict[str, Any], str]:
    repo_arg = repo_arg.expanduser()
    if not repo_arg.exists() or not repo_arg.is_dir():
        raise InputError("--repo must name an existing Git checkout directory.")
    try:
        root_raw = git_command(repo_arg, "rev-parse", "--show-toplevel")
        root = Path(os.fsdecode(root_raw).strip()).resolve()
    except ExecutionError as exc:
        if "Git is required" in str(exc):
            raise
        raise InputError("--repo must be inside an existing Git checkout.") from exc
    revision = os.fsdecode(git_command(root, "rev-parse", "--verify", "HEAD")).strip()
    branch_raw = git_command(root, "rev-parse", "--abbrev-ref", "HEAD")
    branch_value = os.fsdecode(branch_raw).strip()
    branch = None if branch_value == "HEAD" else branch_value
    dirty = git_dirty_paths(root)
    tracked_raw = git_command(root, "ls-files", "-z")
    tracked_paths = [decode_git_path(item) for item in tracked_raw.split(b"\0") if item]
    tracked_set = set(tracked_paths)
    source_paths = [value for value in tracked_paths if is_source_candidate(value) and not is_sensitive_repo_path(value)]
    if includes:
        source_paths = [value for value in source_paths if matches_patterns(value, includes)]
    source_paths.sort()
    if len(source_paths) > MAX_HASHED_FILES:
        raise InputError(f"Selected source set exceeds the {MAX_HASHED_FILES}-file handoff limit.")

    files: list[dict[str, Any]] = []
    skipped = {"too_large": 0, "unreadable": 0, "symlink": 0, "outside_checkout": 0, "budget": 0}
    total_bytes = 0
    for relative in source_paths:
        posix = PurePosixPath(relative)
        if posix.is_absolute() or ".." in posix.parts:
            skipped["outside_checkout"] += 1
            continue
        file_path = root.joinpath(*posix.parts)
        file_status = dirty.get(relative, "clean")
        try:
            metadata = file_path.lstat()
        except FileNotFoundError:
            files.append({"path": relative, "status": "deleted", "bytes": None, "sha256": None})
            continue
        except OSError:
            skipped["unreadable"] += 1
            continue
        if stat.S_ISLNK(metadata.st_mode):
            skipped["symlink"] += 1
            continue
        if not stat.S_ISREG(metadata.st_mode):
            skipped["unreadable"] += 1
            continue
        if metadata.st_size > MAX_HANDOFF_FILE_BYTES:
            skipped["too_large"] += 1
            continue
        if total_bytes + metadata.st_size > MAX_HASHED_BYTES:
            skipped["budget"] += 1
            continue
        digest = hashlib.sha256()
        size = 0
        try:
            with file_path.open("rb") as handle:
                while True:
                    chunk = handle.read(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_HANDOFF_FILE_BYTES or total_bytes + size > MAX_HASHED_BYTES:
                        raise OverflowError
                    digest.update(chunk)
        except OverflowError:
            skipped["budget"] += 1
            continue
        except OSError:
            skipped["unreadable"] += 1
            continue
        total_bytes += size
        files.append({
            "path": relative,
            "status": "clean" if file_status == "clean" else "modified",
            "bytes": size,
            "sha256": digest.hexdigest(),
        })

    command_list = git_build_commands(root, tracked_set)
    dirty_entries = [{"path": path, "status": status} for path, status in sorted(dirty.items())]
    repo_label = markdown_cell(root.name or "repository")
    payload = {
        "schema_version": 1,
        "tool": TOOL,
        "kind": "repo_handoff_manifest",
        "repository": repo_label,
        "revision": revision,
        "branch": branch,
        "dirty_paths": dirty_entries,
        "commands": command_list,
        "source_files": files,
        "source_file_count": len(files),
        "skipped": skipped,
        "limits": {
            "tracked_files_only": True,
            "untracked_files_included": False,
            "file_contents_included": False,
            "uncommitted_changes_backed_up": False,
        },
    }
    lines = [
        f"# Repository handoff: {repo_label}",
        "",
        f"- Revision: `{revision}`",
        f"- Branch: `{markdown_cell(branch or 'detached HEAD')}`",
        f"- Dirty paths: {len(dirty_entries)}",
        f"- Selected tracked source files: {len(files)}",
        "",
        "This handoff records tracked source-file metadata and SHA-256 hashes. It does not include file contents, untracked files, or a backup of uncommitted changes. Modified tracked files are hashed from the current working tree; dirty paths are listed separately.",
        "",
        "## Project commands",
        "",
    ]
    lines.extend(f"- `{markdown_cell(command)}`" for command in command_list) if command_list else lines.append("- No recognized build or test command was found.")
    lines.extend(["", "## Dirty paths", ""])
    if dirty_entries:
        lines.extend(["| Status | Path |", "| --- | --- |"])
        lines.extend(f"| `{markdown_cell(item['status'])}` | `{markdown_cell(item['path'])}` |" for item in dirty_entries)
    else:
        lines.append("Working tree is clean.")
    lines.extend(["", "## Selected tracked source files", ""])
    if files:
        lines.extend(["| Status | Bytes | SHA-256 | Path |", "| --- | ---: | --- | --- |"])
        for item in files:
            lines.append(
                f"| `{markdown_cell(item['status'])}` | {item['bytes'] if item['bytes'] is not None else '—'} | "
                f"`{item['sha256'] or '—'}` | `{markdown_cell(item['path'])}` |"
            )
    else:
        lines.append("No selected tracked source files met the handoff rules.")
    lines.extend(["", "## Skipped files", ""])
    skipped_summary = ", ".join(f"{key}: {value}" for key, value in skipped.items() if value)
    lines.append(skipped_summary if skipped_summary else "None.")
    lines.append("")
    return payload, "\n".join(lines)


def output_path_for(out_dir_value: str, root_label: str) -> Path:
    return Path(out_dir_value).expanduser().resolve(strict=False)


def ensure_output_outside_repo(out_dir: Path, repo_root: Path) -> None:
    try:
        if os.path.commonpath([str(out_dir), str(repo_root)]) == str(repo_root):
            raise InputError("--out-dir must be outside the inspected checkout so the handoff does not change it.")
    except ValueError:
        pass


def write_handoff(out_dir: Path, manifest: dict[str, Any], report: str) -> list[dict[str, str]]:
    targets = [
        (out_dir / "handoff.md", report, "text/markdown", "Repository handoff"),
        (out_dir / "manifest.json", json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", "application/json", "Source metadata manifest"),
    ]
    existing = [path.name for path, _, _, _ in targets if path.exists()]
    if existing:
        raise ExecutionError(f"Refusing to overwrite existing handoff output(s): {', '.join(existing)}.")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExecutionError("Could not create the requested handoff output directory.") from exc
    created: list[Path] = []
    try:
        for path, content, _, _ in targets:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created.append(path)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
    except OSError as exc:
        for path in created:
            try:
                path.unlink()
            except OSError:
                pass
        raise ExecutionError("Could not write the complete handoff receipt; partial files were removed when possible.") from exc
    return [
        {"path": str(path), "label": label, "media_type": media_type}
        for path, _, media_type, label in targets
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shipproof",
        description="Verify a deployed response against explicit local release assets, or create a metadata-only Git handoff.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="Compare one or more supplied URLs with local expected assets.")
    source = verify.add_mutually_exclusive_group(required=True)
    source.add_argument("--asset", help="Local expected asset for a single --url.")
    source.add_argument("--manifest", help="UTF-8 JSON manifest of expected assets and resource URLs.")
    verify.add_argument("--url", help="Exact URL of the asset response; required with --asset.")
    verify.add_argument("--marker", action="append", default=[], help="UTF-8 marker expected in both local and served bytes (repeatable).")
    verify.add_argument("--header", action="append", default=[], metavar="NAME:VALUE", help="Safe response header expectation (repeatable).")
    verify.add_argument("--timeout", type=float, default=8.0, help="Per-request timeout in seconds (0.1 to 30; default 8).")
    verify.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="Maximum bytes read per local/remote asset (1024 to 10485760).")

    handoff = commands.add_parser("handoff", help="Inspect an explicit Git checkout and write metadata-only receipts.")
    handoff.add_argument("--repo", required=True, help="The Git checkout to inspect; no scripts are executed.")
    handoff.add_argument("--out-dir", required=True, help="Directory for handoff.md and manifest.json, outside the checkout.")
    handoff.add_argument("--include", action="append", default=[], help="Limit source selection with a repository-relative glob (repeatable).")
    handoff.add_argument("--preview", action="store_true", help="Show the exact output plan without writing files.")
    return parser


def parse_headers(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values:
        if ":" not in item:
            raise InputError("--header must use NAME:VALUE form.")
        name, value = item.split(":", 1)
        parsed = parse_expected_headers({name: value}, "--header")
        result.update(parsed)
    return result


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, allow_nan=False))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            if not 0.1 <= args.timeout <= 30:
                raise InputError("--timeout must be between 0.1 and 30 seconds.")
            if not 1024 <= args.max_bytes <= MAX_ALLOWED_BYTES:
                raise InputError(f"--max-bytes must be between 1024 and {MAX_ALLOWED_BYTES}.")
            if args.asset is not None:
                if not args.url:
                    raise InputError("--url is required with --asset.")
                url = validate_url(args.url)
                headers = parse_headers(args.header)
                markers = args.marker
                if any(not marker or "\x00" in marker for marker in markers):
                    raise InputError("--marker values must be non-empty UTF-8 strings without NUL.")
                resource = {
                    "name": "release asset", "url": url, "asset_path": Path(args.asset).expanduser(),
                    "expected_sha256": None, "markers": markers, "expected_status": 200,
                    "expected_headers": headers,
                }
                result = run_verification([resource], args.timeout, args.max_bytes)
                emit(result)
                return 0
            if args.manifest is not None and (args.url or args.marker or args.header):
                raise InputError("--url, --marker, and --header are used with --asset; use the manifest for multiple resources.")
            if args.manifest is None:
                raise InputError("Provide --asset with --url, or provide --manifest.")
            resources = load_manifest(Path(args.manifest).expanduser())
            emit(run_verification(resources, args.timeout, args.max_bytes))
            return 0

        if args.command == "handoff":
            patterns = validate_include_patterns(args.include)
            manifest, report = collect_handoff(Path(args.repo), patterns)
            output_dir = output_path_for(args.out_dir, manifest["repository"])
            try:
                repo_root = Path(os.fsdecode(git_command(Path(args.repo).expanduser(), "rev-parse", "--show-toplevel")).strip()).resolve()
            except ExecutionError as exc:
                raise ExecutionError("Could not resolve the supplied checkout after inspection.") from exc
            ensure_output_outside_repo(output_dir, repo_root)
            planned = [str(output_dir / "handoff.md"), str(output_dir / "manifest.json")]
            if args.preview:
                warnings = ["Dirty paths are metadata only; this handoff does not back up working-tree changes."] if manifest["dirty_paths"] else []
                emit(envelope(
                    "needs_attention" if warnings or any(manifest["skipped"].values()) else "ok",
                    f"Previewed a handoff for {manifest['source_file_count']} tracked source file(s) from {manifest['repository']}.",
                    {"preview": True, "revision": manifest["revision"], "branch": manifest["branch"],
                     "dirty_path_count": len(manifest["dirty_paths"]), "planned_outputs": planned,
                     "source_file_count": manifest["source_file_count"], "skipped": manifest["skipped"]},
                    warnings=warnings,
                ))
                return 0
            artifacts = write_handoff(output_dir, manifest, report)
            warnings = ["Dirty paths are metadata only; this handoff does not back up working-tree changes."] if manifest["dirty_paths"] else []
            if any(manifest["skipped"].values()):
                warnings.append("Some selected files were omitted by the documented size, link, or read limits.")
            status = "needs_attention" if warnings else "ok"
            emit(envelope(
                status,
                f"Wrote a metadata-only handoff for {manifest['source_file_count']} tracked source file(s) from {manifest['repository']}.",
                {"revision": manifest["revision"], "branch": manifest["branch"],
                 "dirty_path_count": len(manifest["dirty_paths"]), "source_file_count": manifest["source_file_count"],
                 "skipped": manifest["skipped"]},
                artifacts=artifacts,
                warnings=warnings,
            ))
            return 0
        parser.error("unknown command")
    except InputError as exc:
        emit(envelope("error", str(exc), {"error_code": "invalid_input"}))
        return 2
    except ExecutionError as exc:
        emit(envelope("error", str(exc), {"error_code": "execution_failed"}))
        return 1
    except (OSError, UnicodeError) as exc:
        emit(envelope("error", f"The operation could not be completed: {type(exc).__name__}.", {"error_code": "io_error"}))
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
