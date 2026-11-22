/*
 * streamsieve_capi.h — Stable C ABI for the streamsieve SIMD C++20 runtime.
 *
 * Copyright (c) 2026 StreamSieve Contributors
 * Licensed under the Apache License, Version 2.0.
 */
#ifndef STREAMSIEVE_CAPI_H
#define STREAMSIEVE_CAPI_H

#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32) || defined(__CYGWIN__)
  #if defined(STREAMSIEVE_BUILDING_DLL)
    #define SS_API __declspec(dllexport)
  #else
    #define SS_API
  #endif
#elif defined(__GNUC__) && __GNUC__ >= 4
  #define SS_API __attribute__((visibility("default")))
#else
  #define SS_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* Status codes */
#define SS_OK                 0
#define SS_ERR_NULL          -1
#define SS_ERR_INVALID_ARG   -2
#define SS_ERR_OOM           -3
#define SS_ERR_BOUNDS        -4
#define SS_ERR_PARSE         -5
#define SS_ERR_BUFFER_SMALL  -6

/* Evidence merge modes for duplicate provenance IDs (all strictly CRDT-idempotent) */
#define SS_MERGE_STRICT_ORIGIN    0  /* Keep canonical earliest (timestamp_ns, agent_id) */
#define SS_MERGE_MAX_RELIABILITY  1  /* Keep highest (reliability_q16, agent_id) */

/* Opaque handles */
typedef struct ss_estream_t ss_estream_t;
typedef struct ss_lattice_t ss_lattice_t;
typedef struct ss_sifter_t  ss_sifter_t;
typedef struct ss_trust_t   ss_trust_t;

/* =========================================================================
 * Version & CPU Capabilities
 * ========================================================================= */
SS_API const char* ss_version(void);
SS_API int ss_has_avx2(void);

/* =========================================================================
 * 1. EStreamEngine — AVX2 Anytime-Valid Dual-Resolution E-Process & e-BH
 * ========================================================================= */

typedef struct {
    uint32_t n_channels;
    uint32_t warmup;             /* Causal warmup ticks before betting (default 32) */
    uint32_t ring_capacity;      /* Raw tick ring buffer per channel for lossless drill-down (default 256) */
    double   alpha;              /* Target FDR / Type-I error level (default 0.05) */
    double   burst_tail_p0;      /* Null tail rate for burst e-process (default 0.025) */
    double   ewma_reset_decay;   /* Reserved soft decay parameter (0.0 = fixed post-warmup) */
    uint8_t  two_sided;          /* 1 = two-sided shift, 0 = positive shift only */
    uint32_t season_period;      /* 0 = off; P >= 2 subtracts frozen causal per-phase seasonal median */
    double   detrend_alpha;      /* 0.0 = off; in (0, 1] causal EWMA innovation / 1.0 = first difference */
} ss_estream_config_t;

typedef struct {
    uint32_t channel;
    uint32_t changepoint_tick;
    double   e_value;            /* Combined anytime-valid e-value (0.5*e_mean + 0.5*e_burst) */
    double   log_e_mean;         /* Log location-scale invariant two-sample e-value */
    double   log_e_burst;        /* Log tail-burst Bernoulli e-value */
    double   z_score;            /* Causal robust z-score of latest tick */
    double   baseline_loc;       /* Causal baseline location */
    double   baseline_scale;     /* Causal baseline scale */
    uint8_t  mode;               /* 0=calm, 1=mean_shift, 2=burst, 3=both */
    uint8_t  rejected_ebh;       /* 1 if rejected by e-BH at configured alpha */
} ss_channel_status_t;

SS_API void ss_estream_default_config(ss_estream_config_t* cfg, uint32_t n_channels);
SS_API ss_estream_t* ss_estream_create(const ss_estream_config_t* cfg);
SS_API void ss_estream_destroy(ss_estream_t* st);
SS_API int  ss_estream_reset(ss_estream_t* st);

/* Ingest one tick across all n_channels (length = n_channels). */
SS_API int ss_estream_update_row(ss_estream_t* st, const double* values, uint32_t n_values);

/* Bulk ingest n_ticks rows (row-major n_ticks * n_channels). Hot cache-tiled AVX2 path. */
SS_API int ss_estream_update_batch(ss_estream_t* st, const double* matrix, uint32_t n_ticks, uint32_t n_channels);

/* Ingest a single channel value (scalar / sparse tick update). */
SS_API int ss_estream_update_one(ss_estream_t* st, uint32_t channel, double value);

/* Run exact online e-BH across all channels at level alpha.
 * Writes rejected channel indices into out_indices (capacity max_out) and
 * returns count via *out_count. */
SS_API int ss_estream_ebh_reject(const ss_estream_t* st, double alpha,
                                 uint32_t* out_indices, uint32_t max_out, uint32_t* out_count);

/* Get top-K channels ranked by combined e-value. */
SS_API int ss_estream_topk(const ss_estream_t* st, uint32_t k,
                           ss_channel_status_t* out_status, uint32_t* out_count);

/* Get status of a single channel. */
SS_API int ss_estream_get_channel(const ss_estream_t* st, uint32_t channel, ss_channel_status_t* out_status);

/* Lossless raw tick drill-down from per-channel ring buffer.
 * Copies up to max_out most-recent raw ticks in chronological order. */
SS_API int ss_estream_fetch_raw(const ss_estream_t* st, uint32_t channel,
                                double* out_values, uint32_t* out_ticks,
                                uint32_t max_out, uint32_t* out_count);

SS_API uint32_t ss_estream_n_channels(const ss_estream_t* st);
SS_API uint64_t ss_estream_tick_count(const ss_estream_t* st);

/* =========================================================================
 * 2. EvidenceLattice — Zero-Echo Provenance-Deduplicated CRDT Fusion
 * ========================================================================= */

typedef struct {
    uint64_t hyp_id;
    uint64_t provenance_id;
    double   llr;                /* Log-likelihood ratio contribution */
    double   log_e;              /* Log e-value contribution (<=0 or >0; e = exp(log_e)) */
    uint32_t agent_id;
    uint32_t reliability_q16;    /* Fixed-point [0..65536] agent reliability tie-breaker */
    uint64_t timestamp_ns;
} ss_evidence_item_t;

typedef struct {
    uint64_t hyp_id;
    double   posterior_prob;     /* Sigmoid(prior_log_odds + sum_unique weighted LLR) */
    double   total_log_odds;     /* Prior + deduplicated weighted LLR */
    double   merged_e_value;     /* Vovk-Wang admissible cross-agent merged e-value */
    double   merged_log_e;       /* log(merged_e_value) */
    uint32_t unique_evidence;    /* Unique provenance IDs retained */
    uint32_t echoes_suppressed;  /* Duplicate/reprinted provenance IDs suppressed */
    uint32_t distinct_agents;    /* Distinct contributing agents */
} ss_hyp_summary_t;

SS_API ss_lattice_t* ss_lattice_create(int merge_mode);
SS_API void ss_lattice_destroy(ss_lattice_t* lat);
SS_API int  ss_lattice_set_prior(ss_lattice_t* lat, uint64_t hyp_id, double prior_log_odds);
SS_API int  ss_lattice_set_agent_weight(ss_lattice_t* lat, uint32_t agent_id, double weight);

/* Observe one evidence item. Returns 1 in *was_echo if provenance_id was already seen. */
SS_API int  ss_lattice_observe(ss_lattice_t* lat, const ss_evidence_item_t* item, int* was_echo);

/* CRDT Join: dst = merge(dst, src). Strictly commutative, associative, idempotent. */
SS_API int  ss_lattice_merge(ss_lattice_t* dst, const ss_lattice_t* src);

/* Query fused hypothesis summary. */
SS_API int  ss_lattice_query(const ss_lattice_t* lat, uint64_t hyp_id, ss_hyp_summary_t* out_summary);

/* List all hypothesis IDs currently tracked. */
SS_API int  ss_lattice_list_hyps(const ss_lattice_t* lat, uint64_t* out_hyps, uint32_t max_out, uint32_t* out_count);

/* Compact binary wire serialization (for strict byte-budget inter-agent sync).
 * Pass out_buf = NULL to query exact required size in *out_len. */
SS_API int  ss_lattice_serialize(const ss_lattice_t* lat, uint8_t* out_buf, size_t cap, size_t* out_len);
SS_API ss_lattice_t* ss_lattice_deserialize(const uint8_t* buf, size_t len);

/* Deterministic 64-bit provenance hash helper (XXH-style SplitMix64). */
SS_API uint64_t ss_hash_provenance(const uint8_t* data, size_t len);

/* =========================================================================
 * 3. VOISifter — Hierarchical Multi-Agent Value-of-Information Attention
 * ========================================================================= */

typedef struct {
    uint32_t n_channels;
    uint32_t n_sites;            /* Channels grouped evenly into sites (1 = flat) */
    double   prior_active;       /* Prior P(channel active), e.g. 0.02 */
    double   hot_site_prior;     /* Prior P(site is hot), e.g. 0.10 */
    double   hot_site_boost;     /* Active probability inside hot site, e.g. 0.25 */
    double   shift_snr;          /* Expected shift SNR in std units, e.g. 1.5 */
    double   burst_prob;         /* Expected burst duty cycle, e.g. 0.15 */
    double   burst_snr;          /* Expected burst SNR, e.g. 2.5 */
    uint32_t ticks_per_epoch;    /* Raw ticks per channel per epoch, e.g. 8 */
} ss_sifter_config_t;

typedef struct {
    uint32_t agent_slot;         /* Assigned agent index in [0, n_agents) */
    uint8_t  tool_kind;          /* 0=site_agg, 1=channel_mean, 2=channel_ticks */
    uint32_t target_id;          /* Site ID (kind=0) or channel ID (kind=1,2) */
    uint32_t cost;               /* Attention units consumed */
    double   expected_voi;       /* Expected Brier-risk reduction per unit cost */
    double   current_prob;       /* Current posterior P(active) */
} ss_voi_action_t;

SS_API void ss_sifter_default_config(ss_sifter_config_t* cfg, uint32_t n_channels, uint32_t n_sites);
SS_API ss_sifter_t* ss_sifter_create(const ss_sifter_config_t* cfg);
SS_API void ss_sifter_destroy(ss_sifter_t* sf);

/* Set known or estimated noise std per channel (for loud-channel immunity). */
SS_API int ss_sifter_set_noise_scales(ss_sifter_t* sf, const double* scales, uint32_t n_channels);

/* Feed observations at any of the 3 granularities:
 *   observe_site_agg: average standardized reading across site's channels
 *   observe_mean:     epoch mean reading for a single channel
 *   observe_ticks:    raw tick array (length n_ticks) for a single channel */
SS_API int ss_sifter_observe_site_agg(ss_sifter_t* sf, uint32_t site_id, double agg_value);
SS_API int ss_sifter_observe_mean(ss_sifter_t* sf, uint32_t channel, double mean_value);
SS_API int ss_sifter_observe_ticks(ss_sifter_t* sf, uint32_t channel, const double* ticks, uint32_t n_ticks);

/* Greedy VOI attention allocation partitioned with ZERO overlap across n_agents. */
SS_API int ss_sifter_allocate(const ss_sifter_t* sf, uint32_t budget_per_agent, uint32_t n_agents,
                              uint32_t allow_site_agg, uint32_t allow_mean, uint32_t allow_ticks,
                              ss_voi_action_t* out_actions, uint32_t max_out, uint32_t* out_count);

/* Read current posterior P(channel active) for all n_channels. */
SS_API int ss_sifter_posteriors(const ss_sifter_t* sf, double* out_probs, uint32_t n_channels);

/* Advance epoch (optional slight decay toward prior for non-stationary streams). */
SS_API int ss_sifter_step_epoch(ss_sifter_t* sf, double stay_prob);

/* =========================================================================
 * 4. TrustCalibrator — Delayed-Label Agent Trust, Quarantine & Conformal Act
 * ========================================================================= */

typedef struct {
    uint32_t n_agents;
    double   fixed_share_alpha;  /* Fixed-share mixing for rapid flip recovery (default 0.04) */
    double   eta;                /* Learning rate for BOA/Hedge weights (default 0.8) */
    double   quarantine_e_thresh;/* Ville e-value threshold to quarantine poisoned agent (default 20.0) */
    double   recovery_e_thresh;  /* Shadow recovery threshold (default 5.0) */
    double   platt_lr;           /* Online temperature/bias recalibration lr (default 0.15) */
    double   platt_l2;           /* Regularization pulling uncalibrated agents toward 0 (default 0.05) */
    double   conformal_alpha;    /* Target miscoverage for online AgACI (default 0.10 -> 90% CI) */
} ss_trust_config_t;

typedef struct {
    uint32_t agent_id;
    double   weight;             /* Normalized ensemble weight (0.0 if quarantined) */
    double   platt_slope;        /* Learned calibration slope */
    double   platt_bias;         /* Learned calibration intercept */
    double   ema_brier;          /* Causal EMA Brier / squared loss */
    double   quarantine_e;       /* Ville degradation e-value */
    uint8_t  quarantined;        /* 1 if quarantined */
    uint64_t n_resolved;         /* Resolved delayed labels seen */
} ss_agent_trust_t;

typedef struct {
    double   fused_prob;         /* Calibrated trust-weighted probability (binary mode) */
    double   fused_point;        /* Calibrated trust-weighted point prediction (continuous mode) */
    double   ci_low;             /* Online AgACI lower bound */
    double   ci_high;            /* Online AgACI upper bound */
    double   optimal_action;     /* Bayes-optimal cost-asymmetric quantile action */
    uint32_t active_agents;      /* Non-quarantined agents contributing */
} ss_trust_decision_t;

SS_API void ss_trust_default_config(ss_trust_config_t* cfg, uint32_t n_agents);
SS_API ss_trust_t* ss_trust_create(const ss_trust_config_t* cfg);
SS_API void ss_trust_destroy(ss_trust_t* tc);

/* Binary/probability prediction fusion with delayed label tracking.
 * agent_probs: array of length n_agents (NaN for abstaining agents).
 * underage_cost / overage_cost control optimal_action threshold (default 1.0, 1.0). */
SS_API int ss_trust_predict_prob(ss_trust_t* tc, uint64_t decision_id,
                                 const double* agent_probs, uint32_t n_agents,
                                 double underage_cost, double overage_cost,
                                 ss_trust_decision_t* out_dec);

/* Continuous forecast fusion + crossing-free delayed AgACI + Bayes-optimal order.
 * agent_means, agent_stds: length n_agents (NaN for abstaining). */
SS_API int ss_trust_predict_scalar(ss_trust_t* tc, uint64_t decision_id,
                                   const double* agent_means, const double* agent_stds, uint32_t n_agents,
                                   double underage_cost, double overage_cost,
                                   ss_trust_decision_t* out_dec);

/* Strictly causal delayed label resolution (out-of-order safe!). */
SS_API int ss_trust_resolve(ss_trust_t* tc, uint64_t decision_id, double outcome);

/* Query trust & calibration diagnostics for all agents. */
SS_API int ss_trust_get_agents(const ss_trust_t* tc, ss_agent_trust_t* out_agents, uint32_t n_agents);

#ifdef __cplusplus
}
#endif

#endif /* STREAMSIEVE_CAPI_H */
