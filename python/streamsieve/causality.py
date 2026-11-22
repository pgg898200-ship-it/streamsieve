"""Bit-exact zero-lookahead future-poisoning causality verification."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
import math
from typing import Any, Callable, Sequence

from .monitor import EStream

_POISON_VALUES: tuple[tuple[str, float], ...] = (
    ("+1e12", 1e12),
    ("-1e12", -1e12),
    ("NaN", float("nan")),
    ("+Inf", float("inf")),
)


class CausalityViolationError(AssertionError):
    """Raised when an algorithm's output at ``probe_tick`` changes under future poisoning."""


def _is_2d_series(series: Any) -> bool:
    ai = getattr(series, "__array_interface__", None)
    if isinstance(ai, dict):
        shape = ai.get("shape", ())
        return len(shape) == 2
    if len(series) == 0:
        return False
    first = series[0]
    return not isinstance(first, (int, float, bool, str, bytes))


def _clone_and_poison(series: Any, probe_tick: int, poison: float) -> Any:
    """Return a deep copy of ``series`` with every tick ``t' > probe_tick`` set to ``poison``."""
    ai = getattr(series, "__array_interface__", None)
    if isinstance(ai, dict) and callable(getattr(series, "copy", None)):
        arr = series.copy()
        if probe_tick + 1 < len(arr):
            arr[probe_tick + 1 :] = poison
        return arr

    n = len(series)
    if _is_2d_series(series):
        out: list[list[float]] = []
        for t in range(n):
            row = list(series[t])
            if t > probe_tick:
                out.append([poison] * len(row))
            else:
                out.append(row)
        return out

    return [poison if t > probe_tick else series[t] for t in range(n)]


def _values_equal(a: Any, b: Any, *, rtol: float, atol: float, path: str = "root") -> tuple[bool, str]:
    if hasattr(a, "__array_interface__") and callable(getattr(a, "tolist", None)):
        a = a.tolist()
    if hasattr(b, "__array_interface__") and callable(getattr(b, "tolist", None)):
        b = b.tolist()
    if type(a) is float or type(b) is float:
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            fa = float(a)
            fb = float(b)
            if math.isnan(fa) and math.isnan(fb):
                return (True, "")
            if rtol == 0.0 and atol == 0.0:
                if fa == fb:
                    return (True, "")
                return (False, f"{path}: {fa!r} != {fb!r}")
            if math.isclose(fa, fb, rel_tol=rtol, abs_tol=atol):
                return (True, "")
            return (False, f"{path}: {fa!r} != {fb!r} (rtol={rtol}, atol={atol})")
        return (False, f"{path}: type mismatch {type(a).__name__} vs {type(b).__name__}")

    if is_dataclass(a) and is_dataclass(b) and type(a) is type(b):
        for f in fields(a):
            ok, msg = _values_equal(
                getattr(a, f.name), getattr(b, f.name), rtol=rtol, atol=atol, path=f"{path}.{f.name}"
            )
            if not ok:
                return (False, msg)
        return (True, "")

    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return (False, f"{path}: dict keys differ ({sorted(a.keys())} vs {sorted(b.keys())})")
        for k in a:
            ok, msg = _values_equal(a[k], b[k], rtol=rtol, atol=atol, path=f"{path}[{k!r}]")
            if not ok:
                return (False, msg)
        return (True, "")

    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return (False, f"{path}: length mismatch ({len(a)} vs {len(b)})")
        for idx, (xa, xb) in enumerate(zip(a, b)):
            ok, msg = _values_equal(xa, xb, rtol=rtol, atol=atol, path=f"{path}[{idx}]")
            if not ok:
                return (False, msg)
        return (True, "")

    if a == b:
        return (True, "")
    return (False, f"{path}: {a!r} != {b!r}")


def _default_probe_ticks(n_ticks: int) -> list[int]:
    if n_ticks <= 1:
        return [0] if n_ticks == 1 else []
    if n_ticks <= 6:
        return list(range(n_ticks - 1))
    candidates = [
        0,
        min(1, n_ticks - 2),
        n_ticks // 4,
        n_ticks // 2,
        (3 * n_ticks) // 4,
        n_ticks - 2,
    ]
    return sorted({t for t in candidates if 0 <= t < n_ticks - 1})


def assert_causal(
    step_fn: Callable[[Any], Sequence[Any]],
    series: Any,
    probe_ticks: Sequence[int] | None = None,
    rtol: float = 0.0,
    atol: float = 0.0,
) -> bool:
    """Verify bit-exact zero-lookahead causality of ``step_fn`` over ``series``.

    Runs ``step_fn`` once on the original ``series`` and then re-runs it with ALL
    future ticks ``t' > probe_tick`` overwritten by ``+1e12``, ``-1e12``, ``NaN``,
    and ``+Inf``. Asserts exact equality (or tolerance if ``rtol``/``atol`` > 0)
    at every ``probe_tick``.

    Raises :class:`CausalityViolationError` on any future leakage; returns ``True``
    if all probes pass.
    """
    n_ticks = len(series)
    if n_ticks == 0:
        return True

    probes = list(probe_ticks) if probe_ticks is not None else _default_probe_ticks(n_ticks)
    for p in probes:
        if p < 0 or p >= n_ticks:
            raise ValueError(f"probe_tick {p} out of range for series of length {n_ticks}")

    baseline = step_fn(series)
    if len(baseline) != n_ticks:
        raise ValueError(
            f"step_fn returned {len(baseline)} outputs for series of length {n_ticks}"
        )

    for probe in probes:
        for label, poison in _POISON_VALUES:
            perturbed = _clone_and_poison(series, probe, poison)
            pert_out = step_fn(perturbed)
            if len(pert_out) != n_ticks:
                raise CausalityViolationError(
                    f"Output length changed under future poison {label} after tick {probe}: "
                    f"{len(pert_out)} vs {n_ticks}"
                )
            ok, diff = _values_equal(
                baseline[probe],
                pert_out[probe],
                rtol=rtol,
                atol=atol,
                path=f"out[{probe}]",
            )
            if not ok:
                raise CausalityViolationError(
                    f"Lookahead causality violation at probe_tick={probe} under future poison "
                    f"{label}: {diff}"
                )
    return True


def verify_estream_causality(
    matrix: Any,
    probe_ticks: Sequence[int] | None = None,
    *,
    warmup: int = 32,
    alpha: float = 0.05,
    ring_capacity: int = 64,
    burst_tail_p0: float = 0.025,
    ewma_reset_decay: float = 0.0,
    two_sided: bool = True,
    season_period: int = 0,
    detrend_alpha: float = 0.0,
) -> bool:
    """Verify bit-exact zero-lookahead causality of :class:`EStream` on ``matrix``."""
    n_ticks = len(matrix)
    if n_ticks == 0:
        return True
    n_channels = len(matrix[0])

    def _run(stream: Any) -> list[tuple[Any, ...]]:
        outputs: list[tuple[Any, ...]] = []
        with EStream(
            n_channels,
            warmup=warmup,
            ring_capacity=ring_capacity,
            alpha=alpha,
            burst_tail_p0=burst_tail_p0,
            ewma_reset_decay=ewma_reset_decay,
            two_sided=two_sided,
            season_period=season_period,
            detrend_alpha=detrend_alpha,
        ) as est:
            for t in range(len(stream)):
                est.update_row(stream[t])
                statuses = tuple(est.channel_status(c) for c in range(n_channels))
                rejected = tuple(est.ebh_reject())
                outputs.append((statuses, rejected))
        return outputs

    return assert_causal(_run, matrix, probe_ticks=probe_ticks, rtol=0.0, atol=0.0)
