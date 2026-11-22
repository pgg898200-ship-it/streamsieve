#ifndef TAILSTATE_H
#define TAILSTATE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct ts_store ts_store;

/* TS_DONE and TS_ROW are the two successful results of ts_next and ts_get. */
enum {
    TS_OK = 0,
    TS_DONE = 0,
    TS_ROW = 1,
    TS_INVALID = -1,
    TS_RANGE = -2,
    TS_OVERFLOW = -3,
    TS_STALE = -4
};

typedef struct {
    uint64_t seq;
    uint64_t value;
    uint32_t key;
    int deleted;
} ts_change;

typedef void (*ts_visit)(const ts_change *change, void *context);

/* Stack-allocated iterator. Initialize with ts_begin, leave fields untouched,
 * and keep its store alive until the final ts_next call. */
typedef struct {
    const ts_store *store;
    uint64_t version;
    uint32_t next_key;
} ts_iter;

/* Capacity must be positive. Valid keys are 0..capacity-1. Returns NULL for
 * an invalid capacity, unrepresentable allocation size, or allocation failure.
 * The caller serializes writers with all readers. */
ts_store *ts_create(uint32_t capacity);
/* Same contract, plus a preallocated prefix index for arbitrary numeric
 * cursors. Seeks perform at most two 58-level tree descents and scan at
 * most 64 rows. On typical 64-bit systems, this reserves an additional
 * 24 bytes per possible binary branch (at most capacity-1 branches)
 * and a ring of recent key IDs (at most four additional bytes per key).
 * Changes to epoch representatives may also traverse the index on write. */
ts_store *ts_create_indexed(uint32_t capacity);
void ts_destroy(ts_store *store);

/* Sequence numbers start at one. Zero is the empty store's head.
 * A cursor is meaningful only for the store lifetime in which it was issued. */
uint64_t ts_head(const ts_store *store);

/* Live allocation sizes requested by this index, excluding allocator metadata. */
size_t ts_store_bytes(const ts_store *store);

/* Every successful mutation advances the head, including identical puts and
 * deletion of an untouched key. At UINT64_MAX, writes fail with TS_OVERFLOW.
 * Errors leave the index unchanged. */
int ts_put(ts_store *store, uint32_t key, uint64_t value);
int ts_delete(ts_store *store, uint32_t key);

/* Return TS_ROW for a live value or a tombstone, TS_DONE for an untouched key.
 * Tombstones carry value zero. */
int ts_get(const ts_store *store, uint32_t key, ts_change *out);

/* Iterate the latest mutation for each key changed after cursor, in ascending
 * sequence order. A cursor ahead of the head is TS_RANGE. With ts_create,
 * begin takes O(k+1)
 * for k matching keys; each next takes O(1). After a write, next reports
 * TS_STALE, even if the iterator was exhausted. Resume from the last emitted
 * sequence with ts_begin to obtain a fresh, coalesced view. */
int ts_begin(const ts_store *store, uint64_t cursor, ts_iter *iter);

/* After consuming part of a stream, retain the last row's (seq,key). Use it
 * here after intervening writes to seek in O(1) if the slot is unchanged.
 * All hints, including stale or invalid keys, produce the same rows as
 * ts_begin(store,cursor,iter). */
int ts_resume(const ts_store *store, uint64_t cursor, uint32_t last_key, ts_iter *iter);
int ts_next(ts_iter *iter, ts_change *out);

/* Synchronous iteration. The change pointer is valid inside the callback only.
 * The callback must not mutate or destroy the store. */
int ts_since(const ts_store *store, uint64_t cursor, ts_visit visit, void *context);

#ifdef __cplusplus
}
#endif

#endif
