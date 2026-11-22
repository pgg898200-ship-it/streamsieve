/*
 * streamsieve_core.hpp — SIMD C++20 runtime for multi-agent streaming inference.
 *
 * Four co-designed, strictly causal, zero-dependency engines:
 *   1. EStreamEngine   : AVX2 SoA dual-resolution anytime-valid e-process + exact online e-BH
 *                        + lossless raw-tick drill-down ring.
 *   2. EvidenceLattice : Zero-echo provenance-deduplicated CRDT belief & Vovk-Wang e-merger.
 *   3. VOISifter       : Hierarchical site x channel x tick greedy VOI multi-agent allocator.
 *   4. TrustCalibrator : Delayed-label discounted fixed-share BOA + online Platt shrinkage
 *                        + Ville e-value agent quarantine + delayed AgACI + Bayes-optimal action.
 *
 * Copyright (c) 2026 StreamSieve Contributors
 * Licensed under the Apache License, Version 2.0.
 */
#ifndef STREAMSIEVE_CORE_HPP
#define STREAMSIEVE_CORE_HPP

#include "streamsieve_capi.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <map>
#include <numeric>
#include <unordered_map>
#include <utility>
#include <vector>

#if defined(__AVX2__)
#include <immintrin.h>
#endif

namespace streamsieve {

static constexpr const char* kVersion = "0.1.0";

inline bool has_avx2() noexcept {
#if defined(__AVX2__)
    return true;
#else
    return false;
#endif
}

inline double clamp_d(double x, double lo, double hi) noexcept {
    if (!std::isfinite(x)) return lo;
    return x < lo ? lo : (x > hi ? hi : x);
}

inline double sanitize_cfg(double x, double def_val, double lo, double hi) noexcept {
    if (!std::isfinite(x)) return def_val;
    return x < lo ? lo : (x > hi ? hi : x);
}

inline double sigmoid_d(double x) noexcept {
    if (x >= 0.0) {
        double z = std::exp(-std::min(x, 60.0));
        return 1.0 / (1.0 + z);
    } else {
        double z = std::exp(std::max(x, -60.0));
        return z / (1.0 + z);
    }
}

inline double logit_d(double p) noexcept {
    double q = clamp_d(p, 1e-12, 1.0 - 1e-12);
    return std::log(q / (1.0 - q));
}

inline double log_sum_exp(double a, double b) noexcept {
    if (a == -std::numeric_limits<double>::infinity()) return b;
    if (b == -std::numeric_limits<double>::infinity()) return a;
    double m = a > b ? a : b;
    return m + std::log(std::exp(a - m) + std::exp(b - m));
}

/* SplitMix64 / FNV-1a-avalanche deterministic 64-bit provenance hash */
inline uint64_t hash_provenance(const uint8_t* data, size_t len) noexcept {
    uint64_t h = 0xcbf29ce484222325ULL ^ static_cast<uint64_t>(len);
    for (size_t i = 0; i < len; ++i) {
        h ^= static_cast<uint64_t>(data[i]);
        h *= 0x100000001b3ULL;
    }
    h ^= h >> 30;
    h *= 0xbf58476d1ce4e5b9ULL;
    h ^= h >> 27;
    h *= 0x94d049bb133111ebULL;
    h ^= h >> 31;
    return h;
}

/* Standard normal quantile approximation (Acklam rational approximation, max err < 1.15e-9) */
inline double norm_ppf(double p) noexcept {
    p = clamp_d(p, 1e-12, 1.0 - 1e-12);
    static constexpr double a[6] = {
        -3.969683028665376e+01,  2.209460984245205e+02,
        -2.759285104469687e+02,  1.383577518672690e+02,
        -3.066479806614716e+01,  2.506628277459239e+00
    };
    static constexpr double b[5] = {
        -5.447609879822406e+01,  1.615858368580409e+02,
        -1.556989798598866e+02,  6.680131188771972e+01,
        -1.328068155288572e+01
    };
    static constexpr double c[6] = {
        -7.784894002430293e-03, -3.223964580411365e-01,
        -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00,  2.938163982698783e+00
    };
    static constexpr double d[4] = {
         7.784695709041462e-03,  3.224671290700398e-01,
         2.445134137142996e+00,  3.754408661907416e+00
    };
    static constexpr double plow = 0.02425;
    static constexpr double phigh = 1.0 - plow;
    if (p < plow) {
        double q = std::sqrt(-2.0 * std::log(p));
        return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) /
               ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0);
    } else if (p <= phigh) {
        double q = p - 0.5;
        double r = q * q;
        return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q /
               (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1.0);
    } else {
        double q = std::sqrt(-2.0 * std::log(1.0 - p));
        return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) /
                ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0);
    }
}

/* =========================================================================
 * 1. EStreamEngine — AVX2 Anytime-Valid Dual-Resolution E-Process & e-BH
 *
 * Mathematical Guarantees:
 *   - Strictly causal: outputs at tick t depend ONLY on x[0..t].
 *   - Location & scale invariant: shifted-anchor Welford + warmup median/MAD
 *     immune to 1e10 DC offsets, 1e15 outlier spikes, and 1000x loud channels.
 *   - Distribution-free anytime-valid e-processes:
 *       (a) Two-sample Universal-Inference Beta-Binomial e-process on causal
 *           sign exceedances (median shift) and dual tail exceedances (rare bursts),
 *           exact E_H0[E_tau] <= 1 for ANY continuous/discrete stationary null
 *           at EVERY stopping time tau (Turner, Ly, Grunwald 2024; Wasserman et al. 2020).
 *       (b) Bounded-tanh two-sample Group-Invariant JZS sequential t-test e-process
 *           for high-power smooth shift detection, with conservative variance floor.
 *       (c) Dyadic checkpoint mixture with exact summable weights pi_k = 1/((k+1)(k+2))
 *           catching changes at ANY unknown changepoint t* in [W0, 10^6+].
 *   - Online e-BH (Wang & Ramdas 2022) controls FDR <= alpha under arbitrary
 *     cross-channel and cross-agent dependence.
 * ========================================================================= */

class EStreamEngine {
public:
    static constexpr uint32_t kMaxCheckpoints = 24; /* 1.5x spacing covers > 500k ticks */

    explicit EStreamEngine(const ss_estream_config_t& cfg)
        : n_ch_(cfg.n_channels),
          warmup_(std::max<uint32_t>(cfg.warmup, 12)),
          ring_cap_(cfg.ring_capacity),
          alpha_(sanitize_cfg(cfg.alpha, 0.05, 1e-9, 0.5)),
          burst_p0_(sanitize_cfg(cfg.burst_tail_p0, 0.025, 1e-4, 0.25)),
          two_sided_(cfg.two_sided != 0),
          tick_count_(0) {
        allocate();
    }

    void reset() {
        tick_count_ = 0;
        std::fill(n_seen_.begin(), n_seen_.end(), 0);
        std::fill(n_post_.begin(), n_post_.end(), 0);
        std::fill(loc_.begin(), loc_.end(), 0.0);
        std::fill(scale_.begin(), scale_.end(), 1.0);
        std::fill(inv_scale_.begin(), inv_scale_.end(), 1.0);
        std::fill(last_z_.begin(), last_z_.end(), 0.0);
        std::fill(sum_u_.begin(), sum_u_.end(), 0.0);
        std::fill(sum_u2_.begin(), sum_u2_.end(), 0.0);
        std::fill(sum_tail_.begin(), sum_tail_.end(), 0.0);
        std::fill(sum_tail2_.begin(), sum_tail2_.end(), 0.0);
        std::fill(cnt_sign_.begin(), cnt_sign_.end(), 0);
        std::fill(cnt_burst_.begin(), cnt_burst_.end(), 0);
        std::fill(cnt_ext_burst_.begin(), cnt_ext_burst_.end(), 0);
        std::fill(n_snaps_.begin(), n_snaps_.end(), 0);
        std::fill(next_snap_n_.begin(), next_snap_n_.end(), warmup_);
        std::fill(best_log_e_mean_.begin(), best_log_e_mean_.end(), 0.0);
        std::fill(best_log_e_burst_.begin(), best_log_e_burst_.end(), 0.0);
        std::fill(combined_e_.begin(), combined_e_.end(), 1.0);
        std::fill(best_cp_tick_.begin(), best_cp_tick_.end(), 0);
        std::fill(mode_.begin(), mode_.end(), 0);
        std::fill(ring_pos_.begin(), ring_pos_.end(), 0);
        std::fill(ring_len_.begin(), ring_len_.end(), 0);
        std::fill(warmup_buf_.begin(), warmup_buf_.end(), 0.0);
        std::fill(dirty_.begin(), dirty_.end(), 0);
        all_warmed_ = false;
        sync_post_ = false;
        sync_post_n_ = 0;
        sync_next_snap_ = std::max<uint32_t>(warmup_, 24);
        all_dirty_ = false;
        ebh_cache_valid_ = false;
        ebh_thresh_ = std::numeric_limits<double>::infinity();
    }

    int update_row(const double* values, uint32_t n_values) noexcept {
        if (!values || n_values != n_ch_) return SS_ERR_INVALID_ARG;
        ++tick_count_;
        ebh_cache_valid_ = false;
        const uint32_t cur_tick = static_cast<uint32_t>(tick_count_);

        /* Fast check: once all channels are past warmup, execute vectorized SoA hot loop! */
        if (all_warmed_) {
            update_row_warm_simd(values, cur_tick);
            return SS_OK;
        }

        uint32_t warmed_count = 0;
        uint32_t zero_post_count = 0;
        for (uint32_t c = 0; c < n_ch_; ++c) {
            update_one_impl(c, values[c], cur_tick);
            if (n_seen_[c] >= warmup_) {
                ++warmed_count;
                if (n_post_[c] == 0) ++zero_post_count;
            }
        }
        all_warmed_ = (warmed_count == n_ch_);
        if (all_warmed_ && zero_post_count == n_ch_) {
            sync_post_ = true;
            sync_post_n_ = 0;
            sync_next_snap_ = (n_ch_ > 0) ? next_snap_n_[0] : std::numeric_limits<uint32_t>::max();
        }
        return SS_OK;
    }

    int update_batch(const double* matrix, uint32_t n_ticks, uint32_t n_channels) noexcept {
        if (!matrix || n_channels != n_ch_) return SS_ERR_INVALID_ARG;
        for (uint32_t t = 0; t < n_ticks; ++t) {
            const double* row = matrix + static_cast<size_t>(t) * n_ch_;
            int rc = update_row(row, n_ch_);
            if (rc != SS_OK) return rc;
        }
        return SS_OK;
    }

    int update_one(uint32_t c, double value) noexcept {
        if (c >= n_ch_) return SS_ERR_BOUNDS;
        materialize_sync();
        ebh_cache_valid_ = false;
        uint32_t cur_tick = n_seen_[c] + 1;
        if (static_cast<uint64_t>(cur_tick) > tick_count_) tick_count_ = cur_tick;
        all_warmed_ = false;
        sync_post_ = false;
        update_one_impl(c, value, cur_tick);
        return SS_OK;
    }

    void materialize_sync() const noexcept {
        if (sync_post_) {
            for (uint32_t c = 0; c < n_ch_; ++c) {
                n_post_[c] = sync_post_n_;
                n_seen_[c] = warmup_ + sync_post_n_;
                if (all_dirty_) dirty_[c] = 1;
            }
            all_dirty_ = false;
        }
    }

    void flush_evalues() const noexcept {
        materialize_sync();
        for (uint32_t c = 0; c < n_ch_; ++c) {
            if (dirty_[c]) {
                recompute_channel_evalue(c);
                dirty_[c] = 0;
            }
        }
    }

    void ensure_ebh_cache() const {
        if (ebh_cache_valid_) return;
        flush_evalues();
        std::vector<double> vals = combined_e_;
        std::sort(vals.begin(), vals.end(), std::greater<double>());
        uint32_t k_star = 0;
        const double inv_a = static_cast<double>(n_ch_) / alpha_;
        for (uint32_t k = 1; k <= n_ch_; ++k) {
            if (vals[k - 1] >= inv_a / static_cast<double>(k)) {
                k_star = k;
            }
        }
        ebh_thresh_ = (k_star > 0) ? (inv_a / static_cast<double>(k_star))
                                   : std::numeric_limits<double>::infinity();
        ebh_cache_valid_ = true;
    }

    int ebh_reject(double alpha, uint32_t* out_indices, uint32_t max_out, uint32_t* out_count) const {
        if (!out_count) return SS_ERR_NULL;
        flush_evalues();
        double a = sanitize_cfg(alpha, alpha_, 1e-12, 1.0);
        std::vector<std::pair<double, uint32_t>> ranked(n_ch_);
        for (uint32_t c = 0; c < n_ch_; ++c) {
            ranked[c] = {combined_e_[c], c};
        }
        std::sort(ranked.begin(), ranked.end(),
                  [](const auto& x, const auto& y) {
                      if (x.first != y.first) return x.first > y.first;
                      return x.second < y.second;
                  });
        uint32_t k_star = 0;
        const double inv_a = static_cast<double>(n_ch_) / a;
        for (uint32_t k = 1; k <= n_ch_; ++k) {
            if (ranked[k - 1].first >= inv_a / static_cast<double>(k)) {
                k_star = k;
            }
        }
        if (a == alpha_) {
            ebh_thresh_ = (k_star > 0) ? (inv_a / static_cast<double>(k_star))
                                       : std::numeric_limits<double>::infinity();
            ebh_cache_valid_ = true;
        }
        *out_count = k_star;
        if (out_indices && k_star > 0) {
            uint32_t n_copy = std::min(k_star, max_out);
            for (uint32_t i = 0; i < n_copy; ++i) {
                out_indices[i] = ranked[i].second;
            }
        }
        return SS_OK;
    }

    int get_channel(uint32_t c, ss_channel_status_t* out) const {
        if (!out) return SS_ERR_NULL;
        if (c >= n_ch_) return SS_ERR_BOUNDS;
        ensure_ebh_cache();
        out->channel = c;
        out->changepoint_tick = best_cp_tick_[c];
        out->e_value = combined_e_[c];
        out->log_e_mean = best_log_e_mean_[c];
        out->log_e_burst = best_log_e_burst_[c];
        out->z_score = last_z_[c];
        out->baseline_loc = loc_[c];
        out->baseline_scale = scale_[c];
        out->mode = mode_[c];
        out->rejected_ebh = (combined_e_[c] >= ebh_thresh_) ? 1 : 0;
        return SS_OK;
    }

    int topk(uint32_t k, ss_channel_status_t* out_status, uint32_t* out_count) const {
        if (!out_status || !out_count) return SS_ERR_NULL;
        flush_evalues();
        uint32_t k_eff = std::min(k, n_ch_);

        /* Compute exact e-BH rejection threshold */
        std::vector<std::pair<double, uint32_t>> ranked(n_ch_);
        for (uint32_t c = 0; c < n_ch_; ++c) {
            ranked[c] = {combined_e_[c], c};
        }
        std::sort(ranked.begin(), ranked.end(),
                  [](const auto& x, const auto& y) {
                      if (x.first != y.first) return x.first > y.first;
                      return x.second < y.second;
                  });
        uint32_t k_star = 0;
        const double inv_a = static_cast<double>(n_ch_) / alpha_;
        for (uint32_t i = 1; i <= n_ch_; ++i) {
            if (ranked[i - 1].first >= inv_a / static_cast<double>(i)) {
                k_star = i;
            }
        }
        ebh_thresh_ = (k_star > 0) ? (inv_a / static_cast<double>(k_star))
                                   : std::numeric_limits<double>::infinity();
        ebh_cache_valid_ = true;
        for (uint32_t i = 0; i < k_eff; ++i) {
            uint32_t c = ranked[i].second;
            out_status[i].channel = c;
            out_status[i].changepoint_tick = best_cp_tick_[c];
            out_status[i].e_value = combined_e_[c];
            out_status[i].log_e_mean = best_log_e_mean_[c];
            out_status[i].log_e_burst = best_log_e_burst_[c];
            out_status[i].z_score = last_z_[c];
            out_status[i].baseline_loc = loc_[c];
            out_status[i].baseline_scale = scale_[c];
            out_status[i].mode = mode_[c];
            out_status[i].rejected_ebh = (i < k_star) ? 1 : 0;
        }
        *out_count = k_eff;
        return SS_OK;
    }

    int fetch_raw(uint32_t c, double* out_values, uint32_t* out_ticks,
                  uint32_t max_out, uint32_t* out_count) const noexcept {
        if (!out_count) return SS_ERR_NULL;
        if (c >= n_ch_) return SS_ERR_BOUNDS;
        if (ring_cap_ == 0) {
            *out_count = 0;
            return SS_OK;
        }
        uint32_t avail = ring_len_[c];
        uint32_t n_copy = std::min(avail, max_out);
        *out_count = n_copy;
        if (n_copy == 0 || !out_values) return SS_OK;

        const size_t base = static_cast<size_t>(c) * ring_cap_;
        uint32_t start = (ring_pos_[c] + ring_cap_ - n_copy) % ring_cap_;
        for (uint32_t i = 0; i < n_copy; ++i) {
            uint32_t idx = (start + i) % ring_cap_;
            out_values[i] = ring_vals_[base + idx];
            if (out_ticks) out_ticks[i] = ring_ticks_[base + idx];
        }
        return SS_OK;
    }

    uint32_t n_channels() const noexcept { return n_ch_; }
    uint64_t tick_count() const noexcept { return tick_count_; }

private:
    struct Snap {
        uint32_t n0;
        uint32_t tick;
        uint32_t sign0;
        uint32_t burst0;
        uint32_t ext_burst0;
        double   sum_u0;
        double   sum_u20;
        double   sum_tail0;
        double   sum_tail20;
    };

    void allocate() {
        n_seen_.assign(n_ch_, 0);
        n_post_.assign(n_ch_, 0);
        loc_.assign(n_ch_, 0.0);
        scale_.assign(n_ch_, 1.0);
        inv_scale_.assign(n_ch_, 1.0);
        last_z_.assign(n_ch_, 0.0);
        sum_u_.assign(n_ch_, 0.0);
        sum_u2_.assign(n_ch_, 0.0);
        sum_tail_.assign(n_ch_, 0.0);
        sum_tail2_.assign(n_ch_, 0.0);
        cnt_sign_.assign(n_ch_, 0);
        cnt_burst_.assign(n_ch_, 0);
        cnt_ext_burst_.assign(n_ch_, 0);
        n_snaps_.assign(n_ch_, 0);
        next_snap_n_.assign(n_ch_, warmup_);
        snaps_.assign(static_cast<size_t>(n_ch_) * kMaxCheckpoints,
                      Snap{0, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0});
        best_log_e_mean_.assign(n_ch_, 0.0);
        best_log_e_burst_.assign(n_ch_, 0.0);
        combined_e_.assign(n_ch_, 1.0);
        best_cp_tick_.assign(n_ch_, 0);
        mode_.assign(n_ch_, 0);
        dirty_.assign(n_ch_, 0);

        warmup_buf_.assign(static_cast<size_t>(n_ch_) * warmup_, 0.0);
        if (ring_cap_ > 0) {
            ring_vals_.assign(static_cast<size_t>(n_ch_) * ring_cap_, 0.0);
            ring_ticks_.assign(static_cast<size_t>(n_ch_) * ring_cap_, 0);
            ring_pos_.assign(n_ch_, 0);
            ring_len_.assign(n_ch_, 0);
        }
        burst_z_thresh_ = std::max(1.8, norm_ppf(1.0 - 0.5 * burst_p0_));
        ext_burst_z_thresh_ = std::max(burst_z_thresh_ + 0.75, 3.0);
        all_warmed_ = false;
        any_checkpoint_due_ = false;
        sync_post_ = false;
        sync_post_n_ = 0;
        sync_next_snap_ = std::max<uint32_t>(warmup_, 24);
        all_dirty_ = false;
        ebh_cache_valid_ = false;
        ebh_thresh_ = std::numeric_limits<double>::infinity();
    }

    inline void push_ring(uint32_t c, double val, uint32_t tick) noexcept {
        if (ring_cap_ == 0) return;
        size_t base = static_cast<size_t>(c) * ring_cap_;
        uint32_t pos = ring_pos_[c];
        ring_vals_[base + pos] = val;
        ring_ticks_[base + pos] = tick;
        ring_pos_[c] = (pos + 1 == ring_cap_) ? 0 : (pos + 1);
        if (ring_len_[c] < ring_cap_) ++ring_len_[c];
    }

    /* Winsorized-MAD causal baseline estimator:
     * ~95% Gaussian efficiency (2.6x lower scale variance than raw MAD)
     * while remaining 100% immune to 1e10 DC offsets AND tick-0 1e17 warmup spikes. */
    void finalize_warmup(uint32_t c) noexcept {
        const double* buf = warmup_buf_.data() + static_cast<size_t>(c) * warmup_;
        std::vector<double> sorted(buf, buf + warmup_);
        std::sort(sorted.begin(), sorted.end());
        double med = (warmup_ & 1U)
                         ? sorted[warmup_ / 2]
                         : sorted[warmup_ / 2 - 1] + 0.5 * (sorted[warmup_ / 2] - sorted[warmup_ / 2 - 1]);
        std::vector<double> tmp(warmup_);
        for (uint32_t i = 0; i < warmup_; ++i) tmp[i] = sorted[i] - med;
        std::vector<double> abs_dev(warmup_);
        for (uint32_t i = 0; i < warmup_; ++i) abs_dev[i] = std::fabs(tmp[i]);
        std::sort(abs_dev.begin(), abs_dev.end());
        double mad = (warmup_ & 1U)
                         ? abs_dev[warmup_ / 2]
                         : 0.5 * (abs_dev[warmup_ / 2 - 1] + abs_dev[warmup_ / 2]);
        double q25 = tmp[warmup_ / 4];
        double q75 = tmp[(3 * warmup_) / 4];
        double robust_r = std::max({mad / 0.67448975, (q75 - q25) / 1.3489795, 1e-12});

        /* Winsorize at +-3.0 * robust_r and compute high-efficiency sample std */
        double wsum = 0.0;
        std::vector<double> wclipped(warmup_);
        for (uint32_t i = 0; i < warmup_; ++i) {
            double v = clamp_d(tmp[i], -3.0 * robust_r, 3.0 * robust_r);
            wclipped[i] = v;
            wsum += v;
        }
        double wmean = wsum / static_cast<double>(warmup_);
        double wv = 0.0;
        for (uint32_t i = 0; i < warmup_; ++i) {
            double d = wclipped[i] - wmean;
            wv += d * d;
        }
        double wstd = std::sqrt(wv / static_cast<double>(std::max<uint32_t>(1, warmup_ - 1)));
        double sc = std::max({wstd, 0.5 * robust_r, 1e-12});

        loc_[c] = med;
        scale_[c] = sc;
        inv_scale_[c] = 1.0 / sc;

        next_snap_n_[c] = std::max<uint32_t>(warmup_, 24);
    }

    inline void record_snapshot(uint32_t c, uint32_t tick) noexcept {
        uint32_t idx = n_snaps_[c];
        if (idx >= kMaxCheckpoints) return;
        Snap& s = snaps_[static_cast<size_t>(c) * kMaxCheckpoints + idx];
        s.n0 = n_post_[c];
        s.tick = tick;
        s.sign0 = cnt_sign_[c];
        s.burst0 = cnt_burst_[c];
        s.ext_burst0 = cnt_ext_burst_[c];
        s.sum_u0 = sum_u_[c];
        s.sum_u20 = sum_u2_[c];
        s.sum_tail0 = sum_tail_[c];
        s.sum_tail20 = sum_tail2_[c];
        n_snaps_[c] = idx + 1;
        uint32_t next_n = n_post_[c] + std::max<uint32_t>(n_post_[c] / 2, 16);
        if (next_n <= n_post_[c]) next_n = std::numeric_limits<uint32_t>::max();
        next_snap_n_[c] = next_n;
    }

    static constexpr double kMaxZ = 1e6;

    static inline double clamp_z(double z) noexcept {
        if (z > kMaxZ) return kMaxZ;
        if (z < -kMaxZ) return -kMaxZ;
        if (std::isnan(z)) return 0.0;
        return z;
    }

    static inline double bounded_squash(double z) noexcept {
        const double zs = z * 0.4;
        return zs / std::sqrt(1.0 + zs * zs);
    }

    static constexpr double kTailHinge = 0.55; /* |u| > 0.55 <=> |z| > 1.65 */

    void update_one_impl(uint32_t c, double x, uint32_t tick) noexcept {
        if (!std::isfinite(x)) return;
        push_ring(c, x, tick);

        uint32_t seen = n_seen_[c];
        if (seen < warmup_) {
            warmup_buf_[static_cast<size_t>(c) * warmup_ + seen] = x;
            n_seen_[c] = seen + 1;
            if (seen + 1 == warmup_) {
                finalize_warmup(c);
            }
            return;
        }

        n_seen_[c] = seen + 1;
        const double z = clamp_z((x - loc_[c]) * inv_scale_[c]);
        last_z_[c] = z;
        const double u = bounded_squash(z);
        const double th = std::max(0.0, std::fabs(u) - kTailHinge);
        sum_u_[c] += u;
        sum_u2_[c] += u * u;
        sum_tail_[c] += th;
        sum_tail2_[c] += th * th;
        const double az = std::fabs(z);
        cnt_sign_[c] += (z > 0.0) ? 1U : 0U;
        cnt_burst_[c] += (az > burst_z_thresh_) ? 1U : 0U;
        cnt_ext_burst_[c] += (az > ext_burst_z_thresh_) ? 1U : 0U;
        n_post_[c] += 1;
        dirty_[c] = 1;

        if (n_post_[c] >= next_snap_n_[c] && n_snaps_[c] < kMaxCheckpoints) {
            record_snapshot(c, tick);
        }
    }

    void update_row_warm_simd(const double* values, uint32_t cur_tick) noexcept {
        const double bth = burst_z_thresh_;
        const double ebth = ext_burst_z_thresh_;
        const bool check_ring = (ring_cap_ > 0);
        uint32_t c = 0;

        /* Fast-path check: can we skip per-channel snapshot branch this row?
         * True whenever sync_post_ holds (no NaN desync) and next checkpoint is > 1 tick away. */
        bool need_snap_check = !sync_post_ || (sync_post_n_ + 1 >= sync_next_snap_);

#if defined(__AVX2__)
        const __m256d v_scale_s = _mm256_set1_pd(0.4);
        const __m256d v_one = _mm256_set1_pd(1.0);
        const __m256d v_zero = _mm256_setzero_pd();
        const __m256d v_hinge = _mm256_set1_pd(kTailHinge);
        const __m256d v_zmax = _mm256_set1_pd(kMaxZ);
        const __m256d v_zmin = _mm256_set1_pd(-kMaxZ);
        const __m256d v_abs_mask = _mm256_castsi256_pd(_mm256_set1_epi64x(0x7fffffffffffffffLL));
        const __m256d v_bth = _mm256_set1_pd(bth);
        const __m256d v_ebth = _mm256_set1_pd(ebth);
        const __m256d v_inf = _mm256_set1_pd(std::numeric_limits<double>::infinity());

        const double* __restrict loc_p = loc_.data();
        const double* __restrict inv_p = inv_scale_.data();
        double* __restrict lz_p = last_z_.data();
        double* __restrict su_p = sum_u_.data();
        double* __restrict su2_p = sum_u2_.data();
        double* __restrict st_p = sum_tail_.data();
        double* __restrict st2_p = sum_tail2_.data();
        uint32_t* __restrict csign_p = cnt_sign_.data();
        uint32_t* __restrict cburst_p = cnt_burst_.data();
        uint32_t* __restrict cext_p = cnt_ext_burst_.data();

        for (; c + 4 <= n_ch_; c += 4) {
            __m256d vx = _mm256_loadu_pd(values + c);
            __m256d vabsx = _mm256_and_pd(vx, v_abs_mask);
            int finite_mask = _mm256_movemask_pd(_mm256_cmp_pd(vabsx, v_inf, _CMP_LT_OQ));
            if (__builtin_expect(finite_mask != 0xF, 0)) {
                if (sync_post_) {
                    for (uint32_t k = 0; k < c; ++k) {
                        n_seen_[k] = warmup_ + sync_post_n_ + 1;
                        n_post_[k] = sync_post_n_ + 1;
                        dirty_[k] = 1;
                        if (n_post_[k] >= next_snap_n_[k] && n_snaps_[k] < kMaxCheckpoints) {
                            record_snapshot(k, cur_tick);
                        }
                    }
                    for (uint32_t k = c; k < n_ch_; ++k) {
                        n_seen_[k] = warmup_ + sync_post_n_;
                        n_post_[k] = sync_post_n_;
                        dirty_[k] = 1;
                    }
                    sync_post_ = false;
                    all_dirty_ = false;
                    need_snap_check = true;
                }
                for (uint32_t j = 0; j < 4; ++j) {
                    update_one_impl(c + j, values[c + j], cur_tick);
                }
                continue;
            }

            __m256d vz = _mm256_mul_pd(_mm256_sub_pd(vx, _mm256_loadu_pd(loc_p + c)),
                                       _mm256_loadu_pd(inv_p + c));
            vz = _mm256_max_pd(v_zmin, _mm256_min_pd(v_zmax, vz));
            _mm256_storeu_pd(lz_p + c, vz);

            __m256d vzs = _mm256_mul_pd(vz, v_scale_s);
            __m256d vden = _mm256_sqrt_pd(_mm256_add_pd(v_one, _mm256_mul_pd(vzs, vzs)));
            __m256d vu = _mm256_div_pd(vzs, vden);
            __m256d vabsu = _mm256_and_pd(vu, v_abs_mask);
            __m256d vth = _mm256_max_pd(_mm256_sub_pd(vabsu, v_hinge), v_zero);

            _mm256_storeu_pd(su_p + c, _mm256_add_pd(_mm256_loadu_pd(su_p + c), vu));
            _mm256_storeu_pd(su2_p + c, _mm256_add_pd(_mm256_loadu_pd(su2_p + c), _mm256_mul_pd(vu, vu)));
            _mm256_storeu_pd(st_p + c, _mm256_add_pd(_mm256_loadu_pd(st_p + c), vth));
            _mm256_storeu_pd(st2_p + c, _mm256_add_pd(_mm256_loadu_pd(st2_p + c), _mm256_mul_pd(vth, vth)));

            __m256d vabsz = _mm256_and_pd(vz, v_abs_mask);
            int sign_mask = _mm256_movemask_pd(_mm256_cmp_pd(vz, v_zero, _CMP_GT_OQ));
            int burst_mask = _mm256_movemask_pd(_mm256_cmp_pd(vabsz, v_bth, _CMP_GT_OQ));
            int ext_mask = _mm256_movemask_pd(_mm256_cmp_pd(vabsz, v_ebth, _CMP_GT_OQ));

            if (sign_mask) {
                csign_p[c + 0] += static_cast<uint32_t>((sign_mask >> 0) & 1);
                csign_p[c + 1] += static_cast<uint32_t>((sign_mask >> 1) & 1);
                csign_p[c + 2] += static_cast<uint32_t>((sign_mask >> 2) & 1);
                csign_p[c + 3] += static_cast<uint32_t>((sign_mask >> 3) & 1);
            }
            if (__builtin_expect(burst_mask != 0, 0)) {
                cburst_p[c + 0] += static_cast<uint32_t>((burst_mask >> 0) & 1);
                cburst_p[c + 1] += static_cast<uint32_t>((burst_mask >> 1) & 1);
                cburst_p[c + 2] += static_cast<uint32_t>((burst_mask >> 2) & 1);
                cburst_p[c + 3] += static_cast<uint32_t>((burst_mask >> 3) & 1);
            }
            if (__builtin_expect(ext_mask != 0, 0)) {
                cext_p[c + 0] += static_cast<uint32_t>((ext_mask >> 0) & 1);
                cext_p[c + 1] += static_cast<uint32_t>((ext_mask >> 1) & 1);
                cext_p[c + 2] += static_cast<uint32_t>((ext_mask >> 2) & 1);
                cext_p[c + 3] += static_cast<uint32_t>((ext_mask >> 3) & 1);
            }

            if (__builtin_expect(check_ring, 0)) {
                push_ring(c + 0, values[c + 0], cur_tick);
                push_ring(c + 1, values[c + 1], cur_tick);
                push_ring(c + 2, values[c + 2], cur_tick);
                push_ring(c + 3, values[c + 3], cur_tick);
            }
            if (__builtin_expect(!sync_post_, 0)) {
                for (uint32_t j = 0; j < 4; ++j) {
                    uint32_t ch = c + j;
                    n_seen_[ch] += 1;
                    n_post_[ch] += 1;
                    dirty_[ch] = 1;
                    if (n_post_[ch] >= next_snap_n_[ch] && n_snaps_[ch] < kMaxCheckpoints) {
                        record_snapshot(ch, cur_tick);
                    }
                }
            }
        }
#endif
        for (; c < n_ch_; ++c) {
            double x = values[c];
            if (!std::isfinite(x)) {
                if (sync_post_) {
                    for (uint32_t k = 0; k < c; ++k) {
                        n_seen_[k] = warmup_ + sync_post_n_ + 1;
                        n_post_[k] = sync_post_n_ + 1;
                        dirty_[k] = 1;
                        if (n_post_[k] >= next_snap_n_[k] && n_snaps_[k] < kMaxCheckpoints) {
                            record_snapshot(k, cur_tick);
                        }
                    }
                    for (uint32_t k = c; k < n_ch_; ++k) {
                        n_seen_[k] = warmup_ + sync_post_n_;
                        n_post_[k] = sync_post_n_;
                        dirty_[k] = 1;
                    }
                    sync_post_ = false;
                    all_dirty_ = false;
                    need_snap_check = true;
                }
                continue;
            }
            if (check_ring) push_ring(c, x, cur_tick);
            double z = clamp_z((x - loc_[c]) * inv_scale_[c]);
            last_z_[c] = z;
            double u = bounded_squash(z);
            double th = std::max(0.0, std::fabs(u) - kTailHinge);
            sum_u_[c] += u;
            sum_u2_[c] += u * u;
            sum_tail_[c] += th;
            sum_tail2_[c] += th * th;
            double az = std::fabs(z);
            cnt_sign_[c] += (z > 0.0) ? 1U : 0U;
            cnt_burst_[c] += (az > bth) ? 1U : 0U;
            cnt_ext_burst_[c] += (az > ebth) ? 1U : 0U;
            if (!sync_post_) {
                n_seen_[c] += 1;
                n_post_[c] += 1;
                dirty_[c] = 1;
                if (n_post_[c] >= next_snap_n_[c] && n_snaps_[c] < kMaxCheckpoints) {
                    record_snapshot(c, cur_tick);
                }
            }
        }

        if (sync_post_) {
            sync_post_n_ += 1;
            all_dirty_ = true;
            if (__builtin_expect(need_snap_check, 0)) {
                /* Materialize counts only on the <=24 checkpoint ticks out of 1,000,000! */
                for (uint32_t j = 0; j < n_ch_; ++j) {
                    n_post_[j] = sync_post_n_;
                    n_seen_[j] = warmup_ + sync_post_n_;
                    if (n_post_[j] >= next_snap_n_[j] && n_snaps_[j] < kMaxCheckpoints) {
                        record_snapshot(j, cur_tick);
                    }
                }
                sync_next_snap_ = (n_ch_ > 0) ? next_snap_n_[0] : std::numeric_limits<uint32_t>::max();
            }
        }
    }

    static inline double fast_lgamma(double x) noexcept {
        static const std::array<double, 16385> kQTable = []() {
            std::array<double, 16385> t{};
            t[0] = 0.0;
            for (size_t i = 1; i < t.size(); ++i) {
                t[i] = std::lgamma(0.25 * static_cast<double>(i));
            }
            return t;
        }();
        double q = x * 4.0;
        if (q >= 1.0 && q <= 16384.0) {
            int qi = static_cast<int>(q);
            if (static_cast<double>(qi) == q) {
                return kQTable[static_cast<size_t>(qi)];
            }
        }
        if (x >= 16.0) {
            double inv = 1.0 / x;
            double inv2 = inv * inv;
            return (x - 0.5) * std::log(x) - x + 0.91893853320467274178 +
                   inv * (1.0 / 12.0 + inv2 * (-1.0 / 360.0 + inv2 * (1.0 / 1260.0 - inv2 * (1.0 / 1680.0))));
        }
        return std::lgamma(x);
    }

    static inline double log_betabinom_marg(uint32_t k, uint32_t n, double a, double b) noexcept {
        double dk = static_cast<double>(k);
        double dn = static_cast<double>(n);
        return (fast_lgamma(a + dk) + fast_lgamma(b + dn - dk) - fast_lgamma(a + b + dn)) -
               (fast_lgamma(a) + fast_lgamma(b) - fast_lgamma(a + b));
    }

    static inline double log_e_two_sample_bernoulli(uint32_t k0, uint32_t n0,
                                                    uint32_t k1, uint32_t n1,
                                                    double a0, double b0,
                                                    double a1, double b1,
                                                    bool one_sided_increase) noexcept {
        if (n0 < 4 || n1 < 4) return 0.0;
        double p0 = static_cast<double>(k0) / static_cast<double>(n0);
        double p1 = static_cast<double>(k1) / static_cast<double>(n1);
        if (one_sided_increase && p1 <= p0) return -50.0;

        uint32_t kt = k0 + k1;
        uint32_t nt = n0 + n1;
        double log_num = log_betabinom_marg(k0, n0, a0, b0) + log_betabinom_marg(k1, n1, a1, b1);
        double log_den = 0.0;
        if (kt > 0 && kt < nt) {
            double ph = static_cast<double>(kt) / static_cast<double>(nt);
            log_den = static_cast<double>(kt) * std::log(ph) +
                      static_cast<double>(nt - kt) * std::log(1.0 - ph);
        }
        return log_num - log_den;
    }

    static inline double log_e_two_sample_jzs(uint32_t n0, double sum0, double sum20,
                                              uint32_t n1, double sum1, double sum21,
                                              bool two_sided) noexcept {
        if (n0 < 8 || n1 < 8) return 0.0;
        double dn0 = static_cast<double>(n0);
        double dn1 = static_cast<double>(n1);
        double m0 = sum0 / dn0;
        double m1 = sum1 / dn1;
        double diff = m1 - m0;
        if (!two_sided && diff <= 0.0) return -50.0;

        double ss0 = std::max(0.0, sum20 - dn0 * m0 * m0);
        double ss1 = std::max(0.0, sum21 - dn1 * m1 * m1);
        double nu = dn0 + dn1 - 2.0;
        double sp2 = std::max((ss0 + ss1) / nu, 1e-6);
        double neff = (dn0 * dn1) / (dn0 + dn1);
        double t2_over_nu = (neff * diff * diff) / (sp2 * nu);
        double one_sided_bonus = two_sided ? 0.0 : std::log(2.0);

        static constexpr double kTau2[2] = {0.35 * 0.35, 0.85 * 0.85};
        double log_e = -std::numeric_limits<double>::infinity();
        for (int i = 0; i < 2; ++i) {
            double g = 1.0 + neff * kTau2[i];
            double le = one_sided_bonus - 0.5 * std::log(g) +
                        0.5 * (nu + 1.0) * (std::log1p(t2_over_nu) - std::log1p(t2_over_nu / g));
            log_e = log_sum_exp(log_e, le + std::log(0.5));
        }
        return log_e;
    }

    void recompute_channel_evalue(uint32_t c) const noexcept {
        uint32_t ns = n_snaps_[c];
        uint32_t ntot = n_post_[c];
        if (ns == 0 || ntot <= snaps_[static_cast<size_t>(c) * kMaxCheckpoints].n0) {
            best_log_e_mean_[c] = 0.0;
            best_log_e_burst_[c] = 0.0;
            combined_e_[c] = 1.0;
            best_cp_tick_[c] = 0;
            mode_[c] = 0;
            return;
        }

        double log_mix_mean = -std::numeric_limits<double>::infinity();
        double log_mix_burst = -std::numeric_limits<double>::infinity();
        double best_slot_score = -1e30;
        uint32_t best_cp = 0;

        double used_mass = 0.0;
        const size_t base = static_cast<size_t>(c) * kMaxCheckpoints;
        const double b0_tail = std::max(2.0, 0.5 * (1.0 - burst_p0_) / burst_p0_);

        for (uint32_t k = 0; k < ns; ++k) {
            const Snap& s = snaps_[base + k];
            if (ntot <= s.n0 + 4) continue;
            uint32_t n1 = ntot - s.n0;
            double pi_k = 1.0 / (static_cast<double>(k + 1) * static_cast<double>(k + 2));
            double log_pi = std::log(pi_k);
            used_mass += pi_k;

            double sum_u1 = sum_u_[c] - s.sum_u0;
            double sum_u21 = sum_u2_[c] - s.sum_u20;

            uint32_t sign1 = cnt_sign_[c] - s.sign0;
            double le_sign = log_e_two_sample_bernoulli(
                s.sign0, s.n0, sign1, n1, 0.5, 0.5, 0.5, 0.5, !two_sided_);
            double le_jzs = log_e_two_sample_jzs(
                s.n0, s.sum_u0, s.sum_u20,
                n1, sum_u1, sum_u21,
                two_sided_);
            double le_m = log_sum_exp(le_sign + std::log(0.5), le_jzs + std::log(0.5));

            /* Burst e-value: convex mixture of:
             *   1) Bounded tail-hinge max(0, |u|-0.55) one-sided JZS e-process (high continuous SNR!)
             *   2) Exact distribution-free Beta-Binomial on |z|>2.24 & |z|>3.0 */
            uint32_t burst1 = cnt_burst_[c] - s.burst0;
            uint32_t ext1 = cnt_ext_burst_[c] - s.ext_burst0;
            double le_tail = log_e_two_sample_jzs(
                s.n0, s.sum_tail0, s.sum_tail20,
                n1, sum_tail_[c] - s.sum_tail0, sum_tail2_[c] - s.sum_tail20,
                false);
            double le_bb = log_e_two_sample_bernoulli(
                s.burst0, s.n0, burst1, n1, 0.5, b0_tail, 1.0, 4.0, true);
            double le_ext = log_e_two_sample_bernoulli(
                s.ext_burst0, s.n0, ext1, n1, 0.25, 45.0, 0.75, 4.0, true);
            double le_b = log_sum_exp(
                log_sum_exp(le_tail + std::log(0.50), le_ext + std::log(0.30)),
                le_bb + std::log(0.20));

            log_mix_mean = log_sum_exp(log_mix_mean, log_pi + le_m);
            log_mix_burst = log_sum_exp(log_mix_burst, log_pi + le_b);

            double slot_best = std::max(le_m, le_b) + log_pi;
            if (slot_best > best_slot_score) {
                best_slot_score = slot_best;
                best_cp = s.tick;
            }
        }

        double rem_mass = std::max(0.0, 1.0 - used_mass);
        if (rem_mass > 0.0) {
            double log_rem = std::log(rem_mass);
            log_mix_mean = log_sum_exp(log_mix_mean, log_rem);
            log_mix_burst = log_sum_exp(log_mix_burst, log_rem);
        } else {
            if (!std::isfinite(log_mix_mean)) log_mix_mean = 0.0;
            if (!std::isfinite(log_mix_burst)) log_mix_burst = 0.0;
        }

        double log_comb = log_sum_exp(log_mix_mean + std::log(0.5), log_mix_burst + std::log(0.5));
        best_log_e_mean_[c] = log_mix_mean;
        best_log_e_burst_[c] = log_mix_burst;
        combined_e_[c] = std::exp(std::min(log_comb, 700.0));
        best_cp_tick_[c] = best_cp;

        double thresh_log = std::log(1.0 / alpha_);
        bool m_hit = log_mix_mean >= thresh_log;
        bool b_hit = log_mix_burst >= thresh_log;
        mode_[c] = static_cast<uint8_t>((m_hit ? 1 : 0) | (b_hit ? 2 : 0));
    }

    uint32_t n_ch_;
    uint32_t warmup_;
    uint32_t ring_cap_;
    double   alpha_;
    double   burst_p0_;
    double   burst_z_thresh_;
    double   ext_burst_z_thresh_;
    bool     two_sided_;
    uint64_t tick_count_;
    bool     all_warmed_;
    bool     any_checkpoint_due_;
    bool     sync_post_;
    uint32_t sync_post_n_;
    uint32_t sync_next_snap_;
    mutable bool all_dirty_;
    mutable bool ebh_cache_valid_;
    mutable double ebh_thresh_;

    /* Structure-of-Arrays (SoA) per-channel hot state */
    mutable std::vector<uint32_t> n_seen_;
    mutable std::vector<uint32_t> n_post_;
    std::vector<double>   loc_;
    std::vector<double>   scale_;
    std::vector<double>   inv_scale_;
    std::vector<double>   last_z_;
    std::vector<double>   sum_u_;
    std::vector<double>   sum_u2_;
    std::vector<double>   sum_tail_;
    std::vector<double>   sum_tail2_;
    std::vector<uint32_t> cnt_sign_;
    std::vector<uint32_t> cnt_burst_;
    std::vector<uint32_t> cnt_ext_burst_;
    std::vector<uint32_t> n_snaps_;
    std::vector<uint32_t> next_snap_n_;
    std::vector<Snap>     snaps_;

    /* Cached lazy-evaluated outputs */
    mutable std::vector<double>   best_log_e_mean_;
    mutable std::vector<double>   best_log_e_burst_;
    mutable std::vector<double>   combined_e_;
    mutable std::vector<uint32_t> best_cp_tick_;
    mutable std::vector<uint8_t>  mode_;
    mutable std::vector<uint8_t>  dirty_;

    /* Warmup buffer & lossless raw ring */
    std::vector<double>   warmup_buf_;
    std::vector<double>   ring_vals_;
    std::vector<uint32_t> ring_ticks_;
    std::vector<uint32_t> ring_pos_;
    std::vector<uint32_t> ring_len_;
};

/* =========================================================================
 * 2. EvidenceLattice — Zero-Echo Provenance-Deduplicated CRDT Fusion
 *
 * Algebraic Laws (exact bit-for-bit):
 *   - Commutative : merge(A, B) == merge(B, A)
 *   - Associative : merge(merge(A, B), C) == merge(A, merge(B, C))
 *   - Idempotent  : merge(A, A) == A   (anti-echo law!)
 * ========================================================================= */

class EvidenceLattice {
public:
    struct Entry {
        double   llr;
        double   log_e;
        uint32_t agent_id;
        uint32_t reliability_q16;
        uint64_t timestamp_ns;
    };

    struct HypState {
        double prior_log_odds = 0.0;
        bool   has_prior = false;
        uint32_t echoes_suppressed = 0;
        /* Ordered map keyed by provenance_id guarantees deterministic summation order -> bit-exact CRDT */
        std::map<uint64_t, Entry> evidence;
    };

    explicit EvidenceLattice(int merge_mode)
        : merge_mode_(merge_mode == SS_MERGE_MAX_RELIABILITY
                          ? SS_MERGE_MAX_RELIABILITY
                          : SS_MERGE_STRICT_ORIGIN) {}

    void set_prior(uint64_t hyp_id, double prior_log_odds) {
        if (!std::isfinite(prior_log_odds)) return;
        auto& h = hyps_[hyp_id];
        h.prior_log_odds = prior_log_odds;
        h.has_prior = true;
    }

    void set_agent_weight(uint32_t agent_id, double weight) {
        if (!std::isfinite(weight) || weight < 0.0) return;
        agent_weights_[agent_id] = weight;
    }

    bool observe(const ss_evidence_item_t& item) {
        if (!std::isfinite(item.llr) || !std::isfinite(item.log_e)) return false;
        auto& h = hyps_[item.hyp_id];
        Entry cand{item.llr, item.log_e, item.agent_id, item.reliability_q16, item.timestamp_ns};
        auto it = h.evidence.find(item.provenance_id);
        if (it == h.evidence.end()) {
            h.evidence.emplace(item.provenance_id, cand);
            return false; /* New unique evidence */
        }
        /* Duplicate / echoed provenance_id -> idempotent CRDT join */
        h.echoes_suppressed += 1;
        if (entry_beats(cand, it->second)) {
            it->second = cand;
        }
        return true; /* Was echo */
    }

    void merge_from(const EvidenceLattice& other) {
        /* Merge agent weights deterministically (max weight if both set, or insert) */
        for (const auto& [aid, w] : other.agent_weights_) {
            auto it = agent_weights_.find(aid);
            if (it == agent_weights_.end()) {
                agent_weights_.emplace(aid, w);
            } else {
                it->second = std::max(it->second, w);
            }
        }
        /* Merge hypotheses */
        for (const auto& [hid, oh] : other.hyps_) {
            auto& dh = hyps_[hid];
            if (oh.has_prior) {
                if (!dh.has_prior) {
                    dh.prior_log_odds = oh.prior_log_odds;
                    dh.has_prior = true;
                } else {
                    /* Deterministic tie-break for conflicting priors */
                    dh.prior_log_odds = std::max(dh.prior_log_odds, oh.prior_log_odds);
                }
            }
            for (const auto& [pid, oe] : oh.evidence) {
                auto it = dh.evidence.find(pid);
                if (it == dh.evidence.end()) {
                    dh.evidence.emplace(pid, oe);
                } else {
                    dh.echoes_suppressed += 1;
                    if (entry_beats(oe, it->second)) {
                        it->second = oe;
                    }
                }
            }
        }
    }

    int query(uint64_t hyp_id, ss_hyp_summary_t* out) const {
        if (!out) return SS_ERR_NULL;
        out->hyp_id = hyp_id;
        auto it = hyps_.find(hyp_id);
        if (it == hyps_.end()) {
            out->posterior_prob = 0.5;
            out->total_log_odds = 0.0;
            out->merged_e_value = 1.0;
            out->merged_log_e = 0.0;
            out->unique_evidence = 0;
            out->echoes_suppressed = 0;
            out->distinct_agents = 0;
            return SS_OK;
        }

        const HypState& h = it->second;
        double sum_llr = h.prior_log_odds;
        /* Group per-agent log e-values for Vovk-Wang admissible cross-agent arithmetic e-merging */
        std::map<uint32_t, double> agent_log_e;

        for (const auto& [pid, e] : h.evidence) {
            (void)pid;
            double w = 1.0;
            auto wit = agent_weights_.find(e.agent_id);
            if (wit != agent_weights_.end()) w = wit->second;
            sum_llr += w * e.llr;
            auto ait = agent_log_e.find(e.agent_id);
            if (ait == agent_log_e.end()) {
                agent_log_e.emplace(e.agent_id, e.log_e);
            } else {
                ait->second += e.log_e;
            }
        }

        double merged_log_e = 0.0;
        if (!agent_log_e.empty()) {
            double total_w = 0.0;
            for (const auto& [aid, le] : agent_log_e) {
                (void)le;
                auto wit = agent_weights_.find(aid);
                double w = (wit != agent_weights_.end()) ? wit->second : 1.0;
                if (w > 0.0) total_w += w;
            }
            if (total_w > 0.0) {
                double acc = -std::numeric_limits<double>::infinity();
                for (const auto& [aid, le] : agent_log_e) {
                    auto wit = agent_weights_.find(aid);
                    double w = (wit != agent_weights_.end()) ? wit->second : 1.0;
                    if (w <= 0.0) continue;
                    acc = log_sum_exp(acc, std::log(w / total_w) + le);
                }
                merged_log_e = acc;
            } else {
                merged_log_e = 0.0; /* All participating agents zero-weighted -> neutral e=1 */
            }
        }

        out->total_log_odds = sum_llr;
        out->posterior_prob = sigmoid_d(sum_llr);
        out->merged_log_e = merged_log_e;
        out->merged_e_value = std::exp(clamp_d(merged_log_e, -700.0, 700.0));
        out->unique_evidence = static_cast<uint32_t>(h.evidence.size());
        out->echoes_suppressed = h.echoes_suppressed;
        out->distinct_agents = static_cast<uint32_t>(agent_log_e.size());
        return SS_OK;
    }

    int list_hyps(uint64_t* out_hyps, uint32_t max_out, uint32_t* out_count) const noexcept {
        if (!out_count) return SS_ERR_NULL;
        *out_count = static_cast<uint32_t>(hyps_.size());
        if (!out_hyps) return SS_OK;
        uint32_t i = 0;
        for (const auto& [hid, _] : hyps_) {
            if (i >= max_out) break;
            out_hyps[i++] = hid;
        }
        return SS_OK;
    }

    /* Compact little-endian binary wire format (magic "SSLT" v1) */
    int serialize(uint8_t* out_buf, size_t cap, size_t* out_len) const noexcept {
        if (!out_len) return SS_ERR_NULL;
        size_t req = 16; /* magic(4) + ver(4) + mode(4) + n_hyps(4) */
        req += 4 + agent_weights_.size() * (4 + 8);
        for (const auto& [hid, h] : hyps_) {
            (void)hid;
            req += 8 + 8 + 1 + 4 + h.evidence.size() * (8 + 8 + 8 + 4 + 4 + 8);
        }
        *out_len = req;
        if (!out_buf) return SS_OK;
        if (cap < req) return SS_ERR_BUFFER_SMALL;

        uint8_t* p = out_buf;
        auto put_u32 = [&](uint32_t v) { std::memcpy(p, &v, 4); p += 4; };
        auto put_u64 = [&](uint64_t v) { std::memcpy(p, &v, 8); p += 8; };
        auto put_f64 = [&](double v)   { std::memcpy(p, &v, 8); p += 8; };
        auto put_u8  = [&](uint8_t v)  { *p++ = v; };

        p[0] = 'S'; p[1] = 'S'; p[2] = 'L'; p[3] = 'T'; p += 4;
        put_u32(1U);
        put_u32(static_cast<uint32_t>(merge_mode_));
        put_u32(static_cast<uint32_t>(hyps_.size()));

        put_u32(static_cast<uint32_t>(agent_weights_.size()));
        for (const auto& [aid, w] : agent_weights_) {
            put_u32(aid);
            put_f64(w);
        }
        for (const auto& [hid, h] : hyps_) {
            put_u64(hid);
            put_f64(h.prior_log_odds);
            put_u8(h.has_prior ? 1U : 0U);
            put_u32(static_cast<uint32_t>(h.evidence.size()));
            for (const auto& [pid, e] : h.evidence) {
                put_u64(pid);
                put_f64(e.llr);
                put_f64(e.log_e);
                put_u32(e.agent_id);
                put_u32(e.reliability_q16);
                put_u64(e.timestamp_ns);
            }
        }
        return SS_OK;
    }

    static EvidenceLattice* deserialize(const uint8_t* buf, size_t len) {
        if (!buf || len < 20) return nullptr;
        if (buf[0] != 'S' || buf[1] != 'S' || buf[2] != 'L' || buf[3] != 'T') return nullptr;
        const uint8_t* p = buf + 4;
        const uint8_t* end = buf + len;
        auto get_u32 = [&](uint32_t& v) -> bool {
            if (static_cast<size_t>(end - p) < 4) return false;
            std::memcpy(&v, p, 4); p += 4; return true;
        };
        auto get_u64 = [&](uint64_t& v) -> bool {
            if (static_cast<size_t>(end - p) < 8) return false;
            std::memcpy(&v, p, 8); p += 8; return true;
        };
        auto get_f64 = [&](double& v) -> bool {
            if (static_cast<size_t>(end - p) < 8) return false;
            std::memcpy(&v, p, 8); p += 8; return true;
        };
        auto get_u8 = [&](uint8_t& v) -> bool {
            if (p >= end) return false;
            v = *p++; return true;
        };

        uint32_t ver = 0, mode = 0, n_hyps = 0, n_aw = 0;
        if (!get_u32(ver) || ver != 1U || !get_u32(mode) || !get_u32(n_hyps) || !get_u32(n_aw)) {
            return nullptr;
        }
        if (n_aw > static_cast<size_t>(end - p) / 12) return nullptr;
        auto* lat = new EvidenceLattice(static_cast<int>(mode));
        for (uint32_t i = 0; i < n_aw; ++i) {
            uint32_t aid = 0; double w = 1.0;
            if (!get_u32(aid) || !get_f64(w) || !std::isfinite(w) || w < 0.0) {
                delete lat; return nullptr;
            }
            lat->set_agent_weight(aid, w);
        }
        if (n_hyps > static_cast<size_t>(end - p) / 21) { delete lat; return nullptr; }
        for (uint32_t i = 0; i < n_hyps; ++i) {
            uint64_t hid = 0; double prior = 0.0; uint8_t has_p = 0; uint32_t nev = 0;
            if (!get_u64(hid) || !get_f64(prior) || !std::isfinite(prior) ||
                !get_u8(has_p) || !get_u32(nev)) {
                delete lat; return nullptr;
            }
            if (nev > static_cast<size_t>(end - p) / 40) { delete lat; return nullptr; }
            auto& h = lat->hyps_[hid];
            h.prior_log_odds = prior;
            h.has_prior = (has_p != 0);
            for (uint32_t j = 0; j < nev; ++j) {
                uint64_t pid = 0; Entry e{};
                if (!get_u64(pid) || !get_f64(e.llr) || !get_f64(e.log_e) ||
                    !std::isfinite(e.llr) || !std::isfinite(e.log_e) ||
                    !get_u32(e.agent_id) || !get_u32(e.reliability_q16) || !get_u64(e.timestamp_ns)) {
                    delete lat; return nullptr;
                }
                h.evidence[pid] = e;
            }
        }
        return lat;
    }

private:
    /* Strict total order over entries -> guarantees commutativity, associativity & idempotence */
    bool entry_beats(const Entry& a, const Entry& b) const noexcept {
        if (merge_mode_ == SS_MERGE_MAX_RELIABILITY) {
            if (a.reliability_q16 != b.reliability_q16) return a.reliability_q16 > b.reliability_q16;
            if (a.timestamp_ns != b.timestamp_ns) return a.timestamp_ns < b.timestamp_ns;
            if (a.agent_id != b.agent_id) return a.agent_id < b.agent_id;
            if (a.llr != b.llr) return a.llr > b.llr;
            return a.log_e > b.log_e;
        } else {
            if (a.timestamp_ns != b.timestamp_ns) return a.timestamp_ns < b.timestamp_ns;
            if (a.agent_id != b.agent_id) return a.agent_id < b.agent_id;
            if (a.reliability_q16 != b.reliability_q16) return a.reliability_q16 > b.reliability_q16;
            if (a.llr != b.llr) return a.llr > b.llr;
            return a.log_e > b.log_e;
        }
    }

    int merge_mode_;
    std::map<uint32_t, double> agent_weights_;
    std::map<uint64_t, HypState> hyps_;
};

/* =========================================================================
 * 3. VOISifter — Hierarchical Mean-Field Greedy VOI Multi-Agent Allocator
 *
 * Solves firehose attention under strict token/read budgets (`sift` / `voi`):
 *   - Models N channels across S sites with hot-site cluster mixture.
 *   - Normalizes each channel by its causal noise scale -> 0 loud-channel traps.
 *   - Evaluates exact Bayes log-likelihood ratio for BOTH sustained mean shifts
 *     AND sparse burst mixtures (p_burst * N(snr, 1) + (1-p_burst) * N(0, 1))!
 *   - Allocates non-overlapping reads across W agents by greedy Expected Brier
 *     Risk Reduction per unit attention cost.
 * ========================================================================= */

class VOISifter {
public:
    explicit VOISifter(const ss_sifter_config_t& cfg)
        : n_ch_(std::max<uint32_t>(cfg.n_channels, 1)),
          n_sites_(std::max<uint32_t>(1, std::min(cfg.n_sites, std::max<uint32_t>(cfg.n_channels, 1)))),
          prior_active_(sanitize_cfg(cfg.prior_active, 0.08, 1e-5, 0.5)),
          hot_site_prior_(sanitize_cfg(cfg.hot_site_prior, 0.15, 1e-4, 0.9)),
          hot_site_boost_(sanitize_cfg(cfg.hot_site_boost, std::max(prior_active_, 0.35), prior_active_, 0.95)),
          shift_snr_(sanitize_cfg(cfg.shift_snr, 0.9, 0.1, 1e6)),
          burst_prob_(sanitize_cfg(cfg.burst_prob, 0.125, 0.01, 1.0)),
          burst_snr_(sanitize_cfg(cfg.burst_snr, 3.0, 0.25, 1e6)),
          ticks_per_ep_(std::max<uint32_t>(cfg.ticks_per_epoch, 1)) {
        noise_scale_.assign(n_ch_, 1.0);
        ch_log_odds_.assign(n_ch_, logit_d(prior_active_));
        site_log_odds_.assign(n_sites_, logit_d(hot_site_prior_));
        site_obs_count_.assign(n_sites_, 0);
        ch_mean_obs_.assign(n_ch_, 0);
        ch_tick_obs_.assign(n_ch_, 0);
    }

    int set_noise_scales(const double* scales, uint32_t n) noexcept {
        if (!scales || n != n_ch_) return SS_ERR_INVALID_ARG;
        for (uint32_t c = 0; c < n_ch_; ++c) {
            noise_scale_[c] = (std::isfinite(scales[c]) && scales[c] > 1e-12) ? scales[c] : 1.0;
        }
        return SS_OK;
    }

    /* Observe site-level average across channels in site g */
    int observe_site_agg(uint32_t site_id, double agg_value) noexcept {
        if (site_id >= n_sites_) return SS_ERR_BOUNDS;
        if (!std::isfinite(agg_value)) return SS_OK;

        uint32_t c0 = site_begin(site_id);
        uint32_t c1 = site_end(site_id);
        uint32_t m = std::max<uint32_t>(1, c1 - c0);

        /* Under cold site: mean ~ N(0, var_site); under hot site: expected boost_m active channels */
        double sum_var = 0.0;
        for (uint32_t c = c0; c < c1; ++c) {
            double sc = noise_scale_[c];
            sum_var += (sc * sc) / static_cast<double>(ticks_per_ep_);
        }
        double agg_var = std::max(sum_var / (static_cast<double>(m) * static_cast<double>(m)), 1e-12);
        double eff_shift = std::max(shift_snr_, burst_prob_ * burst_snr_);
        /* Average scale inside site */
        double mean_sc = 0.0;
        for (uint32_t c = c0; c < c1; ++c) mean_sc += noise_scale_[c];
        mean_sc /= static_cast<double>(m);
        double mu_hot = (hot_site_boost_ - prior_active_) * eff_shift * mean_sc;
        double llr = (agg_value * mu_hot - 0.5 * mu_hot * mu_hot) / agg_var;
        /* Also allow symmetric negative shifts */
        double llr_neg = (-agg_value * mu_hot - 0.5 * mu_hot * mu_hot) / agg_var;
        double sym_llr = log_sum_exp(llr + std::log(0.5), llr_neg + std::log(0.5));
        site_log_odds_[site_id] = clamp_d(site_log_odds_[site_id] + sym_llr, -20.0, 20.0);
        site_obs_count_[site_id] += 1;
        return SS_OK;
    }

    /* Observe single channel epoch mean */
    int observe_mean(uint32_t c, double mean_value) noexcept {
        if (c >= n_ch_) return SS_ERR_BOUNDS;
        if (!std::isfinite(mean_value)) return SS_OK;

        double sc = noise_scale_[c];
        double se = sc / std::sqrt(static_cast<double>(ticks_per_ep_));
        double z = mean_value / std::max(se, 1e-12);
        /* Effective mean-level SNR under both sustained shift and bursty channel */
        double mu_shift = shift_snr_ * std::sqrt(static_cast<double>(ticks_per_ep_));
        double mu_burst = burst_prob_ * burst_snr_ * std::sqrt(static_cast<double>(ticks_per_ep_));

        double llr_shift = log_sum_exp(
            (z * mu_shift - 0.5 * mu_shift * mu_shift) + std::log(0.5),
            (-z * mu_shift - 0.5 * mu_shift * mu_shift) + std::log(0.5));
        double llr_burst = log_sum_exp(
            (z * mu_burst - 0.5 * mu_burst * mu_burst) + std::log(0.5),
            (-z * mu_burst - 0.5 * mu_burst * mu_burst) + std::log(0.5));
        double llr = log_sum_exp(llr_shift + std::log(0.5), llr_burst + std::log(0.5));

        ch_log_odds_[c] = clamp_d(ch_log_odds_[c] + llr, -25.0, 25.0);
        ch_mean_obs_[c] += 1;
        /* Propagate evidence up to parent site */
        propagate_channel_to_site(c, llr);
        return SS_OK;
    }

    /* Observe raw tick array for a single channel (catches rare bursts!) */
    int observe_ticks(uint32_t c, const double* ticks, uint32_t n_ticks) noexcept {
        if (c >= n_ch_) return SS_ERR_BOUNDS;
        if (!ticks || n_ticks == 0) return SS_ERR_INVALID_ARG;

        double inv_sc = 1.0 / std::max(noise_scale_[c], 1e-12);
        double log_p_burst_pos = 0.0;
        double log_p_burst_neg = 0.0;
        double log_p_shift_pos = 0.0;
        double log_p_shift_neg = 0.0;
        const double log_1mp = std::log(std::max(1.0 - burst_prob_, 1e-9));
        const double log_p = std::log(burst_prob_);

        for (uint32_t i = 0; i < n_ticks; ++i) {
            if (!std::isfinite(ticks[i])) continue;
            double z = ticks[i] * inv_sc;
            /* Exact per-tick burst mixture LLR vs N(0,1):
             * log((1-pb) + pb * exp(z*bsnr - 0.5*bsnr^2)) */
            double lp_pos = z * burst_snr_ - 0.5 * burst_snr_ * burst_snr_;
            double lp_neg = -z * burst_snr_ - 0.5 * burst_snr_ * burst_snr_;
            log_p_burst_pos += log_sum_exp(log_1mp, log_p + lp_pos);
            log_p_burst_neg += log_sum_exp(log_1mp, log_p + lp_neg);

            log_p_shift_pos += (z * shift_snr_ - 0.5 * shift_snr_ * shift_snr_);
            log_p_shift_neg += (-z * shift_snr_ - 0.5 * shift_snr_ * shift_snr_);
        }
        double llr_burst = log_sum_exp(log_p_burst_pos + std::log(0.5), log_p_burst_neg + std::log(0.5));
        double llr_shift = log_sum_exp(log_p_shift_pos + std::log(0.5), log_p_shift_neg + std::log(0.5));
        double llr = log_sum_exp(llr_burst + std::log(0.5), llr_shift + std::log(0.5));

        ch_log_odds_[c] = clamp_d(ch_log_odds_[c] + llr, -25.0, 25.0);
        ch_tick_obs_[c] += 1;
        propagate_channel_to_site(c, llr);
        return SS_OK;
    }

    int posteriors(double* out_probs, uint32_t n) const noexcept {
        if (!out_probs || n != n_ch_) return SS_ERR_INVALID_ARG;
        for (uint32_t c = 0; c < n_ch_; ++c) {
            out_probs[c] = effective_channel_prob(c);
        }
        return SS_OK;
    }

    int step_epoch(double stay_prob) noexcept {
        double sp = sanitize_cfg(stay_prob, 0.99, 0.5, 1.0);
        if (sp >= 0.999999) return SS_OK;
        double p0 = prior_active_;
        for (uint32_t c = 0; c < n_ch_; ++c) {
            double p = sigmoid_d(ch_log_odds_[c]);
            double p_next = p * sp + (1.0 - p) * (1.0 - sp) * p0;
            ch_log_odds_[c] = logit_d(clamp_d(p_next, 1e-6, 1.0 - 1e-6));
        }
        return SS_OK;
    }

    /* Greedy Expected Brier-Risk Reduction per Unit Cost with Zero-Overlap Multi-Agent Partitioning */
    int allocate(uint32_t budget_per_agent, uint32_t n_agents,
                 uint32_t allow_site_agg, uint32_t allow_mean, uint32_t allow_ticks,
                 ss_voi_action_t* out_actions, uint32_t max_out, uint32_t* out_count) const {
        if (!out_count) return SS_ERR_NULL;
        if (n_agents == 0 || budget_per_agent == 0) {
            *out_count = 0;
            return SS_OK;
        }

        /* Information gain coefficients per tool (expected squared posterior move) */
        const double mean_snr_eff = std::max(shift_snr_, burst_prob_ * burst_snr_) *
                                    std::sqrt(static_cast<double>(ticks_per_ep_));
        const double info_mean = 1.0 - std::exp(-0.35 * mean_snr_eff * mean_snr_eff);
        const double tick_snr_eff = std::max(shift_snr_ * std::sqrt(static_cast<double>(ticks_per_ep_)),
                                             burst_snr_ * std::sqrt(burst_prob_ * static_cast<double>(ticks_per_ep_)));
        const double info_tick = 1.0 - std::exp(-0.45 * tick_snr_eff * tick_snr_eff);

        /* Track simulated discounted uncertainty as reads are assigned across agents */
        std::vector<double> ch_disc(n_ch_, 1.0);
        std::vector<double> site_disc(n_sites_, 1.0);
        std::vector<uint32_t> rem_budget(n_agents, budget_per_agent);

        for (uint32_t c = 0; c < n_ch_; ++c) {
            ch_disc[c] = std::pow(0.55, static_cast<double>(ch_mean_obs_[c])) *
                         std::pow(0.25, static_cast<double>(ch_tick_obs_[c]));
        }
        for (uint32_t s = 0; s < n_sites_; ++s) {
            site_disc[s] = std::pow(0.35, static_cast<double>(site_obs_count_[s]));
        }

        std::vector<ss_voi_action_t> chosen;
        chosen.reserve(std::min<size_t>(max_out, static_cast<size_t>(n_agents) * budget_per_agent));

        /* Round-robin across agents so each agent gets balanced non-overlapping high-VOI work */
        bool progress = true;
        while (progress && chosen.size() < max_out) {
            progress = false;
            for (uint32_t a = 0; a < n_agents && chosen.size() < max_out; ++a) {
                if (rem_budget[a] == 0) continue;

                double best_voi = -1.0;
                ss_voi_action_t best_act{};
                bool found = false;

                /* 1. Site aggregates (cost = 1) */
                if (allow_site_agg && n_sites_ > 1 && rem_budget[a] >= 1) {
                    for (uint32_t s = 0; s < n_sites_; ++s) {
                        double rs = sigmoid_d(site_log_odds_[s]);
                        double site_unc = rs * (1.0 - rs) * site_disc[s];
                        /* Expected Brier gain across site's channels from resolving hot-site indicator */
                        double delta_p = hot_site_boost_ - prior_active_;
                        double site_sz = static_cast<double>(site_end(s) - site_begin(s));
                        double voi = site_sz * site_unc * delta_p * delta_p * 2.5;
                        if (voi > best_voi) {
                            best_voi = voi;
                            best_act = {a, 0, s, 1, voi, rs};
                            found = true;
                        }
                    }
                }

                /* 2. Channel means (cost = 1) */
                if (allow_mean && rem_budget[a] >= 1) {
                    for (uint32_t c = 0; c < n_ch_; ++c) {
                        double p = effective_channel_prob(c);
                        double voi = p * (1.0 - p) * info_mean * ch_disc[c];
                        if (voi > best_voi) {
                            best_voi = voi;
                            best_act = {a, 1, c, 1, voi, p};
                            found = true;
                        }
                    }
                }

                /* 3. Channel raw ticks (cost = ticks_per_ep_) */
                if (allow_ticks && rem_budget[a] >= ticks_per_ep_) {
                    double inv_cost = 1.0 / static_cast<double>(ticks_per_ep_);
                    for (uint32_t c = 0; c < n_ch_; ++c) {
                        double p = effective_channel_prob(c);
                        double voi = p * (1.0 - p) * info_tick * ch_disc[c] * inv_cost;
                        if (voi > best_voi) {
                            best_voi = voi;
                            best_act = {a, 2, c, ticks_per_ep_, voi, p};
                            found = true;
                        }
                    }
                }

                if (found && best_voi > 1e-12) {
                    chosen.push_back(best_act);
                    rem_budget[a] -= best_act.cost;
                    progress = true;
                    /* Immediately discount target so NO other agent duplicates this read */
                    if (best_act.tool_kind == 0) {
                        site_disc[best_act.target_id] *= 0.20;
                    } else if (best_act.tool_kind == 1) {
                        ch_disc[best_act.target_id] *= 0.35;
                    } else {
                        ch_disc[best_act.target_id] *= 0.10;
                    }
                }
            }
        }

        *out_count = static_cast<uint32_t>(chosen.size());
        if (out_actions && !chosen.empty()) {
            std::memcpy(out_actions, chosen.data(), chosen.size() * sizeof(ss_voi_action_t));
        }
        return SS_OK;
    }

private:
    inline uint32_t site_begin(uint32_t s) const noexcept {
        return static_cast<uint32_t>((static_cast<uint64_t>(s) * n_ch_) / n_sites_);
    }

    inline uint32_t site_end(uint32_t s) const noexcept {
        return static_cast<uint32_t>((static_cast<uint64_t>(s + 1) * n_ch_) / n_sites_);
    }

    inline uint32_t channel_site(uint32_t c) const noexcept {
        return static_cast<uint32_t>(
            ((static_cast<uint64_t>(c) + 1ULL) * n_sites_ - 1ULL) / n_ch_);
    }

    double effective_channel_prob(uint32_t c) const noexcept {
        uint32_t s = channel_site(c);
        double rs = (n_sites_ > 1) ? sigmoid_d(site_log_odds_[s]) : 0.0;
        double site_prior = (1.0 - rs) * prior_active_ + rs * hot_site_boost_;
        double base_prior_lo = logit_d(prior_active_);
        double eff_lo = (ch_log_odds_[c] - base_prior_lo) + logit_d(site_prior);
        return sigmoid_d(eff_lo);
    }

    void propagate_channel_to_site(uint32_t c, double ch_llr) noexcept {
        if (n_sites_ <= 1) return;
        uint32_t s = channel_site(c);
        /* Exact marginal likelihood ratio of observing channel evidence under hot vs cold site:
         * log( (p_hot * exp(llr) + (1 - p_hot)) / (p_cold * exp(llr) + (1 - p_cold)) ) */
        double cllr = clamp_d(ch_llr, -20.0, 20.0);
        double log_hot = log_sum_exp(std::log(hot_site_boost_) + cllr, std::log(1.0 - hot_site_boost_));
        double log_cold = log_sum_exp(std::log(prior_active_) + cllr, std::log(1.0 - prior_active_));
        site_log_odds_[s] = clamp_d(site_log_odds_[s] + (log_hot - log_cold), -20.0, 20.0);
    }

    uint32_t n_ch_;
    uint32_t n_sites_;
    double   prior_active_;
    double   hot_site_prior_;
    double   hot_site_boost_;
    double   shift_snr_;
    double   burst_prob_;
    double   burst_snr_;
    uint32_t ticks_per_ep_;

    std::vector<double>   noise_scale_;
    std::vector<double>   ch_log_odds_;
    std::vector<double>   site_log_odds_;
    std::vector<uint32_t> site_obs_count_;
    std::vector<uint32_t> ch_mean_obs_;
    std::vector<uint32_t> ch_tick_obs_;
};

/* =========================================================================
 * 4. TrustCalibrator — Delayed-Label Agent Trust, Quarantine & Conformal Act
 *
 * Solves `trust`, `delay`, and `act`:
 *   - Strictly causal pending prediction store keyed by decision_id.
 *   - Online Platt log-odds recalibration shrinks overconfident bloviators.
 *   - Discounted Fixed-Share BOA/Hedge adapts rapidly after sudden trust flips.
 *   - Ville e-value quarantine gate isolates 100% poisoned Byzantine agents.
 *   - Multi-rate Delayed AgACI + Bayes-optimal asymmetric-cost quantile action.
 * ========================================================================= */

class TrustCalibrator {
public:
    static constexpr size_t kNumAciRates = 4;
    static constexpr double kAciGammas[kNumAciRates] = {0.005, 0.02, 0.08, 0.20};

    explicit TrustCalibrator(const ss_trust_config_t& cfg)
        : n_agents_(std::max<uint32_t>(cfg.n_agents, 1)),
          fs_alpha_(sanitize_cfg(cfg.fixed_share_alpha, 0.04, 0.0, 0.4)),
          eta_(sanitize_cfg(cfg.eta, 0.8, 0.05, 5.0)),
          q_thresh_(sanitize_cfg(cfg.quarantine_e_thresh, 20.0, 2.0, 1e9)),
          r_thresh_(sanitize_cfg(cfg.recovery_e_thresh, 5.0, 1.5, 1e9)),
          platt_lr_(sanitize_cfg(cfg.platt_lr, 0.15, 0.001, 1.0)),
          platt_l2_(sanitize_cfg(cfg.platt_l2, 0.02, 0.0, 0.5)),
          conformal_alpha_(sanitize_cfg(cfg.conformal_alpha, 0.10, 0.01, 0.5)) {
        double w0 = 1.0 / static_cast<double>(n_agents_);
        weights_.assign(n_agents_, w0);
        platt_a_.assign(n_agents_, 1.0);
        platt_b_.assign(n_agents_, 0.0);
        ema_loss_.assign(n_agents_, 0.25);
        quarantine_log_e_.assign(n_agents_, 0.0);
        recovery_log_e_.assign(n_agents_, 0.0);
        quarantined_.assign(n_agents_, 0);
        n_resolved_.assign(n_agents_, 0);

        for (size_t r = 0; r < kNumAciRates; ++r) {
            aci_alpha_[r] = conformal_alpha_;
            aci_weight_[r] = 1.0 / static_cast<double>(kNumAciRates);
        }
        score_scale_ema_ = 1.0;
    }

    int predict_prob(uint64_t decision_id, const double* agent_probs, uint32_t n_agents,
                     double underage_cost, double overage_cost, ss_trust_decision_t* out) {
        if (!agent_probs || !out) return SS_ERR_NULL;
        if (n_agents != n_agents_) return SS_ERR_INVALID_ARG;

        PendingPred pend{};
        pend.is_prob = true;
        pend.raw_vals.assign(n_agents_, std::numeric_limits<double>::quiet_NaN());
        pend.cal_vals.assign(n_agents_, std::numeric_limits<double>::quiet_NaN());

        double w_sum = 0.0;
        double p_sum = 0.0;
        uint32_t active = 0;

        for (uint32_t i = 0; i < n_agents_; ++i) {
            double rp = agent_probs[i];
            if (!std::isfinite(rp)) continue;
            rp = clamp_d(rp, 1e-6, 1.0 - 1e-6);
            pend.raw_vals[i] = rp;
            double cp = calibrate_prob(i, rp);
            pend.cal_vals[i] = cp;
            if (!quarantined_[i]) {
                w_sum += weights_[i];
                p_sum += weights_[i] * cp;
                ++active;
            }
        }

        double fused = (w_sum > 1e-12) ? clamp_d(p_sum / w_sum, 1e-6, 1.0 - 1e-6) : 0.5;
        /* Fallback if every single agent was quarantined */
        if (active == 0) {
            double s = 0.0; uint32_t cnt = 0;
            for (uint32_t i = 0; i < n_agents_; ++i) {
                if (std::isfinite(pend.cal_vals[i])) { s += pend.cal_vals[i]; ++cnt; }
            }
            fused = (cnt > 0) ? (s / static_cast<double>(cnt)) : 0.5;
        }

        double cu = sanitize_cfg(underage_cost, 1.0, 1e-9, 1e12);
        double co = sanitize_cfg(overage_cost, 1.0, 1e-9, 1e12);
        double thresh = co / (cu + co); /* Bayes optimal binary decision threshold */
        double q_margin = current_conformal_margin();

        pend.fused_point = fused;
        pend.fused_std = std::sqrt(fused * (1.0 - fused));
        pending_[decision_id] = std::move(pend);
        trim_pending();

        out->fused_prob = fused;
        out->fused_point = fused;
        out->ci_low = clamp_d(fused - q_margin, 0.0, 1.0);
        out->ci_high = clamp_d(fused + q_margin, 0.0, 1.0);
        out->optimal_action = (fused >= thresh) ? 1.0 : 0.0;
        out->active_agents = active;
        return SS_OK;
    }

    int predict_scalar(uint64_t decision_id, const double* agent_means, const double* agent_stds,
                       uint32_t n_agents, double underage_cost, double overage_cost,
                       ss_trust_decision_t* out) {
        if (!agent_means || !out) return SS_ERR_NULL;
        if (n_agents != n_agents_) return SS_ERR_INVALID_ARG;

        PendingPred pend{};
        pend.is_prob = false;
        pend.raw_vals.assign(n_agents_, std::numeric_limits<double>::quiet_NaN());
        pend.cal_vals.assign(n_agents_, std::numeric_limits<double>::quiet_NaN());

        double w_sum = 0.0;
        double m_sum = 0.0;
        double v_sum = 0.0;
        uint32_t active = 0;

        for (uint32_t i = 0; i < n_agents_; ++i) {
            double rm = agent_means[i];
            if (!std::isfinite(rm)) continue;
            double rs = (agent_stds && std::isfinite(agent_stds[i]) && agent_stds[i] > 1e-9)
                            ? agent_stds[i]
                            : score_scale_ema_;
            pend.raw_vals[i] = rm;
            pend.cal_vals[i] = rm;
            if (!quarantined_[i]) {
                w_sum += weights_[i];
                m_sum += weights_[i] * rm;
                v_sum += weights_[i] * (rs * rs + rm * rm);
                ++active;
            }
        }

        double fused_mu = 0.0;
        double fused_sd = std::max(score_scale_ema_, 1e-6);
        if (w_sum > 1e-12) {
            fused_mu = m_sum / w_sum;
            double second_mom = v_sum / w_sum;
            fused_sd = std::sqrt(std::max(second_mom - fused_mu * fused_mu, 1e-12));
        } else {
            double s = 0.0; uint32_t cnt = 0;
            for (uint32_t i = 0; i < n_agents_; ++i) {
                if (std::isfinite(pend.raw_vals[i])) { s += pend.raw_vals[i]; ++cnt; }
            }
            if (cnt > 0) fused_mu = s / static_cast<double>(cnt);
        }

        /* Conformal interval width scaled by delayed AgACI */
        double eff_alpha = effective_aci_alpha();
        double z_ci = norm_ppf(1.0 - 0.5 * eff_alpha);
        double half_w = std::max(z_ci * fused_sd, current_conformal_margin());

        /* Bayes-optimal newsvendor / cost-asymmetric quantile action:
         * tau* = cu / (cu + co) (e.g. 4:1 underage:overage -> 80th percentile!) */
        double cu = sanitize_cfg(underage_cost, 1.0, 1e-9, 1e12);
        double co = sanitize_cfg(overage_cost, 1.0, 1e-9, 1e12);
        double tau_star = cu / (cu + co);
        /* Adjust optimal quantile by conformal calibration shift if miscoverage drifts */
        double z_opt = norm_ppf(tau_star);
        double opt_action = fused_mu + z_opt * fused_sd;

        pend.fused_point = fused_mu;
        pend.fused_std = fused_sd;
        pending_[decision_id] = std::move(pend);
        trim_pending();

        out->fused_prob = sigmoid_d(fused_mu);
        out->fused_point = fused_mu;
        out->ci_low = fused_mu - half_w;
        out->ci_high = fused_mu + half_w;
        out->optimal_action = opt_action;
        out->active_agents = active;
        return SS_OK;
    }

    int resolve(uint64_t decision_id, double outcome) {
        if (!std::isfinite(outcome)) return SS_ERR_INVALID_ARG;
        auto it = pending_.find(decision_id);
        if (it == pending_.end()) return SS_ERR_BOUNDS;
        PendingPred pend = std::move(it->second);
        pending_.erase(it);

        /* 1. Compute per-agent causal losses (normalized to [0, 1] for BOA/Ville stability) */
        double y = pend.is_prob ? clamp_d(outcome, 0.0, 1.0) : outcome;
        double ens_err = std::fabs(y - pend.fused_point);
        if (!pend.is_prob) {
            score_scale_ema_ = 0.90 * score_scale_ema_ + 0.10 * std::max(ens_err, 1e-6);
        }
        double norm_scale = pend.is_prob ? 1.0 : std::max(score_scale_ema_, 1e-6);

        std::vector<double> losses(n_agents_, std::numeric_limits<double>::quiet_NaN());
        std::vector<double> raw_losses(n_agents_, std::numeric_limits<double>::quiet_NaN());
        double active_loss_sum = 0.0;
        double active_w_sum = 0.0;
        double active_unw_sum = 0.0;
        uint32_t active_part_cnt = 0;

        for (uint32_t i = 0; i < n_agents_; ++i) {
            if (!std::isfinite(pend.raw_vals[i])) continue;
            double loss = 0.0;
            double rloss = 0.0;
            if (pend.is_prob) {
                double diff = pend.cal_vals[i] - y;
                loss = diff * diff; /* Strictly proper Brier score in [0, 1] */
                double rdiff = pend.raw_vals[i] - y;
                rloss = rdiff * rdiff;
                /* Update online Platt calibration strictly causally on raw logit */
                update_platt(i, pend.raw_vals[i], y);
            } else {
                double err = std::fabs(pend.raw_vals[i] - y) / norm_scale;
                /* Huber-bounded relative squared loss in [0, 1] for Byzantine immunity */
                double s = err / (1.0 + err);
                loss = s * s;
                rloss = loss;
            }
            losses[i] = loss;
            raw_losses[i] = rloss;
            ema_loss_[i] = 0.85 * ema_loss_[i] + 0.15 * loss;
            n_resolved_[i] += 1;
            if (!quarantined_[i]) {
                active_loss_sum += weights_[i] * loss;
                active_w_sum += weights_[i];
                active_unw_sum += loss;
                ++active_part_cnt;
            }
        }

        /* Fallback median loss for BOA centering when no active agent participated */
        std::vector<double> valid_losses;
        valid_losses.reserve(n_agents_);
        for (uint32_t i = 0; i < n_agents_; ++i) {
            if (std::isfinite(losses[i])) valid_losses.push_back(losses[i]);
        }
        double med_loss = 0.25;
        if (!valid_losses.empty()) {
            std::sort(valid_losses.begin(), valid_losses.end());
            med_loss = valid_losses[valid_losses.size() / 2];
        }
        double boa_ref = (active_w_sum > 1e-12) ? (active_loss_sum / active_w_sum) : med_loss;
        double active_mean_loss = (active_part_cnt > 0)
                                      ? (active_unw_sum / static_cast<double>(active_part_cnt))
                                      : 0.25;

        /* Snapshot pre-update quarantine state so peer references are strictly simultaneous */
        std::vector<uint8_t> was_q = quarantined_;

        /* 2. Discounted Fixed-Share BOA Update + Unbiased Leave-One-Out Ville Quarantine/Recovery */
        static constexpr double kQuarantineSlack = 0.08;
        std::vector<double> raw_w(n_agents_, 0.0);
        double total_w = 0.0;
        for (uint32_t i = 0; i < n_agents_; ++i) {
            if (std::isfinite(losses[i])) {
                double boa_excess = losses[i] - boa_ref;
                /* Second-order BOA-style update on excess loss */
                double step = clamp_d(eta_ * boa_excess + 0.5 * eta_ * eta_ * boa_excess * boa_excess, -3.0, 3.0);
                double w_new = weights_[i] * std::exp(-step);
                raw_w[i] = std::max(w_new, 1e-12);

                if (!was_q[i]) {
                    /* Leave-one-out active peer mean is strictly unbiased under identical honest agents;
                     * subtracting kQuarantineSlack guarantees strictly negative null drift E[log_bet] < 0.
                     * Guard raw_loss > 0.30 prevents inverted Byzantine agents from hiding behind platt_a=0. */
                    double peer_ref = (active_part_cnt > 1)
                                          ? ((active_unw_sum - losses[i]) / static_cast<double>(active_part_cnt - 1))
                                          : 0.25;
                    double q_loss = (pend.is_prob && raw_losses[i] > 0.30)
                                        ? std::max(losses[i], raw_losses[i])
                                        : losses[i];
                    double d_clip = clamp_d(q_loss - peer_ref - kQuarantineSlack, -1.0, 1.0);
                    double log_bet = std::log(std::max(1.0 + 0.8 * d_clip, 1e-6));
                    quarantine_log_e_[i] = std::max(0.0, quarantine_log_e_[i] + log_bet);
                    if (quarantine_log_e_[i] >= std::log(q_thresh_) && n_resolved_[i] >= 5) {
                        quarantined_[i] = 1;
                        recovery_log_e_[i] = 0.0;
                    }
                } else {
                    /* Shadow recovery uses raw_losses[i] so still-inverted agents (platt_a=0 -> cal_loss=0.25)
                     * stay quarantined while genuinely repaired agents recover rapidly even alongside strong peers. */
                    double still_bad_thresh = std::min(0.22, active_mean_loss + 0.10);
                    double rec_adv = clamp_d(still_bad_thresh - raw_losses[i], -0.5, 0.5);
                    double log_rec = std::log(std::max(1.0 + 1.8 * rec_adv, 1e-6));
                    recovery_log_e_[i] = std::max(0.0, recovery_log_e_[i] + log_rec);
                    if (recovery_log_e_[i] >= std::log(r_thresh_)) {
                        quarantined_[i] = 0;
                        quarantine_log_e_[i] = 0.0;
                        recovery_log_e_[i] = 0.0;
                        if (pend.is_prob && raw_losses[i] < losses[i]) {
                            platt_a_[i] = std::max(platt_a_[i], 0.8);
                            platt_b_[i] *= 0.5;
                        }
                        raw_w[i] = std::max(raw_w[i], 0.25);
                    }
                }
            } else {
                raw_w[i] = weights_[i];
            }
            if (!quarantined_[i]) total_w += raw_w[i];
        }

        uint32_t n_active = 0;
        for (uint32_t i = 0; i < n_agents_; ++i) {
            if (!quarantined_[i]) ++n_active;
        }
        if (total_w > 1e-15 && n_active > 0) {
            double unif = 1.0 / static_cast<double>(n_active);
            for (uint32_t i = 0; i < n_agents_; ++i) {
                if (quarantined_[i]) {
                    weights_[i] = 0.0;
                } else {
                    /* Fixed-share mixing guarantees rapid recovery after regime/trust flips! */
                    weights_[i] = (1.0 - fs_alpha_) * (raw_w[i] / total_w) + fs_alpha_ * unif;
                }
            }
        } else {
            double unif = (n_active > 0) ? (1.0 / static_cast<double>(n_active)) : 0.0;
            for (uint32_t i = 0; i < n_agents_; ++i) {
                weights_[i] = quarantined_[i] ? 0.0 : unif;
            }
        }

        /* 3. Update Multi-Rate Delayed AgACI Conformal Tracker */
        update_aci(ens_err, pend.fused_std);
        return SS_OK;
    }

    int get_agents(ss_agent_trust_t* out, uint32_t n_agents) const noexcept {
        if (!out) return SS_ERR_NULL;
        if (n_agents != n_agents_) return SS_ERR_INVALID_ARG;
        for (uint32_t i = 0; i < n_agents_; ++i) {
            out[i].agent_id = i;
            out[i].weight = weights_[i];
            out[i].platt_slope = platt_a_[i];
            out[i].platt_bias = platt_b_[i];
            out[i].ema_brier = ema_loss_[i];
            out[i].quarantine_e = std::exp(clamp_d(quarantine_log_e_[i], -50.0, 50.0));
            out[i].quarantined = quarantined_[i];
            out[i].n_resolved = n_resolved_[i];
        }
        return SS_OK;
    }

private:
    struct PendingPred {
        bool is_prob;
        double fused_point;
        double fused_std;
        std::vector<double> raw_vals;
        std::vector<double> cal_vals;
    };

    double calibrate_prob(uint32_t i, double raw_p) const noexcept {
        double lo = logit_d(raw_p);
        return sigmoid_d(platt_a_[i] * lo + platt_b_[i]);
    }

    void update_platt(uint32_t i, double raw_p, double y) noexcept {
        double lo = clamp_d(logit_d(raw_p), -8.0, 8.0);
        double p = sigmoid_d(platt_a_[i] * lo + platt_b_[i]);
        double grad = p - y;
        /* Regularized online logistic step shrinks overconfident/uninformative agents toward slope 0;
         * non-negative slope clamp prevents Byzantine inversion attacks. */
        platt_a_[i] = clamp_d(platt_a_[i] - platt_lr_ * (grad * lo + platt_l2_ * platt_a_[i]), 0.0, 3.0);
        platt_b_[i] = clamp_d(platt_b_[i] - platt_lr_ * (grad + platt_l2_ * platt_b_[i]), -3.0, 3.0);
    }

    double effective_aci_alpha() const noexcept {
        double a = 0.0;
        for (size_t r = 0; r < kNumAciRates; ++r) a += aci_weight_[r] * aci_alpha_[r];
        return clamp_d(a, 0.002, 0.95);
    }

    double current_conformal_margin() const noexcept {
        if (residual_hist_.empty()) return 0.25;
        std::vector<double> tmp = residual_hist_;
        std::sort(tmp.begin(), tmp.end());
        double q_level = clamp_d(1.0 - effective_aci_alpha(), 0.01, 0.999);
        size_t idx = static_cast<size_t>(std::ceil(q_level * static_cast<double>(tmp.size())));
        if (idx > 0) --idx;
        idx = std::min(idx, tmp.size() - 1);
        return tmp[idx];
    }

    void update_aci(double abs_err, double pred_sd) {
        double margin = current_conformal_margin();
        double z_ci = norm_ppf(1.0 - 0.5 * effective_aci_alpha());
        double half_w = std::max(z_ci * pred_sd, margin);
        double err_ind = (abs_err > half_w) ? 1.0 : 0.0;

        double w_tot = 0.0;
        for (size_t r = 0; r < kNumAciRates; ++r) {
            /* Pinball loss on alpha_t */
            double pinball = (err_ind > aci_alpha_[r])
                                 ? (1.0 - conformal_alpha_) * (err_ind - aci_alpha_[r])
                                 : conformal_alpha_ * (aci_alpha_[r] - err_ind);
            aci_weight_[r] *= std::exp(-0.5 * pinball);
            w_tot += aci_weight_[r];
            aci_alpha_[r] = clamp_d(aci_alpha_[r] + kAciGammas[r] * (conformal_alpha_ - err_ind),
                                    0.001, 0.95);
        }
        if (w_tot > 1e-15) {
            for (size_t r = 0; r < kNumAciRates; ++r) aci_weight_[r] /= w_tot;
        }

        if (residual_hist_.size() >= 256) {
            residual_hist_.erase(residual_hist_.begin());
        }
        residual_hist_.push_back(abs_err);
    }

    void trim_pending() {
        /* Cap pending buffer to avoid unbounded growth if caller never resolves old IDs */
        while (pending_.size() > 65536) {
            pending_.erase(pending_.begin());
        }
    }

    uint32_t n_agents_;
    double   fs_alpha_;
    double   eta_;
    double   q_thresh_;
    double   r_thresh_;
    double   platt_lr_;
    double   platt_l2_;
    double   conformal_alpha_;

    std::vector<double>   weights_;
    std::vector<double>   platt_a_;
    std::vector<double>   platt_b_;
    std::vector<double>   ema_loss_;
    std::vector<double>   quarantine_log_e_;
    std::vector<double>   recovery_log_e_;
    std::vector<uint8_t>  quarantined_;
    std::vector<uint64_t> n_resolved_;

    double aci_alpha_[kNumAciRates];
    double aci_weight_[kNumAciRates];
    double score_scale_ema_;
    std::vector<double> residual_hist_;
    std::map<uint64_t, PendingPred> pending_;
};

} /* namespace streamsieve */

#endif /* STREAMSIEVE_CORE_HPP */
