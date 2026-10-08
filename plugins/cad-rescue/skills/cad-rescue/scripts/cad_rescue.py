#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Inspect, safely preview, convert, or recover explicitly selected CAD files."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any, Callable


TOOL = "cad-rescue"
SCHEMA_VERSION = 1
BRIDGE = Path(__file__).with_name("libredwg_bridge.mjs")
MAX_DXF_BYTES = 256 * 1024 * 1024
MAX_SVG_BYTES = 32 * 1024 * 1024
MAX_SVG_ELEMENTS = 150_000
MAX_SVG_DEPTH = 256
MAX_PATH_COMMANDS = 250_000
SVG_NS = "http://www.w3.org/2000/svg"
SVG_PRESENTATION_ATTRS = {
    "fill",
    "fill-rule",
    "stroke",
    "stroke-width",
    "stroke-linecap",
    "stroke-linejoin",
    "stroke-dasharray",
    "stroke-dashoffset",
    "opacity",
}
SVG_GEOMETRY_ATTRS = {
    "path": {"d"},
    "line": {"x1", "y1", "x2", "y2"},
    "polyline": {"points"},
    "polygon": {"points"},
    "rect": {"x", "y", "width", "height", "rx", "ry"},
    "circle": {"cx", "cy", "r"},
    "ellipse": {"cx", "cy", "rx", "ry"},
}
SVG_GEOMETRY_TAGS = set(SVG_GEOMETRY_ATTRS)
SVG_NUMERIC_LIST_RE = re.compile(r"^[\dEe+.,\-\s]+$")
SVG_PATH_DATA_RE = re.compile(r"^[MmZzLlHhVvCcSsQqTtAaEe0-9+.,\-\s]*$")
SVG_NUMBER_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
SVG_TRANSFORM_RE = re.compile(
    r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^()]*)\)"
)
SVG_TRANSFORM_ARITIES = {
    "matrix": {6},
    "translate": {1, 2},
    "scale": {1, 2},
    "rotate": {1, 3},
    "skewX": {1},
    "skewY": {1},
}
SVG_COLOR_RE = re.compile(r"^(?:#[0-9a-fA-F]{3,8}|[A-Za-z]{1,24})$")
SVG_LENGTH_RE = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(?:px)?$")


class CadRescueError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: str = "error",
        exit_code: int = 1,
        details: dict[str, Any] | None = None,
        warnings: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.exit_code = exit_code
        self.details = details or {}
        self.warnings = warnings or []


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        _emit(
            _envelope(
                "error",
                "The command arguments are incomplete or invalid.",
                data={"error_code": "invalid_arguments", "reason": message},
            )
        )
        self.exit(2)


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


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))


def _attention(code: str, message: str, **details: Any) -> CadRescueError:
    return CadRescueError(
        code,
        message,
        status="needs_attention",
        exit_code=0,
        details=details,
        warnings=[{"code": code, "message": message}],
    )


def _dependency() -> tuple[Any, Any, Any, Any, Any, Any]:
    try:
        import ezdxf
        from ezdxf import bbox, disassemble, recover, units
        from ezdxf.path import Command, make_path
    except ImportError as exc:
        raise CadRescueError(
            "dependency_missing",
            "DXF operations require ezdxf. Install it with `python3 -m pip install 'ezdxf>=1.4.4,<2'`.",
        ) from exc
    return ezdxf, bbox, disassemble, recover, units, (Command, make_path)


def _input_path(raw: str, *, allowed_suffixes: set[str]) -> Path:
    if not raw:
        raise CadRescueError("input_required", "Choose one drawing file explicitly.")
    path = Path(raw).expanduser()
    try:
        info = path.stat()
    except OSError as exc:
        raise _attention("input_unavailable", "The selected drawing cannot be read.", reason=exc.strerror or exc.__class__.__name__) from exc
    if not stat.S_ISREG(info.st_mode):
        raise _attention("input_unavailable", "The selected path is not a regular file.")
    if path.suffix.lower() not in allowed_suffixes:
        suffixes = ", ".join(sorted(allowed_suffixes))
        raise _attention("unsupported_format", f"This command supports {suffixes}.", extension=path.suffix.lower())
    max_bytes = MAX_SVG_BYTES if path.suffix.lower() == ".svg" else MAX_DXF_BYTES
    if info.st_size > max_bytes:
        raise _attention(
            "input_too_large",
            f"The selected file exceeds the {max_bytes // (1024 * 1024)} MiB input limit.",
            size_bytes=int(info.st_size),
            limit_bytes=max_bytes,
        )
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(output: Path, content: bytes, *, source: Path | None = None) -> dict[str, Any]:
    target = output.expanduser().absolute()
    if source is not None:
        try:
            if target.resolve(strict=False) == source.resolve(strict=True):
                raise CadRescueError("same_input_output", "Choose an output path separate from the source drawing.")
        except OSError:
            pass
    if not target.parent.is_dir():
        raise _attention("output_directory_missing", "The output directory must already exist.")
    if os.path.lexists(target):
        raise CadRescueError("output_exists", "The chosen output already exists; no file was replaced.")
    temporary: Path | None = None
    try:
        fd, raw_temp = tempfile.mkstemp(prefix=f".{target.name}.cad-rescue-", dir=target.parent)
        temporary = Path(raw_temp)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
    except FileExistsError as exc:
        raise CadRescueError("output_exists", "The chosen output already exists; no file was replaced.") from exc
    except OSError as exc:
        raise CadRescueError(
            "output_write_failed",
            "The preview could not be written safely.",
            details={"reason": exc.strerror or exc.__class__.__name__},
        ) from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return {"path": str(target), "sha256": _sha256(target), "size_bytes": target.stat().st_size}


def _preflight_output(output: Path, source: Path) -> Path:
    target = output.expanduser().absolute()
    if target.resolve(strict=False) == source.resolve(strict=True):
        raise CadRescueError("same_input_output", "Choose an output path separate from the source drawing.")
    if os.path.lexists(target):
        raise CadRescueError("output_exists", "The chosen output already exists; no file was replaced.")
    if not target.parent.is_dir():
        raise _attention("output_directory_missing", "The output directory must already exist.")
    return target


def _audit_item(item: Any) -> dict[str, str]:
    code = getattr(item, "code", None)
    code_name = getattr(code, "name", None) or (str(code) if code is not None else "unknown")
    message = getattr(item, "message", None)
    if not isinstance(message, str) or not message:
        message = code_name.replace("_", " ").lower()
    entity = getattr(item, "dxf_entity", None)
    entity_type = entity.dxftype() if entity is not None and hasattr(entity, "dxftype") else ""
    result = {"code": code_name, "message": message}
    if entity_type:
        result["entity_type"] = entity_type
    return result


def _dxf_bounds(document: Any, bbox_module: Any) -> dict[str, float] | None:
    try:
        bounds = bbox_module.extents(document.modelspace(), fast=False)
        if not bounds.has_data:
            return None
        minimum, maximum = bounds.extmin, bounds.extmax
        values = {
            "min_x": float(minimum.x),
            "min_y": float(minimum.y),
            "min_z": float(minimum.z),
            "max_x": float(maximum.x),
            "max_y": float(maximum.y),
            "max_z": float(maximum.z),
        }
        if not all(math.isfinite(value) for value in values.values()):
            return None
        return values
    except Exception:
        return None


def _read_dxf_document(path: Path, ezdxf_module: Any) -> Any:
    try:
        return ezdxf_module.readfile(path)
    except Exception as exc:
        raise _attention(
            "dxf_parse_failed",
            "The DXF parser could not read this drawing. Try DXF Recovery if the file appears truncated.",
            reason=exc.__class__.__name__,
        ) from exc


def _inspect_dxf(
    path: Path,
    *,
    source_format: str = "dxf",
    converter: dict[str, str] | None = None,
    parsed_document: Any | None = None,
) -> dict[str, Any]:
    ezdxf, bbox_module, _disassemble, _recover, units, _path_api = _dependency()
    document = parsed_document if parsed_document is not None else _read_dxf_document(path, ezdxf)
    try:
        auditor = document.audit()
    except Exception as exc:
        auditor = None
        audit_error = exc.__class__.__name__
    else:
        audit_error = ""
    entity_count = 0
    by_type: Counter[str] = Counter()
    by_layer: dict[str, Counter[str]] = {}
    for entity in document.modelspace():
        entity_count += 1
        entity_type = entity.dxftype()
        by_type[entity_type] += 1
        layer = str(entity.dxf.get("layer", "0"))
        by_layer.setdefault(layer, Counter())[entity_type] += 1
    unit_code = int(document.units)
    unit_name = str(units.unit_name(unit_code))
    bounds = _dxf_bounds(document, bbox_module)
    fixes = [_audit_item(item) for item in getattr(auditor, "fixes", [])]
    errors = [_audit_item(item) for item in getattr(auditor, "errors", [])]
    warnings: list[dict[str, str]] = []
    if fixes:
        warnings.append({"code": "audit_fixes", "message": f"The in-memory audit reported {len(fixes)} possible repairs; the source file was not changed."})
    if errors:
        warnings.append({"code": "audit_errors", "message": f"The in-memory audit found {len(errors)} unresolved drawing issues."})
    if audit_error:
        warnings.append({"code": "audit_failed", "message": f"The drawing audit stopped with {audit_error}."})
    if source_format == "dwg":
        warnings.append({
            "code": "dwg_converted_for_inspection",
            "message": "Geometry and units are reported from LibreDWG's temporary DXF export; unsupported DWG features may not transfer.",
        })
    data: dict[str, Any] = {
        "source_file": path.name if source_format == "dxf" else None,
        "format": source_format,
        "parser": {"name": "ezdxf", "version": ezdxf.__version__},
        "entity_count": entity_count,
        "entity_types": dict(sorted(by_type.items())),
        "layers": {
            name: {"entities": sum(counts.values()), "entity_types": dict(sorted(counts.items()))}
            for name, counts in sorted(by_layer.items())
        },
        "units": {
            "code": unit_code,
            "name": unit_name,
            "status": "not_declared" if unit_code == 0 else "declared",
        },
        "extents": bounds,
        "extents_basis": "computed_modelspace_entities",
        "audit": {"fixes": fixes, "errors": errors},
    }
    if converter:
        data["conversion"] = converter
    if not entity_count:
        warnings.append({"code": "empty_drawing", "message": "The drawing has no model-space entities to inspect."})
        return _envelope("needs_attention", "The drawing has no model-space entities to inspect.", data=data, warnings=warnings)
    if bounds is None:
        warnings.append({"code": "extents_unavailable", "message": "No finite model-space bounds could be computed."})
    status = "needs_attention" if errors or bounds is None else "ok"
    summary = (
        f"Inspected {entity_count} model-space entities on {len(by_layer)} layers."
        if status == "ok"
        else f"Read {entity_count} model-space entities, with drawing issues that need review."
    )
    return _envelope(status, summary, data=data, warnings=warnings)


def _number(raw: str | None) -> float | None:
    if raw is None:
        return None
    match = SVG_LENGTH_RE.fullmatch(raw.strip())
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _svg_viewbox(root: ET.Element) -> tuple[float, float, float, float]:
    raw = root.attrib.get("viewBox")
    if raw:
        if not SVG_NUMERIC_LIST_RE.fullmatch(raw):
            raise _attention("svg_viewbox_invalid", "The SVG viewBox must contain four finite numbers.")
        try:
            values = [float(part) for part in re.split(r"[\s,]+", raw.strip()) if part]
        except ValueError as exc:
            raise _attention("svg_viewbox_invalid", "The SVG viewBox must contain four finite numbers.") from exc
        if len(values) == 4 and all(math.isfinite(value) for value in values) and values[2] > 0 and values[3] > 0 and math.isfinite(values[0] + values[2]) and math.isfinite(values[1] + values[3]):
            return values[0], values[1], values[2], values[3]
        raise _attention("svg_viewbox_invalid", "The SVG viewBox must have positive width and height.")
    width = _number(root.attrib.get("width"))
    height = _number(root.attrib.get("height"))
    if width is None or height is None or width <= 0 or height <= 0:
        raise _attention("svg_viewbox_missing", "The SVG needs a viewBox or numeric width and height.")
    return 0.0, 0.0, width, height


def _read_svg(path: Path) -> tuple[ET.Element, tuple[float, float, float, float], int]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _attention("input_unavailable", "The selected SVG cannot be read.", reason=exc.strerror or exc.__class__.__name__) from exc
    if len(raw) > MAX_SVG_BYTES:
        raise _attention("input_too_large", "The SVG grew beyond the safe input limit while it was being read.", limit_bytes=MAX_SVG_BYTES)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _attention("svg_encoding_unsupported", "SVG input must use UTF-8 encoding.") from exc
    if re.search(r"<!\s*(DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
        raise _attention("svg_external_declarations", "SVG document type and entity declarations are not supported.")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise _attention("svg_parse_failed", "The SVG is malformed and could not be inspected.", reason=str(exc)[:200]) from exc
    if root.tag.split("}")[-1].lower() != "svg":
        raise _attention("svg_root_invalid", "The selected XML file does not have an SVG root element.")
    namespace = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
    if namespace not in {"", SVG_NS}:
        raise _attention("svg_namespace_unsupported", "The SVG root uses an unsupported XML namespace.")
    node_count = sum(1 for _ in root.iter())
    if node_count > MAX_SVG_ELEMENTS:
        raise _attention("svg_too_many_elements", "The SVG contains too many elements to preview safely.", elements=node_count)
    stack: list[tuple[ET.Element, int]] = [(root, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_SVG_DEPTH:
            raise _attention("svg_too_deep", "The SVG element nesting is too deep to preview safely.")
        stack.extend((child, depth + 1) for child in current)
    return root, _svg_viewbox(root), node_count


def _svg_inspection(path: Path) -> dict[str, Any]:
    root, (x, y, width, height), node_count = _read_svg(path)
    counts = Counter(
        element.tag.split("}")[-1].lower()
        for element in root.iter()
        if element is not root and element.tag.split("}")[-1].lower() in SVG_GEOMETRY_TAGS
    )
    geometry_count = sum(counts.values())
    warnings = [{
        "code": "svg_viewbox_extents",
        "message": "SVG bounds use the declared viewBox; path geometry, clipping, masks and group transforms are not measured.",
    }]
    if geometry_count == 0:
        warnings.append({"code": "svg_no_supported_geometry", "message": "No supported static vector geometry was found."})
    data = {
        "source_file": path.name,
        "format": "svg",
        "parser": {"name": "Python XML parser", "version": sys.version.split()[0]},
        "entity_count": geometry_count,
        "entity_types": dict(sorted(counts.items())),
        "layers": {},
        "layer_status": "not_defined_by_svg",
        "units": {"status": "not_declared", "name": None},
        "extents": {"min_x": x, "min_y": y, "max_x": x + width, "max_y": y + height},
        "extents_basis": "source_viewbox",
        "xml_element_count": node_count,
    }
    status = "ok" if geometry_count else "needs_attention"
    summary = f"Inspected {geometry_count} static SVG geometry elements." if geometry_count else "The SVG contains no supported static vector geometry."
    return _envelope(status, summary, data=data, warnings=warnings)


def _safe_transform(raw: str) -> str | None:
    cursor = 0
    found = False
    for match in SVG_TRANSFORM_RE.finditer(raw):
        if raw[cursor:match.start()].strip(" ,\t\r\n"):
            return None
        args = match.group(2)
        value_count = 0
        arg_cursor = 0
        for number in SVG_NUMBER_RE.finditer(args):
            separator = args[arg_cursor:number.start()]
            if separator.strip(" ,\t\r\n") or (value_count and not separator):
                return None
            if not math.isfinite(float(number.group())):
                return None
            value_count += 1
            if value_count > 6:
                return None
            arg_cursor = number.end()
        if args[arg_cursor:].strip(" ,\t\r\n") or value_count not in SVG_TRANSFORM_ARITIES[match.group(1)]:
            return None
        cursor = match.end()
        found = True
    if not found or raw[cursor:].strip(" ,\t\r\n"):
        return None
    return raw


def _safe_svg_attr(name: str, value: str) -> str | None:
    if name in {"x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "width", "height", "stroke-width", "stroke-dashoffset"}:
        return value if _number(value) is not None else None
    if name in {"d"}:
        return value if len(value) <= 2_000_000 and SVG_PATH_DATA_RE.fullmatch(value) else None
    if name == "points":
        return value if len(value) <= 2_000_000 and SVG_NUMERIC_LIST_RE.fullmatch(value) else None
    if name == "transform":
        return _safe_transform(value)
    if name in {"fill", "stroke"}:
        value = value.strip()
        return value if value in {"none", "currentColor", "transparent"} or SVG_COLOR_RE.fullmatch(value) else None
    if name == "opacity":
        number = _number(value)
        return value if number is not None and 0 <= number <= 1 else None
    if name in {"fill-rule"}:
        return value if value in {"nonzero", "evenodd"} else None
    if name in {"stroke-linecap"}:
        return value if value in {"butt", "round", "square"} else None
    if name in {"stroke-linejoin"}:
        return value if value in {"miter", "round", "bevel"} else None
    if name == "stroke-dasharray":
        return value if value == "none" or SVG_NUMERIC_LIST_RE.fullmatch(value) else None
    return None


def _safe_svg_style(raw_style: str) -> tuple[dict[str, str], list[str], bool]:
    attributes: dict[str, str] = {}
    invalid: list[str] = []
    invalid_transform = False
    if len(raw_style) > 20_000:
        return attributes, ["style"], invalid_transform
    for declaration in raw_style.split(";"):
        if ":" not in declaration:
            continue
        name, value = declaration.split(":", 1)
        name = name.strip().lower()
        if name == "transform":
            invalid_transform = True
            continue
        if name not in SVG_PRESENTATION_ATTRS:
            continue
        safe_value = _safe_svg_attr(name, value.strip())
        if safe_value is None:
            invalid.append(name)
        else:
            attributes[name] = safe_value
    return attributes, invalid, invalid_transform


def _safe_svg(source: Path) -> tuple[bytes, dict[str, Any], list[dict[str, str]]]:
    root, viewbox, node_count = _read_svg(source)
    x, y, width, height = viewbox
    output_viewbox = _fit_viewbox(x, y, width, height)
    svg_root = ET.Element(
        f"{{{SVG_NS}}}svg",
        {
            "width": "1600",
            "height": "1200",
            "viewBox": " ".join(_fmt(value) for value in output_viewbox),
            "preserveAspectRatio": "xMidYMid meet",
        },
    )
    ET.SubElement(
        svg_root,
        f"{{{SVG_NS}}}rect",
        {"x": _fmt(output_viewbox[0]), "y": _fmt(output_viewbox[1]), "width": _fmt(output_viewbox[2]), "height": _fmt(output_viewbox[3]), "fill": "#ffffff", "stroke": "none"},
    )
    geometry_count = 0
    skipped: Counter[str] = Counter()
    invalid: Counter[str] = Counter()

    def copy_element(original: ET.Element, parent: ET.Element, depth: int) -> None:
        nonlocal geometry_count
        local = original.tag.split("}")[-1].lower()
        namespace = original.tag[1:].split("}", 1)[0] if original.tag.startswith("{") else ""
        if namespace not in {"", SVG_NS}:
            skipped["foreign_namespace"] += 1
            return
        if depth > MAX_SVG_DEPTH:
            skipped["over_depth"] += 1
            return
        if local not in SVG_GEOMETRY_TAGS | {"g"}:
            if local in {"svg", "title", "desc"}:
                for child in original:
                    copy_element(child, parent, depth + 1)
                return
            skipped[local] += 1
            return
        attrs: dict[str, str] = {}
        allowed = SVG_PRESENTATION_ATTRS | (SVG_GEOMETRY_ATTRS.get(local, set()))
        allowed |= {"transform"}
        inline_style: dict[str, str] = {}
        invalid_transform = False
        for key, raw_value in original.attrib.items():
            attr = key.split("}")[-1].lower()
            if attr == "style":
                style_attrs, invalid_attrs, bad_transform = _safe_svg_style(raw_value)
                inline_style.update(style_attrs)
                for invalid_attr in invalid_attrs:
                    invalid[invalid_attr] += 1
                invalid_transform |= bad_transform
                continue
            if attr not in allowed:
                continue
            value = _safe_svg_attr(attr, raw_value)
            if value is None:
                invalid[attr] += 1
                if attr == "transform":
                    invalid_transform = True
            else:
                attrs[attr] = value
        attrs.update(inline_style)
        if invalid_transform:
            skipped["invalid_transform"] += 1
            return
        required = SVG_GEOMETRY_ATTRS.get(local, set())
        if any(name not in attrs for name in required):
            if local != "g":
                skipped[f"invalid_{local}"] += 1
                return
        clone = ET.SubElement(parent, f"{{{SVG_NS}}}{local}", attrs)
        if local in SVG_GEOMETRY_TAGS:
            geometry_count += 1
        for child in original:
            copy_element(child, clone, depth + 1)

    ET.register_namespace("", SVG_NS)
    for child in root:
        copy_element(child, svg_root, 1)
    if geometry_count == 0:
        raise _attention("svg_no_supported_geometry", "The SVG contains no safe, supported vector geometry to preview.")
    warnings: list[dict[str, str]] = [{
        "code": "active_svg_content_removed",
        "message": "The preview was regenerated from static vector elements; scripts, links, images and foreign objects were removed. Only approved paint attributes are retained.",
    }]
    if skipped:
        warnings.append({
            "code": "svg_elements_omitted",
            "message": "Unsupported SVG elements were omitted: " + ", ".join(f"{name} ({count})" for name, count in sorted(skipped.items())) + ".",
        })
    if invalid:
        warnings.append({
            "code": "svg_attributes_omitted",
            "message": "Malformed or unsupported SVG attributes were omitted from the safe preview.",
        })
    report = {
        "format": "svg",
        "preview_kind": "sanitized_static_vector",
        "geometry_elements": geometry_count,
        "entity_count": geometry_count,
        "source_viewbox": {"min_x": x, "min_y": y, "width": width, "height": height},
        "extents_basis": "source_viewbox",
        "output_viewbox": {"min_x": output_viewbox[0], "min_y": output_viewbox[1], "width": output_viewbox[2], "height": output_viewbox[3]},
        "source_xml_element_count": node_count,
        "units": {"status": "not_declared", "name": None},
    }
    return ET.tostring(svg_root, encoding="utf-8", xml_declaration=True), report, warnings


def _fmt(value: float) -> str:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("non-finite CAD coordinate")
    return format(value, ".12g")


def _fit_viewbox(x: float, y: float, width: float, height: float, aspect: float = 4 / 3) -> tuple[float, float, float, float]:
    if width / height < aspect:
        fitted_width = height * aspect
        x -= (fitted_width - width) / 2
        width = fitted_width
    else:
        fitted_height = width / aspect
        y -= (fitted_height - height) / 2
        height = fitted_height
    return x, y, width, height


def _path_svg_data(path: Any, Command: Any) -> str:
    parts = [f"M {_fmt(path.start.x)} {_fmt(path.start.y)}"]
    commands = path.commands()
    if len(commands) > MAX_PATH_COMMANDS:
        raise ValueError("path command limit exceeded")
    for command in commands:
        if command.type == Command.LINE_TO:
            parts.append(f"L {_fmt(command.end.x)} {_fmt(command.end.y)}")
        elif command.type == Command.CURVE3_TO:
            parts.append(
                f"Q {_fmt(command.ctrl.x)} {_fmt(command.ctrl.y)} {_fmt(command.end.x)} {_fmt(command.end.y)}"
            )
        elif command.type == Command.CURVE4_TO:
            parts.append(
                "C "
                f"{_fmt(command.ctrl1.x)} {_fmt(command.ctrl1.y)} "
                f"{_fmt(command.ctrl2.x)} {_fmt(command.ctrl2.y)} "
                f"{_fmt(command.end.x)} {_fmt(command.end.y)}"
            )
        elif command.type == Command.MOVE_TO:
            parts.append(f"M {_fmt(command.end.x)} {_fmt(command.end.y)}")
    if path.is_closed:
        parts.append("Z")
    return " ".join(parts)


def _dxf_preview(path: Path, *, source_format: str = "dxf", converter: dict[str, str] | None = None) -> tuple[bytes, dict[str, Any], list[dict[str, str]]]:
    ezdxf, _bbox_module, disassemble, _recover, _units, path_api = _dependency()
    document = _read_dxf_document(path, ezdxf)
    result = _inspect_dxf(path, source_format=source_format, converter=converter, parsed_document=document)
    if result["status"] == "needs_attention" and result["data"].get("entity_count", 0) == 0:
        raise _attention("empty_drawing", "The drawing has no model-space geometry to preview.", report=result["data"], warnings=result["warnings"])
    bounds = result["data"]["extents"]
    Command, make_path = path_api
    path_data: list[str] = []
    unsupported: Counter[str] = Counter()
    drawable_bounds: list[float] | None = None
    expanded_entity_count = 0
    for entity in disassemble.recursive_decompose(document.modelspace()):
        expanded_entity_count += 1
        if expanded_entity_count > 250_000:
            raise _attention("preview_entity_limit", "The expanded drawing contains too many entities to preview safely.", entities=expanded_entity_count)
        try:
            geometry = make_path(entity)
            if len(geometry) == 0:
                unsupported[entity.dxftype()] += 1
                continue
            data = _path_svg_data(geometry, Command)
            if data:
                path_bounds = geometry.bbox()
                if not path_bounds.has_data:
                    unsupported[entity.dxftype()] += 1
                    continue
                current = [
                    float(path_bounds.extmin.x),
                    float(path_bounds.extmin.y),
                    float(path_bounds.extmax.x),
                    float(path_bounds.extmax.y),
                ]
                if not all(math.isfinite(value) for value in current):
                    unsupported[entity.dxftype()] += 1
                    continue
                if drawable_bounds is None:
                    drawable_bounds = current
                else:
                    drawable_bounds[0] = min(drawable_bounds[0], current[0])
                    drawable_bounds[1] = min(drawable_bounds[1], current[1])
                    drawable_bounds[2] = max(drawable_bounds[2], current[2])
                    drawable_bounds[3] = max(drawable_bounds[3], current[3])
                path_data.append(data)
        except Exception:
            unsupported[entity.dxftype()] += 1
            continue
    if not path_data:
        raise _attention(
            "no_preview_geometry",
            "The drawing contains entities this preview renderer cannot represent.",
            entity_types=dict(sorted(unsupported.items())),
        )
    if drawable_bounds is None:
        raise _attention("extents_unavailable", "No finite preview geometry bounds could be computed.")
    minimum_x, minimum_y, maximum_x, maximum_y = drawable_bounds
    width = maximum_x - minimum_x
    height = maximum_y - minimum_y
    if width < 1e-9:
        width = max(1.0, abs(minimum_x) * 1e-9)
        minimum_x -= width / 2
    if height < 1e-9:
        height = max(1.0, abs(minimum_y) * 1e-9)
        minimum_y -= height / 2
    margin = max(width, height) * 0.02
    view_x, view_y = minimum_x - margin, minimum_y - margin
    view_width, view_height = width + margin * 2, height + margin * 2
    view_x, view_y, view_width, view_height = _fit_viewbox(view_x, view_y, view_width, view_height)
    transform_y = 2 * view_y + view_height
    body = "\n".join(
        f'<path d="{data}" fill="none" stroke="#17212b" stroke-width="1.25" vector-effect="non-scaling-stroke" stroke-linecap="round" stroke-linejoin="round"/>'
        for data in path_data
    )
    background = f'<rect x="{_fmt(view_x)}" y="{_fmt(view_y)}" width="{_fmt(view_width)}" height="{_fmt(view_height)}" fill="#ffffff"/>'
    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="{SVG_NS}" width="1600" height="1200" viewBox="{_fmt(view_x)} {_fmt(view_y)} {_fmt(view_width)} {_fmt(view_height)}" preserveAspectRatio="xMidYMid meet">\n'
        f'{background}\n<g transform="translate(0 {_fmt(transform_y)}) scale(1 -1)">\n{body}\n</g>\n</svg>\n'
    )
    warnings = list(result["warnings"])
    if unsupported:
        warnings.append({
            "code": "entities_omitted",
            "message": "Some entity types are not represented in the linework preview: " + ", ".join(f"{name} ({count})" for name, count in sorted(unsupported.items())) + ".",
        })
    if bounds is not None and bounds["min_z"] != bounds["max_z"]:
        warnings.append({
            "code": "xy_projection",
            "message": "The preview projects geometry onto the XY plane; non-planar 3D features are not shown as a 3D model.",
        })
    report = {
        "format": source_format,
        "preview_kind": "generated_vector_linework",
        "paths_written": len(path_data),
        "expanded_entity_count": expanded_entity_count,
        "source_extents": bounds,
        "preview_extents": {"min_x": minimum_x, "min_y": minimum_y, "max_x": maximum_x, "max_y": maximum_y},
        "viewbox": {"min_x": view_x, "min_y": view_y, "width": view_width, "height": view_height},
    }
    if converter:
        report["conversion"] = converter
    return svg.encode("utf-8"), report, warnings


def _runtime_package(runtime_dir: str | None) -> Path:
    if not runtime_dir:
        raise _attention(
            "converter_runtime_required",
            "DWG input needs the optional LibreDWG runtime. Install @mlightcad/libredwg-web@0.7.9 in a local runtime directory and pass --runtime-dir.",
            setup_command="npm install --prefix <runtime-dir> --no-save @mlightcad/libredwg-web@0.7.9",
        )
    root = Path(runtime_dir).expanduser()
    package = root / "node_modules" / "@mlightcad" / "libredwg-web"
    if not (package / "package.json").is_file():
        raise _attention(
            "converter_runtime_missing",
            "The selected runtime directory does not contain @mlightcad/libredwg-web@0.7.9.",
            setup_command="npm install --prefix <runtime-dir> --no-save @mlightcad/libredwg-web@0.7.9",
        )
    return package


def _run_dwg_bridge(source: Path, runtime_dir: str | None, output: Path) -> dict[str, str]:
    _runtime_package(runtime_dir)
    node = shutil.which("node")
    if not node:
        raise _attention("node_missing", "DWG conversion requires Node.js 20 or later; no node executable was found.")
    try:
        node_version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _attention("node_unavailable", "Node.js was found but its version could not be checked safely.") from exc
    version_match = re.fullmatch(r"v(\d+)(?:\.\d+){0,2}\s*", node_version.stdout)
    if node_version.returncode != 0 or version_match is None or int(version_match.group(1)) < 20:
        raise _attention("node_version_unsupported", "DWG conversion requires Node.js 20 or later.", version=node_version.stdout.strip()[:40])
    try:
        completed = subprocess.run(
            [
                node,
                str(BRIDGE),
                "--input",
                str(source),
                "--output",
                str(output),
                "--runtime-dir",
                str(Path(runtime_dir or "").expanduser().absolute()),
            ],
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CadRescueError("conversion_timeout", "DWG conversion exceeded the four-minute limit.") from exc
    except OSError as exc:
        raise CadRescueError("converter_launch_failed", "The Node.js DWG converter could not be started.", details={"reason": exc.strerror or exc.__class__.__name__}) from exc
    try:
        bridge_result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CadRescueError(
            "converter_failed",
            "The DWG converter did not return a valid result.",
            details={"exit_code": completed.returncode},
        ) from exc
    if completed.returncode != 0 or bridge_result.get("status") != "ok":
        code = str(bridge_result.get("error_code", "conversion_failed"))
        message = str(bridge_result.get("summary", "DWG could not be converted to DXF."))
        raise _attention(code, message)
    if not output.is_file() or output.stat().st_size == 0:
        raise _attention("conversion_output_missing", "The DWG converter returned without producing DXF data.")
    return {"name": "libredwg-web", "version": str(bridge_result.get("version", "0.7.9"))}


def _converted_dxf_report(source: Path, runtime_dir: str | None, operation: Callable[[Path, dict[str, str]], dict[str, Any]]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="cad-rescue-dwg-") as directory:
        converted = Path(directory) / "converted.dxf"
        converter = _run_dwg_bridge(source, runtime_dir, converted)
        try:
            result = operation(converted, converter)
        except CadRescueError:
            raise
        result.setdefault("data", {})["source_file"] = source.name
        result["data"]["source_format"] = "dwg"
        return result


def _inspect(args: argparse.Namespace) -> dict[str, Any]:
    path = _input_path(args.input, allowed_suffixes={".dxf", ".svg", ".dwg"})
    suffix = path.suffix.lower()
    if suffix == ".svg":
        return _svg_inspection(path)
    if suffix == ".dwg":
        return _converted_dxf_report(path, args.runtime_dir, lambda converted, converter: _inspect_dxf(converted, source_format="dwg", converter=converter))
    return _inspect_dxf(path)


def _preview(args: argparse.Namespace) -> dict[str, Any]:
    path = _input_path(args.input, allowed_suffixes={".dxf", ".svg", ".dwg"})
    output = _preflight_output(Path(args.output), path)
    if path.suffix.lower() == ".svg":
        contents, data, warnings = _safe_svg(path)
    elif path.suffix.lower() == ".dwg":
        def render(converted: Path, converter: dict[str, str]) -> dict[str, Any]:
            contents, data, warnings = _dxf_preview(converted, source_format="dwg", converter=converter)
            receipt = _atomic_write(output, contents, source=path)
            data["receipt"] = receipt
            return _envelope("needs_attention" if any(w["code"] == "entities_omitted" for w in warnings) else "ok", "Created a safe generated preview from the temporary DWG-to-DXF export.", data=data, artifacts=[{"path": receipt["path"], "label": "Generated vector preview", "media_type": "image/svg+xml"}], warnings=warnings)
        return _converted_dxf_report(path, args.runtime_dir, render)
    else:
        contents, data, warnings = _dxf_preview(path)
    receipt = _atomic_write(output, contents, source=path)
    data["receipt"] = receipt
    status = "needs_attention" if any(w["code"] == "entities_omitted" for w in warnings) else "ok"
    return _envelope(
        status,
        "Created a safe vector preview from the drawing geometry.",
        data=data,
        artifacts=[{"path": receipt["path"], "label": "Generated vector preview", "media_type": "image/svg+xml"}],
        warnings=warnings,
    )


def _recover(args: argparse.Namespace) -> dict[str, Any]:
    ezdxf, _bbox_module, _disassemble, recover_module, _units, _path_api = _dependency()
    source = _input_path(args.input, allowed_suffixes={".dxf"})
    output = Path(args.output).expanduser().absolute()
    if output.resolve(strict=False) == source.resolve(strict=True):
        raise CadRescueError("same_input_output", "Choose an output path separate from the source drawing.")
    if os.path.lexists(output):
        raise CadRescueError("output_exists", "The chosen output already exists; no file was replaced.")
    try:
        document, recovery_auditor = recover_module.readfile(source)
    except Exception as exc:
        raise _attention(
            "recovery_failed",
            "ezdxf could not recover a valid drawing from this DXF; no output was written.",
            reason=exc.__class__.__name__,
        ) from exc
    recovery_fixes = [_audit_item(item) for item in getattr(recovery_auditor, "fixes", [])]
    recovery_errors = [_audit_item(item) for item in getattr(recovery_auditor, "errors", [])]
    if not recovery_fixes and not recovery_errors:
        raise _attention("nothing_to_recover", "ezdxf found no repairable DXF structure; no copy was written.")
    try:
        final_audit = document.audit()
    except Exception as exc:
        raise _attention("recovery_validation_failed", "The recovered in-memory drawing could not pass validation.", reason=exc.__class__.__name__) from exc
    remaining_errors = [_audit_item(item) for item in getattr(final_audit, "errors", [])]
    source_entities = Counter(entity.dxftype() for entity in document.modelspace())
    if remaining_errors:
        raise _attention(
            "recovery_validation_failed",
            "The recovered drawing still has unresolved audit errors; no output was written.",
            remaining_errors=remaining_errors,
        )
    if not output.parent.is_dir():
        raise _attention("output_directory_missing", "The output directory must already exist.")
    temporary_dir: str | None = None
    try:
        temporary_dir = tempfile.mkdtemp(prefix=f".{output.name}.cad-rescue-", dir=output.parent)
        candidate = Path(temporary_dir) / "recovered.dxf"
        document.saveas(candidate)
        validated = ezdxf.readfile(candidate)
        candidate_audit = validated.audit()
        if getattr(candidate_audit, "errors", []):
            raise _attention("recovery_validation_failed", "The exported recovery did not reopen cleanly; no output was written.")
        output_entities = Counter(entity.dxftype() for entity in validated.modelspace())
        if output_entities != source_entities:
            raise _attention(
                "recovery_geometry_changed",
                "The exported recovery changed model-space entity counts; no output was written.",
                before=dict(sorted(source_entities.items())),
                after=dict(sorted(output_entities.items())),
            )
        temporary = candidate
        try:
            os.link(temporary, output)
        except FileExistsError as exc:
            raise CadRescueError("output_exists", "The chosen output already exists; no file was replaced.") from exc
        except OSError as exc:
            raise CadRescueError("output_write_failed", "The recovered DXF could not be written safely.", details={"reason": exc.strerror or exc.__class__.__name__}) from exc
    finally:
        if temporary_dir is not None:
            shutil.rmtree(temporary_dir, ignore_errors=True)
    receipt = {"path": str(output), "sha256": _sha256(output), "size_bytes": output.stat().st_size}
    return _envelope(
        "ok",
        f"Recovered and reopened a DXF with {len(source_entities)} model-space entities; the source file was preserved.",
        data={
            "source_file": source.name,
            "source_sha256": _sha256(source),
            "output_sha256": receipt["sha256"],
            "entity_count": sum(source_entities.values()),
            "recovery_fixes": len(recovery_fixes),
            "recovery_errors": recovery_errors,
            "validation": {"reopened": True, "remaining_audit_errors": 0, "entity_counts_preserved": True},
            "receipt": receipt,
        },
        artifacts=[{"path": receipt["path"], "label": "Recovered DXF copy", "media_type": "application/dxf"}],
        warnings=[{"code": "source_preserved", "message": "The original DXF was read only and remains unchanged."}],
    )


def _convert(args: argparse.Namespace) -> dict[str, Any]:
    source = _input_path(args.input, allowed_suffixes={".dwg"})
    output = Path(args.output).expanduser().absolute()
    if output.suffix.lower() != ".dxf":
        raise CadRescueError("output_extension", "DWG conversion output must use the .dxf extension.")
    if not output.parent.is_dir():
        raise _attention("output_directory_missing", "The output directory must already exist.")
    if output.resolve(strict=False) == source.resolve(strict=True):
        raise CadRescueError("same_input_output", "Choose an output path separate from the source drawing.")
    if os.path.lexists(output):
        raise CadRescueError("output_exists", "The chosen output already exists; no file was replaced.")
    with tempfile.TemporaryDirectory(prefix=f".{output.name}.cad-rescue-", dir=output.parent) as directory:
        candidate = Path(directory) / "converted.dxf"
        converter = _run_dwg_bridge(source, args.runtime_dir, candidate)
        ezdxf, _bbox_module, _disassemble, _recover, _units, _path_api = _dependency()
        try:
            document = ezdxf.readfile(candidate)
            validation = document.audit()
        except Exception as exc:
            raise _attention("conversion_validation_failed", "The produced DXF could not be reopened; no output was written.", reason=exc.__class__.__name__) from exc
        if getattr(validation, "errors", []):
            raise _attention("conversion_validation_failed", "The produced DXF contains audit errors; no output was written.")
        entity_count = len(document.modelspace())
        if entity_count == 0:
            raise _attention("empty_conversion", "The DWG export contains no model-space entities; no DXF was written.")
        source_hash = _sha256(source)
        try:
            os.link(candidate, output)
        except FileExistsError as exc:
            raise CadRescueError("output_exists", "The chosen output already exists; no file was replaced.") from exc
        except OSError as exc:
            raise CadRescueError("output_write_failed", "The converted DXF could not be written safely.", details={"reason": exc.strerror or exc.__class__.__name__}) from exc
    receipt = {"path": str(output), "sha256": _sha256(output), "size_bytes": output.stat().st_size}
    return _envelope(
        "ok",
        f"Converted the DWG to a DXF that ezdxf reopened with {entity_count} model-space entities.",
        data={
            "source_file": source.name,
            "source_sha256": source_hash,
            "output_sha256": receipt["sha256"],
            "entity_count": entity_count,
            "converter": converter,
            "validation": {"reopened": True, "audit_errors": 0},
            "receipt": receipt,
        },
        artifacts=[{"path": receipt["path"], "label": "Converted DXF", "media_type": "application/dxf"}],
        warnings=[{"code": "conversion_limits", "message": "LibreDWG conversion may omit unsupported DWG features; review the DXF in your CAD application before relying on it."}],
    )


def _parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(prog="cad_rescue.py", description="Inspect and preview DXF/SVG drawings; convert DWG only with an explicit optional runtime.")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=JsonArgumentParser)
    for command, help_text in (
        ("inspect", "Report drawing geometry, units, layers, extents and diagnostics."),
        ("preview", "Create a safe generated SVG preview."),
        ("recover", "Write a validated copy when ezdxf reports real DXF repairs."),
        ("convert", "Convert DWG to DXF using the optional LibreDWG runtime."),
    ):
        subparser = commands.add_parser(command, help=help_text)
        subparser.add_argument("--input", required=True, help="One selected .dxf, .svg or .dwg file as supported by the command.")
        if command in {"preview", "recover", "convert"}:
            subparser.add_argument("--output", required=True, help="New output path; existing files are never replaced.")
        if command in {"inspect", "preview", "convert"}:
            subparser.add_argument("--runtime-dir", help="Directory whose node_modules contains @mlightcad/libredwg-web@0.7.9 (DWG only).")
        subparser.set_defaults(handler={"inspect": _inspect, "preview": _preview, "recover": _recover, "convert": _convert}[command])
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        result = args.handler(args)
    except CadRescueError as exc:
        result = _envelope(exc.status, exc.message, data={"error_code": exc.code, **exc.details}, warnings=exc.warnings)
        _emit(result)
        return exc.exit_code
    except KeyboardInterrupt:
        _emit(_envelope("error", "The operation was interrupted.", data={"error_code": "cancelled"}))
        return 130
    except Exception as exc:
        _emit(_envelope("error", "CAD Rescue could not complete this operation.", data={"error_code": "operation_failed", "reason": exc.__class__.__name__}))
        return 1
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
