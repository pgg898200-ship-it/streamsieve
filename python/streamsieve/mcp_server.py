"""Zero-dependency JSON-RPC 2.0 over stdio MCP server for streamsieve.

Exposes 7 stateful session tools for AI coding/ops/quant agents:
  1. ``sift_stream``      — Ingest rows/CSV/JSON arrays -> compact e-BH brief + handles + token savings
  2. ``fetch_raw_ticks``  — Lossless raw tick drill-down for any channel handle
  3. ``lattice_observe``  — Submit provenance-tagged evidence -> echo check + fused belief/e-value
  4. ``lattice_merge``    — CRDT-merge two session lattices or a Base85 wire payload
  5. ``lattice_query``    — Query deduplicated posterior, Vovk-Wang merged e-value & echo count
  6. ``voi_allocate``     — Multi-agent non-overlapping VOI read allocation across firehose
  7. ``trust_step``       — Multi-agent forecast fusion & delayed-label trust/quarantine updates

Run directly with::

    python3 -m streamsieve.mcp_server
"""

from __future__ import annotations

import csv
import io
import json
import math
import sys
from typing import Any, BinaryIO, Sequence

from ._native import StreamSieveError, has_avx2, version
from .lattice import EvidenceLattice
from .monitor import EStream, parse_channel_handle
from .sifter import VOISifter
from .trust import TrustCalibrator

PROTOCOL_VERSION = "2024-11-05"


def _sanitize_json(obj: Any) -> Any:
    """Recursively replace non-finite floats so JSON output is strictly RFC-8259 valid."""
    if isinstance(obj, float):
        if math.isfinite(obj):
            return obj
        return None
    if isinstance(obj, dict):
        return {str(k): _sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_json(x) for x in obj]
    return obj


def _parse_csv_rows(csv_text: str) -> tuple[list[list[float]], list[str] | None]:
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


def _as_int(val: Any, name: str) -> int:
    """Strictly parse a finite integer parameter, raising ValueError on bool/NaN/Inf/overflow."""
    if isinstance(val, bool) or val is None:
        raise ValueError(f"Parameter {name!r} must be an integer")
    if isinstance(val, float):
        if not math.isfinite(val):
            raise ValueError(f"Parameter {name!r} must be a finite integer, got {val!r}")
        return int(val)
    try:
        return int(val)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Parameter {name!r} must be an integer: {exc}") from exc


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


def _parse_json_stream(data: Any) -> tuple[list[list[float]], list[str] | None]:
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


def _parse_jsonl_rows(
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


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "sift_stream",
        "description": (
            "Ingest multivariate streaming telemetry (rows, CSV/TSV, JSONL/NDJSON, or JSON arrays) into a "
            "stateful AVX2 anytime-valid e-process monitor (with optional causal deseasonalizing & detrending) "
            "and return an ultra-compact e-BH brief with lossless channel drill-down handles "
            "(ch:<id>@t=<cp>..<tick>) and token savings stats."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "rows": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "number"}},
                    "description": "2D row-major numeric matrix (ticks x channels).",
                },
                "csv_text": {
                    "type": "string",
                    "description": "CSV/TSV/whitespace numeric stream with optional header row.",
                },
                "jsonl_text": {
                    "type": "string",
                    "description": "Newline-delimited JSON objects (NDJSON/JSONL); extracts numeric top-level fields.",
                },
                "json_data": {
                    "description": "JSON string or parsed array of numeric rows or dicts.",
                },
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional channel names.",
                },
                "alpha": {"type": "number", "default": 0.05},
                "warmup": {"type": "integer", "default": 32},
                "season_period": {
                    "type": "integer",
                    "default": 0,
                    "description": "Optional causal seasonal period P >= 2 (0 = disabled).",
                },
                "detrend_alpha": {
                    "type": "number",
                    "default": 0.0,
                    "description": "Optional causal detrend alpha in (0, 1]; 1.0 = first diff (0 = disabled).",
                },
                "max_channels": {"type": "integer", "default": 10},
                "ring_capacity": {"type": "integer", "default": 256},
                "append": {
                    "type": "boolean",
                    "default": False,
                    "description": "Append to an existing session monitor if compatible.",
                },
            },
        },
    },
    {
        "name": "fetch_raw_ticks",
        "description": (
            "Lossless drill-down of exact raw (tick, value) samples from a channel's ring buffer "
            "using a channel handle (e.g. 'ch:3@t=180..600') or integer channel index."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "handle": {
                    "type": "string",
                    "description": "Channel drill-down handle from sift_stream (e.g. 'ch:3@t=180..600').",
                },
                "channel": {
                    "type": "integer",
                    "description": "Channel index (alternative to handle).",
                },
                "limit": {"type": "integer", "description": "Maximum number of recent ticks to return."},
                "start_tick": {"type": "integer", "description": "Optional inclusive lower tick filter."},
                "end_tick": {"type": "integer", "description": "Optional inclusive upper tick filter."},
            },
        },
    },
    {
        "name": "lattice_observe",
        "description": (
            "Submit provenance-tagged agent evidence to a stateful CRDT EvidenceLattice. "
            "Automatically deduplicates echoed provenance IDs and returns echo status plus "
            "updated posterior probability and Vovk-Wang merged e-value."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["hyp_id", "provenance_id", "llr"],
            "properties": {
                "lattice_id": {"type": "string", "default": "default"},
                "merge_mode": {
                    "type": "string",
                    "enum": ["strict_origin", "max_reliability"],
                    "default": "strict_origin",
                },
                "hyp_id": {"description": "Hypothesis identifier (string or uint64 int)."},
                "provenance_id": {"description": "Unique evidence provenance ID (string or uint64 int)."},
                "llr": {"type": "number", "description": "Log-likelihood ratio contribution."},
                "log_e": {"type": "number", "description": "Log e-value contribution (defaults to llr)."},
                "agent_id": {"type": "integer", "default": 0},
                "reliability": {"type": "number", "default": 1.0},
                "timestamp_ns": {"type": "integer", "default": 0},
                "prior_log_odds": {"type": "number", "description": "Optional prior log-odds override."},
                "agent_weight": {"type": "number", "description": "Optional agent weight override."},
            },
        },
    },
    {
        "name": "lattice_merge",
        "description": (
            "Order-independent CRDT join of two EvidenceLattices (by session lattice_id or "
            "serialized Base85 wire payload). Strictly commutative, associative, and idempotent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_id": {"type": "string", "default": "default"},
                "source_id": {
                    "type": "string",
                    "description": "Existing session lattice ID to merge into target_id.",
                },
                "source_base85": {
                    "type": "string",
                    "description": "Serialized Base85 wire lattice payload to merge into target_id.",
                },
                "merge_mode": {
                    "type": "string",
                    "enum": ["strict_origin", "max_reliability"],
                    "default": "strict_origin",
                },
                "return_base85": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "lattice_query",
        "description": (
            "Query a stateful EvidenceLattice for deduplicated posterior probability, "
            "Vovk-Wang admissible merged e-value, unique evidence count, and echoes suppressed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "lattice_id": {"type": "string", "default": "default"},
                "hyp_id": {
                    "description": "Optional hypothesis ID (string or int). If omitted, lists all hypotheses.",
                },
                "include_base85": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "voi_allocate",
        "description": (
            "Feed hierarchical site/channel/tick observations into a stateful VOISifter and "
            "compute zero-overlap multi-agent Value-of-Information read allocations."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sifter_id": {"type": "string", "default": "default"},
                "n_channels": {"type": "integer", "description": "Number of channels (required on create)."},
                "n_sites": {"type": "integer", "default": 1},
                "ticks_per_epoch": {"type": "integer", "default": 8},
                "reset": {"type": "boolean", "default": False},
                "noise_scales": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Optional per-channel noise stds for loud-channel immunity.",
                },
                "observations": {
                    "type": "array",
                    "description": (
                        "Optional list of observations: "
                        "{kind: 'site_agg'|'mean'|'ticks', target_id: int, value: number|number[]}."
                    ),
                    "items": {"type": "object"},
                },
                "budget_per_agent": {"type": "integer", "default": 4},
                "n_agents": {"type": "integer", "default": 1},
                "allow_site_agg": {"type": "boolean", "default": True},
                "allow_mean": {"type": "boolean", "default": True},
                "allow_ticks": {"type": "boolean", "default": True},
                "step_epoch": {"type": "boolean", "default": False},
                "stay_prob": {"type": "number", "default": 0.99},
                "top_posteriors": {"type": "integer", "default": 10},
            },
        },
    },
    {
        "name": "trust_step",
        "description": (
            "Fuse multi-agent probability or continuous forecasts with Platt shrinkage, online AgACI "
            "conformal intervals, and Bayes-optimal cost-asymmetric actions, or resolve delayed "
            "ground-truth outcomes with Ville quarantine updates."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["action"],
            "properties": {
                "calibrator_id": {"type": "string", "default": "default"},
                "action": {
                    "type": "string",
                    "enum": ["predict_prob", "predict_scalar", "resolve", "status"],
                },
                "n_agents": {"type": "integer"},
                "decision_id": {"description": "Decision identifier (uint64 int or string)."},
                "agent_probs": {
                    "type": "array",
                    "items": {"type": ["number", "null"]},
                },
                "agent_means": {
                    "type": "array",
                    "items": {"type": ["number", "null"]},
                },
                "agent_stds": {
                    "type": "array",
                    "items": {"type": ["number", "null"]},
                },
                "underage_cost": {"type": "number", "default": 1.0},
                "overage_cost": {"type": "number", "default": 1.0},
                "outcome": {"type": "number"},
                "conformal_alpha": {"type": "number", "default": 0.10},
                "quarantine_e_thresh": {"type": "number", "default": 20.0},
                "fixed_share_alpha": {"type": "number", "default": 0.04},
                "eta": {"type": "number", "default": 0.8},
            },
        },
    },
]


class MCPServer:
    """Stateful JSON-RPC 2.0 MCP server hosting streamsieve engines."""

    def __init__(self) -> None:
        self._streams: dict[str, EStream] = {}
        self._lattices: dict[str, EvidenceLattice] = {}
        self._sifters: dict[str, VOISifter] = {}
        self._trusts: dict[str, TrustCalibrator] = {}

    def close(self) -> None:
        """Release all native engines held by open sessions."""
        for store in (self._streams, self._lattices, self._sifters, self._trusts):
            for obj in list(store.values()):
                try:
                    obj.close()
                except Exception:
                    pass
            store.clear()

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _tool_sift_stream(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id", "default"))
        alpha = _as_float(args.get("alpha", 0.05), "alpha")
        warmup = _as_int(args.get("warmup", 32), "warmup")
        season_period = _as_int(args.get("season_period", 0), "season_period")
        detrend_alpha = _as_float(args.get("detrend_alpha", 0.0), "detrend_alpha")
        max_channels = _as_int(args.get("max_channels", 10), "max_channels")
        ring_capacity = _as_int(args.get("ring_capacity", 256), "ring_capacity")
        append = bool(args.get("append", False))
        explicit_names = args.get("names")

        existing = self._streams.get(session_id)
        expected_jsonl_keys = (
            existing.names
            if (append and existing is not None and existing.names)
            else None
        )

        rows: list[list[float]] = []
        parsed_names: list[str] | None = None
        raw_chars = 0

        if "rows" in args and args["rows"] is not None:
            raw_rows = args["rows"]
            if not isinstance(raw_rows, list):
                raise ValueError("'rows' must be a 2D array of numbers")
            rows = []
            for r_idx, r in enumerate(raw_rows):
                if not isinstance(r, list):
                    raise ValueError(f"rows[{r_idx}] must be an array of numbers")
                rows.append([_as_float(v, f"rows[{r_idx}]", allow_nonfinite=True) for v in r])
            raw_chars = len(json.dumps(_sanitize_json(raw_rows), separators=(",", ":")))
        elif "jsonl_text" in args and args["jsonl_text"] is not None:
            jsonl_text = str(args["jsonl_text"])
            raw_chars = len(jsonl_text)
            rows, parsed_names = _parse_jsonl_rows(jsonl_text, expected_keys=expected_jsonl_keys)
        elif "csv_text" in args and args["csv_text"] is not None:
            csv_text = str(args["csv_text"])
            raw_chars = len(csv_text)
            if csv_text.lstrip().startswith("{"):
                rows, parsed_names = _parse_jsonl_rows(csv_text, expected_keys=expected_jsonl_keys)
            else:
                rows, parsed_names = _parse_csv_rows(csv_text)
        elif "json_data" in args and args["json_data"] is not None:
            jd = args["json_data"]
            raw_chars = len(jd) if isinstance(jd, str) else len(json.dumps(_sanitize_json(jd), separators=(",", ":")))
            rows, parsed_names = _parse_json_stream(jd)
        else:
            raise ValueError("Provide one of 'rows', 'csv_text', 'jsonl_text', or 'json_data'")

        if not rows or not rows[0]:
            raise ValueError("No numeric rows found in input stream")

        n_ch = len(rows[0])
        names = (
            [str(n) for n in explicit_names]
            if explicit_names is not None
            else (parsed_names if parsed_names and len(parsed_names) == n_ch else None)
        )

        eff_season = min(season_period, 4096) if season_period >= 2 else 0
        eff_detrend = min(1.0, max(0.0, detrend_alpha))
        est = existing
        need_new = (
            not append
            or est is None
            or est.n_channels != n_ch
            or est.season_period != eff_season
            or est.detrend_alpha != eff_detrend
        )
        if need_new:
            new_est = EStream(
                n_ch,
                warmup=warmup,
                ring_capacity=ring_capacity,
                alpha=alpha,
                season_period=season_period,
                detrend_alpha=detrend_alpha,
                names=names,
            )
            try:
                new_est.update_batch(rows)
            except Exception:
                new_est.close()
                raise
            if est is not None:
                est.close()
            self._streams[session_id] = new_est
            est = new_est
        else:
            est.update_batch(rows)

        brief = est.brief(max_channels=max_channels, names=names, alpha=alpha)
        compact_text = brief.to_compact_text()
        brief_chars = len(compact_text)
        est_raw_tokens = max(1, (raw_chars + 3) // 4)
        est_brief_tokens = max(1, (brief_chars + 3) // 4)
        ratio = round(raw_chars / max(1, brief_chars), 2)
        savings_pct = round(max(0.0, (1.0 - brief_chars / max(1, raw_chars)) * 100.0), 2)

        payload = dict(brief)
        payload["session_id"] = session_id
        payload["rows_ingested_now"] = len(rows)
        payload["season_period"] = est.season_period
        payload["detrend_alpha"] = est.detrend_alpha
        payload["compact_text"] = compact_text
        payload["token_savings"] = {
            "raw_chars": raw_chars,
            "brief_chars": brief_chars,
            "est_raw_tokens": est_raw_tokens,
            "est_brief_tokens": est_brief_tokens,
            "compression_ratio": ratio,
            "savings_pct": savings_pct,
        }
        return payload

    def _tool_fetch_raw_ticks(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id", "default"))
        est = self._streams.get(session_id)
        if est is None:
            raise ValueError(f"Unknown stream session_id {session_id!r}; call sift_stream first")

        target = args.get("handle")
        if target is None:
            target = args.get("channel")
        if target is None:
            raise ValueError("Provide either 'handle' or 'channel'")

        ch, _, _ = parse_channel_handle(target)
        limit = args.get("limit")
        start_tick = args.get("start_tick")
        end_tick = args.get("end_tick")

        ticks = est.fetch_raw(
            target,
            limit=_as_int(limit, "limit") if limit is not None else None,
            start_tick=_as_int(start_tick, "start_tick") if start_tick is not None else None,
            end_tick=_as_int(end_tick, "end_tick") if end_tick is not None else None,
        )
        status = est.channel_status(ch)
        return {
            "session_id": session_id,
            "channel": ch,
            "name": status.name,
            "handle": status.handle,
            "count": len(ticks),
            "ticks": [[t, v] for t, v in ticks],
        }

    def _get_or_create_lattice(self, lattice_id: str, merge_mode: str = "strict_origin") -> EvidenceLattice:
        lat = self._lattices.get(lattice_id)
        if lat is None:
            lat = EvidenceLattice(merge_mode=merge_mode)
            self._lattices[lattice_id] = lat
        return lat

    def _tool_lattice_observe(self, args: dict[str, Any]) -> dict[str, Any]:
        lattice_id = str(args.get("lattice_id", "default"))
        merge_mode = str(args.get("merge_mode", "strict_origin"))
        lat = self._get_or_create_lattice(lattice_id, merge_mode=merge_mode)

        if "hyp_id" not in args or "provenance_id" not in args or "llr" not in args:
            raise ValueError("lattice_observe requires 'hyp_id', 'provenance_id', and 'llr'")

        hyp = args["hyp_id"]
        prov = args["provenance_id"]
        llr = _as_float(args["llr"], "llr")
        log_e = _as_float(args["log_e"], "log_e") if args.get("log_e") is not None else None
        agent_id = _as_int(args.get("agent_id", 0), "agent_id")
        reliability = _as_float(args.get("reliability", 1.0), "reliability")
        timestamp_ns = _as_int(args.get("timestamp_ns", 0), "timestamp_ns")

        if args.get("prior_log_odds") is not None:
            lat.set_prior(hyp, _as_float(args["prior_log_odds"], "prior_log_odds"))
        if args.get("agent_weight") is not None:
            lat.set_agent_weight(agent_id, _as_float(args["agent_weight"], "agent_weight"))

        was_echo = lat.observe(
            hyp,
            prov,
            llr,
            log_e=log_e,
            agent_id=agent_id,
            reliability=reliability,
            timestamp_ns=timestamp_ns,
        )
        summary = lat.query(hyp)
        return {
            "lattice_id": lattice_id,
            "was_echo": was_echo,
            "summary": summary.to_dict(),
        }

    def _tool_lattice_merge(self, args: dict[str, Any]) -> dict[str, Any]:
        target_id = str(args.get("target_id", "default"))
        merge_mode = str(args.get("merge_mode", "strict_origin"))
        source_id = args.get("source_id")
        source_base85 = args.get("source_base85")
        return_base85 = bool(args.get("return_base85", True))

        if source_id is None and source_base85 is None:
            raise ValueError("lattice_merge requires 'source_id' and/or 'source_base85'")

        dst = self._get_or_create_lattice(target_id, merge_mode=merge_mode)

        if source_id is not None:
            src_name = str(source_id)
            src = self._lattices.get(src_name)
            if src is None:
                raise ValueError(f"Unknown source lattice_id {src_name!r}")
            dst.merge(src)

        if source_base85 is not None:
            with EvidenceLattice.from_base85(str(source_base85)) as wire_lat:
                dst.merge(wire_lat)

        hyps = dst.list_hyps()
        summaries = {str(h): dst.query(h).to_dict() for h in hyps}
        wire = dst.to_bytes()
        result: dict[str, Any] = {
            "target_id": target_id,
            "merge_mode": dst.merge_mode,
            "hypotheses": hyps,
            "summaries": summaries,
            "wire_bytes": len(wire),
        }
        if return_base85:
            result["base85"] = dst.to_base85()
        return result

    def _tool_lattice_query(self, args: dict[str, Any]) -> dict[str, Any]:
        lattice_id = str(args.get("lattice_id", "default"))
        lat = self._lattices.get(lattice_id)
        if lat is None:
            raise ValueError(f"Unknown lattice_id {lattice_id!r}; call lattice_observe first")

        include_b85 = bool(args.get("include_base85", False))
        hyp = args.get("hyp_id")
        if hyp is not None:
            summary = lat.query(hyp)
            out: dict[str, Any] = {
                "lattice_id": lattice_id,
                "merge_mode": lat.merge_mode,
                "summary": summary.to_dict(),
            }
        else:
            hyps = lat.list_hyps()
            out = {
                "lattice_id": lattice_id,
                "merge_mode": lat.merge_mode,
                "hypotheses": hyps,
                "summaries": [lat.query(h).to_dict() for h in hyps],
            }
        if include_b85:
            out["base85"] = lat.to_base85()
        return out

    def _tool_voi_allocate(self, args: dict[str, Any]) -> dict[str, Any]:
        sifter_id = str(args.get("sifter_id", "default"))
        reset = bool(args.get("reset", False))
        sf = self._sifters.get(sifter_id)

        if sf is None or reset:
            if "n_channels" not in args or args["n_channels"] is None:
                raise ValueError("voi_allocate requires 'n_channels' when creating or resetting a sifter")
            new_sf = VOISifter(
                _as_int(args["n_channels"], "n_channels"),
                n_sites=_as_int(args.get("n_sites", 1), "n_sites"),
                ticks_per_epoch=_as_int(args.get("ticks_per_epoch", 8), "ticks_per_epoch"),
            )
            if sf is not None:
                sf.close()
            self._sifters[sifter_id] = new_sf
            sf = new_sf

        if args.get("noise_scales") is not None:
            ns = args["noise_scales"]
            if not isinstance(ns, list):
                raise ValueError("'noise_scales' must be a list of numbers")
            sf.set_noise_scales([_as_float(x, "noise_scales") for x in ns])

        raw_obs = args.get("observations")
        if raw_obs is not None:
            if not isinstance(raw_obs, list):
                raise ValueError("'observations' must be a list of observation objects")
            for idx, obs in enumerate(raw_obs):
                if not isinstance(obs, dict):
                    raise ValueError(f"observations[{idx}] must be an object, got {type(obs).__name__}")
                if "target_id" not in obs or "value" not in obs:
                    raise ValueError(f"observations[{idx}] requires 'target_id' and 'value'")
                kind = str(obs.get("kind", "")).strip()
                target_id = _as_int(obs["target_id"], f"observations[{idx}].target_id")
                val = obs["value"]
                if kind == "site_agg":
                    sf.observe_site_agg(target_id, _as_float(val, f"observations[{idx}].value", allow_nonfinite=True))
                elif kind in ("mean", "channel_mean"):
                    sf.observe_mean(target_id, _as_float(val, f"observations[{idx}].value", allow_nonfinite=True))
                elif kind in ("ticks", "channel_ticks"):
                    if not isinstance(val, list):
                        raise ValueError(f"observations[{idx}].value must be a list of numbers for ticks")
                    sf.observe_ticks(
                        target_id,
                        [_as_float(x, f"observations[{idx}].value", allow_nonfinite=True) for x in val],
                    )
                else:
                    raise ValueError(f"Unknown observation kind {kind!r}; expected site_agg|mean|ticks")

        if bool(args.get("step_epoch", False)):
            sf.step_epoch(_as_float(args.get("stay_prob", 0.99), "stay_prob"))

        actions = sf.allocate(
            budget_per_agent=_as_int(args.get("budget_per_agent", 4), "budget_per_agent"),
            n_agents=_as_int(args.get("n_agents", 1), "n_agents"),
            allow_site_agg=bool(args.get("allow_site_agg", True)),
            allow_mean=bool(args.get("allow_mean", True)),
            allow_ticks=bool(args.get("allow_ticks", True)),
        )
        probs = sf.posteriors()
        top_n = max(1, min(_as_int(args.get("top_posteriors", 10), "top_posteriors"), len(probs)))
        ranked = sorted(enumerate(probs), key=lambda kv: (-kv[1], kv[0]))[:top_n]
        return {
            "sifter_id": sifter_id,
            "n_channels": sf.n_channels,
            "n_sites": sf.n_sites,
            "n_actions": len(actions),
            "actions": [a.to_dict() for a in actions],
            "top_posteriors": [{"channel": ch, "prob": p} for ch, p in ranked],
        }

    def _tool_trust_step(self, args: dict[str, Any]) -> dict[str, Any]:
        calibrator_id = str(args.get("calibrator_id", "default"))
        action = str(args.get("action", "")).strip()
        tc = self._trusts.get(calibrator_id)

        if tc is None:
            if action in ("resolve", "status"):
                raise ValueError(
                    f"Unknown calibrator_id {calibrator_id!r}; run predict_prob or predict_scalar first"
                )
            n_agents = args.get("n_agents")
            if n_agents is None:
                if isinstance(args.get("agent_probs"), list):
                    n_agents = len(args["agent_probs"])
                elif isinstance(args.get("agent_means"), list):
                    n_agents = len(args["agent_means"])
            if n_agents is None:
                raise ValueError("Specify 'n_agents' or provide agent_probs/agent_means")
            n_agents_i = _as_int(n_agents, "n_agents")
            if n_agents_i <= 0:
                raise ValueError("Parameter 'n_agents' must be positive")
            tc = TrustCalibrator(
                n_agents_i,
                fixed_share_alpha=_as_float(args.get("fixed_share_alpha", 0.04), "fixed_share_alpha"),
                eta=_as_float(args.get("eta", 0.8), "eta"),
                quarantine_e_thresh=_as_float(args.get("quarantine_e_thresh", 20.0), "quarantine_e_thresh"),
                conformal_alpha=_as_float(args.get("conformal_alpha", 0.10), "conformal_alpha"),
            )
            self._trusts[calibrator_id] = tc

        if action == "predict_prob":
            if "decision_id" not in args or "agent_probs" not in args:
                raise ValueError("predict_prob requires 'decision_id' and 'agent_probs'")
            if not isinstance(args["agent_probs"], list):
                raise ValueError("'agent_probs' must be a list")
            probs_in = [
                None if v is None else _as_float(v, "agent_probs", allow_nonfinite=True)
                for v in args["agent_probs"]
            ]
            dec = tc.predict_prob(
                args["decision_id"],
                probs_in,
                underage_cost=_as_float(args.get("underage_cost", 1.0), "underage_cost"),
                overage_cost=_as_float(args.get("overage_cost", 1.0), "overage_cost"),
            )
            return {
                "calibrator_id": calibrator_id,
                "action": action,
                "decision": dec.to_dict(),
                "agents": [a.to_dict() for a in tc.agents()],
            }

        if action == "predict_scalar":
            if "decision_id" not in args or "agent_means" not in args:
                raise ValueError("predict_scalar requires 'decision_id' and 'agent_means'")
            if not isinstance(args["agent_means"], list):
                raise ValueError("'agent_means' must be a list")
            means_in = [
                None if v is None else _as_float(v, "agent_means", allow_nonfinite=True)
                for v in args["agent_means"]
            ]
            stds_raw = args.get("agent_stds")
            stds_in = None
            if stds_raw is not None:
                if not isinstance(stds_raw, list):
                    raise ValueError("'agent_stds' must be a list")
                stds_in = [
                    None if v is None else _as_float(v, "agent_stds", allow_nonfinite=True)
                    for v in stds_raw
                ]
            dec = tc.predict_scalar(
                args["decision_id"],
                means_in,
                agent_stds=stds_in,
                underage_cost=_as_float(args.get("underage_cost", 1.0), "underage_cost"),
                overage_cost=_as_float(args.get("overage_cost", 1.0), "overage_cost"),
            )
            return {
                "calibrator_id": calibrator_id,
                "action": action,
                "decision": dec.to_dict(),
                "agents": [a.to_dict() for a in tc.agents()],
            }

        if action == "resolve":
            if "decision_id" not in args or "outcome" not in args:
                raise ValueError("resolve requires 'decision_id' and 'outcome'")
            outcome = _as_float(args["outcome"], "outcome")
            tc.resolve(args["decision_id"], outcome)
            return {
                "calibrator_id": calibrator_id,
                "action": action,
                "resolved_decision_id": args["decision_id"],
                "outcome": outcome,
                "agents": [a.to_dict() for a in tc.agents()],
            }

        if action == "status":
            return {
                "calibrator_id": calibrator_id,
                "action": action,
                "agents": [a.to_dict() for a in tc.agents()],
            }

        raise ValueError(
            f"Unknown trust_step action {action!r}; expected predict_prob|predict_scalar|resolve|status"
        )

    # ------------------------------------------------------------------
    # JSON-RPC 2.0 protocol dispatcher
    # ------------------------------------------------------------------

    def call_tool(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        """Dispatch a tool call and return an MCP ``CallToolResult`` dict."""
        dispatch = {
            "sift_stream": self._tool_sift_stream,
            "fetch_raw_ticks": self._tool_fetch_raw_ticks,
            "lattice_observe": self._tool_lattice_observe,
            "lattice_merge": self._tool_lattice_merge,
            "lattice_query": self._tool_lattice_query,
            "voi_allocate": self._tool_voi_allocate,
            "trust_step": self._tool_trust_step,
        }
        fn = dispatch.get(name)
        if fn is None:
            raise KeyError(f"Unknown tool: {name}")
        args = arguments if isinstance(arguments, dict) else {}
        try:
            payload = _sanitize_json(fn(args))
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(payload, separators=(",", ":"), allow_nan=False),
                    }
                ],
                "structuredContent": payload,
                "isError": False,
            }
        except (
            ValueError,
            TypeError,
            OverflowError,
            RecursionError,
            AttributeError,
            KeyError,
            StreamSieveError,
        ) as exc:
            err_payload = {"error": str(exc), "tool": name}
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(err_payload, separators=(",", ":"), allow_nan=False),
                    }
                ],
                "structuredContent": err_payload,
                "isError": True,
            }

    def handle_message(self, msg: Any) -> dict[str, Any] | None:
        """Process one JSON-RPC 2.0 request/notification; return response dict or None."""
        if not isinstance(msg, dict):
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request: expected JSON object"},
            }

        msg_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        # Notifications carry no id and MUST NOT receive a response
        if method == "notifications/initialized" or (
            isinstance(method, str) and method.startswith("notifications/") and "id" not in msg
        ):
            return None

        if not isinstance(method, str):
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32600, "message": "Invalid Request: missing method"},
            }

        if method == "initialize":
            client_proto = (
                params.get("protocolVersion") if isinstance(params, dict) else None
            ) or PROTOCOL_VERSION
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": str(client_proto),
                    "capabilities": {
                        "tools": {"listChanged": False},
                    },
                    "serverInfo": {
                        "name": "streamsieve",
                        "version": version(),
                        "avx2": has_avx2(),
                    },
                },
            }

        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": TOOL_DEFINITIONS},
            }

        if method == "tools/call":
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32602, "message": "Invalid params: expected tool name"},
                }
            tool_name = params["name"]
            tool_args = params.get("arguments")
            if tool_args is not None and not isinstance(tool_args, dict):
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32602, "message": "Invalid params: arguments must be an object"},
                }
            try:
                result = self.call_tool(tool_name, tool_args)
            except KeyError as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32602, "message": str(exc)},
                }
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}

        if "id" not in msg:
            return None

        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }


def _write_message(out_stream: BinaryIO, payload: dict[str, Any], *, framed: bool) -> None:
    body = json.dumps(_sanitize_json(payload), separators=(",", ":"), allow_nan=False).encode("utf-8")
    if framed:
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        out_stream.write(header + body)
    else:
        out_stream.write(body + b"\n")
    out_stream.flush()


def serve_stdio(
    in_stream: BinaryIO | None = None,
    out_stream: BinaryIO | None = None,
) -> int:
    """Run the JSON-RPC 2.0 MCP server loop over stdio (NDJSON or Content-Length framed)."""
    inp = in_stream if in_stream is not None else sys.stdin.buffer
    out = out_stream if out_stream is not None else sys.stdout.buffer

    server = MCPServer()
    try:
        while True:
            line = inp.readline()
            if not line:
                break
            stripped = line.strip()
            if not stripped:
                continue

            framed = False
            if stripped.lower().startswith(b"content-length:"):
                framed = True
                try:
                    length = int(stripped.split(b":", 1)[1].strip())
                except ValueError:
                    length = -1
                # Consume remaining headers up to blank line to keep framing synchronized
                while True:
                    hdr = inp.readline()
                    if not hdr or hdr in (b"\r\n", b"\n"):
                        break
                if length < 0 or length > 64 * 1024 * 1024:
                    _write_message(
                        out,
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32700, "message": "Invalid Content-Length header"},
                        },
                        framed=True,
                    )
                    continue
                raw_body = inp.read(length)
                if len(raw_body) < length:
                    break
            else:
                raw_body = stripped

            try:
                msg = json.loads(raw_body.decode("utf-8"))
            except Exception as exc:
                _write_message(
                    out,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": f"Parse error: {exc}"},
                    },
                    framed=framed,
                )
                continue

            try:
                resp = server.handle_message(msg)
            except Exception as exc:
                resp = {
                    "jsonrpc": "2.0",
                    "id": msg.get("id") if isinstance(msg, dict) else None,
                    "error": {"code": -32603, "message": f"Internal error: {exc}"},
                }
            if resp is not None:
                _write_message(out, resp, framed=framed)
    finally:
        server.close()
    return 0


def main() -> int:
    """CLI entry point for running the streamsieve MCP stdio server."""
    return serve_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
