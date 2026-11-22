#define _POSIX_C_SOURCE 200809L
#include "tailstate.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* Warm single-threaded integer-key kernels. See README for the contract. */
enum { CAP = 65536, ROUNDS = 32, TRIALS = 8, READS = 11, RING_SIZE = 8192, COMPACT_CAP = CAP + 16384 };
enum method { TAIL, DENSE, JOURNAL, REVERSE, BITMAP, RING, INDEXED, COMPACT, METHODS };
static const char *const names[] = {"tailstate", "dense-radix", "journal-latest", "journal-reverse", "journal-bitmap", "ring-bitmap", "tailstate-indexed", "compact-bitmap"};

typedef struct { uint32_t key; uint64_t value; int deleted; } mutation;
typedef struct { uint64_t hash, rows; } digest;
typedef struct {
    const char *name;
    uint32_t hot, batch, lag, clients, priming, slow_every;
} scenario;

static const scenario cases[] = {
    {"hot-duplicates", 32, 1024, 8192, 8, 8192, 0},
    {"hot-onset", 32, 1024, 8192, 8, 0, 0},
    {"hot-write-heavy", 32, 1024, 8192, 1, 8192, 0},
    {"multirate", 32, 1024, 32768, 8, 32768, 1024},
    {"uniform-recent", CAP, 1024, 64, 8, 64, 0},
    {"uniform-backlog", CAP, 1024, 16384, 8, 16384, 0},
    {"initial-sync", CAP, 1024, UINT32_MAX, 1, 0, 0}
};

static void fail(const char *msg)
{
    fprintf(stderr, "benchmark: %s\n", msg);
    exit(1);
}

static void *allocate(size_t count, size_t bytes)
{
    void *p = calloc(count, bytes);
    if (!p) fail("allocation failed");
    return p;
}

static uint64_t now_ns(void)
{
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) fail("clock_gettime failed");
    return (uint64_t)t.tv_sec * UINT64_C(1000000000) + (uint64_t)t.tv_nsec;
}

static uint32_t random_u32(uint32_t *state)
{
    uint32_t x = *state;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    return *state = x;
}

static mutation *make_trace(const scenario *s)
{
    size_t n = CAP + s->priming + ROUNDS * s->batch;
    mutation *trace = allocate(n, sizeof(*trace));
    uint32_t rng = 20260523;
    for (size_t i = 0; i < n; ++i) {
        /* The multiplicative permutation spreads hot keys throughout the table. */
        uint32_t raw = i < CAP ? (uint32_t)i : random_u32(&rng) % s->hot;
        if (s->slow_every && i >= CAP && (i - CAP) % s->slow_every == 0)
            raw = s->hot + (uint32_t)((i - CAP) / s->slow_every);
        trace[i].key = (raw * UINT32_C(40503)) % CAP;
        trace[i].deleted = (random_u32(&rng) & 15u) == 0;
        trace[i].value = ((uint64_t)random_u32(&rng) << 32) | (uint64_t)i;
    }
    return trace;
}

typedef struct {
    enum method method;
    ts_store *tail;
    uint64_t head, safe_floor;
    size_t bytes, compact_used, compact_base;
    uint64_t compact_head, *compact_seq;
    uint64_t *version, *value, *live, *summary;
    unsigned char *deleted;
    uint32_t *log, *seen, *keys, *scratch;
    uint32_t generation;
} index_state;

static index_state build(enum method m, size_t n)
{
    index_state a = {.method = m};
    if (m == TAIL || m == INDEXED) {
        a.tail = m == INDEXED ? ts_create_indexed(CAP) : ts_create(CAP);
        if (!a.tail) fail("ts_create failed");
        a.bytes = ts_store_bytes(a.tail);
        return a;
    }
    if (m == RING) n = RING_SIZE;
    if (m == COMPACT) {
        n = COMPACT_CAP;
        a.compact_seq = allocate(CAP, sizeof(*a.compact_seq));
    }
    a.version = allocate(CAP, sizeof(*a.version));
    a.value = allocate(CAP, sizeof(*a.value));
    a.deleted = allocate(CAP, 1);
    if (m != DENSE) a.log = allocate(n, sizeof(*a.log));
    if (m == DENSE || m == REVERSE || m == RING) a.keys = allocate(CAP, sizeof(*a.keys));
    if (m == DENSE || m == RING) a.scratch = allocate(CAP, sizeof(*a.scratch));
    if (m == REVERSE || m == COMPACT) a.seen = allocate(CAP, sizeof(*a.seen));
    if (m == BITMAP || m == RING || m == COMPACT) {
        a.live = allocate((n + 63) / 64, sizeof(*a.live));
        a.summary = allocate((n + 4095) / 4096, sizeof(*a.summary));
    }
    a.bytes = sizeof(a) + CAP * (sizeof(*a.version) + sizeof(*a.value) + sizeof(*a.deleted));
    if (a.compact_seq) a.bytes += CAP * sizeof(*a.compact_seq);
    if (a.log) a.bytes += n * sizeof(*a.log);
    if (a.keys) a.bytes += CAP * sizeof(*a.keys);
    if (a.scratch) a.bytes += CAP * sizeof(*a.scratch);
    if (a.seen) a.bytes += CAP * sizeof(*a.seen);
    if (a.live) a.bytes += ((n + 63) / 64 + (n + 4095) / 4096) * sizeof(*a.live);
    return a;
}

static void release(index_state *a)
{
    ts_destroy(a->tail);
    free(a->version);
    free(a->value);
    free(a->deleted);
    free(a->log);
    free(a->seen);
    free(a->keys);
    free(a->scratch);
    free(a->live);
    free(a->summary);
    free(a->compact_seq);
}

/* Pack current rows in place, preserving sequence order. Since at most CAP
 * keys survive, at least 16,384 new writes fit before another compaction. */
static void compact(index_state *a)
{
    size_t used = 0;
    for (size_t old = 0; old < a->compact_used; ++old) {
        uint32_t key = a->log[old];
        if (a->seen[key] != old) continue;
        a->compact_seq[used] = a->version[key];
        a->log[used] = key;
        a->seen[key] = (uint32_t)used++;
    }
    size_t full_words = used / 64, words = (used + 63) / 64;
    memset(a->live, 0, ((COMPACT_CAP + 63) / 64) * sizeof(*a->live));
    memset(a->summary, 0, ((COMPACT_CAP + 4095) / 4096) * sizeof(*a->summary));
    memset(a->live, 0xff, full_words * sizeof(*a->live));
    if (used % 64) a->live[full_words] = (UINT64_C(1) << (used % 64)) - 1;
    memset(a->summary, 0xff, (words / 64) * sizeof(*a->summary));
    if (words % 64) a->summary[words / 64] = (UINT64_C(1) << (words % 64)) - 1;
    a->compact_head = a->head;
    a->compact_base = used;
    a->compact_used = used;
}

static void apply(index_state *a, const mutation *trace, size_t count)
{
    if (a->tail) {
        for (size_t i = 0; i < count; ++i) {
            const mutation *m = &trace[i];
            int rc = m->deleted ? ts_delete(a->tail, m->key) : ts_put(a->tail, m->key, m->value);
            if (rc != TS_OK) fail("Tailstate update failed");
        }
        a->head = ts_head(a->tail);
        return;
    }
    for (size_t i = 0; i < count; ++i) {
        const mutation *m = &trace[i];
        uint64_t position = a->head;
        if (a->method == RING) {
            position %= RING_SIZE;
            if (a->head >= RING_SIZE) {
                uint64_t evicted = a->head - RING_SIZE + 1;
                uint32_t k = a->log[position];
                if (k != m->key && a->version[k] == evicted)
                    a->safe_floor = evicted;
            }
        }
        if (a->method == COMPACT) {
            if (a->compact_used == COMPACT_CAP) compact(a);
            position = a->compact_used++;
        }
        if (a->live) {
            uint64_t old_seq = a->version[m->key];
            if (old_seq && (a->method != RING || a->head - old_seq < RING_SIZE)) {
                uint64_t pos = old_seq - 1;
                if (a->method == RING) pos %= RING_SIZE;
                if (a->method == COMPACT) pos = a->seen[m->key];
                uint64_t word = pos / 64;
                a->live[word] &= ~(UINT64_C(1) << (pos % 64));
                if (!a->live[word])
                    a->summary[word / 64] &= ~(UINT64_C(1) << (word % 64));
            }
            uint64_t word = position / 64;
            a->live[word] |= UINT64_C(1) << (position % 64);
            a->summary[word / 64] |= UINT64_C(1) << (word % 64);
        }
        if (a->method == COMPACT) a->seen[m->key] = (uint32_t)position;
        if (a->log) a->log[position] = m->key;
        a->version[m->key] = ++a->head;
        a->value[m->key] = m->deleted ? 0 : m->value;
        a->deleted[m->key] = (unsigned char)m->deleted;
    }
}

static void consume(const ts_change *row, void *ctx)
{
    digest *out = ctx;
    uint64_t packed = row->seq ^ ((uint64_t)row->key << 32) ^ (uint64_t)row->deleted;
    out->hash = (out->hash ^ row->value) * UINT64_C(1099511628211) + packed;
    ++out->rows;
}

static void emit(const index_state *a, uint32_t k, digest *out)
{
    ts_change row = {.seq=a->version[k], .value=a->value[k], .key=k, .deleted=a->deleted[k]};
    consume(&row, out);
}

/* Stable LSD radix ordering; only digits which could differ need a pass. */
static uint32_t *ordered(index_state *a, size_t n, uint64_t differing)
{
    uint32_t *src = a->keys, *dst = a->scratch;
    for (unsigned shift = 0; differing; shift += 8, differing >>= 8) {
        if (!(differing & 255)) continue;
        size_t pos[256] = {0};
        for (size_t j = 0; j < n; ++j) ++pos[(a->version[src[j]] >> shift) & 255];
        size_t offset = 0;
        for (size_t b = 0; b < 256; ++b) {
            size_t c = pos[b];
            pos[b] = offset;
            offset += c;
        }
        for (size_t j = 0; j < n; ++j) {
            uint32_t key = src[j];
            dst[pos[(a->version[key] >> shift) & 255]++] = key;
        }
        uint32_t *swap = src;
        src = dst;
        dst = swap;
    }
    return src;
}

static unsigned low_bit(uint64_t nonzero)
{
#if defined(__GNUC__) || defined(__clang__)
    return (unsigned)__builtin_ctzll(nonzero);
#else
    unsigned index = 0;
    while (!(nonzero & 1)) { nonzero >>= 1; ++index; }
    return index;
#endif
}

static void bitmap_query(index_state *a, uint64_t cursor, uint64_t end, digest *out)
{
    if (cursor == end) return;
    for (uint64_t g = cursor / 4096; g < (end + 4095) / 4096; ++g) {
        uint64_t words = a->summary[g];
        if (g == cursor / 4096) words &= UINT64_MAX << ((cursor / 64) % 64);
        while (words) {
            uint64_t word = g * 64 + low_bit(words);
            uint64_t bits = a->live[word];
            if (word == cursor / 64) bits &= UINT64_MAX << (cursor % 64);
            if (word == end / 64) bits &= (UINT64_C(1) << (end % 64)) - 1;
            if (word > (end - 1) / 64) break;
            while (bits) {
                uint64_t i = word * 64 + low_bit(bits);
                emit(a, a->log[i], out);
                bits &= bits - 1;
            }
            words &= words - 1;
        }
    }
}

static digest query(index_state *a, uint64_t cursor)
{
    digest out = {.hash=1469598103934665603ULL};
    size_t used = 0;
    switch (a->method) {
    case INDEXED:
    case TAIL:
        if (ts_since(a->tail, cursor, consume, &out) != TS_OK) fail("ts_since failed");
        break;
    case RING:
        if (cursor >= a->safe_floor) {
            uint64_t oldest = a->head > RING_SIZE ? a->head - RING_SIZE : 0;
            if (cursor < oldest) cursor = oldest;
            uint64_t count = a->head - cursor, first = cursor % RING_SIZE;
            uint64_t first_n = count < RING_SIZE - first ? count : RING_SIZE - first;
            bitmap_query(a, first, first + first_n, &out);
            bitmap_query(a, 0, count - first_n, &out);
            break;
        }
        /* An evicted current row may be needed. The dense fallback remains exact. */
        /* fall through */
    case DENSE: {
        uint64_t differing = 0;
        for (uint32_t k = 0; k < CAP; ++k) {
            if (a->version[k] <= cursor) continue;
            a->keys[used++] = k;
            differing |= a->version[k] ^ a->version[a->keys[0]];
        }
        uint32_t *keys = ordered(a, used, differing);
        for (size_t j = 0; j < used; ++j) emit(a, keys[j], &out);
        break;
    }
    case JOURNAL:
        /* Index directly at the cursor. Latest-sequence filtering preserves order
         * without a hash, bitmap, generation stamps, sort or output buffer. */
        for (uint64_t i = cursor; i < a->head; ++i) {
            uint32_t k = a->log[i];
            if (a->version[k] == i + 1) emit(a, k, &out);
        }
        break;
    case COMPACT: {
        size_t pos;
        if (cursor >= a->compact_head) {
            pos = a->compact_base + (size_t)(cursor - a->compact_head);
        } else {
            size_t lo = 0, hi = a->compact_base;
            while (lo < hi) {
                size_t mid = lo + (hi - lo) / 2;
                if (a->compact_seq[mid] <= cursor) lo = mid + 1;
                else hi = mid;
            }
            pos = lo;
        }
        bitmap_query(a, pos, a->compact_used, &out);
        break;
    }
    case BITMAP:
        bitmap_query(a, cursor, a->head, &out);
        break;
    case REVERSE:
        if (++a->generation == 0) {
            memset(a->seen, 0, CAP * sizeof(*a->seen));
            a->generation = 1;
        }
        for (uint64_t i = a->head; i > cursor; --i) {
            uint32_t k = a->log[i - 1];
            if (a->seen[k] == a->generation) continue;
            a->seen[k] = a->generation;
            a->keys[used++] = k;
        }
        while (used) emit(a, a->keys[--used], &out);
        break;
    default: fail("invalid method");
    }
    return out;
}

static uint64_t cursor_for(uint64_t head, uint32_t lag, unsigned client)
{
    if (lag == UINT32_MAX) return 0;
    /* Similar but distinct subscribers, all within the generated trace. */
    uint32_t distance = lag - (client * 13 % (lag / 4 + 1));
    return head > distance ? head - distance : 0;
}

static int compare_time(const void *a, const void *b)
{
    uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
    return (x > y) - (x < y);
}

static uint64_t median_ns(const uint64_t *ordered_ns, size_t count)
{
    uint64_t lo = ordered_ns[(count - 1) / 2], hi = ordered_ns[count / 2];
    return lo + (hi - lo) / 2;
}

typedef struct {
    uint64_t writes[TRIALS], mixed[TRIALS], reads[TRIALS * READS], rounds[TRIALS * ROUNDS];
    size_t bytes;
    digest final;
} samples;

static void check_result(digest got, digest *ref, int record)
{
    if (record) *ref = got;
    else if (ref->rows != got.rows || ref->hash != got.hash) fail("ordered output mismatch");
}

static void run_case(const scenario *s)
{
    const size_t mutations = ROUNDS * s->batch, seed = CAP + s->priming, total = seed + mutations;
    mutation *trace = make_trace(s);
    samples all[METHODS] = {0};
    digest *ref = allocate((size_t)ROUNDS * s->clients + READS, sizeof(*ref));

    for (unsigned t = 0; t < TRIALS; ++t) {
        for (unsigned j = 0; j < METHODS; ++j) {
            enum method m = (enum method)((j + t) % METHODS);
            int record = t == 0 && j == 0;
            samples *v = &all[m];
            index_state a = build(m, total);
            v->bytes = a.bytes;
            apply(&a, trace, seed);
            uint64_t start = now_ns();
            apply(&a, trace + seed, mutations);
            v->writes[t] = now_ns() - start;
            /* One untimed warm-up for query work buffers. */
            (void)query(&a, cursor_for(a.head, s->lag, 0));
            for (unsigned q = 0; q < READS; ++q) {
                uint64_t cursor = cursor_for(a.head, s->lag, q);
                start = now_ns();
                digest got = query(&a, cursor);
                v->reads[t * READS + q] = now_ns() - start;
                check_result(got, &ref[ROUNDS * s->clients + q], record);
                if (q == 0) v->final = got;
            }
            release(&a);
            a = build(m, total);
            apply(&a, trace, seed);
            uint64_t elapsed = 0;
            /* Exclude correctness checking from the fused path's elapsed time. */
            digest *answers = allocate(s->clients, sizeof(*answers));
            for (unsigned r = 0; r < ROUNDS; ++r) {
                start = now_ns();
                apply(&a, trace + seed + (size_t)r * s->batch, s->batch);
                for (unsigned q = 0; q < s->clients; ++q)
                    answers[q] = query(&a, cursor_for(a.head, s->lag, q));
                v->rounds[t * ROUNDS + r] = now_ns() - start;
                elapsed += v->rounds[t * ROUNDS + r];
                for (unsigned q = 0; q < s->clients; ++q)
                    check_result(answers[q], &ref[r * s->clients + q], record);
            }
            free(answers);
            v->mixed[t] = elapsed;
            release(&a);
        }
    }

    for (unsigned m = 0; m < METHODS; ++m) {
        samples *v = &all[m];
        qsort(v->writes, TRIALS, sizeof(uint64_t), compare_time);
        qsort(v->mixed, TRIALS, sizeof(uint64_t), compare_time);
        qsort(v->rounds, ROUNDS * TRIALS, sizeof(uint64_t), compare_time);
        qsort(v->reads, READS * TRIALS, sizeof(uint64_t), compare_time);
        printf("%s,%s,%u,%u,%u,%u,%" PRIu64 ",%.1f,%" PRIu64 ",%" PRIu64 ",%.1f,%" PRIu64 ",%zu,%016" PRIx64 "\n",
               s->name, names[m], s->hot, s->batch, s->lag, s->clients, v->final.rows,
               (double)median_ns(v->writes, TRIALS) / mutations, median_ns(v->reads, TRIALS * READS),
               v->reads[((TRIALS*READS*95 + 99) / 100)-1], (double)median_ns(v->mixed, TRIALS) / ROUNDS,
               v->rounds[((TRIALS * ROUNDS * 99 + 99) / 100) - 1], v->bytes, v->final.hash);
    }
    free(ref);
    free(trace);
}

int main(void)
{
    puts("scenario,method,hot_pool_size,writes_per_round,nominal_lag,clients_per_round,rows_nominal,write_ns_each,read_p50_ns,read_p95_ns,mixed_ns_per_round,mixed_round_p99_ns,index_bytes,digest_nominal");
    for (size_t c = 0; c < sizeof(cases)/sizeof(cases[0]); ++c) run_case(&cases[c]);
    return 0;
}
