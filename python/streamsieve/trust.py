"""Delayed-label agent trust calibration, Ville quarantine & conformal action (TrustCalibrator)."""

from __future__ import annotations

from ctypes import byref
from dataclasses import dataclass
from typing import Any

from ._native import (
    SSAgentTrust,
    SSTrustConfig,
    SSTrustDecision,
    SS_ERR_INVALID_ARG,
    StreamSieveError,
    as_double_ptr,
    check_status,
    get_lib,
    hash_provenance,
    validate_uint32,
    validate_uint64,
)


@dataclass(frozen=True)
class TrustDecision:
    """Fused calibrated prediction, AgACI interval, and cost-asymmetric optimal action."""

    decision_id: int
    fused_prob: float
    fused_point: float
    ci_low: float
    ci_high: float
    optimal_action: float
    active_agents: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize decision into a compact JSON-ready dictionary."""
        return {
            "decision_id": self.decision_id,
            "fused_prob": self.fused_prob,
            "fused_point": self.fused_point,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "optimal_action": self.optimal_action,
            "active_agents": self.active_agents,
        }


@dataclass(frozen=True)
class AgentTrust:
    """Per-agent trust weight, Platt calibration parameters, and Ville quarantine state."""

    agent_id: int
    weight: float
    platt_slope: float
    platt_bias: float
    ema_brier: float
    quarantine_e: float
    quarantined: bool
    n_resolved: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize agent trust diagnostics into a compact JSON-ready dictionary."""
        return {
            "agent_id": self.agent_id,
            "weight": self.weight,
            "platt_slope": self.platt_slope,
            "platt_bias": self.platt_bias,
            "ema_brier": self.ema_brier,
            "quarantine_e": self.quarantine_e,
            "quarantined": self.quarantined,
            "n_resolved": self.n_resolved,
        }


def _resolve_decision_id(decision_id: int | str) -> int:
    if isinstance(decision_id, str):
        return hash_provenance(decision_id)
    return validate_uint64(decision_id, "decision_id")


class TrustCalibrator:
    """Online multi-agent trust calibration with delayed-label safety & Ville quarantine."""

    def __init__(
        self,
        n_agents: int,
        *,
        fixed_share_alpha: float = 0.04,
        eta: float = 0.8,
        quarantine_e_thresh: float = 20.0,
        recovery_e_thresh: float = 5.0,
        platt_lr: float = 0.15,
        platt_l2: float = 0.05,
        conformal_alpha: float = 0.10,
    ) -> None:
        self._ptr: int | None = None
        self._lib = get_lib()

        n_ag = validate_uint32(n_agents, "n_agents")
        if n_ag == 0:
            raise StreamSieveError(SS_ERR_INVALID_ARG, "n_agents must be >= 1")

        cfg = SSTrustConfig()
        self._lib.ss_trust_default_config(byref(cfg), n_ag)
        cfg.fixed_share_alpha = float(fixed_share_alpha)
        cfg.eta = float(eta)
        cfg.quarantine_e_thresh = float(quarantine_e_thresh)
        cfg.recovery_e_thresh = float(recovery_e_thresh)
        cfg.platt_lr = float(platt_lr)
        cfg.platt_l2 = float(platt_l2)
        cfg.conformal_alpha = float(conformal_alpha)

        ptr = self._lib.ss_trust_create(byref(cfg))
        if not ptr:
            raise StreamSieveError(SS_ERR_INVALID_ARG, "ss_trust_create failed")

        self._ptr = ptr
        self._n_agents = n_ag

    def _require_open(self) -> int:
        if not self._ptr:
            raise StreamSieveError(SS_ERR_INVALID_ARG, "TrustCalibrator has been closed")
        return self._ptr

    def close(self) -> None:
        """Explicitly release the underlying native trust calibrator."""
        ptr = getattr(self, "_ptr", None)
        if ptr:
            self._ptr = None
            self._lib.ss_trust_destroy(ptr)

    def __enter__(self) -> TrustCalibrator:
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
    def n_agents(self) -> int:
        return self._n_agents

    def predict_prob(
        self,
        decision_id: int | str,
        agent_probs: Any,
        underage_cost: float = 1.0,
        overage_cost: float = 1.0,
    ) -> TrustDecision:
        """Fuse binary probability forecasts (use ``None`` or ``NaN`` for abstaining agents)."""
        ptr = self._require_open()
        did = _resolve_decision_id(decision_id)
        p_ptr, n_p, _owner = as_double_ptr(agent_probs, allow_none_as_nan=True)
        if n_p != self._n_agents:
            raise StreamSieveError(
                SS_ERR_INVALID_ARG,
                f"predict_prob expected {self._n_agents} probabilities, got {n_p}",
            )
        out = SSTrustDecision()
        rc = self._lib.ss_trust_predict_prob(
            ptr, did, p_ptr, n_p, float(underage_cost), float(overage_cost), byref(out)
        )
        check_status(rc, "ss_trust_predict_prob")
        return TrustDecision(
            decision_id=did,
            fused_prob=float(out.fused_prob),
            fused_point=float(out.fused_point),
            ci_low=float(out.ci_low),
            ci_high=float(out.ci_high),
            optimal_action=float(out.optimal_action),
            active_agents=int(out.active_agents),
        )

    def predict_scalar(
        self,
        decision_id: int | str,
        agent_means: Any,
        agent_stds: Any = None,
        underage_cost: float = 1.0,
        overage_cost: float = 1.0,
    ) -> TrustDecision:
        """Fuse continuous scalar forecasts with delayed AgACI CIs and Bayes-optimal action."""
        ptr = self._require_open()
        did = _resolve_decision_id(decision_id)
        m_ptr, n_m, _m_owner = as_double_ptr(agent_means, allow_none_as_nan=True)
        if n_m != self._n_agents:
            raise StreamSieveError(
                SS_ERR_INVALID_ARG,
                f"predict_scalar expected {self._n_agents} means, got {n_m}",
            )

        s_ptr = None
        _s_owner = None
        if agent_stds is not None:
            s_ptr, n_s, _s_owner = as_double_ptr(agent_stds, allow_none_as_nan=True)
            if n_s != self._n_agents:
                raise StreamSieveError(
                    SS_ERR_INVALID_ARG,
                    f"predict_scalar expected {self._n_agents} stds, got {n_s}",
                )

        out = SSTrustDecision()
        rc = self._lib.ss_trust_predict_scalar(
            ptr,
            did,
            m_ptr,
            s_ptr,
            n_m,
            float(underage_cost),
            float(overage_cost),
            byref(out),
        )
        check_status(rc, "ss_trust_predict_scalar")
        return TrustDecision(
            decision_id=did,
            fused_prob=float(out.fused_prob),
            fused_point=float(out.fused_point),
            ci_low=float(out.ci_low),
            ci_high=float(out.ci_high),
            optimal_action=float(out.optimal_action),
            active_agents=int(out.active_agents),
        )

    def resolve(self, decision_id: int | str, outcome: float) -> None:
        """Resolve a delayed ground-truth label causally (safe for out-of-order arrivals)."""
        ptr = self._require_open()
        did = _resolve_decision_id(decision_id)
        rc = self._lib.ss_trust_resolve(ptr, did, float(outcome))
        check_status(rc, "ss_trust_resolve")

    def agents(self) -> list[AgentTrust]:
        """Return trust weights, Platt calibration, and quarantine diagnostics for all agents."""
        ptr = self._require_open()
        out_buf = (SSAgentTrust * self._n_agents)()
        rc = self._lib.ss_trust_get_agents(ptr, out_buf, self._n_agents)
        check_status(rc, "ss_trust_get_agents")
        return [
            AgentTrust(
                agent_id=int(out_buf[i].agent_id),
                weight=float(out_buf[i].weight),
                platt_slope=float(out_buf[i].platt_slope),
                platt_bias=float(out_buf[i].platt_bias),
                ema_brier=float(out_buf[i].ema_brier),
                quarantine_e=float(out_buf[i].quarantine_e),
                quarantined=bool(out_buf[i].quarantined),
                n_resolved=int(out_buf[i].n_resolved),
            )
            for i in range(self._n_agents)
        ]
