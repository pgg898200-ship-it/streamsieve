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

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <limits>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

namespace {

void print_help() {
    std::cout
        << "streamsieve " << ss_version() << " (" << (ss_has_avx2() ? "AVX2+FMA" : "scalar") << ")\n"
        << "SIMD C++20 multi-agent streaming inference & anytime-valid firehose sifter.\n\n"
        << "Usage:\n"
        << "  streamsieve sift [OPTIONS] [FILE|-]   Sift CSV/TSV/JSONL stream into compact e-BH brief\n"
        << "  streamsieve bench [OPTIONS]           Run native AVX2 throughput self-benchmark\n"
        << "  streamsieve version                   Print version & SIMD capability\n\n"
        << "Options for sift:\n"
        << "  --alpha FLOAT    Anytime-valid e-BH FDR level (default: 0.05)\n"
        << "  --top INT        Max channels in compact brief (default: 10)\n"
        << "  --warmup INT     Causal warmup ticks (default: 32)\n"
        << "  --season INT     Causal seasonal period P >= 2 (default: 0 = off)\n"
        << "  --detrend FLOAT  Causal detrend alpha in (0, 1]; 1.0 = first diff (default: 0 = off)\n"
        << "  --jsonl          Parse newline-delimited JSON objects (auto-detected if line starts with '{')\n"
        << "  --json           Emit machine-readable JSON brief\n";
}

uint32_t parse_u32_clamped(const char* s, uint32_t def_val, uint32_t max_val) {
    if (!s || !*s) return def_val;
    char* end = nullptr;
    long long v = std::strtoll(s, &end, 10);
    if (end == s || v < 0) return def_val;
    if (v > static_cast<long long>(max_val)) return max_val;
    return static_cast<uint32_t>(v);
}

double parse_f64_safe(const char* s, double def_val) {
    if (!s || !*s) return def_val;
    char* end = nullptr;
    double v = std::strtod(s, &end);
    if (end == s || !std::isfinite(v)) return def_val;
    return v;
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

inline void skip_ws(const char*& p) {
    while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') ++p;
}

bool parse_json_string(const char*& p, std::string& out) {
    out.clear();
    if (*p != '"') return false;
    ++p;
    while (*p) {
        unsigned char uc = static_cast<unsigned char>(*p);
        if (uc < 0x20) return false; /* Unescaped control chars are invalid in RFC-8259 */
        char c = *p++;
        if (c == '"') return true;
        if (c == '\\') {
            char e = *p++;
            if (!e) return false;
            switch (e) {
                case '"':  out.push_back('"');  break;
                case '\\': out.push_back('\\'); break;
                case '/':  out.push_back('/');  break;
                case 'b':  out.push_back('\b'); break;
                case 'f':  out.push_back('\f'); break;
                case 'n':  out.push_back('\n'); break;
                case 'r':  out.push_back('\r'); break;
                case 't':  out.push_back('\t'); break;
                case 'u': {
                    for (int k = 0; k < 4; ++k) {
                        unsigned char hc = static_cast<unsigned char>(*p);
                        bool is_hex = (hc >= '0' && hc <= '9') ||
                                      (hc >= 'a' && hc <= 'f') ||
                                      (hc >= 'A' && hc <= 'F');
                        if (!is_hex) return false;
                        ++p;
                    }
                    out.push_back('?');
                    break;
                }
                default:
                    return false;
            }
        } else {
            out.push_back(c);
        }
    }
    return false;
}

bool parse_json_number(const char*& p, double& out_val) {
    const char* start = p;
    if (*p == '-') ++p;
    if (*p == '0') {
        ++p;
    } else if (*p >= '1' && *p <= '9') {
        while (*p >= '0' && *p <= '9') ++p;
    } else {
        p = start;
        return false;
    }
    if (*p == '.') {
        ++p;
        if (!(*p >= '0' && *p <= '9')) { p = start; return false; }
        while (*p >= '0' && *p <= '9') ++p;
    }
    if (*p == 'e' || *p == 'E') {
        ++p;
        if (*p == '+' || *p == '-') ++p;
        if (!(*p >= '0' && *p <= '9')) { p = start; return false; }
        while (*p >= '0' && *p <= '9') ++p;
    }
    /* Reject non-delimiter trailing identifier chars (e.g. 0x10 or 123abc) */
    if ((*p >= '0' && *p <= '9') || (*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') || *p == '_' || *p == '.') {
        p = start;
        return false;
    }
    char* end = nullptr;
    out_val = std::strtod(start, &end);
    if (end != p) {
        p = start;
        return false;
    }
    return true;
}

bool skip_json_value(const char*& p, int depth) {
    if (depth > 64) return false;
    skip_ws(p);
    if (!*p) return false;
    if (*p == '"') {
        std::string tmp;
        return parse_json_string(p, tmp);
    }
    if (*p == '{' || *p == '[') {
        char close_ch = (*p == '{') ? '}' : ']';
        ++p;
        skip_ws(p);
        if (*p == close_ch) { ++p; return true; }
        while (*p) {
            if (close_ch == '}') {
                std::string k;
                if (!parse_json_string(p, k)) return false;
                skip_ws(p);
                if (*p != ':') return false;
                ++p;
            }
            if (!skip_json_value(p, depth + 1)) return false;
            skip_ws(p);
            if (*p == ',') { ++p; skip_ws(p); continue; }
            if (*p == close_ch) { ++p; return true; }
            return false;
        }
        return false;
    }
    if (std::strncmp(p, "true", 4) == 0) { p += 4; return true; }
    if (std::strncmp(p, "false", 5) == 0) { p += 5; return true; }
    if (std::strncmp(p, "null", 4) == 0) { p += 4; return true; }
    double tmp_num = 0.0;
    return parse_json_number(p, tmp_num);
}

bool parse_jsonl_row(const std::string& line,
                     std::vector<std::string>& headers,
                     std::unordered_map<std::string, uint32_t>& key_to_col,
                     std::vector<double>& row,
                     bool schema_locked) {
    const char* p = line.c_str();
    skip_ws(p);
    if (*p != '{') return false;
    ++p;
    skip_ws(p);

    if (schema_locked) {
        row.assign(headers.size(), std::numeric_limits<double>::quiet_NaN());
    } else {
        headers.clear();
        key_to_col.clear();
        row.clear();
    }

    if (*p == '}') {
        ++p;
        skip_ws(p);
        return (*p == '\0') && schema_locked;
    }

    bool closed = false;
    std::string key;
    while (*p) {
        skip_ws(p);
        if (!parse_json_string(p, key)) return false;
        skip_ws(p);
        if (*p != ':') return false;
        ++p;
        skip_ws(p);

        /* Strict RFC-8259 JSON number vs other JSON value */
        if (*p == '-' || (*p >= '0' && *p <= '9')) {
            double val = 0.0;
            if (!parse_json_number(p, val)) return false;
            if (std::isfinite(val)) {
                if (!schema_locked) {
                    auto it = key_to_col.find(key);
                    if (it == key_to_col.end()) {
                        uint32_t idx = static_cast<uint32_t>(headers.size());
                        key_to_col.emplace(key, idx);
                        headers.push_back(key);
                        row.push_back(val);
                    } else {
                        row[it->second] = val;
                    }
                } else {
                    auto it = key_to_col.find(key);
                    if (it != key_to_col.end()) {
                        row[it->second] = val;
                    }
                }
            }
        } else {
            if (!skip_json_value(p, 0)) return false;
        }

        skip_ws(p);
        if (*p == ',') { ++p; continue; }
        if (*p == '}') { ++p; closed = true; break; }
        return false;
    }

    if (!closed) return false;
    skip_ws(p);
    if (*p != '\0') return false;
    return !row.empty();
}

int cmd_sift(int argc, char** argv) {
    double alpha = 0.05;
    uint32_t top_k = 10;
    uint32_t warmup = 32;
    uint32_t season_period = 0;
    double detrend_alpha = 0.0;
    bool as_json = false;
    bool jsonl_mode = false;
    bool mode_decided = false;
    std::string file_path = "-";

    for (int i = 2; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--alpha" && i + 1 < argc) {
            alpha = parse_f64_safe(argv[++i], 0.05);
        } else if (arg == "--top" && i + 1 < argc) {
            top_k = parse_u32_clamped(argv[++i], 10, 1000000);
        } else if (arg == "--warmup" && i + 1 < argc) {
            warmup = parse_u32_clamped(argv[++i], 32, 1000000);
        } else if (arg == "--season" && i + 1 < argc) {
            season_period = parse_u32_clamped(argv[++i], 0, 4096);
        } else if (arg == "--detrend" && i + 1 < argc) {
            detrend_alpha = parse_f64_safe(argv[++i], 0.0);
        } else if (arg == "--jsonl") {
            jsonl_mode = true;
            mode_decided = true;
        } else if (arg == "--json") {
            as_json = true;
        } else if (arg == "-h" || arg == "--help") {
            print_help();
            return 0;
        } else {
            file_path = arg;
        }
    }

    /* Sanitize identically to EStreamEngine so reported JSON config matches effective behavior */
    if (!std::isfinite(alpha)) alpha = 0.05;
    alpha = std::max(1e-9, std::min(alpha, 0.5));
    warmup = std::max<uint32_t>(warmup, 12U);
    season_period = (season_period >= 2U) ? std::min<uint32_t>(season_period, 4096U) : 0U;
    if (!std::isfinite(detrend_alpha)) detrend_alpha = 0.0;
    detrend_alpha = std::max(0.0, std::min(detrend_alpha, 1.0));

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
    std::unordered_map<std::string, uint32_t> key_to_col;
    std::vector<double> row;
    ss_estream_t* st = nullptr;
    uint32_t n_ch = 0;
    uint64_t rows_ingested = 0;
    size_t raw_bytes = 0;

    while (std::getline(*in, line)) {
        raw_bytes += line.size() + 1;
        const char* p = line.c_str();
        skip_ws(p);
        if (!*p) continue;
        if (!mode_decided) {
            jsonl_mode = (*p == '{');
            mode_decided = true;
        }

        bool ok = false;
        if (jsonl_mode) {
            ok = parse_jsonl_row(line, headers, key_to_col, row, st != nullptr);
        } else {
            ok = parse_numeric_row(line, row);
            if (!ok && rows_ingested == 0 && headers.empty()) {
                headers = split_header(line);
            }
        }
        if (!ok) continue;

        if (!st) {
            n_ch = static_cast<uint32_t>(row.size());
            ss_estream_config_t cfg{};
            ss_estream_default_config(&cfg, n_ch);
            cfg.alpha = alpha;
            cfg.warmup = warmup;
            cfg.season_period = season_period;
            cfg.detrend_alpha = detrend_alpha;
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
        const size_t n = s.size();
        for (size_t i = 0; i < n; ) {
            unsigned char ch = static_cast<unsigned char>(s[i]);
            if (ch < 0x80) {
                ++i;
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
            } else {
                /* Validate UTF-8 multibyte sequence; escape invalid raw bytes as \ufffd */
                size_t seq_len = 0;
                if ((ch & 0xE0) == 0xC0 && ch >= 0xC2 && i + 1 < n &&
                    (static_cast<unsigned char>(s[i + 1]) & 0xC0) == 0x80) {
                    seq_len = 2;
                } else if ((ch & 0xF0) == 0xE0 && i + 2 < n) {
                    unsigned char b1 = static_cast<unsigned char>(s[i + 1]);
                    unsigned char b2 = static_cast<unsigned char>(s[i + 2]);
                    if ((b1 & 0xC0) == 0x80 && (b2 & 0xC0) == 0x80 &&
                        !(ch == 0xE0 && b1 < 0xA0) && !(ch == 0xED && b1 >= 0xA0)) {
                        seq_len = 3;
                    }
                } else if ((ch & 0xF8) == 0xF0 && ch <= 0xF4 && i + 3 < n) {
                    unsigned char b1 = static_cast<unsigned char>(s[i + 1]);
                    unsigned char b2 = static_cast<unsigned char>(s[i + 2]);
                    unsigned char b3 = static_cast<unsigned char>(s[i + 3]);
                    if ((b1 & 0xC0) == 0x80 && (b2 & 0xC0) == 0x80 && (b3 & 0xC0) == 0x80 &&
                        !(ch == 0xF0 && b1 < 0x90) && !(ch == 0xF4 && b1 >= 0x90)) {
                        seq_len = 4;
                    }
                }
                if (seq_len > 0) {
                    std::cout.write(s.data() + i, static_cast<std::streamsize>(seq_len));
                    i += seq_len;
                } else {
                    std::cout << "\\ufffd";
                    ++i;
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
                  << ",\"format\":\"" << (jsonl_mode ? "jsonl" : "delimited") << "\""
                  << ",\"channels\":" << n_ch
                  << ",\"ticks\":" << rows_ingested
                  << ",\"raw_bytes\":" << raw_bytes
                  << ",\"alpha\":"; print_json_num(alpha);
        std::cout << ",\"season_period\":" << season_period
                  << ",\"detrend_alpha\":"; print_json_num(detrend_alpha);
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
        if (arg == "--channels" && i + 1 < argc) n_ch = std::max<uint32_t>(1, parse_u32_clamped(argv[++i], 1024, 1000000));
        else if (arg == "--ticks" && i + 1 < argc) n_ticks = std::max<uint32_t>(1, parse_u32_clamped(argv[++i], 2000, 10000000));
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
