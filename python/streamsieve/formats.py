"""Read numeric telemetry rows and serialize results for the CLI and MCP server."""

from __future__ import annotations

import csv
import io
import json
import math
from typing import Any, Sequence


def sanitize_json(obj: Any) -> Any:
    """Recursively replace non-finite floats so JSON output is strictly RFC-8259 valid."""
    if isinstance(obj, float):
        if math.isfinite(obj):
            return obj
        return None
    if isinstance(obj, dict):
        return {str(k): sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_json(x) for x in obj]
    return obj


def parse_csv_rows(csv_text: str) -> tuple[list[list[float]], list[str] | None]:
    """Parse CSV/TSV/whitespace numeric text with optional header row."""
    rows: list[list[float]] = []
    headers: list[str] | None = None

    sample_lines = [ln for ln in csv_text.splitlines() if ln.strip()]
    if not sample_lines:
        return ([], None)

    # Detect delimiter heuristically
    first = sample_lines[0]
    if "," in first:
        delim = ","
    elif "\t" in first:
        delim = "\t"
    elif ";" in first:
        delim = ";"
    else:
        delim = None

    for line in sample_lines:
        if delim is not None:
            parts = [p.strip() for p in next(csv.reader(io.StringIO(line), delimiter=delim))]
            # Drop trailing empty field caused by trailing comma
            if parts and parts[-1] == "":
                parts = parts[:-1]
        else:
            parts = line.split()
        if not parts:
            continue
        try:
            vals = [float(p) for p in parts]
        except ValueError:
            if not rows and headers is None:
                headers = parts
            continue
        rows.append(vals)

    return (rows, headers)


def _as_float(val: Any, name: str, *, allow_nonfinite: bool = False) -> float:
    """Strictly parse a float parameter, rejecting huge overflowing ints and (by default) NaN/Inf."""
    if isinstance(val, bool) or val is None:
        raise ValueError(f"Parameter {name!r} must be a number")
    try:
        out = float(val)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Parameter {name!r} must be a number: {exc}") from exc
    if not allow_nonfinite and not math.isfinite(out):
        raise ValueError(f"Parameter {name!r} must be finite, got {val!r}")
    return out


def parse_json_stream(data: Any) -> tuple[list[list[float]], list[str] | None]:
    """Parse JSON array of numeric rows OR JSON array of dicts."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise ValueError(f"Invalid json_data: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("json_data must be a JSON array of rows or objects")
    if not data:
        return ([], None)
    if isinstance(data[0], dict):
        keys = list(data[0].keys())
        rows: list[list[float]] = []
        for idx, item in enumerate(data):
            if not isinstance(item, dict):
                raise ValueError(f"json_data[{idx}] must be an object")
            rows.append([_as_float(item[k], f"json_data[{idx}][{k!r}]", allow_nonfinite=True) for k in keys])
        return (rows, [str(k) for k in keys])
    rows = []
    for idx, row in enumerate(data):
        if not isinstance(row, list):
            raise ValueError(f"json_data[{idx}] must be an array of numbers")
        rows.append([_as_float(v, f"json_data[{idx}]", allow_nonfinite=True) for v in row])
    return (rows, None)


def _safe_jsonl_float(v: Any) -> float | None:
    """Convert JSONL numeric field to finite float or None (on bool/non-numeric/overflow/non-finite)."""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    try:
        fv = float(v)
    except OverflowError:
        return None
    return fv if math.isfinite(fv) else None


def parse_jsonl_rows(
    jsonl_text: str,
    *,
    expected_keys: Sequence[str] | None = None,
) -> tuple[list[list[float]], list[str] | None]:
    """Parse newline-delimited JSON objects (JSONL/NDJSON), extracting numeric top-level fields.

    When ``expected_keys`` is provided (e.g., on stateful ``append=True`` calls), columns are
    extracted strictly in ``expected_keys`` order even if incoming JSON objects reorder keys.
    """
    rows: list[list[float]] = []
    keys: list[str] | None = [str(k) for k in expected_keys] if expected_keys else None
    for raw_line in jsonl_text.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, RecursionError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        if keys is None:
            cand_keys: list[str] = []
            first_row: list[float] = []
            for k, v in obj.items():
                fv = _safe_jsonl_float(v)
                if fv is not None:
                    cand_keys.append(str(k))
                    first_row.append(fv)
            if cand_keys:
                keys = cand_keys
                rows.append(first_row)
        else:
            row_vals: list[float] = []
            for k in keys:
                fv = _safe_jsonl_float(obj.get(k))
                row_vals.append(fv if fv is not None else math.nan)
            rows.append(row_vals)
    return (rows, keys)
