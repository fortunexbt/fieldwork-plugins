#!/usr/bin/env python3
"""Inspect the structure, geometry metadata, and resource footprint of glTF exports."""

from __future__ import annotations

import argparse
import errno
import html
import json
import math
import os
import re
import stat
import struct
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any

TOOL = "asset-check"
MAX_JSON_BYTES = 64 * 1024 * 1024
COMPONENT_SIZES = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
COMPONENT_NAMES = {
    5120: "BYTE", 5121: "UNSIGNED_BYTE", 5122: "SHORT", 5123: "UNSIGNED_SHORT",
    5125: "UNSIGNED_INT", 5126: "FLOAT",
}
TYPE_SHAPES = {
    "SCALAR": (1, 1), "VEC2": (1, 2), "VEC3": (1, 3), "VEC4": (1, 4),
    "MAT2": (2, 2), "MAT3": (3, 3), "MAT4": (4, 4),
}
MODE_NAMES = {
    0: "POINTS", 1: "LINES", 2: "LINE_LOOP", 3: "LINE_STRIP",
    4: "TRIANGLES", 5: "TRIANGLE_STRIP", 6: "TRIANGLE_FAN",
}
INDEX_COMPONENTS = {5121, 5123, 5125}
BASE64_RE = re.compile(r"[A-Za-z0-9+/]*={0,2}")
DECODER_EXTENSIONS = {"KHR_draco_mesh_compression", "KHR_meshopt_compression"}
INSPECTED_EXTENSIONS = {"EXT_mesh_gpu_instancing"}


class AssetCheckError(Exception):
    def __init__(self, message: str, code: str = "invalid_asset") -> None:
        super().__init__(message)
        self.code = code


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


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def object_value(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AssetCheckError(f"{label} must be an object.")
    return value


def array_value(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise AssetCheckError(f"{label} must be an array.")
    return value


def nonnegative_integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if not is_int(value) or value < minimum:
        raise AssetCheckError(f"{label} must be an integer greater than or equal to {minimum}.")
    return value


def indexed(values: list[Any], index: Any, label: str) -> Any:
    if not is_int(index) or index < 0 or index >= len(values):
        raise AssetCheckError(f"{label} index {index!r} is outside the available array.")
    return values[index]


def reject_json_constant(value: str) -> None:
    raise ValueError(f"JSON constant {value} is not permitted.")


def parse_json_bytes(payload: bytes, label: str) -> dict[str, Any]:
    if len(payload) > MAX_JSON_BYTES:
        raise AssetCheckError(f"{label} JSON exceeds the 64 MiB inspection limit.", "input_too_large")
    try:
        parsed = json.loads(payload.decode("utf-8"), parse_constant=reject_json_constant)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise AssetCheckError(f"{label} JSON is not valid UTF-8 glTF data.") from exc
    if not isinstance(parsed, dict):
        raise AssetCheckError(f"{label} JSON root must be an object.")
    return parsed


def read_glb(path: Path, file_size: int) -> tuple[dict[str, Any], int | None, int | None]:
    try:
        with path.open("rb") as source:
            header = source.read(12)
            if len(header) != 12:
                raise AssetCheckError("GLB header is shorter than 12 bytes.")
            magic, version, declared_length = struct.unpack("<4sII", header)
            if magic != b"glTF":
                raise AssetCheckError("GLB header has an invalid magic value.")
            if version != 2:
                raise AssetCheckError(f"GLB version {version} is unsupported; this tool reads version 2.")
            if declared_length != file_size:
                raise AssetCheckError(
                    f"GLB header length {declared_length} does not match file size {file_size}."
                )

            offset = 12
            json_data: bytes | None = None
            binary_offset: int | None = None
            binary_length: int | None = None
            chunk_index = 0
            while offset < file_size:
                source.seek(offset)
                chunk_header = source.read(8)
                if len(chunk_header) != 8:
                    raise AssetCheckError("GLB ends in a partial chunk header.")
                chunk_length, chunk_type = struct.unpack("<II", chunk_header)
                if chunk_length % 4:
                    raise AssetCheckError(f"GLB chunk {chunk_index} length is not four-byte aligned.")
                data_offset = offset + 8
                chunk_end = data_offset + chunk_length
                if chunk_end > file_size:
                    raise AssetCheckError(f"GLB chunk {chunk_index} extends past the declared file length.")
                if chunk_index == 0 and chunk_type != 0x4E4F534A:
                    raise AssetCheckError("GLB's first chunk must contain JSON.")
                if chunk_type == 0x4E4F534A:
                    if json_data is not None:
                        raise AssetCheckError("GLB contains more than one JSON chunk.")
                    if chunk_length > MAX_JSON_BYTES:
                        raise AssetCheckError("GLB JSON chunk exceeds the 64 MiB inspection limit.", "input_too_large")
                    source.seek(data_offset)
                    json_data = source.read(chunk_length)
                    if len(json_data) != chunk_length:
                        raise AssetCheckError("GLB JSON chunk is truncated.")
                elif chunk_type == 0x004E4942:
                    if binary_offset is not None:
                        raise AssetCheckError("GLB contains more than one BIN chunk.")
                    binary_offset = data_offset
                    binary_length = chunk_length
                offset = chunk_end
                chunk_index += 1
            if offset != file_size:
                raise AssetCheckError("GLB chunk boundaries do not end at the file length.")
            if json_data is None:
                raise AssetCheckError("GLB is missing its required JSON chunk.")
            return parse_json_bytes(json_data.rstrip(b" \t\r\n"), "GLB"), binary_offset, binary_length
    except OSError as exc:
        raise AssetCheckError(f"GLB could not be read: {path.name}.", "source_unreadable") from exc


def load_document(path_arg: str) -> tuple[Path, str, int, dict[str, Any], int | None, int | None]:
    path = Path(path_arg).expanduser()
    suffix = path.suffix.lower()
    if suffix not in {".gltf", ".glb"}:
        raise AssetCheckError("Input must be an explicit .gltf or .glb file.", "unsupported_format")
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except (OSError, RuntimeError) as exc:
        raise AssetCheckError(f"Input file could not be read: {path.name}.", "source_unreadable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise AssetCheckError("Input must be a regular .gltf or .glb file.", "source_not_regular_file")
    if suffix == ".glb":
        document, binary_offset, binary_length = read_glb(resolved, info.st_size)
        file_format = "glb"
    else:
        if info.st_size > MAX_JSON_BYTES:
            raise AssetCheckError("glTF JSON exceeds the 64 MiB inspection limit.", "input_too_large")
        try:
            payload = resolved.read_bytes()
        except OSError as exc:
            raise AssetCheckError(f"Input file could not be read: {resolved.name}.", "source_unreadable") from exc
        document = parse_json_bytes(payload, "glTF")
        binary_offset = None
        binary_length = None
        file_format = "gltf"
    asset = object_value(document.get("asset"), "asset")
    if asset.get("version") != "2.0":
        raise AssetCheckError("Only glTF asset.version 2.0 is supported.", "unsupported_version")
    return resolved, file_format, info.st_size, document, binary_offset, binary_length


def data_uri_size(uri: str, label: str) -> tuple[int, str]:
    comma = uri.find(",", 5)
    if not uri[:5].lower() == "data:" or comma < 0:
        raise AssetCheckError(f"{label} has a malformed data URI.")
    start = comma + 1
    encoded_length = len(uri) - start
    is_base64 = comma - 7 >= 5 and uri[comma - 7:comma].lower() == ";base64"
    if is_base64:
        if encoded_length % 4 or BASE64_RE.fullmatch(uri, start) is None:
            raise AssetCheckError(f"{label} has invalid embedded base64 data.")
        padding = 2 if encoded_length >= 2 and uri.endswith("==") else 1 if encoded_length and uri.endswith("=") else 0
        size = (encoded_length // 4) * 3 - padding
    else:
        size = 0
        position = start
        while position < len(uri):
            char = uri[position]
            if char == "%" and position + 2 < len(uri) \
                    and all(value in "0123456789abcdefABCDEF" for value in uri[position + 1:position + 3]):
                size += 1
                position += 3
                continue
            try:
                size += len(char.encode("utf-8"))
            except UnicodeError as exc:
                raise AssetCheckError(f"{label} has invalid embedded URI text.") from exc
            position += 1
    return size, "embedded_data_uri"


def display_uri(uri: str) -> str:
    if uri.lower().startswith("data:"):
        header = uri.partition(",")[0][:100]
        return f"{header},[embedded payload omitted]"
    try:
        parsed = urllib.parse.urlsplit(uri)
        if parsed.scheme.lower() in {"http", "https"}:
            host = parsed.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            try:
                port = parsed.port
            except ValueError:
                port = None
            if port is not None:
                host = f"{host}:{port}"
            return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, "", ""))[:300]
        if parsed.scheme:
            return f"{parsed.scheme}:[URI omitted]"
        decoded = urllib.parse.unquote(parsed.path)
        if Path(decoded).is_absolute() or PureWindowsPath(decoded).is_absolute() or PureWindowsPath(decoded).drive:
            return "[absolute local path omitted]"
        return parsed.path[:300]
    except (ValueError, UnicodeError):
        return "[invalid URI omitted]"


def inspect_uri(uri_value: Any, base_dir: Path, label: str, declared_bytes: int | None,
                external_sizes: dict[str, int]) -> dict[str, Any]:
    if not isinstance(uri_value, str) or not uri_value:
        raise AssetCheckError(f"{label} URI must be a non-empty string.")
    uri = uri_value
    resource: dict[str, Any] = {
        "uri": display_uri(uri), "storage": "unknown", "status": "unknown",
        "declared_bytes": declared_bytes, "available_bytes": None, "external_bytes": 0,
        "diagnostic": None,
    }
    if uri.lower().startswith("data:"):
        size, storage = data_uri_size(uri, label)
        resource.update(storage=storage, status="available", available_bytes=size)
        if declared_bytes is not None and size < declared_bytes:
            resource.update(status="truncated", diagnostic="embedded data is shorter than declared byteLength")
        return resource

    try:
        parsed = urllib.parse.urlsplit(uri)
    except ValueError as exc:
        raise AssetCheckError(f"{label} URI is malformed.") from exc
    if parsed.scheme or parsed.netloc:
        status = "remote_not_fetched" if parsed.scheme.lower() in {"http", "https"} else "unsupported_uri"
        resource.update(storage="external_uri", status=status, diagnostic="external URI was not opened or fetched")
        return resource
    if parsed.query or parsed.fragment:
        resource.update(storage="external_uri", status="unsupported_uri", diagnostic="query and fragment URIs are not inspected")
        return resource
    try:
        decoded_path = urllib.parse.unquote(parsed.path, errors="strict")
    except UnicodeError as exc:
        raise AssetCheckError(f"{label} URI has invalid percent encoding.") from exc
    if not decoded_path or "\x00" in decoded_path or "\\" in decoded_path:
        resource.update(storage="external_file", status="unsupported_uri", diagnostic="URI is not a supported relative file path")
        return resource
    candidate_path = Path(decoded_path)
    if candidate_path.is_absolute() or PureWindowsPath(decoded_path).is_absolute() or PureWindowsPath(decoded_path).drive:
        resource.update(storage="external_file", status="outside_directory", diagnostic="absolute paths are outside the selected asset directory")
        return resource
    candidate = base_dir / candidate_path
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(base_dir)
    except (OSError, RuntimeError, ValueError):
        resource.update(storage="external_file", status="outside_directory", diagnostic="resolved path is outside the selected asset directory")
        return resource
    resource["storage"] = "external_file"
    try:
        info = resolved.stat()
    except FileNotFoundError:
        resource.update(status="missing", diagnostic="referenced file does not exist")
        return resource
    except OSError:
        resource.update(status="unreadable", diagnostic="referenced file could not be inspected")
        return resource
    if not stat.S_ISREG(info.st_mode):
        resource.update(status="unreadable", diagnostic="referenced path is not a regular file")
        return resource
    resource.update(status="available", available_bytes=info.st_size, external_bytes=info.st_size)
    resource["resolved_key"] = str(resolved)
    if declared_bytes is not None and info.st_size < declared_bytes:
        resource.update(status="truncated", diagnostic="file is shorter than declared byteLength")
    external_sizes[str(resolved)] = info.st_size
    return resource


def build_resources(document: dict[str, Any], file_format: str, source_path: Path,
                    source_size: int, binary_offset: int | None, binary_length: int | None,
                    warnings: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw_buffers = array_value(document.get("buffers", []), "buffers")
    raw_views = array_value(document.get("bufferViews", []), "bufferViews")
    raw_images = array_value(document.get("images", []), "images")
    buffers: list[dict[str, Any]] = []
    external_sizes: dict[str, int] = {}
    for index, value in enumerate(raw_buffers):
        obj = object_value(value, f"buffers[{index}]")
        declared = nonnegative_integer(obj.get("byteLength"), f"buffers[{index}].byteLength", minimum=1)
        uri = obj.get("uri")
        if uri is None and file_format == "glb" and index == 0 and binary_offset is not None:
            actual_chunk_bytes = binary_length or 0
            if actual_chunk_bytes < declared or actual_chunk_bytes - declared > 3:
                raise AssetCheckError("GLB BIN chunk size does not match buffers[0].byteLength.")
            resource = {
                "storage": "embedded_glb", "status": "available", "uri": None,
                "declared_bytes": declared, "available_bytes": declared, "external_bytes": 0,
                "diagnostic": None, "resolved_key": None,
            }
        elif uri is None:
            resource = {
                "storage": "unbound_buffer", "status": "missing", "uri": None,
                "declared_bytes": declared, "available_bytes": None, "external_bytes": 0,
                "diagnostic": "buffer has no URI and is not stored in a GLB BIN chunk", "resolved_key": None,
            }
        else:
            resource = inspect_uri(uri, source_path.parent, f"buffers[{index}]", declared, external_sizes)
        resource.update(kind="buffer", index=index)
        buffers.append(resource)
        if resource["status"] not in {"available"}:
            warnings.append(f"Buffer {index}: {resource.get('diagnostic') or resource['status']}.")

    views: list[dict[str, Any]] = []
    for index, value in enumerate(raw_views):
        obj = object_value(value, f"bufferViews[{index}]")
        buffer_index = nonnegative_integer(obj.get("buffer"), f"bufferViews[{index}].buffer")
        buffer_obj = object_value(indexed(raw_buffers, buffer_index, f"bufferViews[{index}].buffer"), f"buffers[{buffer_index}]")
        buffer_length = nonnegative_integer(buffer_obj.get("byteLength"), f"buffers[{buffer_index}].byteLength", minimum=1)
        length = nonnegative_integer(obj.get("byteLength"), f"bufferViews[{index}].byteLength", minimum=1)
        offset = nonnegative_integer(obj.get("byteOffset", 0), f"bufferViews[{index}].byteOffset")
        if offset + length > buffer_length:
            raise AssetCheckError(f"bufferViews[{index}] byte range exceeds buffers[{buffer_index}].byteLength.")
        stride = obj.get("byteStride")
        if stride is not None:
            stride = nonnegative_integer(stride, f"bufferViews[{index}].byteStride", minimum=4)
            if stride > 252 or stride % 4:
                raise AssetCheckError(f"bufferViews[{index}].byteStride must be a multiple of 4 from 4 through 252.")
        view_extensions = get_extensions(obj.get("extensions"), f"bufferViews[{index}].extensions")
        meshopt = view_extensions.get("KHR_meshopt_compression")
        if meshopt is not None:
            meshopt_obj = object_value(meshopt, f"bufferViews[{index}].KHR_meshopt_compression")
            compressed_buffer_index = nonnegative_integer(meshopt_obj.get("buffer"),
                                                         f"bufferViews[{index}].KHR_meshopt_compression.buffer")
            compressed_buffer_obj = object_value(
                indexed(raw_buffers, compressed_buffer_index, f"bufferViews[{index}].KHR_meshopt_compression.buffer"),
                f"buffers[{compressed_buffer_index}]",
            )
            compressed_offset = nonnegative_integer(meshopt_obj.get("byteOffset", 0),
                                                    f"bufferViews[{index}].KHR_meshopt_compression.byteOffset")
            compressed_length = nonnegative_integer(meshopt_obj.get("byteLength"),
                                                    f"bufferViews[{index}].KHR_meshopt_compression.byteLength", minimum=1)
            compressed_declared_length = nonnegative_integer(
                compressed_buffer_obj.get("byteLength"), f"buffers[{compressed_buffer_index}].byteLength", minimum=1
            )
            if compressed_offset + compressed_length > compressed_declared_length:
                raise AssetCheckError(f"bufferViews[{index}] meshopt compressed byte range exceeds its buffer.")
            nonnegative_integer(meshopt_obj.get("count"), f"bufferViews[{index}].KHR_meshopt_compression.count", minimum=1)
            stride_value = nonnegative_integer(meshopt_obj.get("byteStride"),
                                               f"bufferViews[{index}].KHR_meshopt_compression.byteStride", minimum=1)
            if stride_value > 256:
                raise AssetCheckError(f"bufferViews[{index}] meshopt byteStride must not exceed 256.")
            if meshopt_obj.get("mode") not in {"ATTRIBUTES", "TRIANGLES", "INDICES"}:
                raise AssetCheckError(f"bufferViews[{index}] meshopt mode is invalid.")
        buffer_state = buffers[buffer_index]
        physical_known = buffer_state["status"] == "available"
        if buffer_state["storage"] == "embedded_glb" and binary_offset is not None:
            available = offset + length <= (binary_length or 0)
            physical_known = available
        elif buffer_state["available_bytes"] is not None:
            available = offset + length <= buffer_state["available_bytes"]
            physical_known = physical_known and available
        views.append({
            "buffer": buffer_index, "byte_offset": offset, "byte_length": length,
            "byte_stride": stride, "physical_data_available": physical_known,
            "compressed": meshopt is not None,
        })

    image_resources: list[dict[str, Any]] = []
    for index, value in enumerate(raw_images):
        obj = object_value(value, f"images[{index}]")
        has_uri = "uri" in obj
        has_view = "bufferView" in obj
        if has_uri == has_view:
            raise AssetCheckError(f"images[{index}] must define exactly one of uri or bufferView.")
        if has_uri:
            resource = inspect_uri(obj["uri"], source_path.parent, f"images[{index}]", None, external_sizes)
        else:
            view_index = nonnegative_integer(obj.get("bufferView"), f"images[{index}].bufferView")
            view = object_value(indexed(views, view_index, f"images[{index}].bufferView"), f"bufferViews[{view_index}]")
            state = buffers[view["buffer"]]
            if state["status"] == "available" and view["physical_data_available"]:
                resource_status = "available"
            else:
                resource_status = state["status"] if state["status"] != "available" else "truncated"
            resource = {
                "uri": None, "storage": "embedded_buffer_view", "status": resource_status,
                "declared_bytes": view["byte_length"], "available_bytes": view["byte_length"] if resource_status == "available" else None,
                "external_bytes": 0, "diagnostic": None if resource_status == "available" else "image bufferView bytes are unavailable",
                "buffer_view": view_index,
            }
        resource.update(kind="image", index=index)
        image_resources.append(resource)
        if resource["status"] != "available":
            warnings.append(f"Image {index}: {resource.get('diagnostic') or resource['status']}.")

    unique_external = sum(external_sizes.values())
    unknown_external = any(row["storage"] == "external_uri" or row["status"] != "available"
                           for row in [*buffers, *image_resources]
                           if row["storage"] not in {"embedded_glb", "embedded_data_uri", "embedded_buffer_view"})
    footprint = {
        "source_file_bytes": source_size,
        "external_bytes": None if unknown_external else unique_external,
        "total_asset_bytes": None if unknown_external else source_size + unique_external,
        "external_resource_count": len(external_sizes),
        "embedded_buffer_bytes": sum(row["declared_bytes"] or 0 for row in buffers
                                      if row["storage"] in {"embedded_glb", "embedded_data_uri"}),
        "external_buffer_bytes": sum(row["available_bytes"] or 0 for row in buffers if row["storage"] == "external_file"),
        "embedded_image_bytes": sum(row["available_bytes"] or 0 for row in image_resources
                                     if row["storage"] in {"embedded_data_uri", "embedded_buffer_view"}),
        "external_image_bytes": sum(row["available_bytes"] or 0 for row in image_resources
                                     if row["storage"] == "external_file"),
        "resources": [{key: value for key, value in row.items() if key != "resolved_key"}
                      for row in [*buffers, *image_resources]],
    }
    return views, footprint


def component_size(accessor: dict[str, Any], index: int) -> int:
    value = accessor.get("componentType")
    if not is_int(value) or value not in COMPONENT_SIZES:
        raise AssetCheckError(f"accessors[{index}].componentType is unsupported or invalid.")
    return COMPONENT_SIZES[value]


def accessor_element_size(accessor: dict[str, Any], index: int) -> int:
    component_bytes = component_size(accessor, index)
    kind = accessor.get("type")
    if kind not in TYPE_SHAPES:
        raise AssetCheckError(f"accessors[{index}].type is invalid.")
    columns, rows = TYPE_SHAPES[kind]
    if columns == 1:
        return component_bytes * rows
    column_bytes = rows * component_bytes
    aligned_column_bytes = (column_bytes + 3) // 4 * 4
    return columns * aligned_column_bytes


def check_view_span(views: list[dict[str, Any]], view_index: Any, byte_offset: Any,
                    count: int, element_size: int, component_bytes: int, label: str,
                    stride: int | None = None) -> tuple[str, dict[str, Any]]:
    view = object_value(indexed(views, view_index, f"{label}.bufferView"), f"bufferViews[{view_index}]")
    offset = nonnegative_integer(byte_offset, f"{label}.byteOffset")
    if (view["byte_offset"] + offset) % component_bytes:
        raise AssetCheckError(f"{label}.byteOffset is not aligned to componentType size.")
    effective_stride = stride if stride is not None else element_size
    if effective_stride < element_size or effective_stride % component_bytes:
        raise AssetCheckError(f"{label} accessor stride is too small or misaligned.")
    end = offset + (count - 1) * effective_stride + element_size
    if end > view["byte_length"]:
        raise AssetCheckError(f"{label} byte range exceeds its bufferView.")
    return ("validated" if view["physical_data_available"] else "resource_unavailable"), view


def validate_sparse(sparse_value: Any, accessor: dict[str, Any], index: int,
                    views: list[dict[str, Any]]) -> bool:
    sparse = object_value(sparse_value, f"accessors[{index}].sparse")
    count = nonnegative_integer(sparse.get("count"), f"accessors[{index}].sparse.count", minimum=1)
    accessor_count = nonnegative_integer(accessor.get("count"), f"accessors[{index}].count", minimum=1)
    if count > accessor_count:
        raise AssetCheckError(f"accessors[{index}].sparse.count exceeds accessor count.")
    indices = object_value(sparse.get("indices"), f"accessors[{index}].sparse.indices")
    index_type = indices.get("componentType")
    if index_type not in INDEX_COMPONENTS:
        raise AssetCheckError(f"accessors[{index}].sparse.indices.componentType must be unsigned integer.")
    index_view = indices.get("bufferView")
    _, index_buffer_view = check_view_span(
        views, index_view, indices.get("byteOffset", 0), count, COMPONENT_SIZES[index_type],
        COMPONENT_SIZES[index_type], f"accessors[{index}].sparse.indices",
    )
    values = object_value(sparse.get("values"), f"accessors[{index}].sparse.values")
    _, values_view = check_view_span(
        views, values.get("bufferView"), values.get("byteOffset", 0), count,
        accessor_element_size(accessor, index), component_size(accessor, index),
        f"accessors[{index}].sparse.values",
    )
    return index_buffer_view["physical_data_available"] and values_view["physical_data_available"]


def build_accessors(document: dict[str, Any], views: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_accessors = array_value(document.get("accessors", []), "accessors")
    accessors: list[dict[str, Any]] = []
    public_accessors: list[dict[str, Any]] = []
    for index, value in enumerate(raw_accessors):
        accessor = object_value(value, f"accessors[{index}]")
        count = nonnegative_integer(accessor.get("count"), f"accessors[{index}].count", minimum=1)
        size = accessor_element_size(accessor, index)
        component_bytes = component_size(accessor, index)
        view_ref = accessor.get("bufferView")
        if view_ref is None:
            if "byteOffset" in accessor:
                raise AssetCheckError(f"accessors[{index}].byteOffset requires bufferView.")
            range_status = "implicit_zero"
            sparse_available = True
        else:
            view = object_value(indexed(views, view_ref, f"accessors[{index}].bufferView"), f"bufferViews[{view_ref}]")
            stride = view["byte_stride"] or size
            if view["byte_stride"] is not None and stride < size:
                raise AssetCheckError(f"accessors[{index}] is larger than its bufferView byteStride.")
            accessor_offset = nonnegative_integer(accessor.get("byteOffset", 0), f"accessors[{index}].byteOffset")
            if accessor["type"].startswith("MAT") and component_bytes < 4 \
                    and (view["byte_offset"] + accessor_offset) % 4:
                raise AssetCheckError(f"accessors[{index}] matrix columns are not four-byte aligned.")
            range_status, view = check_view_span(
                views, view_ref, accessor_offset, count, size, component_bytes,
                f"accessors[{index}]", stride,
            )
            if view["compressed"]:
                range_status = "compressed_metadata"
            sparse_available = True
        if "sparse" in accessor:
            sparse_available = validate_sparse(accessor["sparse"], accessor, index, views) and sparse_available
            if not sparse_available and range_status in {"validated", "implicit_zero"}:
                range_status = "resource_unavailable"
        accessor_info = {
            "count": count,
            "type": accessor["type"],
            "component_type": accessor["componentType"],
            "component_name": COMPONENT_NAMES[accessor["componentType"]],
            "element_bytes": size,
            "range_status": range_status,
        }
        accessors.append({"raw": accessor, **accessor_info})
        public_accessors.append({"index": index, **accessor_info})
    return accessors, public_accessors


def get_extensions(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    return object_value(value, label)


def build_geometry(document: dict[str, Any], accessors: list[dict[str, Any]], views: list[dict[str, Any]],
                   required_extensions: set[str], warnings: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw_meshes = array_value(document.get("meshes", []), "meshes")
    raw_materials: list[Any] | None = None
    primitive_rows: list[dict[str, Any]] = []
    mesh_rows: list[list[dict[str, Any]]] = []
    topology: dict[str, dict[str, Any]] = {
        name: {"primitive_count": 0, "declared_vertices": 0, "element_count": 0,
               "triangle_candidates": 0, "line_segments": 0, "points": 0,
               "metric_status": "known"}
        for name in MODE_NAMES.values()
    }

    for mesh_index, mesh_value in enumerate(raw_meshes):
        mesh = object_value(mesh_value, f"meshes[{mesh_index}]")
        primitives = array_value(mesh.get("primitives"), f"meshes[{mesh_index}].primitives")
        if not primitives:
            raise AssetCheckError(f"meshes[{mesh_index}].primitives must not be empty.")
        rows_for_mesh: list[dict[str, Any]] = []
        for primitive_index, primitive_value in enumerate(primitives):
            primitive = object_value(primitive_value, f"meshes[{mesh_index}].primitives[{primitive_index}]")
            label = f"meshes[{mesh_index}].primitives[{primitive_index}]"
            attributes = object_value(primitive.get("attributes"), f"{label}.attributes")
            if not attributes:
                raise AssetCheckError(f"{label}.attributes must not be empty.")
            attribute_refs: dict[str, int] = {}
            attribute_counts: set[int] = set()
            attribute_statuses: list[str] = []
            for semantic, accessor_ref in attributes.items():
                accessor = object_value(indexed(accessors, accessor_ref, f"{label}.attributes.{semantic} accessor"),
                                        f"accessors[{accessor_ref}]")
                attribute_refs[semantic] = accessor_ref
                attribute_counts.add(accessor["count"])
                attribute_statuses.append(accessor["range_status"])
            if len(attribute_counts) != 1:
                raise AssetCheckError(f"{label} attributes do not have matching accessor counts.")
            if "POSITION" not in attribute_refs:
                raise AssetCheckError(f"{label} is missing its POSITION accessor.")
            position_ref = attribute_refs["POSITION"]
            position_accessor = accessors[position_ref]
            if position_accessor["raw"].get("type") != "VEC3":
                raise AssetCheckError(f"{label} POSITION accessor must have type VEC3.")

            primitive_extensions = get_extensions(primitive.get("extensions"), f"{label}.extensions")
            draco_extension = primitive_extensions.get("KHR_draco_mesh_compression")
            index_ref = primitive.get("indices")
            if index_ref is not None:
                index_accessor = object_value(indexed(accessors, index_ref, f"{label}.indices"), f"accessors[{index_ref}]")
                index_raw = index_accessor["raw"]
                if index_raw.get("type") != "SCALAR" or index_raw.get("componentType") not in INDEX_COMPONENTS:
                    raise AssetCheckError(f"{label}.indices must use an unsigned scalar accessor.")
                if "bufferView" not in index_raw and draco_extension is None:
                    raise AssetCheckError(f"{label}.indices accessor must reference a bufferView.")
                if "bufferView" in index_raw:
                    view = views[index_raw["bufferView"]]
                    if view["byte_stride"] is not None:
                        raise AssetCheckError(f"{label}.indices bufferView cannot use byteStride.")
                element_count = index_accessor["count"]
                index_status = index_accessor["range_status"]
                index_accessor_ref: int | None = index_ref
            else:
                element_count = position_accessor["count"]
                index_status = "not_indexed"
                index_accessor_ref = None

            mode_value = primitive.get("mode", 4)
            if not is_int(mode_value) or mode_value not in MODE_NAMES:
                raise AssetCheckError(f"{label}.mode must be a glTF primitive mode from 0 through 6.")
            mode = MODE_NAMES[mode_value]
            candidate_triangles = 0
            line_segments = 0
            points = 0
            if mode == "POINTS":
                points = element_count
            elif mode == "LINES":
                line_segments = element_count // 2
            elif mode == "LINE_LOOP":
                line_segments = element_count if element_count >= 2 else 0
            elif mode == "LINE_STRIP":
                line_segments = max(0, element_count - 1)
            elif mode == "TRIANGLES":
                if element_count % 3:
                    raise AssetCheckError(f"{label} TRIANGLES element count must be divisible by 3.")
                candidate_triangles = element_count // 3
            elif mode in {"TRIANGLE_STRIP", "TRIANGLE_FAN"}:
                candidate_triangles = max(0, element_count - 2)

            compressed = None
            compression_note = None
            if "KHR_draco_mesh_compression" in primitive_extensions:
                draco = object_value(primitive_extensions["KHR_draco_mesh_compression"], f"{label}.extensions.KHR_draco_mesh_compression")
                compressed_view = nonnegative_integer(draco.get("bufferView"), f"{label}.KHR_draco_mesh_compression.bufferView")
                indexed(views, compressed_view, f"{label}.KHR_draco_mesh_compression.bufferView")
                compressed_attributes = object_value(draco.get("attributes"), f"{label}.KHR_draco_mesh_compression.attributes")
                if not compressed_attributes:
                    raise AssetCheckError(f"{label}.KHR_draco_mesh_compression.attributes must not be empty.")
                for semantic, attribute_id in compressed_attributes.items():
                    if semantic not in attribute_refs:
                        raise AssetCheckError(f"{label}.KHR_draco_mesh_compression references undeclared attribute {semantic}.")
                    nonnegative_integer(attribute_id, f"{label}.KHR_draco_mesh_compression.attributes.{semantic}")
                fallback_refs = list(attribute_refs.values()) + ([index_ref] if index_ref is not None else [])
                fallback_available = all(
                    ("bufferView" in accessors[ref]["raw"] or "sparse" in accessors[ref]["raw"])
                    and accessors[ref]["range_status"] in {"validated", "implicit_zero"}
                    for ref in fallback_refs
                ) and (index_ref is None or "bufferView" in accessors[index_ref]["raw"])
                if "KHR_draco_mesh_compression" in required_extensions or not fallback_available:
                    compressed = "KHR_draco_mesh_compression"
                else:
                    compression_note = "optional Draco extension; counts use range-checked uncompressed fallback accessors"
            geometry_status = "validated"
            if any(status in {"resource_unavailable"} for status in [*attribute_statuses, index_status]):
                geometry_status = "resource_unavailable"
            elif "compressed_metadata" in [*attribute_statuses, index_status]:
                compressed = compressed or "KHR_meshopt_compression"
            if compressed:
                geometry_status = "compressed_decoder_unavailable"
            if geometry_status == "compressed_decoder_unavailable":
                warnings.append(f"Mesh {mesh_index}, primitive {primitive_index}: {compressed} geometry is not decoded; vertex and triangle measures are unknown.")

            material_ref = primitive.get("material")
            if material_ref is not None:
                if raw_materials is None:
                    raw_materials = array_value(document.get("materials", []), "materials")
                indexed(raw_materials, material_ref, f"{label}.material")
            for target_index, target_value in enumerate(array_value(primitive.get("targets", []), f"{label}.targets")):
                target = object_value(target_value, f"{label}.targets[{target_index}]")
                for semantic, accessor_ref in target.items():
                    indexed(accessors, accessor_ref, f"{label}.targets[{target_index}].{semantic}")

            row = {
                "mesh_index": mesh_index,
                "mesh_name": mesh.get("name", f"Mesh {mesh_index + 1}"),
                "primitive_index": primitive_index,
                "mode": mode,
                "mode_value": mode_value,
                "position_accessor": position_ref,
                "declared_vertex_count": position_accessor["count"],
                "index_accessor": index_accessor_ref,
                "element_count": element_count,
                "material_index": material_ref,
                "geometry_status": geometry_status,
                "vertex_count": position_accessor["count"] if geometry_status == "validated" else None,
                "triangle_candidates": candidate_triangles if geometry_status == "validated" else None,
                "line_segments": line_segments if geometry_status == "validated" else None,
                "points": points if geometry_status == "validated" else None,
                "compression": compressed or compression_note,
            }
            primitive_rows.append(row)
            rows_for_mesh.append(row)
            summary = topology[mode]
            summary["primitive_count"] += 1
            summary["declared_vertices"] += position_accessor["count"]
            summary["element_count"] += element_count
            if geometry_status == "validated":
                summary["triangle_candidates"] += candidate_triangles
                summary["line_segments"] += line_segments
                summary["points"] += points
            else:
                summary["metric_status"] = "unknown"
        mesh_rows.append(rows_for_mesh)

    known_vertices = sum(row["vertex_count"] for row in primitive_rows if row["vertex_count"] is not None)
    known_triangles = sum(row["triangle_candidates"] for row in primitive_rows
                          if row["triangle_candidates"] is not None)
    geometry = {
        "accessor_count": len(accessors),
        "mesh_count": len(raw_meshes),
        "primitive_count": len(primitive_rows),
        "declared_vertices_in_mesh_primitives": sum(row["declared_vertex_count"] for row in primitive_rows),
        "vertices_in_mesh_primitives": None if any(row["vertex_count"] is None for row in primitive_rows) else known_vertices,
        "triangle_candidates_in_mesh_primitives": None if any(row["triangle_candidates"] is None for row in primitive_rows) else known_triangles,
        "topology": topology,
        "primitives": primitive_rows,
    }
    return geometry, mesh_rows


def transform_info(node: dict[str, Any], index: int) -> dict[str, Any]:
    has_matrix = "matrix" in node
    has_trs = any(key in node for key in ("translation", "rotation", "scale"))
    if has_matrix and has_trs:
        raise AssetCheckError(f"nodes[{index}] cannot define matrix and TRS transforms together.")
    if has_matrix:
        values = node["matrix"]
        if not isinstance(values, list) or len(values) != 16 or any(not finite_number(x) for x in values):
            raise AssetCheckError(f"nodes[{index}].matrix must contain 16 finite numbers.")
        return {"type": "matrix"}
    if has_trs:
        result: dict[str, Any] = {"type": "trs"}
        for key, expected in (("translation", 3), ("rotation", 4), ("scale", 3)):
            if key in node:
                values = node[key]
                if not isinstance(values, list) or len(values) != expected or any(not finite_number(x) for x in values):
                    raise AssetCheckError(f"nodes[{index}].{key} must contain {expected} finite numbers.")
                result[key] = values
        return result
    return {"type": "identity"}


def finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def build_scenes(document: dict[str, Any], mesh_rows: list[list[dict[str, Any]]],
                 accessors: list[dict[str, Any]], warnings: list[str]) -> dict[str, Any]:
    raw_nodes = array_value(document.get("nodes", []), "nodes")
    raw_scenes = array_value(document.get("scenes", []), "scenes")
    raw_meshes = array_value(document.get("meshes", []), "meshes")
    node_transforms: list[dict[str, Any]] = []
    node_children: list[list[int]] = []
    node_meshes: list[int | None] = []
    node_gpu_counts: list[int | None] = []
    node_gpu_exts: list[bool] = []
    parents = [0] * len(raw_nodes)
    for index, value in enumerate(raw_nodes):
        node = object_value(value, f"nodes[{index}]")
        node_transforms.append(transform_info(node, index))
        children_value = node.get("children", [])
        children = [nonnegative_integer(child, f"nodes[{index}].children[]")
                    for child in array_value(children_value, f"nodes[{index}].children")]
        for child in children:
            indexed(raw_nodes, child, f"nodes[{index}].children")
            parents[child] += 1
            if parents[child] > 1:
                raise AssetCheckError(f"nodes[{child}] has more than one parent.")
        node_children.append(children)
        mesh_index = node.get("mesh")
        if mesh_index is not None:
            indexed(raw_meshes, mesh_index, f"nodes[{index}].mesh")
        node_meshes.append(mesh_index)
        extensions = get_extensions(node.get("extensions"), f"nodes[{index}].extensions")
        gpu_extension = extensions.get("EXT_mesh_gpu_instancing")
        if gpu_extension is None:
            node_gpu_counts.append(1)
            node_gpu_exts.append(False)
            continue
        if mesh_index is None:
            raise AssetCheckError(f"nodes[{index}] uses EXT_mesh_gpu_instancing but has no mesh.")
        gpu_obj = object_value(gpu_extension, f"nodes[{index}].extensions.EXT_mesh_gpu_instancing")
        instance_attributes = object_value(gpu_obj.get("attributes"), f"nodes[{index}].EXT_mesh_gpu_instancing.attributes")
        if not instance_attributes:
            raise AssetCheckError(f"nodes[{index}].EXT_mesh_gpu_instancing.attributes must not be empty.")
        instance_counts: set[int] = set()
        available = True
        for semantic, accessor_ref in instance_attributes.items():
            accessor = object_value(indexed(accessors, accessor_ref, f"nodes[{index}].EXT_mesh_gpu_instancing.{semantic}"),
                                    f"accessors[{accessor_ref}]")
            raw_accessor = accessor["raw"]
            semantic_requirements = {
                "TRANSLATION": ("VEC3", {5126}),
                "ROTATION": ("VEC4", {5120, 5122, 5126}),
                "SCALE": ("VEC3", {5126}),
            }
            if semantic in semantic_requirements:
                expected_type, component_types = semantic_requirements[semantic]
                if raw_accessor.get("type") != expected_type or raw_accessor.get("componentType") not in component_types:
                    raise AssetCheckError(f"nodes[{index}] instancing {semantic} accessor has an invalid type or componentType.")
                if semantic == "ROTATION" and raw_accessor.get("componentType") in {5120, 5122} and raw_accessor.get("normalized") is not True:
                    raise AssetCheckError(f"nodes[{index}] integer instancing ROTATION accessors must be normalized.")
            instance_counts.add(accessor["count"])
            if accessor["range_status"] not in {"validated", "implicit_zero"}:
                available = False
        if len(instance_counts) != 1:
            raise AssetCheckError(f"nodes[{index}] EXT_mesh_gpu_instancing accessors must have matching counts.")
        node_gpu_counts.append(next(iter(instance_counts)) if available else None)
        node_gpu_exts.append(True)

    # Reject cycles even when an affected node is not included in a scene.
    visit_state = [0] * len(raw_nodes)

    def verify_tree(index: int) -> None:
        if visit_state[index] == 1:
            raise AssetCheckError(f"Node hierarchy contains a cycle at nodes[{index}].")
        if visit_state[index] == 2:
            return
        visit_state[index] = 1
        for child in node_children[index]:
            verify_tree(child)
        visit_state[index] = 2

    for index in range(len(raw_nodes)):
        verify_tree(index)

    def append_scene_subtree(index: int, scene_index: int, visited: set[int], ordered: list[int]) -> None:
        if index in visited:
            raise AssetCheckError(f"scenes[{scene_index}] reaches nodes[{index}] more than once.")
        visited.add(index)
        ordered.append(index)
        for child in node_children[index]:
            append_scene_subtree(child, scene_index, visited, ordered)

    mesh_totals: list[tuple[int | None, int | None]] = []
    for mesh_primitive_rows in mesh_rows:
        triangle_values = [row["triangle_candidates"] for row in mesh_primitive_rows]
        vertex_values = [row["vertex_count"] for row in mesh_primitive_rows]
        mesh_totals.append((
            None if any(value is None for value in triangle_values) else sum(triangle_values),
            None if any(value is None for value in vertex_values) else sum(vertex_values),
        ))

    scene_results: list[dict[str, Any]] = []
    for scene_index, scene_value in enumerate(raw_scenes):
        scene = object_value(scene_value, f"scenes[{scene_index}]")
        roots = [nonnegative_integer(value, f"scenes[{scene_index}].nodes[]")
                 for value in array_value(scene.get("nodes", []), f"scenes[{scene_index}].nodes")]
        for root in roots:
            indexed(raw_nodes, root, f"scenes[{scene_index}].nodes")
            if parents[root] > 0:
                raise AssetCheckError(f"scenes[{scene_index}] lists child node {root} as a scene root.")
        visited: set[int] = set()
        ordered: list[int] = []

        for root in roots:
            append_scene_subtree(root, scene_index, visited, ordered)

        mesh_nodes: list[dict[str, Any]] = []
        scene_triangles = 0
        scene_vertices = 0
        totals_known = True
        total_gpu_instances = 0
        total_mesh_occurrences = 0
        transforms = [{"node_index": index, "name": raw_nodes[index].get("name", f"Node {index + 1}"),
                       **node_transforms[index]} for index in ordered]
        for node_index in ordered:
            mesh_index = node_meshes[node_index]
            if mesh_index is None:
                continue
            mesh_triangles, mesh_vertices = mesh_totals[mesh_index]
            gpu_instances = node_gpu_counts[node_index]
            if gpu_instances is None or mesh_triangles is None or mesh_vertices is None:
                totals_known = False
                node_triangles = None
                node_vertices = None
            else:
                node_triangles = mesh_triangles * gpu_instances
                node_vertices = mesh_vertices * gpu_instances
                scene_triangles += node_triangles
                scene_vertices += node_vertices
                total_mesh_occurrences += gpu_instances
            if node_gpu_exts[node_index] and gpu_instances is not None:
                total_gpu_instances += gpu_instances
            elif node_gpu_exts[node_index]:
                totals_known = False
            mesh_nodes.append({
                "node_index": node_index,
                "node_name": raw_nodes[node_index].get("name", f"Node {node_index + 1}"),
                "mesh_index": mesh_index,
                "mesh_name": raw_meshes[mesh_index].get("name", f"Mesh {mesh_index + 1}"),
                "local_transform_type": node_transforms[node_index]["type"],
                "gpu_instancing": node_gpu_exts[node_index],
                "gpu_instance_count": gpu_instances,
                "drawn_mesh_occurrences": gpu_instances,
                "triangle_candidates": node_triangles,
                "vertices": node_vertices,
            })
        scene_results.append({
            "index": scene_index, "name": scene.get("name", f"Scene {scene_index + 1}"),
            "reachable_node_count": len(ordered), "mesh_node_count": len(mesh_nodes),
            "mesh_nodes": mesh_nodes, "transforms": transforms,
            "gpu_instance_count": total_gpu_instances if totals_known else None,
            "rendered_mesh_occurrences": total_mesh_occurrences if totals_known else None,
            "triangle_candidates": scene_triangles if totals_known else None,
            "vertices": scene_vertices if totals_known else None,
        })

    active_scene = document.get("scene")
    if active_scene is not None:
        indexed(raw_scenes, active_scene, "scene")
    active_result = scene_results[active_scene] if active_scene is not None else None
    if active_scene is None:
        warnings.append("No default scene is selected; a default-scene triangle budget is unknown.")
    scene_node_indices = {
        row["node_index"] for scene in scene_results for row in scene["transforms"]
    }
    return {
        "active_scene_index": active_scene,
        "active_scene_status": "known" if active_result and active_result["triangle_candidates"] is not None else "unknown",
        "scenes": scene_results,
        "mesh_nodes_outside_scenes": sum(
            1 for index, mesh in enumerate(node_meshes) if mesh is not None and index not in scene_node_indices
        ),
        "transform_note": "Node matrices and TRS affect placement and scale, not cardinality. Scene totals count each reachable mesh node and multiply EXT_mesh_gpu_instancing instances; transforms are reported and are not baked into vertex data.",
    }


def collect_texture_uses(document: dict[str, Any]) -> dict[str, Any]:
    materials = array_value(document.get("materials", []), "materials")
    textures = array_value(document.get("textures", []), "textures")
    images = array_value(document.get("images", []), "images")
    texture_image_sources: list[int | None] = []
    for index, texture_value in enumerate(textures):
        texture = object_value(texture_value, f"textures[{index}]")
        extensions = get_extensions(texture.get("extensions"), f"textures[{index}].extensions")
        basisu = extensions.get("KHR_texture_basisu")
        source = texture.get("source")
        if basisu is not None:
            basisu_obj = object_value(basisu, f"textures[{index}].KHR_texture_basisu")
            source = basisu_obj.get("source", source)
        if source is not None:
            indexed(images, source, f"textures[{index}].source")
        sampler = texture.get("sampler")
        if sampler is not None:
            indexed(array_value(document.get("samplers", []), "samplers"), sampler, f"textures[{index}].sampler")
        texture_image_sources.append(source)

    uses: list[dict[str, Any]] = []

    def walk(value: Any, material_index: int, path: str) -> None:
        if not isinstance(value, dict):
            return
        for key, nested in value.items():
            current = f"{path}.{key}" if path else key
            if key.lower().endswith("texture") and isinstance(nested, dict) and "index" in nested:
                texture_index = nonnegative_integer(nested["index"], f"materials[{material_index}].{current}.index")
                indexed(textures, texture_index, f"materials[{material_index}].{current}.index")
                uses.append({
                    "material_index": material_index,
                    "material_name": object_value(materials[material_index], f"materials[{material_index}]").get("name", f"Material {material_index + 1}"),
                    "slot": current,
                    "texture_index": texture_index,
                    "image_index": texture_image_sources[texture_index],
                })
            elif isinstance(nested, dict):
                walk(nested, material_index, current)

    for index, value in enumerate(materials):
        material = object_value(value, f"materials[{index}]")
        walk(material, index, "")
    return {
        "material_count": len(materials),
        "texture_count": len(textures),
        "image_count": len(images),
        "texture_use_count": len(uses),
        "textures_used_count": len({row["texture_index"] for row in uses}),
        "texture_uses": uses,
    }


def extension_report(document: dict[str, Any], warnings: list[str]) -> tuple[list[str], list[str], list[dict[str, str]]]:
    used_raw = array_value(document.get("extensionsUsed", []), "extensionsUsed")
    required_raw = array_value(document.get("extensionsRequired", []), "extensionsRequired")
    if any(not isinstance(name, str) or not name for name in [*used_raw, *required_raw]):
        raise AssetCheckError("extensionsUsed and extensionsRequired entries must be non-empty strings.")
    used = list(dict.fromkeys(used_raw))
    required = list(dict.fromkeys(required_raw))
    if not set(required).issubset(set(used)):
        raise AssetCheckError("Every extensionsRequired entry must also appear in extensionsUsed.")
    encountered: set[str] = set()

    def visit_extensions(value: Any) -> None:
        if isinstance(value, dict):
            extensions = value.get("extensions")
            if extensions is not None:
                extension_object = object_value(extensions, "extensions")
                encountered.update(extension_object)
            for key, nested in value.items():
                if key != "extras":
                    visit_extensions(nested)
        elif isinstance(value, list):
            for nested in value:
                visit_extensions(nested)

    visit_extensions(document)
    unlisted = sorted(encountered - set(used))
    if unlisted:
        raise AssetCheckError(f"Extension payload {unlisted[0]} is missing from extensionsUsed.")
    states: list[dict[str, str]] = []
    for name in required:
        if name in INSPECTED_EXTENSIONS:
            support = "counted from extension accessors"
        elif name in DECODER_EXTENSIONS:
            support = "decoder unavailable; compressed geometry stays unknown"
            warnings.append(f"Required extension {name} has no decoder in this tool.")
        else:
            support = "extension-specific behavior not interpreted"
            warnings.append(f"Required extension {name} is reported but its behavior is not interpreted.")
        states.append({"name": name, "status": support})
    return used, required, states


def budget(limit: int | None, measured: int | None, basis: str) -> dict[str, Any]:
    if limit is None:
        result = "not_set"
    elif measured is None:
        result = "unknown"
    else:
        result = "within" if measured <= limit else "over"
    return {"limit": limit, "measured": measured, "basis": basis, "result": result}


def make_report_data(path: Path, file_format: str, file_size: int, document: dict[str, Any],
                     binary_offset: int | None, binary_length: int | None,
                     max_triangles: int | None, max_bytes: int | None) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    views, footprint = build_resources(
        document, file_format, path, file_size, binary_offset, binary_length, warnings,
    )
    used_extensions, required_extensions, extension_states = extension_report(document, warnings)
    accessors, public_accessors = build_accessors(document, views)
    geometry, mesh_rows = build_geometry(document, accessors, views, set(required_extensions), warnings)
    scenes = build_scenes(document, mesh_rows, accessors, warnings)
    materials = collect_texture_uses(document)
    for row in footprint["resources"]:
        if row["status"] != "available":
            continue
        if row["storage"] not in {"embedded_glb", "embedded_data_uri", "embedded_buffer_view", "external_file"}:
            warnings.append(f"Resource {row.get('kind')} {row.get('index')} storage is not interpreted.")

    buffer_rows = [row for row in footprint["resources"] if row["kind"] == "buffer"]
    image_rows = [row for row in footprint["resources"] if row["kind"] == "image"]
    measured_triangles = None
    if scenes["active_scene_index"] is not None:
        measured_triangles = scenes["scenes"][scenes["active_scene_index"]]["triangle_candidates"]
    budgets = {
        "triangles": budget(max_triangles, measured_triangles, "active default scene triangle candidates after node and GPU-instance multiplicity"),
        "bytes": budget(max_bytes, footprint["total_asset_bytes"], "source file bytes plus unique local external resource files"),
    }
    for label, result in (("Triangle", budgets["triangles"]), ("Byte", budgets["bytes"])):
        if result["result"] == "over":
            warnings.append(f"{label} budget is over: measured {result['measured']} exceeds limit {result['limit']}.")
        elif result["result"] == "unknown":
            warnings.append(f"{label} budget is unknown because the required measure could not be validated.")

    resource_incomplete = any(row["status"] != "available" for row in [*buffer_rows, *image_rows])
    unavailable_required = any(row["status"] != "counted from extension accessors" for row in extension_states)
    attention = (resource_incomplete or unavailable_required
                 or scenes["active_scene_status"] != "known"
                 or any(row["geometry_status"] != "validated" for row in geometry["primitives"])
                 or any(result["result"] in {"over", "unknown"} for result in budgets.values()))
    status = "needs_attention" if attention else "ok"
    summary_triangles = "unknown" if measured_triangles is None else str(measured_triangles)
    summary = (
        f"Primitives: {geometry['primitive_count']}; mesh definitions: {geometry['mesh_count']}. "
        f"Active-scene triangle candidates: {summary_triangles}. Source file: {file_size} bytes."
    )
    data = {
        "source": {"name": path.name, "format": file_format, "bytes": file_size,
                   "asset_version": document["asset"]["version"], "generator": document["asset"].get("generator")},
        "geometry": {**geometry, "accessors": public_accessors},
        "scene": scenes,
        "materials": materials,
        "file_footprint": footprint,
        "extensions_used": used_extensions,
        "required_extensions": required_extensions,
        "required_extension_support": extension_states,
        "budgets": budgets,
        "validation": {
            "structure": "passed",
            "resource_completeness": "needs_attention" if resource_incomplete else "complete",
            "decoder_available": False,
            "geometry_count_basis": "range-checked accessor metadata; index values are not scanned, so triangle totals are candidate primitive counts and may include degenerate faces",
            "transform_basis": scenes["transform_note"],
        },
    }
    return {"status": status, "summary": summary, "data": data}, warnings


def human_bytes(value: int | None) -> str:
    if value is None:
        return "Unknown"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:,.0f} {unit}" if unit == "B" else f"{amount:,.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def metric_html(value: Any) -> str:
    return "Unknown" if value is None else html.escape(f"{value:,}" if isinstance(value, int) else str(value))


def report_html(receipt: dict[str, Any], source_path: Path, output_path: Path) -> str:
    data = receipt["data"]
    geometry = data["geometry"]
    scenes = data["scene"]
    footprint = data["file_footprint"]
    budgets = data["budgets"]
    source_relative = os.path.relpath(source_path, output_path.parent.resolve()).replace(os.sep, "/")
    source_href = urllib.parse.quote(source_relative, safe="/.-_")

    def budget_card(title: str, result: dict[str, Any], formatter) -> str:
        state = result["result"]
        measured = formatter(result["measured"])
        limit = "No limit supplied" if result["limit"] is None else f"Limit {formatter(result['limit'])}"
        return (f'<article class="card budget {html.escape(state)}"><div class="eyebrow">{html.escape(title)} budget</div>'
                f'<div class="metric">{measured}</div><div class="muted">{html.escape(limit)}</div>'
                f'<span class="pill {html.escape(state)}">{html.escape(state.replace("_", " "))}</span></article>')

    topology_rows = []
    for mode, values in geometry["topology"].items():
        if not values["primitive_count"]:
            continue
        topo_triangles = values["triangle_candidates"] if values["metric_status"] == "known" else None
        topology_rows.append(
            "<tr>"
            f"<td>{html.escape(mode)}</td><td>{metric_html(values['primitive_count'])}</td>"
            f"<td>{metric_html(values['declared_vertices'])}</td><td>{metric_html(values['element_count'])}</td>"
            f"<td>{metric_html(topo_triangles)}</td><td>{metric_html(values['line_segments'] if values['metric_status'] == 'known' else None)}</td>"
            f"<td>{metric_html(values['points'] if values['metric_status'] == 'known' else None)}</td>"
            "</tr>"
        )
    if not topology_rows:
        topology_rows.append('<tr><td colspan="7" class="muted">No mesh primitives found.</td></tr>')

    primitive_rows = []
    for row in geometry["primitives"]:
        primitive_rows.append(
            "<tr>"
            f"<td>{html.escape(str(row['mesh_name']))} / {row['primitive_index'] + 1}</td>"
            f"<td>{html.escape(row['mode'])}</td><td>{metric_html(row['declared_vertex_count'])}</td>"
            f"<td>{metric_html(row['element_count'])}</td><td>{metric_html(row['triangle_candidates'])}</td>"
            f"<td>{html.escape(row['geometry_status'].replace('_', ' '))}</td>"
            "</tr>"
        )
    if not primitive_rows:
        primitive_rows.append('<tr><td colspan="6" class="muted">No primitives found.</td></tr>')

    node_rows = []
    for scene in scenes["scenes"]:
        for node in scene["mesh_nodes"]:
            instancing = "GPU instanced" if node["gpu_instancing"] else "single node"
            node_rows.append(
                "<tr>"
                f"<td>{html.escape(str(scene['name']))}</td><td>{html.escape(str(node['node_name']))}</td>"
                f"<td>{html.escape(str(node['mesh_name']))}</td><td>{html.escape(node['local_transform_type'])}</td>"
                f"<td>{html.escape(instancing)} ({metric_html(node['gpu_instance_count'])})</td>"
                f"<td>{metric_html(node['triangle_candidates'])}</td>"
                "</tr>"
            )
    if not node_rows:
        node_rows.append('<tr><td colspan="6" class="muted">No mesh nodes in declared scenes.</td></tr>')

    resource_rows = []
    for row in footprint["resources"]:
        name = row.get("uri") or (f"bufferView {row.get('buffer_view')}" if row.get("buffer_view") is not None else row["storage"])
        resource_rows.append(
            "<tr>"
            f"<td>{html.escape(row['kind'].title())} {row['index']}</td><td>{html.escape(str(name))}</td>"
            f"<td>{html.escape(row['storage'].replace('_', ' '))}</td>"
            f"<td>{html.escape(row['status'].replace('_', ' '))}</td>"
            f"<td>{human_bytes(row['available_bytes'])}</td>"
            f"<td>{html.escape(row.get('diagnostic') or '—')}</td>"
            "</tr>"
        )
    if not resource_rows:
        resource_rows.append('<tr><td colspan="6" class="muted">No buffers or images referenced.</td></tr>')

    extension_rows = []
    for row in data["required_extension_support"]:
        extension_rows.append(f"<tr><td>{html.escape(row['name'])}</td><td>{html.escape(row['status'])}</td></tr>")
    if not extension_rows:
        extension_rows.append('<tr><td colspan="2" class="muted">No required extensions declared.</td></tr>')

    size_values = [footprint["source_file_bytes"], footprint["external_bytes"]]
    largest_size = max((value for value in size_values if value is not None), default=0)

    def size_row(label: str, value: int | None) -> str:
        width = 0 if value is None or largest_size == 0 else max(0, min(100, value / largest_size * 100))
        return (
            f'<div class="size-row"><span>{html.escape(label)}</span>'
            f'<div class="bar-track"><i class="bar-fill" style="width:{width:.2f}%"></i></div>'
            f'<strong>{html.escape(human_bytes(value))}</strong></div>'
        )

    payload_size_rows = size_row("Selected source file", footprint["source_file_bytes"])
    payload_size_rows += size_row("Unique local external resources", footprint["external_bytes"])

    material_rows = []
    for use in data["materials"]["texture_uses"]:
        image_ref = "—" if use["image_index"] is None else str(use["image_index"])
        material_rows.append(
            f"<tr><td>{html.escape(str(use['material_name']))}</td><td>{html.escape(use['slot'])}</td>"
            f"<td>{use['texture_index']}</td><td>{html.escape(image_ref)}</td></tr>"
        )
    if not material_rows:
        material_rows.append('<tr><td colspan="4" class="muted">No material texture references.</td></tr>')

    warning_items = "".join(f"<li>{html.escape(value)}</li>" for value in receipt["warnings"])
    if not warning_items:
        warning_items = "<li>No missing resource, decoder, or budget issues were found.</li>"
    selected_scene = None
    active_index = scenes["active_scene_index"]
    if active_index is not None:
        selected_scene = scenes["scenes"][active_index]
    active_triangles = None if selected_scene is None else selected_scene["triangle_candidates"]
    status = receipt["status"]
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Asset Check · {html.escape(data['source']['name'])}</title>
<style>
:root{{color-scheme:dark;--bg:#10151d;--panel:#171f2b;--line:#2b3747;--ink:#ecf2f8;--muted:#9eacbc;--teal:#59dbc2;--amber:#f1bc60;--red:#ff827c;--blue:#8ab7ff}}
*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(1000px 400px at 5% -8%,#173b3a 0,transparent 58%),var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{max-width:1240px;margin:0 auto;padding:48px 24px 72px}}h1,h2,p{{margin-top:0}}h1{{font-size:clamp(30px,5vw,50px);letter-spacing:-.045em;line-height:1.05;margin-bottom:14px}}h2{{font-size:18px;letter-spacing:-.02em;margin-bottom:8px}}a{{color:var(--teal)}}.eyebrow{{color:var(--teal);font-size:11px;font-weight:800;letter-spacing:.13em;text-transform:uppercase}}.muted,.subtle{{color:var(--muted)}}
.hero{{display:flex;justify-content:space-between;gap:24px;align-items:flex-end;padding:18px 0 30px;border-bottom:1px solid var(--line)}}.hero-copy{{max-width:760px}}.source{{overflow-wrap:anywhere;margin:0}}.status{{flex:none;padding:9px 14px;border:1px solid var(--line);border-radius:999px;font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.08em}}.status.ok,.pill.within{{color:var(--teal);border-color:#28685c;background:#13332f}}.status.needs_attention,.pill.over{{color:var(--red);border-color:#75423f;background:#381f21}}.pill.unknown,.pill.not_set{{color:var(--amber);border-color:#705a32;background:#302919}}
.grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:24px 0}}.card,.panel{{background:linear-gradient(150deg,#1b2633,var(--panel));border:1px solid var(--line);border-radius:16px;padding:18px}}.metric{{font-size:26px;font-weight:760;letter-spacing:-.035em;margin:8px 0 2px}}.budget{{position:relative;min-height:136px}}.pill{{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:3px 8px;margin-top:10px;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.08em}}.section{{margin-top:28px}}.section-head{{display:flex;justify-content:space-between;align-items:baseline;gap:16px;margin-bottom:10px}}.panel{{padding:0;overflow:hidden}}table{{width:100%;border-collapse:collapse;text-align:left;font-size:13px}}th,td{{padding:11px 13px;border-bottom:1px solid var(--line);vertical-align:top}}th{{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.1em;background:#121a24;white-space:nowrap}}tbody tr:last-child td{{border-bottom:0}}td{{overflow-wrap:anywhere}}.two-col{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}.two-col>*{{min-width:0}}.callout{{border-left:3px solid var(--blue);padding:12px 16px;background:#16202c;border-radius:0 12px 12px 0;color:var(--muted)}}.callout strong{{color:var(--ink)}}.warnings{{padding-left:20px;margin:8px 0 0}}.warnings li{{padding:3px 0;color:var(--muted)}}footer{{margin-top:32px;padding-top:16px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}}
.size-breakdown{{padding:18px 20px}}.size-row{{display:grid;grid-template-columns:minmax(160px,1fr) minmax(120px,2fr) 92px;gap:12px;align-items:center;padding:7px 0}}.size-row strong{{text-align:right;font-variant-numeric:tabular-nums}}.bar-track{{height:9px;border-radius:9px;background:#0c121a;overflow:hidden;border:1px solid #273341}}.bar-fill{{display:block;height:100%;border-radius:9px;background:linear-gradient(90deg,#388e86,var(--teal));min-width:0}}
@media(max-width:850px){{.grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}.two-col{{grid-template-columns:1fr}}main{{padding:30px 16px 56px}}}}
@media(max-width:540px){{.size-row{{grid-template-columns:1fr 84px;gap:8px}}.size-row .bar-track{{grid-column:1/-1;grid-row:2}}}}
@media(max-width:540px){{.hero{{align-items:flex-start;flex-direction:column}}.grid{{grid-template-columns:1fr 1fr;gap:8px}}.card{{padding:13px}}.metric{{font-size:21px}}.panel{{overflow-x:auto}}table{{min-width:680px}}}}
</style></head><body><main>
<header class="hero"><div class="hero-copy"><div class="eyebrow">glTF export inspection · {html.escape(data['source']['format'].upper())}</div>
<h1>Asset Check</h1><p class="source"><a href="{html.escape(source_href, quote=True)}">Source: {html.escape(data['source']['name'])}</a> · inspected {generated}</p>
<p class="subtle">{html.escape(receipt['summary'])}</p></div><div class="status {status}">{status.replace('_',' ')}</div></header>
<section class="grid">
<article class="card"><div class="eyebrow">Mesh primitives</div><div class="metric">{metric_html(geometry['primitive_count'])}</div><div class="muted">Mesh definitions: {metric_html(geometry['mesh_count'])}</div></article>
<article class="card"><div class="eyebrow">Declared vertices</div><div class="metric">{metric_html(geometry['declared_vertices_in_mesh_primitives'])}</div><div class="muted">Summed per primitive</div></article>
<article class="card"><div class="eyebrow">Scene draw overview</div><div class="metric">{metric_html(active_triangles)}</div><div class="muted">triangle candidates after node and instance counts</div></article>
<article class="card"><div class="eyebrow">Asset footprint</div><div class="metric">{human_bytes(footprint['total_asset_bytes'])}</div><div class="muted">source {human_bytes(footprint['source_file_bytes'])} · external {human_bytes(footprint['external_bytes'])}</div></article>
{budget_card('Triangle candidates', budgets['triangles'], metric_html)}
{budget_card('Total bytes', budgets['bytes'], human_bytes)}
<article class="card"><div class="eyebrow">Material use</div><div class="metric">{data['materials']['material_count']}</div><div class="muted">Texture slots: {data['materials']['texture_use_count']} · images: {data['materials']['image_count']}</div></article>
<article class="card"><div class="eyebrow">Required extensions</div><div class="metric">{len(data['required_extensions'])}</div><div class="muted">{html.escape(', '.join(data['required_extensions']) or 'None declared')}</div></article>
</section>
<section class="section"><div class="section-head"><div><div class="eyebrow">Transfer footprint</div><h2>Payload sizes</h2></div><span class="muted">Bars compare source bytes with unique external file bytes.</span></div><div class="panel size-breakdown">{payload_size_rows}</div></section>
<section class="section"><div class="section-head"><div><div class="eyebrow">Drawable topology</div><h2>Topology and primitives</h2></div><span class="muted">Triangle candidates use accessor counts; degenerate index values are not removed.</span></div>
<div class="panel"><table><thead><tr><th>Mode</th><th>Primitives</th><th>Declared vertices</th><th>Elements</th><th>Triangle candidates</th><th>Line segments</th><th>Points</th></tr></thead><tbody>{''.join(topology_rows)}</tbody></table></div>
<div class="panel" style="margin-top:10px"><table><thead><tr><th>Primitive</th><th>Mode</th><th>POSITION count</th><th>Elements</th><th>Triangles</th><th>Metric state</th></tr></thead><tbody>{''.join(primitive_rows)}</tbody></table></div></section>
<section class="section"><div class="section-head"><div><div class="eyebrow">Scene graph</div><h2>Node reuse and transforms</h2></div></div>
<p class="callout"><strong>How counts work.</strong> {html.escape(scenes['transform_note'])}</p>
<div class="panel"><table><thead><tr><th>Scene</th><th>Node</th><th>Mesh</th><th>Local transform</th><th>Instance multiplier</th><th>Triangle candidates</th></tr></thead><tbody>{''.join(node_rows)}</tbody></table></div>
</section>
<section class="section two-col"><div><div class="section-head"><div><div class="eyebrow">Material wiring</div><h2>Texture usage</h2></div></div><div class="panel"><table><thead><tr><th>Material</th><th>Slot</th><th>Texture</th><th>Image</th></tr></thead><tbody>{''.join(material_rows)}</tbody></table></div></div>
<div><div class="section-head"><div><div class="eyebrow">Compatibility</div><h2>Required extensions</h2></div></div><div class="panel"><table><thead><tr><th>Extension</th><th>Inspection state</th></tr></thead><tbody>{''.join(extension_rows)}</tbody></table></div></div></section>
<section class="section"><div class="section-head"><div><div class="eyebrow">Payload inventory</div><h2>Embedded and external resources</h2></div><span class="muted">Remote URIs are never fetched.</span></div>
<div class="panel"><table><thead><tr><th>Resource</th><th>URI / source</th><th>Storage</th><th>State</th><th>Available bytes</th><th>Note</th></tr></thead><tbody>{''.join(resource_rows)}</tbody></table></div></section>
<section class="section"><div class="section-head"><div><div class="eyebrow">Review notes</div><h2>Items to check</h2></div></div><div class="panel" style="padding:16px"><ul class="warnings">{warning_items}</ul></div></section>
<footer>Generated by Asset Check. This is a static metadata report, not a rendered mesh preview or a universal performance score. Source size plus unique local resource files is the byte-budget basis. {geometry['accessor_count']} accessors were structurally checked.</footer>
</main></body></html>"""


def write_new_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o644)
    except FileExistsError as exc:
        raise AssetCheckError(f"Refusing to overwrite existing output: {path.name}.", "output_exists") from exc
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
            raise AssetCheckError(f"Output directory is unavailable for {path.name}.", "output_unavailable") from exc
        raise AssetCheckError(f"Output could not be created: {path.name}.", "output_unavailable") from exc
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
    except OSError as exc:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise AssetCheckError(f"Output could not be written: {path.name}.", "output_unavailable") from exc


def positive_or_zero(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative whole number") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative whole number")
    return parsed


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect a selected glTF/GLB export's geometry metadata, scene instances, resources, and explicit budgets."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="inspect one explicit .gltf or .glb export")
    inspect.add_argument("--input", required=True, help="path to one .gltf or .glb file")
    inspect.add_argument("--max-triangles", type=positive_or_zero, help="user-supplied cap for active-scene triangle candidates")
    inspect.add_argument("--max-bytes", type=positive_or_zero, help="user-supplied cap for source plus unique local external files")
    inspect.add_argument("--report", type=Path, help="write a standalone static HTML report; the file must not already exist")
    inspect.add_argument("--receipt", type=Path, help="write the JSON envelope to a new file; the file must not already exist")
    return parser


def run_inspection(args: argparse.Namespace) -> tuple[dict[str, Any], list[Path]]:
    path, file_format, file_size, document, binary_offset, binary_length = load_document(args.input)
    outputs: list[Path] = []
    for raw_output in (args.report, args.receipt):
        if raw_output is None:
            continue
        output = raw_output.expanduser().resolve(strict=False)
        if output == path:
            raise AssetCheckError("Report and receipt paths must not overwrite the selected source file.", "output_conflict")
        if output in outputs:
            raise AssetCheckError("Report and receipt paths must be different.", "output_conflict")
        outputs.append(output)

    result, warnings = make_report_data(
        path, file_format, file_size, document, binary_offset, binary_length,
        args.max_triangles, args.max_bytes,
    )
    artifacts: list[dict[str, str]] = []
    report_path: Path | None = None
    receipt_path: Path | None = None
    output_iter = iter(outputs)
    if args.report is not None:
        report_path = next(output_iter)
        artifacts.append({"path": str(report_path), "label": "Asset inspection report", "media_type": "text/html"})
    if args.receipt is not None:
        receipt_path = next(output_iter)
        artifacts.append({"path": str(receipt_path), "label": "JSON receipt", "media_type": "application/json"})
    receipt = envelope(result["status"], result["summary"], result["data"], artifacts, warnings)
    report_content = report_html(receipt, path, report_path).encode("utf-8") if report_path else None
    receipt_content = (json.dumps(receipt, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8") if receipt_path else None
    created: list[Path] = []
    try:
        if report_path is not None and report_content is not None:
            write_new_file(report_path, report_content)
            created.append(report_path)
        if receipt_path is not None and receipt_content is not None:
            write_new_file(receipt_path, receipt_content)
            created.append(receipt_path)
    except AssetCheckError:
        for created_path in created:
            try:
                created_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return receipt, created


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        receipt, _ = run_inspection(args)
    except AssetCheckError as exc:
        print(json.dumps(envelope("error", str(exc), {"error_code": exc.code}), ensure_ascii=False))
        return 1
    except OSError:
        print(json.dumps(envelope("error", "The selected asset could not be inspected due to an operating system error.",
                                  {"error_code": "io_error"}), ensure_ascii=False))
        return 1
    print(json.dumps(receipt, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
