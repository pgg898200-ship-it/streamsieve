#include "tailstate.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>

static uint32_t seed;
static unsigned step;

#define CHECK(condition)                                                       \
    do {                                                                       \
        if (!(condition)) {                                                    \
            fprintf(stderr, "%s:%d: %s (seed=%" PRIu32 ", step=%u)\n",        \
                    __FILE__, __LINE__, #condition, seed, step);               \
            exit(EXIT_FAILURE);                                                \
        }                                                                      \
    } while (0)

enum { MAX_KEYS = 129 };

typedef ts_store *(*constructor)(uint32_t);

typedef struct {
    ts_change rows[MAX_KEYS];
    size_t count;
} change_list;

static void collect(const ts_change *row, void *ptr)
{
    change_list *list = ptr;
    CHECK(list->count < MAX_KEYS);
    list->rows[list->count++] = *row;
}

static void same(const ts_change *actual, const ts_change *expected)
{
    CHECK(actual->key == expected->key);
    CHECK(actual->seq == expected->seq);
    CHECK(actual->value == expected->value);
    CHECK(actual->deleted == expected->deleted);
}

static int compare_seq(const void *a, const void *b)
{
    const ts_change *left = a, *right = b;
    return (left->seq > right->seq) - (left->seq < right->seq);
}

/* Independent oracle: full scan followed by sorting. No recency links. */
static void check_delta(ts_store *store, const ts_change *model,
                        uint32_t capacity, uint64_t cursor)
{
    change_list expected = {0}, actual = {0};
    for (uint32_t key = 0; key < capacity; ++key) {
        if (model[key].seq > cursor)
            collect(&model[key], &expected);
    }
    qsort(expected.rows, expected.count, sizeof(ts_change), compare_seq);
    CHECK(ts_since(store, cursor, collect, &actual) == TS_OK);
    CHECK(actual.count == expected.count);
    for (size_t i = 0; i < actual.count; ++i)
        same(&actual.rows[i], &expected.rows[i]);

    uint32_t matching = UINT32_MAX;
    for (uint32_t key = 0; key < capacity; ++key) {
        if (model[key].seq == cursor) {
            matching = key;
            break;
        }
    }
    const uint32_t hints[] = {matching, 0, UINT32_MAX};
    for (size_t h = 0; h < sizeof(hints) / sizeof(*hints); ++h) {
        ts_iter iter;
        ts_change row;
        CHECK(ts_resume(store, cursor, hints[h], &iter) == TS_OK);
        for (size_t i = 0; i < expected.count; ++i) {
            CHECK(ts_next(&iter, &row) == TS_ROW);
            same(&row, &expected.rows[i]);
        }
        CHECK(ts_next(&iter, &row) == TS_DONE);
    }
}

static void boundaries(constructor create)
{
    CHECK(create(0) == NULL);
    ts_destroy(NULL);
    ts_store *store = create(1);
    CHECK(store != NULL);
    size_t bytes = ts_store_bytes(store);
    CHECK(bytes > 0);
    ts_iter iter = {0};
    ts_change row;
    change_list empty = {0};
    CHECK(ts_head(store) == 0);
    CHECK(ts_since(store, 0, collect, &empty) == TS_OK);
    CHECK(ts_get(store, 0, &row) == TS_DONE);
    CHECK(ts_get(store, 0, NULL) == TS_INVALID);
    CHECK(ts_get(store, 1, &row) == TS_RANGE);
    CHECK(ts_put(NULL, 0, 0) == TS_INVALID);
    CHECK(ts_delete(NULL, 0) == TS_INVALID);
    CHECK(ts_get(NULL, 0, &row) == TS_INVALID);
    CHECK(ts_begin(store, 0, NULL) == TS_INVALID);
    CHECK(ts_begin(NULL, 0, &iter) == TS_INVALID);
    CHECK(ts_since(store, 0, NULL, NULL) == TS_INVALID);
    CHECK(ts_since(store, UINT64_MAX, collect, &empty) == TS_RANGE);
    CHECK(ts_resume(store, UINT64_MAX, 0, &iter) == TS_RANGE);
    CHECK(ts_resume(NULL, 0, 0, &iter) == TS_INVALID);
    CHECK(ts_resume(store, 0, 0, NULL) == TS_INVALID);
    CHECK(ts_next(NULL, &row) == TS_INVALID);
    CHECK(ts_next(&iter, &row) == TS_INVALID);
    CHECK(ts_begin(store, 0, &iter) == TS_OK);
    CHECK(ts_next(&iter, NULL) == TS_INVALID);
    CHECK(ts_put(store, 1, 0) == TS_RANGE);
    CHECK(ts_delete(store, UINT32_MAX) == TS_RANGE);
    CHECK(ts_head(store) == 0);
    CHECK(ts_next(&iter, &row) == TS_DONE);
    CHECK(ts_delete(store, 0) == TS_OK);
    CHECK(ts_next(&iter, &row) == TS_STALE);
    CHECK(ts_delete(store, 0) == TS_OK);
    CHECK(ts_get(store, 0, &row) == TS_ROW);
    CHECK(row.deleted && row.value == 0 && row.seq == 2 && row.key == 0);
    CHECK(ts_put(store, 0, UINT64_MAX) == TS_OK);
    CHECK(ts_put(store, 0, UINT64_MAX) == TS_OK);
    CHECK(ts_begin(store, 2, &iter) == TS_OK);
    CHECK(ts_put(store, 1, 17) == TS_RANGE);
    CHECK(ts_next(&iter, &row) == TS_ROW);
    CHECK(!row.deleted && row.seq == 4 && row.value == UINT64_MAX);
    CHECK(ts_next(&iter, &row) == TS_DONE);
    CHECK(ts_next(&iter, &row) == TS_DONE);
    CHECK(ts_since(store, ts_head(store), collect, &empty) == TS_OK);
    CHECK(empty.count == 0);
    CHECK(ts_store_bytes(store) == bytes);
    ts_destroy(store);
}

static uint32_t next_random(uint32_t *state)
{
    uint32_t x = *state;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    *state = x;
    return x;
}

static void random_model(constructor create, uint32_t capacity, uint32_t initial_seed)
{
    ts_store *store = create(capacity);
    ts_change model[MAX_KEYS] = {{0}};
    uint32_t state = initial_seed;
    seed = initial_seed;
    uint64_t head = 0;
    CHECK(store != NULL);
    for (step = 0; step < 6000; ++step) {
        uint32_t key = next_random(&state) % capacity;
        if (next_random(&state) % 2 == 0)
            key = 0;
        uint64_t value = (uint64_t)next_random(&state) << 32;
        value |= next_random(&state);
        int deleted = next_random(&state) % 4 == 0;
        if (deleted)
            CHECK(ts_delete(store, key) == TS_OK);
        else
            CHECK(ts_put(store, key, value) == TS_OK);
        model[key] = (ts_change){++head, deleted ? 0 : value, key, deleted};
        CHECK(ts_head(store) == head);
        key = next_random(&state) % capacity;
        ts_change row;
        if (model[key].seq) {
            CHECK(ts_get(store, key, &row) == TS_ROW);
            same(&row, &model[key]);
        } else {
            CHECK(ts_get(store, key, &row) == TS_DONE);
        }
        if (step % 11 == 0) {
            check_delta(store, model, capacity, 0);
            check_delta(store, model, capacity, head);
            check_delta(store, model, capacity, head - 1);
            check_delta(store, model, capacity, next_random(&state) % head);
        }
    }
    ts_destroy(store);
}

/* A reader acknowledges one row, disconnects, and reconnects after writes.
 * Compare the entire receiver state, including tombstones, with the publisher. */
static void reconnect(constructor create)
{
    ts_store *store = create(4);
    CHECK(store != NULL);
    ts_change replica[4] = {{0}}, row;
    ts_iter iter;
    CHECK(ts_put(store, 2, 200) == TS_OK);
    CHECK(ts_put(store, 1, 100) == TS_OK);
    CHECK(ts_delete(store, 0) == TS_OK);
    CHECK(ts_begin(store, 0, &iter) == TS_OK);
    CHECK(ts_next(&iter, &row) == TS_ROW && row.key == 2);
    replica[row.key] = row;
    uint64_t cursor = row.seq;
    CHECK(ts_delete(store, 2) == TS_OK);
    CHECK(ts_put(store, 0, 40) == TS_OK);
    CHECK(ts_next(&iter, &row) == TS_STALE);
    /* The acknowledged key was subsequently deleted: hint must fall back. */
    CHECK(ts_resume(store, cursor, 2, &iter) == TS_OK);
    int status;
    while ((status = ts_next(&iter, &row)) == TS_ROW) {
        CHECK(row.seq > cursor);
        replica[row.key] = row;
        cursor = row.seq;
    }
    CHECK(status == TS_DONE);
    CHECK(cursor == ts_head(store));
    for (uint32_t key = 0; key < 3; ++key) {
        CHECK(ts_get(store, key, &row) == TS_ROW);
        same(&replica[key], &row);
    }
    CHECK(ts_get(store, 3, &row) == TS_DONE && replica[3].seq == 0);
    ts_destroy(store);
}

/* Fill the index with singleton epochs, leaving holes between them. Then
 * remove the only leaf in each epoch, in a permutation that exercises many
 * internal branch prefixes. Finally move ordered runs within and across
 * epoch boundaries. key 0 churns and revisits both populated and empty epochs. */
static void index_lifecycle(void)
{
    enum { CAP = 128, EPOCH_WIDTH = 64 };
    ts_change model[MAX_KEYS] = {{0}};
    ts_store *store = ts_create_indexed(CAP);
    CHECK(store != NULL);
    const size_t initial_bytes = ts_store_bytes(store);
    uint64_t head = 0;
    for (unsigned cycle = 0; cycle < 8; ++cycle) {
        for (unsigned i = 0; i < CAP - 1; ++i) {
            uint32_t anchor = 1u + (37u * i + cycle) % (CAP - 1);
            for (unsigned offset = 0; offset < 2 * EPOCH_WIDTH; ++offset) {
                uint32_t key = offset == 0 ? anchor : 0;
                int deleted = (offset + i + cycle) % 7 == 0;
                ++head;
                CHECK((deleted ? ts_delete(store, key) : ts_put(store, key, head)) == TS_OK);
                model[key] = (ts_change){head, deleted ? 0 : head, key, deleted};
            }
            check_delta(store, model, CAP, head - 2 * EPOCH_WIDTH);
            check_delta(store, model, CAP, head - EPOCH_WIDTH);
        }
        /* Every live anchor, its exact predecessor, and one cursor in a gap. */
        for (uint32_t key = 0; key < CAP; ++key) {
            check_delta(store, model, CAP, model[key].seq);
            check_delta(store, model, CAP, model[key].seq - 1);
            check_delta(store, model, CAP, model[key].seq / EPOCH_WIDTH * EPOCH_WIDTH);
        }
        for (unsigned i = 0; i < CAP; ++i) {
            uint32_t key = (109u * i + cycle) % CAP;
            ++head;
            CHECK(ts_put(store, key, head) == TS_OK);
            model[key] = (ts_change){head, head, key, 0};
            check_delta(store, model, CAP, head / 2);
            check_delta(store, model, CAP, head - 1);
        }
        for (unsigned i = 0; i < 2 * EPOCH_WIDTH; ++i) {
            uint32_t key = (i * 3u) % EPOCH_WIDTH;
            ++head;
            CHECK(ts_delete(store, key) == TS_OK);
            model[key] = (ts_change){head, 0, key, 1};
            check_delta(store, model, CAP, head - EPOCH_WIDTH);
        }
        CHECK(ts_store_bytes(store) == initial_bytes);
    }
    ts_destroy(store);
}

/* Cursor 126 and a 128-slot ring place the next four writes on both sides
 * of a wrap. Vary how many of those writes are superseded. Advance through
 * the oldest retained position, then past it, to exercise fallback. */
static void wrapped_resume(void)
{
    enum { CAP = 129, RING_WIDTH = 128, CURSOR = 126 };
    for (unsigned overwritten = 0; overwritten <= 4; ++overwritten) {
        ts_store *store = ts_create_indexed(CAP);
        ts_change model[MAX_KEYS] = {{0}};
        CHECK(store != NULL);
        for (uint64_t seq = 1; seq <= CURSOR + RING_WIDTH + 2; ++seq) {
            uint32_t key = 1;
            if (seq == 1)
                key = CAP - 1; /* Keep an ancient row before the tested cursor. */
            else if (seq > CURSOR && seq <= CURSOR + 4)
                key = (uint32_t)(seq - CURSOR + 1);
            else if (seq > CURSOR + 4 && seq <= CURSOR + 4 + overwritten)
                key = (uint32_t)(seq - CURSOR - 3);
            int deleted = seq % 3 == 0;
            CHECK((deleted ? ts_delete(store, key) : ts_put(store, key, seq)) == TS_OK);
            model[key] = (ts_change){seq, deleted ? 0 : seq, key, deleted};
            if (seq > CURSOR) {
                check_delta(store, model, CAP, CURSOR);
                check_delta(store, model, CAP, seq - 1);
            }
        }
        ts_destroy(store);
    }
}

int main(void)
{
    const constructor create[] = {ts_create, ts_create_indexed};
    const uint32_t capacities[] = {1, 7, 8, 9, 63, 64, 65, 129};
    for (size_t mode = 0; mode < sizeof(create) / sizeof(*create); ++mode) {
        boundaries(create[mode]);
        reconnect(create[mode]);
        for (size_t i = 0; i < sizeof(capacities) / sizeof(*capacities); ++i)
            random_model(create[mode], capacities[i], UINT32_C(0x5bc07) + (uint32_t)i);
    }
    index_lifecycle();
    wrapped_resume();
    puts("Passed boundary, resume, index lifecycle, and 96,000 randomized mutation checks.");
    return 0;
}
