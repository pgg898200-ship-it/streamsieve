"""streamsieve — SIMD C++20 multi-agent streaming inference & anytime-valid firehose sifter.

Uses PEP-562 lazy exports so ``import streamsieve`` stays well under 15 ms and
imports zero third-party dependencies.
"""

from __future__ import annotations

__version__ = "0.2.0"

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    # Native helpers & error type
    "StreamSieveError": ("streamsieve._native", "StreamSieveError"),
    "version": ("streamsieve._native", "version"),
    "has_avx2": ("streamsieve._native", "has_avx2"),
    "hash_provenance": ("streamsieve._native", "hash_provenance"),
    # 1. Anytime-valid dual-resolution e-process monitor
    "EStream": ("streamsieve.monitor", "EStream"),
    "ChannelStatus": ("streamsieve.monitor", "ChannelStatus"),
    "StreamBrief": ("streamsieve.monitor", "StreamBrief"),
    "parse_channel_handle": ("streamsieve.monitor", "parse_channel_handle"),
    # 2. Zero-echo CRDT evidence fusion
    "EvidenceLattice": ("streamsieve.lattice", "EvidenceLattice"),
    "HypSummary": ("streamsieve.lattice", "HypSummary"),
    # 3. Hierarchical multi-agent VOI firehose sifter
    "VOISifter": ("streamsieve.sifter", "VOISifter"),
    "VOIAction": ("streamsieve.sifter", "VOIAction"),
    "sift_matrix": ("streamsieve.sifter", "sift_matrix"),
    # 4. Delayed-label trust calibration & Ville quarantine
    "TrustCalibrator": ("streamsieve.trust", "TrustCalibrator"),
    "TrustDecision": ("streamsieve.trust", "TrustDecision"),
    "AgentTrust": ("streamsieve.trust", "AgentTrust"),
    # 5. Zero-lookahead causality verification
    "assert_causal": ("streamsieve.causality", "assert_causal"),
    "verify_estream_causality": ("streamsieve.causality", "verify_estream_causality"),
    "CausalityViolationError": ("streamsieve.causality", "CausalityViolationError"),
    # 6. Stdio MCP server
    "MCPServer": ("streamsieve.mcp_server", "MCPServer"),
    # 7. Drop-in ecosystem adapters (zero third-party imports)
    "EStreamDrift": ("streamsieve.adapters", "EStreamDrift"),
    "SklearnSieveDetector": ("streamsieve.adapters", "SklearnSieveDetector"),
}

__all__ = ["__version__", *_LAZY_EXPORTS.keys()]


def __getattr__(name: str):
    spec = _LAZY_EXPORTS.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    mod_name, attr_name = spec
    module = __import__(mod_name, fromlist=[attr_name])
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
