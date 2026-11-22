"""Anytime-valid dual-resolution e-process stream monitor (EStream)."""

from __future__ import annotations

from array import array
import ctypes
from ctypes import byref, c_double, c_uint32
from dataclasses import dataclass
import math
import re
from typing import Any, Sequence

from ._native import (
    SSChannelStatus,
    SSEStreamConfig,
    SS_ERR_INVALID_ARG,
    StreamSieveError,
    as_double_matrix,
    as_double_ptr,
    check_status,
    get_lib,
    has_avx2,
    validate_uint32,
    version,
)


def _sanitize_cfg(val: float, default: float, lo: float, hi: float) -> float:
    """Mirror C++ sanitize_cfg(v, fallback, lo, hi) so Python properties match native state."""
    fv = float(val)
    if not math.isfinite(fv):
        return default
    if fv < lo:
        return lo
    if fv > hi:
        return hi
    return fv

_MODE_NAMES = {
    0: "calm",
    1: "mean_shift",
    2: "burst",
    3: "both",
}

_HANDLE_RE = re.compile(
    r"^ch:(\d+)(?:@(?:t=)?(\d+)\.\.(\d+))?$"
)


def parse_channel_handle(handle: int | str) -> tuple[int, int | None, int | None]:
    """Parse a channel index or lossless handle ``ch:<id>@t=<cp>..<tick>``.

    Returns ``(channel_id, start_tick, end_tick)``.
    """
    if isinstance(handle, int) and not isinstance(handle, bool):
        return (validate_uint32(handle, "channel"), None, None)
    if not isinstance(handle, str):
        raise TypeError(f"Channel or handle must be int or str, got {type(handle).__name__}")
    m = _HANDLE_RE.match(handle.strip())
    if not m:
        raise ValueError(
            f"Invalid channel handle {handle!r}; expected 'ch:<id>' or 'ch:<id>@t=<cp>..<tick>'"
        )
    ch = int(m.group(1))
    start = int(m.group(2)) if m.group(2) is not None else None
    end = int(m.group(3)) if m.group(3) is not None else None
    return (ch, start, end)


@dataclass(frozen=True)
class ChannelStatus:
    """Anytime-valid statistical status for a single monitored stream channel."""

    channel: int
    name: str
    e_value: float
    log_e_mean: float
    log_e_burst: float
    z_score: float
    changepoint_tick: int
    baseline_loc: float
    baseline_scale: float
    mode: str
    rejected_ebh: bool
    handle: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize status into a compact JSON-ready dictionary."""
        return {
            "channel": self.channel,
            "name": self.name,
            "e_value": self.e_value,
            "log_e_mean": self.log_e_mean,
            "log_e_burst": self.log_e_burst,
            "z_score": self.z_score,
            "changepoint_tick": self.changepoint_tick,
            "baseline_loc": self.baseline_loc,
            "baseline_scale": self.baseline_scale,
            "mode": self.mode,
            "rejected_ebh": self.rejected_ebh,
            "handle": self.handle,
        }


class StreamBrief(dict):
    """Compact agent-ready stream brief (JSON-serializable ``dict`` + compact text).

    Includes lossless drill-down handles of the form ``ch:<id>@t=<cp>..<tick>``.
    """

    def __init__(
        self,
        *,
        n_channels: int,
        ticks: int,
        alpha: float,
        rejected_channels: list[int],
        top_statuses: list[ChannelStatus],
    ) -> None:
        self._statuses = list(top_statuses)
        super().__init__(
            engine="streamsieve",
            version=version(),
            avx2=has_avx2(),
            n_channels=n_channels,
            ticks=ticks,
            alpha=alpha,
            n_rejected=len(rejected_channels),
            rejected_channels=list(rejected_channels),
            top=[s.to_dict() for s in top_statuses],
            handles=[s.handle for s in top_statuses],
        )

    @property
    def statuses(self) -> list[ChannelStatus]:
        """Typed :class:`ChannelStatus` records included in this brief."""
        return list(self._statuses)

    @property
    def n_channels(self) -> int:
        return int(self["n_channels"])

    @property
    def ticks(self) -> int:
        return int(self["ticks"])

    @property
    def alpha(self) -> float:
        return float(self["alpha"])

    @property
    def n_rejected(self) -> int:
        return int(self["n_rejected"])

    @property
    def rejected_channels(self) -> list[int]:
        return list(self["rejected_channels"])

    @property
    def handles(self) -> list[str]:
        return list(self["handles"])

    def to_compact_text(self) -> str:
        """Render an ultra-compact plain-text brief with lossless drill-down handles."""
        lines = [
            (
                f"STREAMSIEVE BRIEF | {self.n_channels} ch x {self.ticks} ticks | "
                f"e-BH(alpha={self.alpha:.3f}): {self.n_rejected}/{self.n_channels} flagged"
            )
        ]
        for s in self._statuses:
            tag = "ALERT" if s.rejected_ebh else "watch"
            lines.append(
                f"  [{tag}] {s.name:<14s} e={s.e_value:10.3g}  "
                f"logE(m={s.log_e_mean:.2f},b={s.log_e_burst:.2f})  "
                f"z={s.z_score:+.2f}  cp@t={s.changepoint_tick}  "
                f"mode={s.mode:<10s}  handle={s.handle}"
            )
        return "\n".join(lines)


class EStream:
    """AVX2-accelerated anytime-valid dual-resolution e-process & e-BH monitor."""

    def __init__(
        self,
        n_channels: int,
        *,
        warmup: int = 32,
        ring_capacity: int = 256,
        alpha: float = 0.05,
        burst_tail_p0: float = 0.025,
        ewma_reset_decay: float = 0.0,
        two_sided: bool = True,
        season_period: int = 0,
        detrend_alpha: float = 0.0,
        names: Sequence[str] | None = None,
    ) -> None:
        self._ptr: int | None = None
        self._lib = get_lib()

        n_ch = validate_uint32(n_channels, "n_channels")
        if n_ch == 0:
            raise StreamSieveError(SS_ERR_INVALID_ARG, "n_channels must be >= 1")
        if names is not None and len(names) != n_ch:
            raise ValueError(f"names length ({len(names)}) must match n_channels ({n_ch})")

        cfg = SSEStreamConfig()
        self._lib.ss_estream_default_config(byref(cfg), n_ch)
        req_warmup = validate_uint32(warmup, "warmup")
        req_ring = validate_uint32(ring_capacity, "ring_capacity")
        req_season = validate_uint32(season_period, "season_period")
        eff_warmup = max(12, req_warmup)
        eff_alpha = _sanitize_cfg(alpha, 0.05, 1e-9, 0.5)
        eff_burst_p0 = _sanitize_cfg(burst_tail_p0, 0.025, 1e-4, 0.25)
        eff_season = min(req_season, 4096) if req_season >= 2 else 0
        eff_detrend = _sanitize_cfg(detrend_alpha, 0.0, 0.0, 1.0)

        cfg.warmup = eff_warmup
        cfg.ring_capacity = req_ring
        cfg.alpha = eff_alpha
        cfg.burst_tail_p0 = eff_burst_p0
        cfg.ewma_reset_decay = float(ewma_reset_decay)
        cfg.two_sided = 1 if two_sided else 0
        cfg.season_period = eff_season
        cfg.detrend_alpha = eff_detrend

        ptr = self._lib.ss_estream_create(byref(cfg))
        if not ptr:
            raise StreamSieveError(SS_ERR_INVALID_ARG, "ss_estream_create failed")

        self._ptr = ptr
        self._n_channels = n_ch
        self._warmup = eff_warmup
        self._ring_capacity = req_ring
        self._alpha = eff_alpha
        self._season_period = eff_season
        self._detrend_alpha = eff_detrend
        self._names: list[str] | None = [str(n) for n in names] if names is not None else None

    def _require_open(self) -> int:
        if not self._ptr:
            raise StreamSieveError(SS_ERR_INVALID_ARG, "EStream has been closed")
        return self._ptr

    def close(self) -> None:
        """Explicitly release the underlying native engine."""
        ptr = getattr(self, "_ptr", None)
        if ptr:
            self._ptr = None
            self._lib.ss_estream_destroy(ptr)

    def __enter__(self) -> EStream:
        self._require_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @property
    def n_channels(self) -> int:
        return int(self._lib.ss_estream_n_channels(self._require_open()))

    @property
    def tick_count(self) -> int:
        return int(self._lib.ss_estream_tick_count(self._require_open()))

    @property
    def alpha(self) -> float:
        return self._alpha

    @property
    def warmup(self) -> int:
        return self._warmup

    @property
    def ring_capacity(self) -> int:
        return self._ring_capacity

    @property
    def season_period(self) -> int:
        return self._season_period

    @property
    def detrend_alpha(self) -> float:
        return self._detrend_alpha

    @property
    def names(self) -> list[str] | None:
        return list(self._names) if self._names is not None else None

    def reset(self) -> None:
        """Reset all channel statistics, ring buffers, and tick counters."""
        rc = self._lib.ss_estream_reset(self._require_open())
        check_status(rc, "ss_estream_reset")

    def update_row(self, values: Any) -> None:
        """Ingest one tick across all channels (zero-copy for contiguous float64)."""
        ptr = self._require_open()
        buf_ptr, length, _owner = as_double_ptr(values)
        if length != self._n_channels:
            raise StreamSieveError(
                SS_ERR_INVALID_ARG,
                f"update_row expected {self._n_channels} values, got {length}",
            )
        rc = self._lib.ss_estream_update_row(ptr, buf_ptr, length)
        check_status(rc, "ss_estream_update_row")

    def update_batch(self, matrix: Any) -> None:
        """Bulk-ingest a row-major ``(n_ticks, n_channels)`` matrix on the AVX2 hot path."""
        ptr = self._require_open()
        buf_ptr, n_ticks, n_cols, _owner = as_double_matrix(
            matrix, expected_cols=self._n_channels
        )
        if n_ticks == 0:
            return
        if n_cols != self._n_channels:
            raise StreamSieveError(
                SS_ERR_INVALID_ARG,
                f"update_batch expected {self._n_channels} channels, got {n_cols}",
            )
        rc = self._lib.ss_estream_update_batch(ptr, buf_ptr, n_ticks, n_cols)
        check_status(rc, "ss_estream_update_batch")

    def update_one(self, channel: int, value: float) -> None:
        """Ingest a single scalar update for one channel."""
        ptr = self._require_open()
        ch = validate_uint32(channel, "channel")
        rc = self._lib.ss_estream_update_one(ptr, ch, float(value))
        check_status(rc, "ss_estream_update_one")

    def ebh_reject(self, alpha: float | None = None) -> list[int]:
        """Run exact anytime-valid online e-BH across all channels at level ``alpha``."""
        ptr = self._require_open()
        a = self._alpha if alpha is None else _sanitize_cfg(alpha, self._alpha, 1e-9, 0.5)
        out_arr = (c_uint32 * self._n_channels)()
        out_count = c_uint32(0)
        rc = self._lib.ss_estream_ebh_reject(
            ptr, a, out_arr, self._n_channels, byref(out_count)
        )
        check_status(rc, "ss_estream_ebh_reject")
        return [int(out_arr[i]) for i in range(out_count.value)]

    def _channel_name(self, ch: int, names: Sequence[str] | None) -> str:
        src = names if names is not None else self._names
        if src is not None and 0 <= ch < len(src) and src[ch]:
            return str(src[ch])
        return f"ch_{ch}"

    def _wrap_status(
        self,
        raw: SSChannelStatus,
        *,
        tick_count: int,
        names: Sequence[str] | None = None,
        override_name: str | None = None,
        rejected_override: bool | None = None,
    ) -> ChannelStatus:
        ch = int(raw.channel)
        cp = int(raw.changepoint_tick)
        name = override_name if override_name is not None else self._channel_name(ch, names)
        rejected = bool(raw.rejected_ebh) if rejected_override is None else rejected_override
        return ChannelStatus(
            channel=ch,
            name=name,
            e_value=float(raw.e_value),
            log_e_mean=float(raw.log_e_mean),
            log_e_burst=float(raw.log_e_burst),
            z_score=float(raw.z_score),
            changepoint_tick=cp,
            baseline_loc=float(raw.baseline_loc),
            baseline_scale=float(raw.baseline_scale),
            mode=_MODE_NAMES.get(int(raw.mode), "calm"),
            rejected_ebh=rejected,
            handle=f"ch:{ch}@t={cp}..{tick_count}",
        )

    def topk(
        self,
        k: int = 10,
        *,
        names: Sequence[str] | None = None,
        alpha: float | None = None,
    ) -> list[ChannelStatus]:
        """Return the top-``k`` channels ranked by combined anytime-valid e-value."""
        ptr = self._require_open()
        if k <= 0:
            return []
        k_req = min(validate_uint32(k, "k"), self._n_channels)
        out_buf = (SSChannelStatus * k_req)()
        out_count = c_uint32(0)
        rc = self._lib.ss_estream_topk(ptr, k_req, out_buf, byref(out_count))
        check_status(rc, "ss_estream_topk")

        ticks = self.tick_count
        rejected_set: set[int] | None = None
        if alpha is not None:
            eff_a = _sanitize_cfg(alpha, self._alpha, 1e-9, 0.5)
            if eff_a != self._alpha:
                rejected_set = set(self.ebh_reject(eff_a))

        result: list[ChannelStatus] = []
        for i in range(out_count.value):
            st = out_buf[i]
            rej_ov = (int(st.channel) in rejected_set) if rejected_set is not None else None
            result.append(
                self._wrap_status(
                    st, tick_count=ticks, names=names, rejected_override=rej_ov
                )
            )
        return result

    def channel_status(
        self,
        channel: int,
        *,
        name: str | None = None,
        alpha: float | None = None,
    ) -> ChannelStatus:
        """Query the current anytime-valid statistical state of a single channel."""
        ptr = self._require_open()
        ch = validate_uint32(channel, "channel")
        out = SSChannelStatus()
        rc = self._lib.ss_estream_get_channel(ptr, ch, byref(out))
        check_status(rc, "ss_estream_get_channel")
        rej_ov: bool | None = None
        if alpha is not None:
            rej_ov = ch in set(self.ebh_reject(alpha))
        return self._wrap_status(
            out,
            tick_count=self.tick_count,
            override_name=name,
            rejected_override=rej_ov,
        )

    def fetch_raw(
        self,
        channel: int | str,
        limit: int | None = None,
        *,
        start_tick: int | None = None,
        end_tick: int | None = None,
    ) -> list[tuple[int, float]]:
        """Lossless raw tick drill-down from the per-channel ring buffer.

        Accepts either an integer channel index or a drill-down handle string
        such as ``ch:3@t=180..600``. Returns chronological ``(tick, value)`` pairs.
        """
        ptr = self._require_open()
        ch, h_start, h_end = parse_channel_handle(channel)
        if start_tick is None and h_start is not None:
            start_tick = h_start
        if end_tick is None and h_end is not None:
            end_tick = h_end

        if limit is not None and limit <= 0:
            return []

        # Query available ring length first so we allocate only what exists
        avail = c_uint32(0)
        rc = self._lib.ss_estream_fetch_raw(ptr, ch, None, None, 0xFFFFFFFF, byref(avail))
        check_status(rc, "ss_estream_fetch_raw")
        n_avail = int(avail.value)
        if n_avail == 0:
            return []

        if start_tick is None and end_tick is None and limit is not None:
            n_req = min(n_avail, int(limit))
        else:
            n_req = n_avail

        vals = array("d", [0.0]) * n_req
        ticks = (c_uint32 * n_req)()
        got = c_uint32(0)
        v_addr, _ = vals.buffer_info()
        rc = self._lib.ss_estream_fetch_raw(
            ptr, ch, ctypes.cast(v_addr, ctypes.POINTER(c_double)), ticks, n_req, byref(got)
        )
        check_status(rc, "ss_estream_fetch_raw")

        pairs = [(int(ticks[i]), float(vals[i])) for i in range(got.value)]
        if start_tick is not None:
            pairs = [(t, v) for t, v in pairs if t >= start_tick]
        if end_tick is not None:
            pairs = [(t, v) for t, v in pairs if t <= end_tick]
        if limit is not None and len(pairs) > limit:
            pairs = pairs[-int(limit) :]
        return pairs

    def brief(
        self,
        max_channels: int = 10,
        names: Sequence[str] | None = None,
        alpha: float | None = None,
    ) -> StreamBrief:
        """Generate an ultra-compact agent-ready e-BH brief with lossless drill-down handles."""
        a = self._alpha if alpha is None else _sanitize_cfg(alpha, self._alpha, 1e-9, 0.5)
        rejected = self.ebh_reject(a)
        top = self.topk(max_channels, names=names, alpha=a)
        return StreamBrief(
            n_channels=self._n_channels,
            ticks=self.tick_count,
            alpha=a,
            rejected_channels=rejected,
            top_statuses=top,
        )
