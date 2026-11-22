/*
 * streamsieve_capi.cpp — Exception-free extern "C" ABI for streamsieve.
 *
 * Copyright (c) 2026 StreamSieve Contributors
 * Licensed under the Apache License, Version 2.0.
 */
#include "streamsieve_capi.h"
#include "streamsieve_core.hpp"

#include <new>

struct ss_estream_t { streamsieve::EStreamEngine impl; };
struct ss_lattice_t { streamsieve::EvidenceLattice impl; };
struct ss_sifter_t  { streamsieve::VOISifter impl; };
struct ss_trust_t   { streamsieve::TrustCalibrator impl; };

extern "C" {

const char* ss_version(void) { return streamsieve::kVersion; }
int ss_has_avx2(void) { return streamsieve::has_avx2() ? 1 : 0; }

/* =========================================================================
 * 1. EStreamEngine C API
 * ========================================================================= */

void ss_estream_default_config(ss_estream_config_t* cfg, uint32_t n_channels) {
    if (!cfg) return;
    cfg->n_channels = n_channels;
    cfg->warmup = 32;
    cfg->ring_capacity = 256;
    cfg->alpha = 0.05;
    cfg->burst_tail_p0 = 0.025;
    cfg->ewma_reset_decay = 0.0;
    cfg->two_sided = 1;
    cfg->season_period = 0;
    cfg->detrend_alpha = 0.0;
}

ss_estream_t* ss_estream_create(const ss_estream_config_t* cfg) {
    if (!cfg || cfg->n_channels == 0) return nullptr;
    try {
        return new ss_estream_t{streamsieve::EStreamEngine(*cfg)};
    } catch (...) {
        return nullptr;
    }
}

void ss_estream_destroy(ss_estream_t* st) {
    delete st;
}

int ss_estream_reset(ss_estream_t* st) {
    if (!st) return SS_ERR_NULL;
    try {
        st->impl.reset();
        return SS_OK;
    } catch (...) {
        return SS_ERR_OOM;
    }
}

int ss_estream_update_row(ss_estream_t* st, const double* values, uint32_t n_values) {
    if (!st) return SS_ERR_NULL;
    return st->impl.update_row(values, n_values);
}

int ss_estream_update_batch(ss_estream_t* st, const double* matrix, uint32_t n_ticks, uint32_t n_channels) {
    if (!st) return SS_ERR_NULL;
    return st->impl.update_batch(matrix, n_ticks, n_channels);
}

int ss_estream_update_one(ss_estream_t* st, uint32_t channel, double value) {
    if (!st) return SS_ERR_NULL;
    return st->impl.update_one(channel, value);
}

int ss_estream_ebh_reject(const ss_estream_t* st, double alpha,
                          uint32_t* out_indices, uint32_t max_out, uint32_t* out_count) {
    if (!st) return SS_ERR_NULL;
    try {
        return st->impl.ebh_reject(alpha, out_indices, max_out, out_count);
    } catch (...) {
        return SS_ERR_OOM;
    }
}

int ss_estream_topk(const ss_estream_t* st, uint32_t k,
                    ss_channel_status_t* out_status, uint32_t* out_count) {
    if (!st) return SS_ERR_NULL;
    try {
        return st->impl.topk(k, out_status, out_count);
    } catch (...) {
        return SS_ERR_OOM;
    }
}

int ss_estream_get_channel(const ss_estream_t* st, uint32_t channel, ss_channel_status_t* out_status) {
    if (!st) return SS_ERR_NULL;
    try {
        return st->impl.get_channel(channel, out_status);
    } catch (...) {
        return SS_ERR_OOM;
    }
}

int ss_estream_fetch_raw(const ss_estream_t* st, uint32_t channel,
                         double* out_values, uint32_t* out_ticks,
                         uint32_t max_out, uint32_t* out_count) {
    if (!st) return SS_ERR_NULL;
    return st->impl.fetch_raw(channel, out_values, out_ticks, max_out, out_count);
}

uint32_t ss_estream_n_channels(const ss_estream_t* st) {
    return st ? st->impl.n_channels() : 0;
}

uint64_t ss_estream_tick_count(const ss_estream_t* st) {
    return st ? st->impl.tick_count() : 0;
}

/* =========================================================================
 * 2. EvidenceLattice C API
 * ========================================================================= */

ss_lattice_t* ss_lattice_create(int merge_mode) {
    try {
        return new ss_lattice_t{streamsieve::EvidenceLattice(merge_mode)};
    } catch (...) {
        return nullptr;
    }
}

void ss_lattice_destroy(ss_lattice_t* lat) {
    delete lat;
}

int ss_lattice_set_prior(ss_lattice_t* lat, uint64_t hyp_id, double prior_log_odds) {
    if (!lat) return SS_ERR_NULL;
    try {
        lat->impl.set_prior(hyp_id, prior_log_odds);
        return SS_OK;
    } catch (...) {
        return SS_ERR_OOM;
    }
}

int ss_lattice_set_agent_weight(ss_lattice_t* lat, uint32_t agent_id, double weight) {
    if (!lat) return SS_ERR_NULL;
    try {
        lat->impl.set_agent_weight(agent_id, weight);
        return SS_OK;
    } catch (...) {
        return SS_ERR_OOM;
    }
}

int ss_lattice_observe(ss_lattice_t* lat, const ss_evidence_item_t* item, int* was_echo) {
    if (!lat || !item) return SS_ERR_NULL;
    try {
        bool echo = lat->impl.observe(*item);
        if (was_echo) *was_echo = echo ? 1 : 0;
        return SS_OK;
    } catch (...) {
        return SS_ERR_OOM;
    }
}

int ss_lattice_merge(ss_lattice_t* dst, const ss_lattice_t* src) {
    if (!dst || !src) return SS_ERR_NULL;
    try {
        if (dst == src) return SS_OK; /* Idempotent self-merge */
        dst->impl.merge_from(src->impl);
        return SS_OK;
    } catch (...) {
        return SS_ERR_OOM;
    }
}

int ss_lattice_query(const ss_lattice_t* lat, uint64_t hyp_id, ss_hyp_summary_t* out_summary) {
    if (!lat) return SS_ERR_NULL;
    try {
        return lat->impl.query(hyp_id, out_summary);
    } catch (...) {
        return SS_ERR_OOM;
    }
}

int ss_lattice_list_hyps(const ss_lattice_t* lat, uint64_t* out_hyps, uint32_t max_out, uint32_t* out_count) {
    if (!lat) return SS_ERR_NULL;
    return lat->impl.list_hyps(out_hyps, max_out, out_count);
}

int ss_lattice_serialize(const ss_lattice_t* lat, uint8_t* out_buf, size_t cap, size_t* out_len) {
    if (!lat) return SS_ERR_NULL;
    return lat->impl.serialize(out_buf, cap, out_len);
}

ss_lattice_t* ss_lattice_deserialize(const uint8_t* buf, size_t len) {
    try {
        auto* impl = streamsieve::EvidenceLattice::deserialize(buf, len);
        if (!impl) return nullptr;
        auto* wrap = new ss_lattice_t{std::move(*impl)};
        delete impl;
        return wrap;
    } catch (...) {
        return nullptr;
    }
}

uint64_t ss_hash_provenance(const uint8_t* data, size_t len) {
    if (!data && len > 0) return 0;
    return streamsieve::hash_provenance(data, len);
}

/* =========================================================================
 * 3. VOISifter C API
 * ========================================================================= */

void ss_sifter_default_config(ss_sifter_config_t* cfg, uint32_t n_channels, uint32_t n_sites) {
    if (!cfg) return;
    cfg->n_channels = n_channels;
    cfg->n_sites = n_sites > 0 ? n_sites : 1;
    cfg->prior_active = 0.02;
    cfg->hot_site_prior = 0.10;
    cfg->hot_site_boost = 0.25;
    cfg->shift_snr = 1.5;
    cfg->burst_prob = 0.15;
    cfg->burst_snr = 2.5;
    cfg->ticks_per_epoch = 8;
}

ss_sifter_t* ss_sifter_create(const ss_sifter_config_t* cfg) {
    if (!cfg || cfg->n_channels == 0) return nullptr;
    try {
        return new ss_sifter_t{streamsieve::VOISifter(*cfg)};
    } catch (...) {
        return nullptr;
    }
}

void ss_sifter_destroy(ss_sifter_t* sf) {
    delete sf;
}

int ss_sifter_set_noise_scales(ss_sifter_t* sf, const double* scales, uint32_t n_channels) {
    if (!sf) return SS_ERR_NULL;
    return sf->impl.set_noise_scales(scales, n_channels);
}

int ss_sifter_observe_site_agg(ss_sifter_t* sf, uint32_t site_id, double agg_value) {
    if (!sf) return SS_ERR_NULL;
    return sf->impl.observe_site_agg(site_id, agg_value);
}

int ss_sifter_observe_mean(ss_sifter_t* sf, uint32_t channel, double mean_value) {
    if (!sf) return SS_ERR_NULL;
    return sf->impl.observe_mean(channel, mean_value);
}

int ss_sifter_observe_ticks(ss_sifter_t* sf, uint32_t channel, const double* ticks, uint32_t n_ticks) {
    if (!sf) return SS_ERR_NULL;
    return sf->impl.observe_ticks(channel, ticks, n_ticks);
}

int ss_sifter_allocate(const ss_sifter_t* sf, uint32_t budget_per_agent, uint32_t n_agents,
                       uint32_t allow_site_agg, uint32_t allow_mean, uint32_t allow_ticks,
                       ss_voi_action_t* out_actions, uint32_t max_out, uint32_t* out_count) {
    if (!sf) return SS_ERR_NULL;
    try {
        return sf->impl.allocate(budget_per_agent, n_agents, allow_site_agg, allow_mean,
                                 allow_ticks, out_actions, max_out, out_count);
    } catch (...) {
        return SS_ERR_OOM;
    }
}

int ss_sifter_posteriors(const ss_sifter_t* sf, double* out_probs, uint32_t n_channels) {
    if (!sf) return SS_ERR_NULL;
    return sf->impl.posteriors(out_probs, n_channels);
}

int ss_sifter_step_epoch(ss_sifter_t* sf, double stay_prob) {
    if (!sf) return SS_ERR_NULL;
    return sf->impl.step_epoch(stay_prob);
}

/* =========================================================================
 * 4. TrustCalibrator C API
 * ========================================================================= */

void ss_trust_default_config(ss_trust_config_t* cfg, uint32_t n_agents) {
    if (!cfg) return;
    cfg->n_agents = n_agents;
    cfg->fixed_share_alpha = 0.04;
    cfg->eta = 0.8;
    cfg->quarantine_e_thresh = 20.0;
    cfg->recovery_e_thresh = 5.0;
    cfg->platt_lr = 0.15;
    cfg->platt_l2 = 0.05;
    cfg->conformal_alpha = 0.10;
}

ss_trust_t* ss_trust_create(const ss_trust_config_t* cfg) {
    if (!cfg || cfg->n_agents == 0) return nullptr;
    try {
        return new ss_trust_t{streamsieve::TrustCalibrator(*cfg)};
    } catch (...) {
        return nullptr;
    }
}

void ss_trust_destroy(ss_trust_t* tc) {
    delete tc;
}

int ss_trust_predict_prob(ss_trust_t* tc, uint64_t decision_id,
                          const double* agent_probs, uint32_t n_agents,
                          double underage_cost, double overage_cost,
                          ss_trust_decision_t* out_dec) {
    if (!tc) return SS_ERR_NULL;
    try {
        return tc->impl.predict_prob(decision_id, agent_probs, n_agents,
                                     underage_cost, overage_cost, out_dec);
    } catch (...) {
        return SS_ERR_OOM;
    }
}

int ss_trust_predict_scalar(ss_trust_t* tc, uint64_t decision_id,
                            const double* agent_means, const double* agent_stds, uint32_t n_agents,
                            double underage_cost, double overage_cost,
                            ss_trust_decision_t* out_dec) {
    if (!tc) return SS_ERR_NULL;
    try {
        return tc->impl.predict_scalar(decision_id, agent_means, agent_stds, n_agents,
                                       underage_cost, overage_cost, out_dec);
    } catch (...) {
        return SS_ERR_OOM;
    }
}

int ss_trust_resolve(ss_trust_t* tc, uint64_t decision_id, double outcome) {
    if (!tc) return SS_ERR_NULL;
    try {
        return tc->impl.resolve(decision_id, outcome);
    } catch (...) {
        return SS_ERR_OOM;
    }
}

int ss_trust_get_agents(const ss_trust_t* tc, ss_agent_trust_t* out_agents, uint32_t n_agents) {
    if (!tc) return SS_ERR_NULL;
    return tc->impl.get_agents(out_agents, n_agents);
}

} /* extern "C" */
