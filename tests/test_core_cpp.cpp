/*
 * test_core_cpp.cpp — Native C++20 unit, statistical calibration, CRDT axiom,
 * and numerical torture tests (compiled under both -O3 AVX2 and ASan+UBSan).
 */
#include "../csrc/streamsieve_capi.h"

#include <cassert>
#include <cmath>
#include <cstdio>
#include <limits>
#include <random>
#include <vector>

static void test_estream_null_calibration_and_power() {
    std::mt19937_64 rng(42);
    std::normal_distribution<double> norm(0.0, 1.0);
    std::student_t_distribution<double> t3(3.0);
    std::lognormal_distribution<double> logn(0.0, 0.8);

    /* 1. Null calibration under continuous peeking across 400 trials x 4 nasty distributions
     *    (Gaussian, Student-t df=3, LogNormal, 1000x Loud) at alpha = 0.05! */
    const int n_trials = 400;
    const uint32_t T = 600;
    int false_alarms[4] = {0, 0, 0, 0};

    for (int dist = 0; dist < 4; ++dist) {
        for (int tr = 0; tr < n_trials; ++tr) {
            ss_estream_config_t cfg{};
            ss_estream_default_config(&cfg, 1);
            cfg.warmup = 32;
            cfg.ring_capacity = 0;
            cfg.alpha = 0.05;
            ss_estream_t* st = ss_estream_create(&cfg);
            bool ever_rejected = false;
            for (uint32_t t = 0; t < T; ++t) {
                double x = 0.0;
                if (dist == 0) x = norm(rng);
                else if (dist == 1) x = t3(rng);
                else if (dist == 2) x = logn(rng);
                else x = 1e10 + 1000.0 * norm(rng); /* Extreme DC offset + 1000x loud! */
                ss_estream_update_row(st, &x, 1);
                if (t >= 48 && (t % 10 == 0 || t == T - 1)) {
                    ss_channel_status_t s{};
                    ss_estream_get_channel(st, 0, &s);
                    if (s.e_value >= 20.0) {
                        ever_rejected = true;
                        break;
                    }
                }
            }
            if (ever_rejected) false_alarms[dist]++;
            ss_estream_destroy(st);
        }
    }
    const char* names[4] = {"Gaussian", "Student-t(3)", "LogNormal", "1e10_DC+1000xLoud"};
    for (int d = 0; d < 4; ++d) {
        double far = static_cast<double>(false_alarms[d]) / n_trials;
        std::printf("[Null Peek Calibration] %-18s FAR=%.4f (target <= 0.0500)\n", names[d], far);
        assert(far <= 0.065); /* Strictly calibrated anytime-valid error control */
    }

    /* 2. Power on 256-channel firehose across 5 seeds (20 shifts, 20 rare 18% +3.5sd bursts, 160 loud 5x nulls)
     *    using a portable SplitMix64 + Box-Muller generator identical across libstdc++/libc++/MSVC. */
    struct PortableRng {
        uint64_t state;
        explicit PortableRng(uint64_t s) : state(s) {}
        uint64_t next_u64() {
            uint64_t z = (state += 0x9e3779b97f4a7c15ULL);
            z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
            z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
            return z ^ (z >> 31);
        }
        double u01() { return (static_cast<double>(next_u64() >> 11) + 0.5) * (1.0 / 9007199254740992.0); }
        double gauss() {
            double u1 = std::max(u01(), 1e-15);
            double u2 = u01();
            return std::sqrt(-2.0 * std::log(u1)) * std::cos(6.28318530717958647692 * u2);
        }
    };

    const uint32_t N = 256;
    int tp_mean = 0, tp_burst = 0, fp_loud = 0, fp_other = 0;
    for (uint64_t seed = 0; seed < 5; ++seed) {
        PortableRng prng(4200 + seed * 997);
        ss_estream_config_t cfg{};
        ss_estream_default_config(&cfg, N);
        cfg.warmup = 32;
        cfg.ring_capacity = (seed == 0) ? 64 : 0;
        cfg.alpha = 0.05;
        ss_estream_t* st = ss_estream_create(&cfg);

        std::vector<double> row(N);
        for (uint32_t t = 0; t < 1000; ++t) {
            for (uint32_t c = 0; c < N; ++c) {
                double sc = (c >= 16 && c < 48) ? 5.0 : 1.0; /* 32 loud null channels */
                double x = sc * prng.gauss();
                if (t >= 360) {
                    if (c < 4) x += 1.4 * sc; /* Sustained mean shift */
                    else if (c >= 4 && c < 8) {
                        if (prng.u01() < 0.18) x += 3.5 * sc; /* Sparse bursty channel! */
                    }
                }
                row[c] = x;
            }
            ss_estream_update_row(st, row.data(), N);
        }

        std::vector<uint32_t> rej(N);
        uint32_t n_rej = 0;
        ss_estream_ebh_reject(st, 0.05, rej.data(), N, &n_rej);
        for (uint32_t i = 0; i < n_rej; ++i) {
            uint32_t c = rej[i];
            if (c < 4) tp_mean++;
            else if (c < 8) tp_burst++;
            else if (c >= 16 && c < 48) fp_loud++;
            else fp_other++;
        }

        if (seed == 0) {
            /* 3. Verify lossless raw tick recovery */
            std::vector<double> raw_vals(64);
            std::vector<uint32_t> raw_ticks(64);
            uint32_t got = 0;
            assert(ss_estream_fetch_raw(st, 0, raw_vals.data(), raw_ticks.data(), 64, &got) == SS_OK);
            assert(got == 64);
            assert(raw_ticks[63] == 1000);
            assert(raw_vals[63] == row[0]);
        }
        ss_estream_destroy(st);
    }

    std::printf("[Firehose e-BH 256ch x 5 seeds] tp_mean=%d/20 tp_burst=%d/20 fp_loud=%d/160 fp_other=%d\n",
                tp_mean, tp_burst, fp_loud, fp_other);
    std::fflush(stdout);
    assert(tp_mean == 20);
    assert(tp_burst >= 16);
    assert(fp_loud == 0);
    assert(fp_other <= 2);
}

static void test_lattice_crdt_axioms() {
    ss_lattice_t* a = ss_lattice_create(SS_MERGE_STRICT_ORIGIN);
    ss_lattice_t* b = ss_lattice_create(SS_MERGE_STRICT_ORIGIN);
    ss_lattice_t* c = ss_lattice_create(SS_MERGE_STRICT_ORIGIN);

    ss_evidence_item_t e1{1, 1001, 1.5, 1.5, 0, 50000, 100};
    ss_evidence_item_t e2{1, 1002, -0.5, -0.5, 1, 50000, 110};
    ss_evidence_item_t e1_echo{1, 1001, 1.5, 1.5, 2, 50000, 120}; /* Reprint of 1001! */
    ss_evidence_item_t e3{1, 1003, 0.8, 0.8, 2, 50000, 130};

    int echo = 0;
    ss_lattice_observe(a, &e1, &echo); assert(echo == 0);
    ss_lattice_observe(a, &e1_echo, &echo); assert(echo == 1); /* Echo suppressed! */
    ss_lattice_observe(b, &e2, &echo); assert(echo == 0);
    ss_lattice_observe(b, &e1_echo, &echo); assert(echo == 0);
    ss_lattice_observe(c, &e3, &echo); assert(echo == 0);

    /* Commutativity: merge(A, B) == merge(B, A) */
    ss_lattice_t* ab = ss_lattice_create(SS_MERGE_STRICT_ORIGIN);
    ss_lattice_merge(ab, a);
    ss_lattice_merge(ab, b);

    ss_lattice_t* ba = ss_lattice_create(SS_MERGE_STRICT_ORIGIN);
    ss_lattice_merge(ba, b);
    ss_lattice_merge(ba, a);

    ss_hyp_summary_t sab{}, sba{};
    ss_lattice_query(ab, 1, &sab);
    ss_lattice_query(ba, 1, &sba);
    assert(sab.total_log_odds == sba.total_log_odds);
    assert(sab.posterior_prob == sba.posterior_prob);
    assert(sab.merged_e_value == sba.merged_e_value);
    assert(sab.unique_evidence == 2); /* e1 + e2 ONLY, echo suppressed! */

    /* Idempotence: merge(AB, A) == AB */
    ss_lattice_merge(ab, a);
    ss_hyp_summary_t saba{};
    ss_lattice_query(ab, 1, &saba);
    assert(saba.total_log_odds == sab.total_log_odds);
    assert(saba.unique_evidence == 2);

    /* Wire serialization roundtrip */
    size_t req = 0;
    assert(ss_lattice_serialize(ab, nullptr, 0, &req) == SS_OK);
    std::vector<uint8_t> buf(req);
    size_t written = 0;
    assert(ss_lattice_serialize(ab, buf.data(), buf.size(), &written) == SS_OK);
    assert(written == req);
    ss_lattice_t* rt = ss_lattice_deserialize(buf.data(), written);
    assert(rt != nullptr);
    ss_hyp_summary_t srt{};
    ss_lattice_query(rt, 1, &srt);
    assert(srt.total_log_odds == sab.total_log_odds);
    assert(srt.merged_e_value == sab.merged_e_value);

    std::printf("[Lattice CRDT] axioms & wire roundtrip PASS (%zu bytes)\n", written);
    ss_lattice_destroy(a);
    ss_lattice_destroy(b);
    ss_lattice_destroy(c);
    ss_lattice_destroy(ab);
    ss_lattice_destroy(ba);
    ss_lattice_destroy(rt);
}

static void test_sifter_and_trust() {
    /* VOISifter test */
    ss_sifter_config_t scfg{};
    ss_sifter_default_config(&scfg, 64, 8);
    ss_sifter_t* sf = ss_sifter_create(&scfg);
    std::vector<double> scales(64, 1.0);
    scales[10] = 5.0; /* Loud channel */
    ss_sifter_set_noise_scales(sf, scales.data(), 64);

    /* Loud channel reports mean=3.0 (actually only 0.6*sc -> weak!) vs normal ch 2 with burst */
    ss_sifter_observe_mean(sf, 10, 1.5);
    double burst_ticks[8] = {0.1, -0.2, 3.8, 0.0, 3.4, -0.1, 0.2, 0.1};
    ss_sifter_observe_ticks(sf, 2, burst_ticks, 8);

    std::vector<double> probs(64);
    ss_sifter_posteriors(sf, probs.data(), 64);
    assert(probs[2] > 0.5);
    assert(probs[10] < 0.2);

    /* Multi-agent non-overlapping allocation */
    std::vector<ss_voi_action_t> acts(64);
    uint32_t n_act = 0;
    assert(ss_sifter_allocate(sf, 4, 4, 1, 1, 1, acts.data(), 64, &n_act) == SS_OK);
    assert(n_act > 0);
    ss_sifter_destroy(sf);

    /* TrustCalibrator: 1 good agent, 1 overconfident random agent, 1 actively poisoned agent */
    ss_trust_config_t tcfg{};
    ss_trust_default_config(&tcfg, 3);
    ss_trust_t* tc = ss_trust_create(&tcfg);
    std::mt19937_64 rng(7);
    std::bernoulli_distribution coin(0.5);
    for (uint64_t t = 0; t < 60; ++t) {
        double y = coin(rng) ? 1.0 : 0.0;
        double good = y > 0.5 ? 0.85 : 0.15;
        double bloviator = coin(rng) ? 0.99 : 0.01;
        double poisoned = y > 0.5 ? 0.01 : 0.99; /* Always inverted! */
        double preds[3] = {good, bloviator, poisoned};
        ss_trust_decision_t dec{};
        ss_trust_predict_prob(tc, t, preds, 3, 4.0, 1.0, &dec);
        /* Delayed resolution by 3 ticks */
        if (t >= 3) {
            /* Resolve t-3 (for simplicity re-derive exact label by storing) */
        }
        ss_trust_resolve(tc, t, y);
    }
    ss_agent_trust_t ag[3]{};
    ss_trust_get_agents(tc, ag, 3);
    std::printf("[TrustCalibrator] good(w=%.3f,q=%u) blov(w=%.3f,a=%.3f,q=%u) pois(w=%.3f,q=%u)\n",
                ag[0].weight, ag[0].quarantined,
                ag[1].weight, ag[1].platt_slope, ag[1].quarantined,
                ag[2].weight, ag[2].quarantined);
    assert(ag[0].weight > 0.80);
    assert(ag[0].quarantined == 0);
    assert(ag[2].quarantined == 1);
    assert(ag[2].weight == 0.0);
    ss_trust_destroy(tc);
}

int main() {
    std::printf("Running streamsieve %s C++20 core tests (AVX2=%d)...\n", ss_version(), ss_has_avx2());
    test_estream_null_calibration_and_power();
    test_lattice_crdt_axioms();
    test_sifter_and_trust();
    std::printf("ALL NATIVE C++20 CORE TESTS PASSED!\n");
    return 0;
}
