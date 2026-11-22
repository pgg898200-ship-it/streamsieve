/*
 * streamsieve_cli.cpp — Standalone <3 ms cold-start native CLI for streamsieve.
 *
 * Subcommands:
 *   streamsieve sift [--alpha 0.05] [--top 10] [--warmup 32] [--json] [FILE|-]
 *   streamsieve bench [--channels 1024] [--ticks 2000]
 *   streamsieve version
 *
 * Zero runtime dependencies: parses CSV / TSV / whitespace numeric streams directly
 * from stdin or file and emits compact agent-ready briefs with lossless drill-down.
 *
 * Copyright (c) 2026 StreamSieve Contributors
 * Licensed under the Apache License, Version 2.0.
 */
#include "streamsieve_capi.h"

#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

namespace {

void print_help() {
    std::cout
        << "streamsieve " << ss_version() << " (" << (ss_has_avx2() ? "AVX2+FMA" : "scalar") << ")\n"
        << "SIMD C++20 multi-agent streaming inference & anytime-valid firehose sifter.\n\n"
        << "Usage:\n"
        << "  streamsieve sift [OPTIONS] [FILE|-]   Sift CSV/TSV stream into compact e-BH brief\n"
        << "  streamsieve bench [OPTIONS]           Run native AVX2 throughput self-benchmark\n"
        << "  streamsieve version                   Print version & SIMD capability\n\n"
        << "Options for sift:\n"
        << "  --alpha FLOAT    Anytime-valid e-BH FDR level (default: 0.05)\n"
        << "  --top INT        Max channels in compact brief (default: 10)\n"
        << "  --warmup INT     Causal warmup ticks (default: 32)\n"
        << "  --json           Emit machine-readable JSON brief\n";
}

bool parse_numeric_row(const std::string& line, std::vector<double>& out) {
    out.clear();
    const char* p = line.c_str();
    while (*p) {
        while (*p == ' ' || *p == '\t' || *p == ',' || *p == ';' || *p == '\r' || *p == '\n') ++p;
        if (!*p) break;
        char* end = nullptr;
        double val = std::strtod(p, &end);
        if (end == p) {
            return false; /* Header or non-numeric line */
        }
        out.push_back(val);
        p = end;
    }
    return !out.empty();
}

std::vector<std::string> split_header(const std::string& line) {
    std::vector<std::string> cols;
    std::string cur;
    for (char ch : line) {
        if (ch == ',' || ch == '\t' || ch == ';') {
            cols.push_back(cur);
            cur.clear();
        } else if (ch != '\r' && ch != '\n') {
            cur.push_back(ch);
        }
    }
    if (!cur.empty()) cols.push_back(cur);
    return cols;
}

int cmd_sift(int argc, char** argv) {
    double alpha = 0.05;
    uint32_t top_k = 10;
    uint32_t warmup = 32;
    bool as_json = false;
    std::string file_path = "-";

    for (int i = 2; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--alpha" && i + 1 < argc) {
            alpha = std::atof(argv[++i]);
        } else if (arg == "--top" && i + 1 < argc) {
            top_k = static_cast<uint32_t>(std::atoi(argv[++i]));
        } else if (arg == "--warmup" && i + 1 < argc) {
            warmup = static_cast<uint32_t>(std::atoi(argv[++i]));
        } else if (arg == "--json") {
            as_json = true;
        } else if (arg == "-h" || arg == "--help") {
            print_help();
            return 0;
        } else {
            file_path = arg;
        }
    }

    std::ifstream fin;
    std::istream* in = &std::cin;
    if (file_path != "-") {
        fin.open(file_path);
        if (!fin) {
            std::cerr << "streamsieve: cannot open " << file_path << "\n";
            return 1;
        }
        in = &fin;
    }

    auto t0 = std::chrono::steady_clock::now();
    std::string line;
    std::vector<std::string> headers;
    std::vector<double> row;
    ss_estream_t* st = nullptr;
    uint32_t n_ch = 0;
    uint64_t rows_ingested = 0;
    size_t raw_bytes = 0;

    while (std::getline(*in, line)) {
        raw_bytes += line.size() + 1;
        if (line.empty()) continue;
        if (!parse_numeric_row(line, row)) {
            if (rows_ingested == 0 && headers.empty()) {
                headers = split_header(line);
            }
            continue;
        }
        if (!st) {
            n_ch = static_cast<uint32_t>(row.size());
            ss_estream_config_t cfg{};
            ss_estream_default_config(&cfg, n_ch);
            cfg.alpha = alpha;
            cfg.warmup = warmup;
            st = ss_estream_create(&cfg);
            if (!st) {
                std::cerr << "streamsieve: failed to initialize engine\n";
                return 1;
            }
        }
        if (row.size() == n_ch) {
            ss_estream_update_row(st, row.data(), n_ch);
            ++rows_ingested;
        }
    }

    if (!st) {
        std::cerr << "streamsieve: no numeric rows found\n";
        return 1;
    }

    uint32_t k_eff = std::min(top_k, n_ch);
    std::vector<ss_channel_status_t> top(k_eff);
    uint32_t out_k = 0;
    ss_estream_topk(st, k_eff, top.data(), &out_k);

    std::vector<uint32_t> rej(n_ch);
    uint32_t n_rej = 0;
    ss_estream_ebh_reject(st, alpha, rej.data(), n_ch, &n_rej);

    auto t1 = std::chrono::steady_clock::now();
    double elapsed_ms = std::chrono::duration<double, std::milli>(t1 - t0).count();

    auto ch_name = [&](uint32_t idx) -> std::string {
        if (idx < headers.size() && !headers[idx].empty()) return headers[idx];
        return "ch_" + std::to_string(idx);
    };
    auto mode_str = [](uint8_t m) -> const char* {
        switch (m) {
            case 1: return "mean_shift";
            case 2: return "burst";
            case 3: return "shift+burst";
            default: return "calm";
        }
    };

    auto print_json_str = [](const std::string& s) {
        std::cout << '"';
        for (unsigned char ch : s) {
            switch (ch) {
                case '"':  std::cout << "\\\""; break;
                case '\\': std::cout << "\\\\"; break;
                case '\b': std::cout << "\\b";  break;
                case '\f': std::cout << "\\f";  break;
                case '\n': std::cout << "\\n";  break;
                case '\r': std::cout << "\\r";  break;
                case '\t': std::cout << "\\t";  break;
                default:
                    if (ch < 0x20) {
                        char esc[8];
                        std::snprintf(esc, sizeof(esc), "\\u%04x", static_cast<unsigned>(ch));
                        std::cout << esc;
                    } else {
                        std::cout << static_cast<char>(ch);
                    }
            }
        }
        std::cout << '"';
    };
    auto print_json_num = [](double v) {
        if (std::isfinite(v)) std::cout << v;
        else std::cout << "null";
    };

    if (as_json) {
        std::cout << "{\"engine\":\"streamsieve\",\"version\":\"" << ss_version() << "\""
                  << ",\"avx2\":" << (ss_has_avx2() ? "true" : "false")
                  << ",\"channels\":" << n_ch
                  << ",\"ticks\":" << rows_ingested
                  << ",\"raw_bytes\":" << raw_bytes
                  << ",\"alpha\":"; print_json_num(alpha);
        std::cout << ",\"ebh_rejected\":" << n_rej
                  << ",\"elapsed_ms\":"; print_json_num(elapsed_ms);
        std::cout << ",\"top\":[";
        for (uint32_t i = 0; i < out_k; ++i) {
            if (i > 0) std::cout << ",";
            const auto& s = top[i];
            std::cout << "{\"channel\":" << s.channel << ",\"name\":";
            print_json_str(ch_name(s.channel));
            std::cout << ",\"e_value\":"; print_json_num(s.e_value);
            std::cout << ",\"log_e_mean\":"; print_json_num(s.log_e_mean);
            std::cout << ",\"log_e_burst\":"; print_json_num(s.log_e_burst);
            std::cout << ",\"z_last\":"; print_json_num(s.z_score);
            std::cout << ",\"changepoint_tick\":" << s.changepoint_tick
                      << ",\"mode\":\"" << mode_str(s.mode) << "\""
                      << ",\"rejected_ebh\":" << (s.rejected_ebh ? "true" : "false")
                      << ",\"handle\":\"ch:" << s.channel << "@t=" << s.changepoint_tick << ".." << rows_ingested << "\"}";
        }
        std::cout << "]}\n";
    } else {
        std::printf("STREAMSIEVE BRIEF | %u ch x %llu ticks (%zu B) | e-BH(alpha=%.3f): %u/%u flagged | %.2f ms\n",
                    n_ch, static_cast<unsigned long long>(rows_ingested), raw_bytes, alpha, n_rej, n_ch, elapsed_ms);
        for (uint32_t i = 0; i < out_k; ++i) {
            const auto& s = top[i];
            if (i >= n_rej && i >= 3) break;
            std::printf("  [%s] %-14s e=%10.2g  logE(m=%.2f,b=%.2f)  z=%+.2f  cp@t=%u  mode=%-11s  ref=ch:%u@%u..%llu\n",
                        s.rejected_ebh ? "ALERT" : "watch",
                        ch_name(s.channel).c_str(),
                        s.e_value, s.log_e_mean, s.log_e_burst, s.z_score,
                        s.changepoint_tick, mode_str(s.mode), s.channel,
                        s.changepoint_tick, static_cast<unsigned long long>(rows_ingested));
        }
    }

    ss_estream_destroy(st);
    return 0;
}

int cmd_bench(int argc, char** argv) {
    uint32_t n_ch = 1024;
    uint32_t n_ticks = 2000;
    for (int i = 2; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--channels" && i + 1 < argc) n_ch = static_cast<uint32_t>(std::atoi(argv[++i]));
        else if (arg == "--ticks" && i + 1 < argc) n_ticks = static_cast<uint32_t>(std::atoi(argv[++i]));
    }

    std::vector<double> data(static_cast<size_t>(n_ticks) * n_ch);
    uint64_t rng = 0x9e3779b97f4a7c15ULL;
    auto next_u01 = [&]() -> double {
        rng ^= rng >> 12; rng ^= rng << 25; rng ^= rng >> 27;
        return static_cast<double>((rng * 2685821657736338717ULL) >> 11) * (1.0 / 9007199254740992.0);
    };
    for (size_t i = 0; i < data.size(); ++i) {
        /* Fast triangular zero-mean unit-scale noise */
        data[i] = (next_u01() + next_u01() - 1.0) * 2.4494897;
    }
    /* Inject 8 shifted channels after tick 500 */
    for (uint32_t t = 500; t < n_ticks; ++t) {
        for (uint32_t c = 0; c < std::min<uint32_t>(8, n_ch); ++c) {
            data[static_cast<size_t>(t) * n_ch + c] += 1.5;
        }
    }

    ss_estream_config_t cfg{};
    ss_estream_default_config(&cfg, n_ch);
    cfg.ring_capacity = 0; /* Pure streaming throughput mode */
    ss_estream_t* st = ss_estream_create(&cfg);

    auto t0 = std::chrono::steady_clock::now();
    ss_estream_update_batch(st, data.data(), n_ticks, n_ch);
    std::vector<uint32_t> rej(n_ch);
    uint32_t n_rej = 0;
    ss_estream_ebh_reject(st, 0.05, rej.data(), n_ch, &n_rej);
    auto t1 = std::chrono::steady_clock::now();

    double sec = std::chrono::duration<double>(t1 - t0).count();
    double total_pts = static_cast<double>(n_ticks) * static_cast<double>(n_ch);
    double mpts_s = (total_pts / sec) / 1e6;
    std::printf("streamsieve %s (%s) | %u ch x %u ticks = %.2fM channel-ticks in %.3f ms (%.2fM ch-ticks/s) | e-BH rejected=%u\n",
                ss_version(), ss_has_avx2() ? "AVX2+FMA" : "scalar",
                n_ch, n_ticks, total_pts / 1e6, sec * 1e3, mpts_s, n_rej);
    ss_estream_destroy(st);
    return 0;
}

} /* namespace */

int main(int argc, char** argv) {
    if (argc < 2) {
        print_help();
        return 0;
    }
    std::string cmd = argv[1];
    if (cmd == "version" || cmd == "--version" || cmd == "-V") {
        std::cout << "streamsieve " << ss_version() << " (" << (ss_has_avx2() ? "AVX2+FMA" : "scalar") << ")\n";
        return 0;
    }
    if (cmd == "sift") return cmd_sift(argc, argv);
    if (cmd == "bench") return cmd_bench(argc, argv);
    print_help();
    return (cmd == "-h" || cmd == "--help") ? 0 : 1;
}
