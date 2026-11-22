"""Zero-dependency drop-in adapters for established streaming APIs (e.g., ``river.drift``).

Allows existing ``river`` pipelines using ``ADWIN`` or ``PageHinkley`` to substitute
``streamsieve`` in one line without changing downstream code or importing ``river``::

    from streamsieve import EStreamDrift as ADWIN

    detector = ADWIN(alpha=0.05)
    for x in stream:
        detector.update(x)
        if detector.drift_detected:
            print("Drift at tick", detector.n_detections, "e=", detector.e_value)
"""

from __future__ import annotations

from typing import Any

from .monitor import ChannelStatus, EStream


class EStreamDrift:
    """Drop-in ``river.drift``-compatible univariate drift & burst detector backed by ``EStream``.

    Implements the ``river.base.DriftDetector`` duck-typed protocol (``update(x) -> self``,
    ``drift_detected``, ``clone()``) while adding:
      - Exact anytime-valid null calibration under continuous peeking (heavy tails, skew, DC offsets)
      - Detection of BOTH mean shifts AND zero-mean symmetric variance/tail bursts
      - Zero runtime dependencies (does not import ``river``, ``numpy``, or ``scipy``)

    Args:
        alpha: Anytime-valid false-alarm budget in ``(0, 0.5]`` (triggers when ``e_value >= 1/alpha``).
        warmup: Causal baseline ticks before testing begins (minimum 12, default 32).
        burst_tail_p0: Baseline tail probability for burst detection (default 0.025).
        two_sided: Detect shifts in both directions (default ``True``).
        auto_reset: If ``True`` (default, matching ``river.drift.ADWIN``), automatically resets the
            underlying e-process on the tick after a detection so multi-changepoint streams work
            out of the box. Set to ``False`` for single-regime anytime-valid stopping.
        check_every: Check the e-value every ``check_every`` ticks (default 1 = every tick).
        ring_capacity: Optional lossless raw-tick ring capacity (default 0 for minimal memory).
    """

    def __init__(
        self,
        alpha: float = 0.05,
        *,
        warmup: int = 32,
        burst_tail_p0: float = 0.025,
        two_sided: bool = True,
        auto_reset: bool = True,
        check_every: int = 1,
        ring_capacity: int = 0,
    ) -> None:
        self._alpha = float(alpha)
        self._warmup = int(warmup)
        self._burst_tail_p0 = float(burst_tail_p0)
        self._two_sided = bool(two_sided)
        self._auto_reset = bool(auto_reset)
        self._check_every = max(1, int(check_every))
        self._ring_capacity = max(0, int(ring_capacity))
        self._threshold = 1.0 / max(1e-9, min(self._alpha, 0.5))

        self._est = EStream(
            1,
            warmup=self._warmup,
            ring_capacity=self._ring_capacity,
            alpha=self._alpha,
            burst_tail_p0=self._burst_tail_p0,
            two_sided=self._two_sided,
        )
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
        """Attribution mode: ``calm``, ``mean_shift``, ``burst``, or ``shift+burst``."""
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
            f"auto_reset={self._auto_reset}, e_value={self.e_value:.2f}, "
            f"drift_detected={self._drift_detected})"
        )
