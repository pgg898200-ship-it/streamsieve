"""Zero-dependency drop-in adapters for established Python streaming & ML APIs.

Includes:
  - :class:`EStreamDrift` — Drop-in ``river.drift`` univariate detector (``update(x)``,
    ``drift_detected``, ``warning_detected``, ``clone()``).
  - :class:`SklearnSieveDetector` — Zero-dependency scikit-learn ``OutlierMixin``-compatible
    multivariate streaming detector (``fit``, ``partial_fit``, ``decision_function``,
    ``predict``, ``fit_predict``, ``get_params``, ``set_params``).

Neither adapter imports ``river``, ``scikit-learn``, ``numpy``, or ``scipy``.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

from .monitor import ChannelStatus, EStream, StreamBrief


class EStreamDrift:
    """Drop-in ``river.drift``-compatible univariate drift & burst detector backed by ``EStream``.

    Implements the ``river.base.DriftDetector`` duck-typed protocol (``update(x) -> self``,
    ``drift_detected``, ``clone()``) while adding:
      - Exact anytime-valid null calibration under continuous peeking (heavy tails, skew, DC offsets)
      - Detection of BOTH mean shifts AND zero-mean symmetric variance/tail bursts
      - Optional strictly causal deseasonalizing (``season_period``) & detrending (``detrend_alpha``)
      - Zero runtime dependencies (does not import ``river``, ``numpy``, or ``scipy``)
    """

    def __init__(
        self,
        alpha: float = 0.05,
        *,
        warmup: int = 32,
        burst_tail_p0: float = 0.025,
        two_sided: bool = True,
        season_period: int = 0,
        detrend_alpha: float = 0.0,
        auto_reset: bool = True,
        check_every: int = 1,
        ring_capacity: int = 0,
    ) -> None:
        self._burst_tail_p0 = float(burst_tail_p0)
        self._two_sided = bool(two_sided)
        self._auto_reset = bool(auto_reset)
        self._check_every = max(1, int(check_every))
        self._ring_capacity = max(0, int(ring_capacity))

        self._est = EStream(
            1,
            warmup=int(warmup),
            ring_capacity=self._ring_capacity,
            alpha=float(alpha),
            burst_tail_p0=self._burst_tail_p0,
            two_sided=self._two_sided,
            season_period=int(season_period),
            detrend_alpha=float(detrend_alpha),
        )
        self._alpha = self._est.alpha
        self._warmup = self._est.warmup
        self._season_period = self._est.season_period
        self._detrend_alpha = self._est.detrend_alpha
        self._threshold = 1.0 / self._alpha
        self._drift_detected = False
        self._pending_reset = False
        self._last_status: ChannelStatus | None = None
        self._ticks_total = 0
        self._ticks_since_reset = 0
        self._n_detections = 0

    def close(self) -> None:
        self._est.close()

    def __enter__(self) -> EStreamDrift:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def clone(self) -> EStreamDrift:
        """Return a fresh detector with identical configuration (``river`` protocol)."""
        return EStreamDrift(
            alpha=self._alpha,
            warmup=self._warmup,
            burst_tail_p0=self._burst_tail_p0,
            two_sided=self._two_sided,
            season_period=self._season_period,
            detrend_alpha=self._detrend_alpha,
            auto_reset=self._auto_reset,
            check_every=self._check_every,
            ring_capacity=self._ring_capacity,
        )

    def reset(self) -> None:
        """Reset internal e-process state."""
        self._est.reset()
        self._drift_detected = False
        self._pending_reset = False
        self._last_status = None
        self._ticks_since_reset = 0

    def update(self, x: float) -> EStreamDrift:
        """Ingest one scalar observation and update ``drift_detected`` (``river`` protocol)."""
        if self._pending_reset:
            self._est.reset()
            self._pending_reset = False
            self._last_status = None
            self._ticks_since_reset = 0

        self._drift_detected = False
        self._est.update_one(0, float(x))
        self._ticks_total += 1
        self._ticks_since_reset += 1

        if self._ticks_since_reset % self._check_every == 0:
            st = self._est.channel_status(0)
            self._last_status = st
            if st.e_value >= self._threshold:
                self._drift_detected = True
                self._n_detections += 1
                if self._auto_reset:
                    self._pending_reset = True
        return self

    @property
    def drift_detected(self) -> bool:
        """``True`` if drift or burst was detected on the most recent ``update(x)``."""
        return self._drift_detected

    @property
    def warning_detected(self) -> bool:
        """``True`` when e-value crosses half the rejection threshold (``river`` compat)."""
        if self._last_status is None:
            return False
        return self._last_status.e_value >= max(2.0, 0.5 * self._threshold)

    @property
    def e_value(self) -> float:
        """Most recently evaluated anytime-valid e-value."""
        return self._last_status.e_value if self._last_status is not None else 1.0

    @property
    def changepoint_tick(self) -> int:
        """Estimated changepoint tick within the current segment."""
        return self._last_status.changepoint_tick if self._last_status is not None else 0

    @property
    def mode(self) -> str:
        """Attribution mode: ``calm``, ``mean_shift``, ``burst``, or ``both``."""
        return self._last_status.mode if self._last_status is not None else "calm"

    @property
    def n_detections(self) -> int:
        """Cumulative number of detections across the lifetime of this detector."""
        return self._n_detections

    @property
    def total_ticks(self) -> int:
        """Total observations processed."""
        return self._ticks_total

    def __repr__(self) -> str:
        return (
            f"EStreamDrift(alpha={self._alpha}, warmup={self._warmup}, "
            f"season_period={self._season_period}, detrend_alpha={self._detrend_alpha}, "
            f"auto_reset={self._auto_reset}, e_value={self.e_value:.2f}, "
            f"drift_detected={self._drift_detected})"
        )


def _normalize_2d_rows(X: Any) -> list[list[float]]:
    """Convert 1D or 2D sequence / duck-typed array into row-major list[list[float]]."""
    ai = getattr(X, "__array_interface__", None)
    if isinstance(ai, dict):
        shape = ai.get("shape", ())
        tolist = getattr(X, "tolist", None)
        raw = tolist() if callable(tolist) else list(X)
        if len(shape) == 1:
            return [[float(v)] for v in raw]
        if len(shape) == 2:
            return [[float(v) for v in row] for row in raw]
        raise ValueError(f"Expected 1D or 2D array, got shape {shape}")

    seq = list(X)
    if not seq:
        return []
    first = seq[0]
    if isinstance(first, (int, float)):
        return [[float(v)] for v in seq]
    rows: list[list[float]] = []
    width: int | None = None
    for idx, row in enumerate(seq):
        r = [float(v) for v in row]
        if width is None:
            width = len(r)
        elif len(r) != width:
            raise ValueError(f"Row {idx} has {len(r)} features; expected {width}")
        rows.append(r)
    return rows


class SklearnSieveDetector:
    """Zero-dependency scikit-learn ``OutlierMixin``-compatible causal streaming detector.

    Follows scikit-learn's anomaly detection conventions without importing ``sklearn`` or ``numpy``:
      - ``fit(X)`` ingests a baseline stream ``(T, D)`` (or 1D ``(T,)``) and computes channel e-values.
      - ``partial_fit(X)`` incrementally appends a new batch to the live causal monitor.
      - ``decision_function(X)`` streams ``X`` causally and returns per-tick e-BH margins
        (``> 0`` = inlier / calm; ``<= 0`` = e-BH rejection / anomaly, lower = stronger anomaly).
      - ``predict(X)`` returns ``+1`` for calm ticks and ``-1`` when anytime-valid e-BH rejects.
      - ``fit_predict(X)`` resets state and returns causal per-tick ``+1`` / ``-1`` labels on ``X``.

    Channel-level attribution is always available after fitting/scoring via
    :attr:`rejected_channels_`, :attr:`channel_e_values_`, :attr:`channel_statuses_`,
    and :meth:`brief`.
    """

    def __init__(
        self,
        *,
        alpha: float = 0.05,
        warmup: int = 32,
        burst_tail_p0: float = 0.025,
        two_sided: bool = True,
        season_period: int = 0,
        detrend_alpha: float = 0.0,
        ring_capacity: int = 0,
        check_every: int = 1,
        names: Sequence[str] | None = None,
    ) -> None:
        self.alpha = float(alpha)
        self.warmup = int(warmup)
        self.burst_tail_p0 = float(burst_tail_p0)
        self.two_sided = bool(two_sided)
        self.season_period = int(season_period)
        self.detrend_alpha = float(detrend_alpha)
        self.ring_capacity = int(ring_capacity)
        self.check_every = max(1, int(check_every))
        self.names = list(names) if names is not None else None

        self._est: EStream | None = None
        self._last_margin_: float | None = None
        self.n_features_in_: int = 0
        self.n_samples_seen_: int = 0
        self.rejected_channels_: list[int] = []
        self.channel_e_values_: list[float] = []
        self.channel_statuses_: list[ChannelStatus] = []
        self.is_fitted_: bool = False

    def close(self) -> None:
        if self._est is not None:
            self._est.close()
            self._est = None
        self._last_margin_ = None

    def __enter__(self) -> SklearnSieveDetector:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def get_params(self, deep: bool = True) -> dict[str, Any]:
        """Return estimator parameters (scikit-learn ``BaseEstimator`` protocol)."""
        _ = deep
        return {
            "alpha": self.alpha,
            "warmup": self.warmup,
            "burst_tail_p0": self.burst_tail_p0,
            "two_sided": self.two_sided,
            "season_period": self.season_period,
            "detrend_alpha": self.detrend_alpha,
            "ring_capacity": self.ring_capacity,
            "check_every": self.check_every,
            "names": list(self.names) if self.names is not None else None,
        }

    def set_params(self, **params: Any) -> SklearnSieveDetector:
        """Update estimator parameters (scikit-learn ``BaseEstimator`` protocol)."""
        valid = self.get_params()
        structural_keys = {
            "alpha",
            "warmup",
            "burst_tail_p0",
            "two_sided",
            "season_period",
            "detrend_alpha",
            "ring_capacity",
            "names",
        }
        structural_changed = False
        for key, val in params.items():
            if key not in valid:
                raise ValueError(f"Invalid parameter {key!r} for SklearnSieveDetector")
            if key in structural_keys and val != valid[key]:
                structural_changed = True
            setattr(self, key, val)
        self.alpha = float(self.alpha)
        self.warmup = int(self.warmup)
        self.burst_tail_p0 = float(self.burst_tail_p0)
        self.two_sided = bool(self.two_sided)
        self.season_period = int(self.season_period)
        self.detrend_alpha = float(self.detrend_alpha)
        self.ring_capacity = int(self.ring_capacity)
        self.check_every = max(1, int(self.check_every))
        if self.names is not None:
            self.names = list(self.names)
        if structural_changed:
            self.close()
            self.is_fitted_ = False
        return self

    def _new_estream(self, n_features: int) -> EStream:
        use_names = self.names if self.names is not None and len(self.names) == n_features else None
        return EStream(
            n_features,
            warmup=self.warmup,
            ring_capacity=self.ring_capacity,
            alpha=self.alpha,
            burst_tail_p0=self.burst_tail_p0,
            two_sided=self.two_sided,
            season_period=self.season_period,
            detrend_alpha=self.detrend_alpha,
            names=use_names,
        )

    def _refresh_fitted_attrs(self) -> None:
        assert self._est is not None
        eff_alpha = self._est.alpha
        n_ch = self._est.n_channels
        statuses = [self._est.channel_status(c, alpha=eff_alpha) for c in range(n_ch)]
        self.channel_statuses_ = statuses
        self.channel_e_values_ = [s.e_value for s in statuses]
        self.rejected_channels_ = self._est.ebh_reject(eff_alpha)
        self.n_samples_seen_ = self._est.tick_count
        self.is_fitted_ = True

    def _current_ebh_margin(self) -> float:
        """Compute exact signed log e-BH margin: <= 0 iff e-BH rejects >= 1 channel."""
        assert self._est is not None
        eff_alpha = self._est.alpha
        n_ch = self._est.n_channels
        top = self._est.topk(n_ch, alpha=eff_alpha)
        inv_a = float(n_ch) / eff_alpha
        min_margin = float("inf")
        for rank_idx, st in enumerate(top, start=1):
            thresh = inv_a / float(rank_idx)
            ev = max(st.e_value, 1e-300)
            margin = math.log(thresh) - math.log(ev)
            if margin < min_margin:
                min_margin = margin
        return min_margin if math.isfinite(min_margin) else 0.0

    def fit(self, X: Any, y: Any = None) -> SklearnSieveDetector:
        """Fit a fresh causal ``EStream`` on baseline stream ``X`` (shape ``(T, D)`` or ``(T,)``)."""
        _ = y
        rows = _normalize_2d_rows(X)
        if not rows or not rows[0]:
            raise ValueError("X must be non-empty")
        n_features = len(rows[0])
        new_est = self._new_estream(n_features)
        try:
            new_est.update_batch(rows)
        except Exception:
            new_est.close()
            raise
        self.close()
        self._est = new_est
        self.n_features_in_ = n_features
        self._last_margin_ = self._current_ebh_margin()
        self._refresh_fitted_attrs()
        return self

    def partial_fit(self, X: Any, y: Any = None) -> SklearnSieveDetector:
        """Incrementally ingest batch ``X`` into the stateful causal monitor."""
        _ = y
        rows = _normalize_2d_rows(X)
        if not rows or not rows[0]:
            return self
        n_features = len(rows[0])
        if self._est is None:
            self._est = self._new_estream(n_features)
            self.n_features_in_ = n_features
        elif n_features != self.n_features_in_:
            raise ValueError(
                f"X has {n_features} features, but detector expects {self.n_features_in_}"
            )
        self._est.update_batch(rows)
        self._last_margin_ = self._current_ebh_margin()
        self._refresh_fitted_attrs()
        return self

    def decision_function(self, X: Any) -> list[float]:
        """Causally score each row of ``X`` with signed e-BH log-margin (``<= 0`` = anomaly).

        Continues causally from the fitted state if ``fit``/``partial_fit`` was called;
        otherwise initializes a fresh monitor on ``X``. Evaluation cadence depends solely
        on absolute ``tick_count % check_every == 0``, ensuring strict batch-size invariance.
        """
        rows = _normalize_2d_rows(X)
        if not rows or not rows[0]:
            return []
        n_features = len(rows[0])
        if self._est is None:
            self._est = self._new_estream(n_features)
            self.n_features_in_ = n_features
        elif n_features != self.n_features_in_:
            raise ValueError(
                f"X has {n_features} features, but detector expects {self.n_features_in_}"
            )

        eff_alpha = self._est.alpha
        scores: list[float] = []
        last_margin = (
            self._last_margin_
            if self._last_margin_ is not None
            else math.log(float(n_features) / eff_alpha)
        )
        for row in rows:
            self._est.update_row(row)
            if self._est.tick_count % self.check_every == 0:
                last_margin = self._current_ebh_margin()
            scores.append(last_margin)
        self._last_margin_ = last_margin
        self._refresh_fitted_attrs()
        return scores

    def predict(self, X: Any) -> list[int]:
        """Return ``+1`` (inlier/calm) or ``-1`` (e-BH anomaly) for each row of ``X``."""
        return [-1 if s <= 0.0 else 1 for s in self.decision_function(X)]

    def fit_predict(self, X: Any, y: Any = None) -> list[int]:
        """Reset state and return strictly causal ``+1`` / ``-1`` predictions over ``X``."""
        _ = y
        self.close()
        self.is_fitted_ = False
        return self.predict(X)

    def brief(self, max_channels: int = 10) -> StreamBrief:
        """Return the compact agent-ready :class:`StreamBrief` for the fitted monitor."""
        if self._est is None:
            raise RuntimeError("SklearnSieveDetector must be fitted before calling brief()")
        return self._est.brief(max_channels=max_channels, alpha=self._est.alpha)

    def __repr__(self) -> str:
        return (
            f"SklearnSieveDetector(alpha={self.alpha}, warmup={self.warmup}, "
            f"season_period={self.season_period}, detrend_alpha={self.detrend_alpha}, "
            f"is_fitted={self.is_fitted_})"
        )
