#!/usr/bin/env python3
"""Inspect and query CSV files without converting their values to floats."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Iterable


TOOL = "tablebeam"
SCHEMA_VERSION = 1
MAX_INPUT_BYTES = 100 * 1024 * 1024
MAX_ROWS = 250_000
MAX_NUMERIC_DIGITS = 4_000
MAX_DECIMAL_EXPONENT = 10_000
MAX_INLINE_RESULT_ROWS = 50
MAX_INLINE_COLUMNS = 100
MAX_INLINE_RANGE_PAIRS = 100
MAX_INLINE_SUMMARY_GROUPS = 5
MAX_SAMPLE_VALUE_CHARS = 200
DEFAULT_DELIMITERS = (",", ";", "\t", "|")
IDENTIFIER_NAME = re.compile(
    r"(?:^|[_\s-])(?:id|identifier|code|sku|zip|postal|postcode|phone|telephone|account|tracking|iban)(?:$|[_\s-])",
    re.IGNORECASE,
)
MEASURE_NAME = re.compile(
    r"(?:^|[_\s-])(?:amount|total|price|cost|revenue|sales|score|rate|percent|pct|quantity|qty|count|number|balance|tax|value|weight|height|latitude|longitude|distance|duration)(?:$|[_\s-])",
    re.IGNORECASE,
)
INTEGER_TEXT = re.compile(r"^[+-]?[0-9]+$")
DECIMAL_TEXT = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")


class TablebeamError(Exception):
    """An expected input, query, or output error with a stable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class SourceRow:
    row_id: int
    line_start: int
    line_end: int
    values: list[str | None]
    extra_cells: list[str]


@dataclass
class CsvTable:
    path: Path
    headers: list[str]
    rows: list[SourceRow]
    encoding: str
    delimiter: str
    delimiter_method: str
    delimiter_confidence: str
    warnings: list[dict[str, Any]]


@dataclass(frozen=True)
class SafeNumber:
    """A calculated finite number known to be safe to write as a CSV number."""

    text: str


def _envelope(
    status: str,
    summary: str,
    *,
    data: dict[str, Any] | None = None,
    warnings: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, str]] | None = None,
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


def _print_envelope(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")))


def _delimiter_value(value: str | None) -> str | None:
    if value is None:
        return None
    aliases = {"tab": "\t", "TAB": "\t", r"\t": "\t"}
    result = aliases.get(value, value)
    if len(result) != 1:
        raise TablebeamError("invalid_delimiter", "Delimiter must be one character; use TAB for a tab.")
    return result


def _sample_rows(text: str, delimiter: str, limit: int = 100) -> list[list[str]]:
    rows: list[list[str]] = []
    try:
        reader = csv.reader(io.StringIO(text[:65_536], newline=""), delimiter=delimiter, strict=True)
        for row in reader:
            if row == []:
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
    except csv.Error:
        return rows
    return rows


def _detect_delimiter(text: str, explicit: str | None) -> tuple[str, str, str, list[dict[str, Any]]]:
    if explicit is not None:
        return explicit, "explicit", "high", []

    sample = text[:65_536]
    sniffed: str | None = None
    try:
        sniffed = csv.Sniffer().sniff(sample, delimiters="".join(DEFAULT_DELIMITERS)).delimiter
    except csv.Error:
        pass

    if sniffed in DEFAULT_DELIMITERS:
        rows = _sample_rows(sample, sniffed)
        widths = [len(row) for row in rows]
        if not widths or max(widths) <= 1:
            return ",", "single_column_fallback", "low", []
        common_width, common_count = Counter(widths).most_common(1)[0]
        consistency = common_count / len(widths)
        confidence = "high" if common_width > 1 and consistency >= 0.9 else "medium"
        warnings = []
        if confidence == "medium":
            warnings.append({
                "code": "delimiter_uncertain",
                "message": "The detected delimiter did not produce a consistent field count; pass --delimiter to confirm it.",
            })
        return sniffed, "sniffer", confidence, warnings

    scored: list[tuple[int, float, int, str]] = []
    for delimiter in DEFAULT_DELIMITERS:
        rows = _sample_rows(sample, delimiter)
        widths = [len(row) for row in rows]
        if not widths:
            continue
        common_width, common_count = Counter(widths).most_common(1)[0]
        if common_width <= 1:
            continue
        scored.append((common_width, common_count / len(widths), common_count, delimiter))

    if not scored:
        return ",", "single_column_fallback", "low", []
    scored.sort(reverse=True)
    best = scored[0]
    tied = [item for item in scored if item[:3] == best[:3]]
    delimiter = best[3]
    if len(tied) > 1:
        return delimiter, "ambiguous_fallback", "low", [{
            "code": "delimiter_uncertain",
            "message": "More than one delimiter fits this sample; pass --delimiter to confirm the intended one.",
            "choices": [item[3] for item in tied],
        }]
    confidence = "high" if best[1] >= 0.9 else "medium"
    warnings = []
    if confidence == "medium":
        warnings.append({
            "code": "delimiter_uncertain",
            "message": "The fallback delimiter did not produce a consistent field count; pass --delimiter to confirm it.",
        })
    return delimiter, "consistent_width_fallback", confidence, warnings


def _read_table(path: Path, encoding: str, delimiter_arg: str | None) -> CsvTable:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise TablebeamError("source_unavailable", f"Cannot read the selected CSV file: {exc.strerror or 'file unavailable'}.") from exc
    if size > MAX_INPUT_BYTES:
        raise TablebeamError("source_too_large", f"CSV exceeds the {MAX_INPUT_BYTES // (1024 * 1024)} MiB input limit.")
    try:
        text = path.read_text(encoding=encoding, errors="strict")
    except LookupError as exc:
        raise TablebeamError("unknown_encoding", f"Unknown text encoding {encoding!r}.") from exc
    except UnicodeDecodeError as exc:
        raise TablebeamError("decode_error", f"Could not decode the CSV as {encoding}; specify the correct --encoding.") from exc
    except OSError as exc:
        raise TablebeamError("source_unavailable", f"Cannot read the selected CSV file: {exc.strerror or 'file unavailable'}.") from exc
    if "\x00" in text:
        raise TablebeamError("binary_input", "The selected file contains null bytes; provide a text CSV export.")

    explicit = _delimiter_value(delimiter_arg)
    delimiter, method, confidence, warnings = _detect_delimiter(text, explicit)
    old_field_size = csv.field_size_limit()
    csv.field_size_limit(max(old_field_size, len(text) + 1))
    stream = io.StringIO(text, newline="")
    reader = csv.reader(stream, delimiter=delimiter, strict=True)
    headers: list[str] | None = None
    rows: list[SourceRow] = []
    width_issues: list[dict[str, int]] = []
    prior_line = 0
    try:
        while True:
            line_start = prior_line + 1
            try:
                record = next(reader)
            except StopIteration:
                break
            line_end = reader.line_num
            prior_line = line_end
            if record == []:
                continue
            if headers is None:
                headers = record
                continue
            if len(rows) >= MAX_ROWS:
                raise TablebeamError("too_many_rows", f"CSV exceeds the {MAX_ROWS:,} row limit.")
            if len(record) != len(headers):
                width_issues.append({"row_id": len(rows) + 1, "expected": len(headers), "actual": len(record), "line": line_end})
            values = record[:len(headers)]
            if len(values) < len(headers):
                values.extend([None] * (len(headers) - len(values)))
            rows.append(SourceRow(len(rows) + 1, line_start, line_end, values, record[len(headers):]))
    except csv.Error as exc:
        raise TablebeamError("malformed_csv", f"Malformed CSV near line {reader.line_num}: {exc}.") from exc
    finally:
        csv.field_size_limit(old_field_size)

    if headers is None:
        raise TablebeamError("missing_header", "The file has no CSV header row.")
    if not headers:
        raise TablebeamError("missing_header", "The file has no named columns in its first row.")

    empty_headers = [index for index, header in enumerate(headers, start=1) if header == ""]
    duplicate_headers = [name for name, count in Counter(headers).items() if count > 1]
    if empty_headers:
        warnings.append({
            "code": "empty_header",
            "message": "One or more header cells are empty; those columns cannot be addressed by name.",
            "columns": empty_headers,
        })
    if duplicate_headers:
        warnings.append({
            "code": "duplicate_headers",
            "message": "Duplicate header names make name-based queries ambiguous.",
            "names": duplicate_headers,
        })
    if width_issues:
        warnings.append({
            "code": "row_width_mismatch",
            "message": "Some records have a different number of cells than the header; short rows are padded as missing and extra cells remain in evidence.",
            "count": len(width_issues),
            "examples": width_issues[:10],
        })
    if not rows:
        warnings.append({"code": "no_data_rows", "message": "The CSV has a header but no data rows."})

    return CsvTable(path, headers, rows, encoding, delimiter, method, confidence, warnings)


def _is_missing(value: str | None) -> bool:
    return value is None or value == ""


def _identifier_header(name: str) -> bool:
    normalized = name.strip()
    return bool(IDENTIFIER_NAME.search(normalized) or normalized.lower() in {"id", "code", "sku", "zip", "phone"})


def _parse_decimal(raw: str, *, location: str = "value") -> Decimal:
    token = raw.strip()
    if not token:
        raise TablebeamError("invalid_decimal", f"{location} is blank and cannot be read as a decimal.")
    if len(token) > MAX_NUMERIC_DIGITS + 32:
        raise TablebeamError("numeric_value_too_long", f"{location} exceeds the supported {MAX_NUMERIC_DIGITS:,} significant-digit limit.")
    if not DECIMAL_TEXT.fullmatch(token):
        raise TablebeamError("invalid_decimal", f"{location} value {raw!r} is not written as a dot-decimal number.")
    try:
        number = Decimal(token)
    except InvalidOperation as exc:
        raise TablebeamError("invalid_decimal", f"{location} value {raw!r} is not a decimal number.") from exc
    if not number.is_finite():
        raise TablebeamError("non_finite_decimal", f"{location} value {raw!r} is not finite.")
    parts = number.as_tuple()
    if len(parts.digits) > MAX_NUMERIC_DIGITS:
        raise TablebeamError("numeric_value_too_long", f"{location} exceeds the supported {MAX_NUMERIC_DIGITS:,} significant-digit limit.")
    if abs(parts.exponent) > MAX_DECIMAL_EXPONENT:
        raise TablebeamError("numeric_exponent_too_large", f"{location} exponent exceeds the supported magnitude of {MAX_DECIMAL_EXPONENT:,}.")
    return number


def _column_type(name: str, values: Iterable[str | None]) -> tuple[str, bool, str]:
    present_count = 0
    all_decimal = True
    leading_zero_integer = False
    tokens_have_fraction = False
    for value in values:
        if _is_missing(value):
            continue
        assert value is not None
        present_count += 1
        token = value.strip()
        try:
            _parse_decimal(value, location=f"column {name!r}")
        except TablebeamError:
            all_decimal = False
            break
        if INTEGER_TEXT.fullmatch(token) and len(token.lstrip("+-")) > 1 and token.lstrip("+-").startswith("0"):
            leading_zero_integer = True
        if "." in value or "e" in value.lower():
            tokens_have_fraction = True
    if present_count == 0:
        return "empty", False, "none"
    if not all_decimal:
        return "text", False, "none"
    if _identifier_header(name) or leading_zero_integer:
        return "identifier_like", True, "low"
    if MEASURE_NAME.search(name) or tokens_have_fraction:
        return "numeric_candidate", True, "high"
    return "ambiguous_numeric", True, "low"


def _profile(table: CsvTable, column_limit: int | None = MAX_INLINE_COLUMNS) -> dict[str, Any]:
    columns = []
    visible_headers = table.headers if column_limit is None else table.headers[:column_limit]
    for index, name in enumerate(visible_headers):
        missing_count = 0
        samples: list[str] = []
        sample_lengths: list[int] = []
        samples_truncated = False
        seen: set[str] = set()
        for row in table.rows:
            value = row.values[index]
            if _is_missing(value):
                missing_count += 1
                continue
            if len(samples) == 5:
                continue
            if value in seen:
                continue
            assert value is not None
            sample_lengths.append(len(value))
            if len(value) > MAX_SAMPLE_VALUE_CHARS:
                samples.append(value[:MAX_SAMPLE_VALUE_CHARS] + "…")
                samples_truncated = True
            else:
                samples.append(value)
            seen.add(value)
        # Type classification can stop at the first non-decimal cell; profiling
        # samples are collected separately so they still describe the column.
        inferred_type, numeric_candidate, confidence = _column_type(
            name, (row.values[index] for row in table.rows)
        )
        columns.append({
            "index": index,
            "name": name,
            "inferred_type": inferred_type,
            "numeric_candidate": numeric_candidate,
            "type_confidence": confidence,
            "non_missing_count": len(table.rows) - missing_count,
            "missing_count": missing_count,
            "sample_values": samples,
            "sample_value_character_counts": sample_lengths,
            "sample_values_truncated": samples_truncated,
        })
    return {
        "row_count": len(table.rows),
        "column_count": len(table.headers),
        "columns": columns,
        "preview": {
            "kind": "columns",
            "limit": column_limit,
            "returned": len(columns),
            "total": len(table.headers),
            "truncated": len(columns) < len(table.headers),
        },
    }


def _inspection_data(table: CsvTable, column_limit: int | None = MAX_INLINE_COLUMNS) -> dict[str, Any]:
    return {
        "source": {"filename": table.path.name},
        "encoding": table.encoding,
        "delimiter": table.delimiter,
        "delimiter_detection": {"method": table.delimiter_method, "confidence": table.delimiter_confidence},
        "profile": _profile(table, column_limit),
    }


def _range_summary(
    values: Iterable[int | tuple[int, int]], limit: int | None
) -> dict[str, Any]:
    visible: list[list[int]] = []
    range_count = 0
    start: int | None = None
    end: int | None = None

    def finish_range(range_start: int, range_end: int) -> None:
        nonlocal range_count
        range_count += 1
        if limit is None or range_count <= limit:
            visible.append([range_start, range_end])

    for item in values:
        item_start, item_end = item if isinstance(item, tuple) else (item, item)
        if start is not None and end is not None and item_start <= end + 1:
            end = max(end, item_end)
            continue
        if start is not None and end is not None:
            finish_range(start, end)
        start, end = item_start, item_end
    if start is not None and end is not None:
        finish_range(start, end)
    return {
        "ranges": visible,
        "range_count": range_count,
        "ranges_truncated": len(visible) < range_count,
    }


def _source_coverage(rows: list[SourceRow], range_limit: int | None = MAX_INLINE_RANGE_PAIRS) -> dict[str, Any]:
    return {
        "matched_row_count": len(rows),
        "row_id_ranges": _range_summary((row.row_id for row in rows), range_limit),
        "source_line_ranges": _range_summary(((row.line_start, row.line_end) for row in rows), range_limit),
    }


def _has_attention(warnings: list[dict[str, Any]]) -> bool:
    return any(warning.get("code") in {
        "delimiter_uncertain", "empty_header", "duplicate_headers", "row_width_mismatch", "no_data_rows"
    } for warning in warnings)


def _require_queryable_headers(table: CsvTable) -> None:
    if any(header == "" for header in table.headers) or len(set(table.headers)) != len(table.headers):
        raise TablebeamError("ambiguous_headers", "Name-based queries require unique, non-empty CSV headers; profile the file and repair its header row first.")


def _header_index(table: CsvTable, name: Any) -> int:
    if not isinstance(name, str) or name == "":
        raise TablebeamError("invalid_column", "Column names must be non-empty strings.")
    try:
        return table.headers.index(name)
    except ValueError as exc:
        raise TablebeamError("unknown_column", f"Column {name!r} is not present in the CSV header.") from exc


def _validate_spec(spec: Any) -> tuple[list[dict[str, Any]], list[str], dict[str, Any] | None]:
    if not isinstance(spec, dict):
        raise TablebeamError("invalid_query", "Query spec must be a JSON object.")
    extra = set(spec) - {"where", "group_by", "aggregate"}
    if extra:
        raise TablebeamError("invalid_query", f"Unsupported query field(s): {', '.join(sorted(map(str, extra)))}.")
    where = spec.get("where", [])
    group_by = spec.get("group_by", [])
    aggregate = spec.get("aggregate")
    if not isinstance(where, list) or not all(isinstance(item, dict) for item in where):
        raise TablebeamError("invalid_query", "where must be a list of filter objects.")
    if not isinstance(group_by, list) or not all(isinstance(item, str) and item for item in group_by):
        raise TablebeamError("invalid_query", "group_by must be a list of non-empty column names.")
    if len(set(group_by)) != len(group_by):
        raise TablebeamError("invalid_query", "group_by cannot repeat a column.")

    validated_filters = []
    valid_ops = {"eq", "ne", "contains", "starts_with", "ends_with", "gt", "gte", "lt", "lte", "is_null", "is_not_null"}
    for position, item in enumerate(where, start=1):
        unknown = set(item) - {"column", "op", "type", "value"}
        if unknown:
            raise TablebeamError("invalid_query", f"Filter {position} has unsupported field(s): {', '.join(sorted(map(str, unknown)))}.")
        column, op = item.get("column"), item.get("op")
        value_type = item.get("type", "text")
        if not isinstance(column, str) or not column:
            raise TablebeamError("invalid_query", f"Filter {position} needs a non-empty column name.")
        if op not in valid_ops:
            raise TablebeamError("invalid_query", f"Filter {position} has unsupported operator {op!r}.")
        if value_type not in {"text", "decimal"}:
            raise TablebeamError("invalid_query", f"Filter {position} type must be text or decimal.")
        if op in {"is_null", "is_not_null"}:
            if "value" in item:
                raise TablebeamError("invalid_query", f"Filter {position} does not take a value.")
        else:
            value = item.get("value")
            if not isinstance(value, str):
                raise TablebeamError("invalid_query", f"Filter {position} value must be a string; this preserves exact CSV spelling and decimal precision.")
            if op in {"contains", "starts_with", "ends_with"} and value_type != "text":
                raise TablebeamError("invalid_query", f"Filter {position} {op} operator requires type text.")
            if op in {"gt", "gte", "lt", "lte"} and value_type not in {"text", "decimal"}:
                raise TablebeamError("invalid_query", f"Filter {position} ordered comparison needs type text or decimal.")
            if value_type == "decimal":
                _parse_decimal(value, location=f"filter {position} threshold")
        validated_filters.append(dict(item, type=value_type))

    if aggregate is not None:
        if not isinstance(aggregate, dict):
            raise TablebeamError("invalid_query", "aggregate must be an object with an op and, when needed, a column.")
        unknown = set(aggregate) - {"op", "column"}
        if unknown:
            raise TablebeamError("invalid_query", f"Aggregate has unsupported field(s): {', '.join(sorted(map(str, unknown)))}.")
        op = aggregate.get("op")
        if op not in {"count", "count_non_missing", "sum", "avg", "min", "max"}:
            raise TablebeamError("invalid_query", "aggregate op must be count, count_non_missing, sum, avg, min, or max.")
        column = aggregate.get("column")
        if op in {"count", "count_non_missing"}:
            if op == "count" and column is not None:
                raise TablebeamError("invalid_query", "count counts matching rows and does not take a column; use count_non_missing to count populated cells.")
            if op == "count_non_missing" and (not isinstance(column, str) or not column):
                raise TablebeamError("invalid_query", "count_non_missing requires a column.")
        elif not isinstance(column, str) or not column:
            raise TablebeamError("invalid_query", f"{op} requires a column.")
        if group_by and op is None:
            raise TablebeamError("invalid_query", "group_by requires an aggregate.")
    elif group_by:
        raise TablebeamError("invalid_query", "group_by requires an aggregate.")
    return validated_filters, group_by, aggregate


def _filter_rows(table: CsvTable, filters: list[dict[str, Any]]) -> list[SourceRow]:
    indexes = [_header_index(table, item["column"]) for item in filters]
    thresholds = [
        _parse_decimal(item["value"], location=f"filter on {item['column']!r}")
        if item.get("type") == "decimal" and "value" in item else None
        for item in filters
    ]
    result = []
    for row in table.rows:
        selected = True
        for item, index, threshold in zip(filters, indexes, thresholds):
            value = row.values[index]
            op = item["op"]
            if op == "is_null":
                matches = _is_missing(value)
            elif op == "is_not_null":
                matches = not _is_missing(value)
            elif _is_missing(value):
                matches = False
            elif item.get("type") == "decimal":
                assert value is not None
                numeric = _parse_decimal(value, location=f"column {item['column']!r}, row {row.row_id}")
                compare = {"eq": numeric == threshold, "ne": numeric != threshold, "gt": numeric > threshold,
                           "gte": numeric >= threshold, "lt": numeric < threshold, "lte": numeric <= threshold}
                matches = compare.get(op, False)
            else:
                assert value is not None
                target = item.get("value", "")
                left = value.casefold()
                right = target.casefold()
                if op == "eq":
                    matches = value == target
                elif op == "ne":
                    matches = value != target
                elif op == "contains":
                    matches = right in left
                elif op == "starts_with":
                    matches = left.startswith(right)
                elif op == "ends_with":
                    matches = left.endswith(right)
                elif op == "gt":
                    matches = value > target
                elif op == "gte":
                    matches = value >= target
                elif op == "lt":
                    matches = value < target
                elif op == "lte":
                    matches = value <= target
                else:
                    matches = False
            if not matches:
                selected = False
                break
        if selected:
            result.append(row)
    return result


def _coefficient_and_exponent(number: Decimal) -> tuple[int, int]:
    parts = number.as_tuple()
    coefficient = 0
    for digit in parts.digits:
        coefficient = coefficient * 10 + digit
    if parts.sign:
        coefficient = -coefficient
    return coefficient, int(parts.exponent)


def _int_to_ascii(number: int) -> str:
    if number == 0:
        return "0"
    sign = "-" if number < 0 else ""
    remaining = abs(number)
    chunks = []
    base = 1_000_000_000
    while remaining:
        remaining, chunk = divmod(remaining, base)
        chunks.append(chunk)
    result = str(chunks.pop())
    while chunks:
        result += f"{chunks.pop():09d}"
    return sign + result


def _coefficient_text(coefficient: int, exponent: int) -> str:
    if coefficient == 0:
        return "0"
    sign = "-" if coefficient < 0 else ""
    value = abs(coefficient)
    while value % 10 == 0:
        value //= 10
        exponent += 1
    digits = _int_to_ascii(value)
    if exponent >= 0:
        return sign + digits + ("0" * exponent)
    split = len(digits) + exponent
    if split > 0:
        return sign + digits[:split] + "." + digits[split:]
    return sign + "0." + ("0" * (-split)) + digits


def _sum_decimal_components(values: list[Decimal]) -> tuple[int, int] | None:
    if not values:
        return None
    exponent = min(_coefficient_and_exponent(value)[1] for value in values)
    total = 0
    for value in values:
        coefficient, value_exponent = _coefficient_and_exponent(value)
        total += coefficient * (10 ** (value_exponent - exponent))
    return total, exponent


def _sum_decimal_text(values: list[Decimal]) -> str | None:
    components = _sum_decimal_components(values)
    if components is None:
        return None
    return _coefficient_text(*components)


def _average_value(values: list[Decimal]) -> dict[str, str] | None:
    if not values:
        return None
    components = _sum_decimal_components(values)
    assert components is not None
    numerator, exponent = components
    if exponent >= 0:
        numerator *= 10 ** exponent
        denominator = len(values)
    else:
        denominator = (10 ** (-exponent)) * len(values)
    divisor = math.gcd(abs(numerator), denominator)
    numerator //= divisor
    denominator //= divisor
    with localcontext() as context:
        context.prec = 34
        decimal_value = str(Decimal(numerator) / Decimal(denominator))
    return {"exact": f"{_int_to_ascii(numerator)}/{_int_to_ascii(denominator)}", "decimal": decimal_value}


def _aggregate_value(op: str, rows: list[SourceRow], column: str | None, index: int | None) -> tuple[Any, int, int]:
    if op == "count":
        return len(rows), len(rows), 0
    if op == "count_non_missing":
        assert index is not None
        count = sum(not _is_missing(row.values[index]) for row in rows)
        return count, count, len(rows) - count

    assert index is not None and column is not None
    missing_count = 0
    if op == "sum":
        numbers: list[Decimal] = []
        for row in rows:
            raw = row.values[index]
            if _is_missing(raw):
                missing_count += 1
                continue
            assert raw is not None
            numbers.append(_parse_decimal(raw, location=f"column {column!r}, row {row.row_id}"))
        result = _sum_decimal_text(numbers)
    elif op == "avg":
        numbers = []
        for row in rows:
            raw = row.values[index]
            if _is_missing(raw):
                missing_count += 1
                continue
            assert raw is not None
            numbers.append(_parse_decimal(raw, location=f"column {column!r}, row {row.row_id}"))
        result = _average_value(numbers)
    elif op in {"min", "max"}:
        best_raw: str | None = None
        best_number: Decimal | None = None
        value_count = 0
        for row in rows:
            raw = row.values[index]
            if _is_missing(raw):
                missing_count += 1
                continue
            assert raw is not None
            number = _parse_decimal(raw, location=f"column {column!r}, row {row.row_id}")
            value_count += 1
            if best_number is None or (number < best_number if op == "min" else number > best_number):
                best_raw = raw
                best_number = number
        return best_raw, value_count, missing_count
    else:
        raise TablebeamError("invalid_query", f"Unsupported aggregate operation {op!r}.")
    return result, len(numbers), missing_count


def _group_rows(rows: list[SourceRow], table: CsvTable, group_by: list[str]) -> list[tuple[dict[str, str | None], list[SourceRow]]]:
    indexes = [_header_index(table, name) for name in group_by]
    groups: dict[tuple[str | None, ...], list[SourceRow]] = {}
    for row in rows:
        key = tuple(None if _is_missing(row.values[index]) else row.values[index] for index in indexes)
        groups.setdefault(key, []).append(row)
    return [({name: value for name, value in zip(group_by, key)}, grouped) for key, grouped in groups.items()]


def _raw_export_columns(headers: list[str]) -> tuple[list[str], tuple[str, str, str, str]]:
    prefix = "_tablebeam"
    while True:
        names = (f"{prefix}_row_id", f"{prefix}_line_start", f"{prefix}_line_end", f"{prefix}_extra_cells_json")
        if not set(names).intersection(headers):
            return [names[0], names[1], names[2], *headers, names[3]], names
        prefix = "_" + prefix


def _query_data(
    table: CsvTable, spec: Any, export_format: str | None
) -> tuple[dict[str, Any], list[dict[str, Any]] | None, str, list[str] | None, dict[str, Any] | None]:
    _require_queryable_headers(table)
    filters, group_by, aggregate = _validate_spec(spec)
    for name in group_by:
        _header_index(table, name)
    if aggregate is not None and aggregate.get("column") is not None:
        _header_index(table, aggregate["column"])

    selected_rows = _filter_rows(table, filters)
    include_csv = export_format == "csv"
    include_json = export_format == "json"
    if aggregate is None:
        export_columns, metadata_columns = _raw_export_columns(table.headers) if include_csv else (None, None)
        preview_rows: list[dict[str, Any]] = []
        json_rows: list[dict[str, Any]] | None = [] if include_json else None
        csv_rows: list[dict[str, Any]] | None = [] if include_csv else None
        for position, row in enumerate(selected_rows):
            if position < MAX_INLINE_RESULT_ROWS or json_rows is not None:
                result_row = {
                    "row_id": row.row_id,
                    "line_start": row.line_start,
                    "line_end": row.line_end,
                    "values": {name: value for name, value in zip(table.headers, row.values)},
                    "extra_cells": list(row.extra_cells),
                }
                if position < MAX_INLINE_RESULT_ROWS:
                    preview_rows.append(result_row)
                if json_rows is not None:
                    json_rows.append(result_row)
            if csv_rows is not None:
                item: dict[str, Any] = {
                    metadata_columns[0]: row.row_id,
                    metadata_columns[1]: row.line_start,
                    metadata_columns[2]: row.line_end,
                    metadata_columns[3]: json.dumps(row.extra_cells, ensure_ascii=False) if row.extra_cells else "",
                }
                item.update({name: value for name, value in zip(table.headers, row.values)})
                csv_rows.append(item)
        preview = {
            "kind": "rows",
            "limit": MAX_INLINE_RESULT_ROWS,
            "returned": min(len(selected_rows), MAX_INLINE_RESULT_ROWS),
            "total": len(selected_rows),
            "truncated": len(selected_rows) > MAX_INLINE_RESULT_ROWS,
        }
        data = {
            "source": {"filename": table.path.name},
            "query": spec,
            "matched_row_count": len(selected_rows),
            "source_coverage": _source_coverage(selected_rows),
            "preview": preview,
            "result_rows": preview_rows,
        }
        full_data = None
        if json_rows is not None:
            full_data = {
                "source": {"filename": table.path.name},
                "query": spec,
                "matched_row_count": len(selected_rows),
                "source_coverage": _source_coverage(selected_rows, None),
                "preview": {**preview, "limit": None, "returned": len(selected_rows), "truncated": False},
                "result_rows": json_rows,
            }
        summary = f"Selected {len(selected_rows):,} matching rows from {table.path.name}."
        if preview["truncated"]:
            summary += f" Showing the first {preview['returned']} row results; full rows are available in an explicit export."
        return data, csv_rows, summary, export_columns, full_data

    op = aggregate["op"]
    column = aggregate.get("column")
    measure_index = _header_index(table, column) if column is not None else None
    grouped = _group_rows(selected_rows, table, group_by) if group_by else [({}, selected_rows)]
    result_rows: list[dict[str, Any]] = []
    full_result_rows: list[dict[str, Any]] | None = [] if include_json else None
    export_rows: list[dict[str, Any]] | None = [] if include_csv else None
    if op == "avg":
        csv_columns = [*[f"group__{name}" for name in group_by], "value_exact", "value_decimal", "rows", "value_count", "missing_count", "source_row_count", "source_row_ids"]
    else:
        csv_columns = [*[f"group__{name}" for name in group_by], "value", "rows", "value_count", "missing_count", "source_row_count", "source_row_ids"]
    summary_groups: list[str] = []
    for group, group_rows in grouped:
        value, value_count, missing_count = _aggregate_value(op, group_rows, column, measure_index)
        if len(result_rows) < MAX_INLINE_RESULT_ROWS:
            result_rows.append({
                "group": group,
                "value": value,
                "rows": len(group_rows),
                "value_count": value_count,
                "missing_count": missing_count,
                "source_row_coverage": {
                    "row_count": len(group_rows),
                    **_range_summary((item.row_id for item in group_rows), MAX_INLINE_RANGE_PAIRS),
                },
            })
        if full_result_rows is not None:
            full_result_rows.append({
                "group": group,
                "value": value,
                "rows": len(group_rows),
                "value_count": value_count,
                "missing_count": missing_count,
                "source_row_ids": [item.row_id for item in group_rows],
            })
        if export_rows is not None:
            flat: dict[str, Any] = {f"group__{name}": value for name, value in group.items()}
            if isinstance(value, dict) and "exact" in value:
                flat["value_exact"] = value["exact"]
                flat["value_decimal"] = SafeNumber(value["decimal"])
            elif isinstance(value, str):
                flat["value"] = SafeNumber(value)
            else:
                flat["value"] = value
            flat["rows"] = len(group_rows)
            flat["value_count"] = value_count
            flat["missing_count"] = missing_count
            flat["source_row_count"] = len(group_rows)
            flat["source_row_ids"] = ";".join(str(item.row_id) for item in group_rows)
            export_rows.append(flat)
        if group_by and len(summary_groups) < MAX_INLINE_SUMMARY_GROUPS:
            label = ", ".join(
                f"{name}={'[missing]' if group_value is None else group_value}"
                for name, group_value in group.items()
            )
            if isinstance(value, dict):
                display = value["decimal"]
            elif value is None:
                display = "no values"
            else:
                display = str(value)
            summary_groups.append(f"{label}: {display}")

    preview = {
        "kind": "groups" if group_by else "rows",
        "limit": MAX_INLINE_RESULT_ROWS,
        "returned": min(len(grouped), MAX_INLINE_RESULT_ROWS),
        "total": len(grouped),
        "truncated": len(grouped) > MAX_INLINE_RESULT_ROWS,
    }
    data = {
        "source": {"filename": table.path.name},
        "query": spec,
        "matched_row_count": len(selected_rows),
        "source_coverage": _source_coverage(selected_rows),
        "preview": preview,
        "aggregate": {"op": op, "column": column, "group_by": group_by},
    }
    if group_by:
        data["result_group_count"] = len(grouped)
    data["result_rows"] = result_rows
    full_data = None
    if full_result_rows is not None:
        full_data = {
            "source": {"filename": table.path.name},
            "query": spec,
            "matched_row_count": len(selected_rows),
            "source_coverage": _source_coverage(selected_rows, None),
            "preview": {**preview, "limit": None, "returned": len(grouped), "truncated": False},
            "aggregate": {"op": op, "column": column, "group_by": group_by},
        }
        if group_by:
            full_data["result_group_count"] = len(grouped)
        full_data["result_rows"] = full_result_rows
    if not grouped:
        rendered = "no groups"
    elif group_by:
        rendered = f"First {len(summary_groups)} groups: " + ", ".join(summary_groups)
        remaining_groups = len(grouped) - len(summary_groups)
        if remaining_groups:
            rendered += f"; plus {remaining_groups:,} other groups"
    elif op == "avg":
        value = result_rows[0]["value"] if result_rows else None
        rendered = value["decimal"] if isinstance(value, dict) else "no values"
    else:
        rendered = str(result_rows[0]["value"]) if result_rows else "no groups"
    unit = f"{op} of {column}" if column else op
    summary = f"Calculated {unit} for {len(selected_rows):,} matching rows: {rendered}."
    if preview["truncated"]:
        summary += f" Showing the first {preview['returned']} groups; full results are available in an explicit export."
    return data, export_rows, summary, csv_columns if include_csv else None, full_data


def _formula_safe_cell(value: Any) -> tuple[Any, bool]:
    # Adapted from Tablebeam's MIT-licensed src/table_analysis.py; see LICENSE.
    if isinstance(value, SafeNumber):
        return value.text, False
    if value is None:
        return "", False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value, False
    text = str(value)
    if text.startswith(("\t", "\r", "\n")) or text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text, True
    return text, False


def _csv_bytes(rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> tuple[bytes, int]:
    columns = list(fieldnames or [])
    if fieldnames is None:
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    escaped = 0
    safe_header = []
    for name in columns:
        value, changed = _formula_safe_cell(name)
        safe_header.append(value)
        escaped += int(changed)
    writer.writerow(safe_header)
    for row in rows:
        cells = []
        for name in columns:
            value, changed = _formula_safe_cell(row.get(name))
            cells.append(value)
            escaped += int(changed)
        writer.writerow(cells)
    return output.getvalue().encode("utf-8-sig"), escaped


def _write_export(path: Path, payload: bytes) -> dict[str, Any]:
    created = False
    try:
        with path.open("xb") as stream:
            created = True
            stream.write(payload)
            stream.flush()
    except FileExistsError as exc:
        raise TablebeamError("output_exists", f"Output file {path.name!r} already exists; choose a new path.") from exc
    except OSError as exc:
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise TablebeamError("export_failed", f"Could not write output file {path.name!r}: {exc.strerror or 'write failed'}.") from exc
    try:
        saved = path.read_bytes()
        stat_size = path.stat().st_size
    except OSError as exc:
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise TablebeamError("export_verification_failed", f"Could not verify output file {path.name!r}.") from exc
    if stat_size != len(payload) or saved != payload:
        try:
            path.unlink()
        except OSError:
            pass
        raise TablebeamError("export_verification_failed", f"Output file {path.name!r} did not match the bytes written.")
    return {"path": str(path), "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "verified": True}


def _load_spec(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise TablebeamError("query_unavailable", f"Could not read the UTF-8 query spec {path.name!r}.") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise TablebeamError("invalid_query_json", f"Query spec is not valid JSON at line {exc.lineno}, column {exc.colno}.") from exc


def _profile_command(args: argparse.Namespace) -> int:
    table = _read_table(args.csv, args.encoding, args.delimiter)
    data = _inspection_data(table, None if args.all_columns else MAX_INLINE_COLUMNS)
    status = "needs_attention" if _has_attention(table.warnings) else "ok"
    summary = f"Inspected {len(table.rows):,} rows and {len(table.headers):,} columns in {table.path.name}."
    _print_envelope(_envelope(status, summary, data=data, warnings=table.warnings))
    return 0


def _query_command(args: argparse.Namespace) -> int:
    table = _read_table(args.csv, args.encoding, args.delimiter)
    spec = _load_spec(args.spec)
    export_format = args.export_format if args.export is not None else None
    data, export_rows, summary, export_columns, full_data = _query_data(table, spec, export_format)
    warnings = list(table.warnings)
    artifacts: list[dict[str, str]] = []
    if args.export is not None:
        if args.preview and (args.export.exists() or args.export.is_symlink()):
            raise TablebeamError("output_exists", f"Output file {args.export.name!r} already exists; choose a new path.")
        if args.export_format == "csv":
            assert export_rows is not None and export_columns is not None
            payload, escaped = _csv_bytes(export_rows, export_columns)
            if escaped:
                warnings.append({
                    "code": "csv_formula_cells_escaped",
                    "message": "Formula-like CSV headers or cells were prefixed with an apostrophe; raw source values remain in this JSON response.",
                    "count": escaped,
                })
        else:
            assert full_data is not None
            payload = (json.dumps({"schema_version": SCHEMA_VERSION, "tool": TOOL, "data": full_data}, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
            escaped = 0
        if args.preview:
            data["export_preview"] = {
                "path": str(args.export),
                "format": args.export_format,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "row_count": data["preview"]["total"],
                "formula_safe": args.export_format == "csv",
                "escaped_formula_cells": escaped,
                "written": False,
            }
            summary = f"{summary} Previewed {args.export_format} export of {data['preview']['total']:,} result rows to {args.export.name}."
        else:
            receipt = _write_export(args.export, payload)
            receipt.update({"format": args.export_format, "formula_safe": args.export_format == "csv", "escaped_formula_cells": escaped})
            data["export_receipt"] = receipt
            artifacts.append({
                "path": str(args.export),
                "label": "Tablebeam query result",
                "media_type": "text/csv" if args.export_format == "csv" else "application/json",
            })
    status = "needs_attention" if _has_attention(table.warnings) else "ok"
    _print_envelope(_envelope(status, summary, data=data, warnings=warnings, artifacts=artifacts))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Profile and query a CSV with exact decimal arithmetic and source-row evidence.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("profile", "Inspect a CSV's headers, dialect, missing cells, and value shapes."),
                            ("query", "Run a declarative filter or aggregate query from a JSON spec.")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("csv", type=Path, help="Explicit CSV file to inspect; no directory scanning is performed.")
        command.add_argument("--encoding", default="utf-8-sig", help="Text encoding (default: utf-8-sig; decoding is strict).")
        command.add_argument("--delimiter", help="One-character delimiter, or TAB; otherwise detect it from the CSV.")
        if name == "query":
            command.add_argument("--spec", required=True, type=Path, help="UTF-8 JSON file containing the declarative query.")
            command.add_argument("--export", type=Path, help="Create a new CSV or JSON result file; existing paths are never overwritten.")
            command.add_argument("--export-format", choices=("csv", "json"), help="Required when --export is used.")
            command.add_argument("--preview", action="store_true", help="Show the exact export size and SHA-256 without writing the file.")
        else:
            command.add_argument("--all-columns", action="store_true", help="Return metadata for every column instead of the default 100-column profile preview.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "query":
        if args.export is not None and args.export_format is None:
            parser.error("query --export requires --export-format csv or json")
        if args.export is None and args.export_format is not None:
            parser.error("query --export-format requires --export PATH")
        if args.preview and args.export is None:
            parser.error("query --preview requires --export PATH and --export-format")
    try:
        if args.command == "profile":
            return _profile_command(args)
        return _query_command(args)
    except TablebeamError as exc:
        _print_envelope(_envelope("error", str(exc), data={"error_code": exc.code}))
        return 1
    except OSError as exc:
        _print_envelope(_envelope("error", f"Operation failed: {exc.strerror or 'file operation failed'}.", data={"error_code": "io_error"}))
        return 1
    except (TypeError, ValueError) as exc:
        _print_envelope(_envelope("error", f"Invalid input: {exc}.", data={"error_code": "invalid_input"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
