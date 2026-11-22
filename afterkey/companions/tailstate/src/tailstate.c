#include "tailstate.h"

#include <stdlib.h>

#define TS_NONE UINT32_MAX

typedef struct {
    uint64_t seq;
    uint64_t value;
    uint32_t prev;
    uint32_t next;
} ts_slot;

/* A Patricia tree over 64-write epochs. A leaf names the FIRST live row
 * in its epoch. Each branch splits an integer prefix at a fixed boundary:
 * its low child has epochs < split, its high child has epochs >= split.
 * Branch bits strictly decrease along any root-to-leaf path. */
typedef struct {
    uint32_t id;
    int leaf;
} ts_edge;

typedef struct {
    uint64_t split;
    ts_edge low;
    ts_edge high;
} ts_branch;

typedef struct {
    ts_edge root;
    uint32_t used;
    uint32_t free_id;
    uint32_t recent_mask;
    uint32_t *recent;
    ts_branch branches[];
} ts_index;

struct ts_store {
    ts_slot *slots;
    unsigned char *deleted;
    ts_index *index;
    uint64_t head;
    size_t bytes;
    uint32_t capacity;
    uint32_t first;
    uint32_t last;
};

ts_store *ts_create(uint32_t capacity)
{
    size_t bitmap_size = (size_t)(capacity / 8) + (capacity % 8 != 0);
    if (capacity == 0 || bitmap_size > SIZE_MAX - sizeof(ts_store))
        return NULL;
    if (capacity > (SIZE_MAX - sizeof(ts_store) - bitmap_size) / sizeof(ts_slot))
        return NULL;

    ts_store *store = malloc(sizeof(*store));
    if (!store)
        return NULL;
    store->index = NULL;
    store->slots = calloc(capacity, sizeof(*store->slots));
    store->deleted = calloc(bitmap_size, sizeof(*store->deleted));
    if (!store->slots || !store->deleted) {
        ts_destroy(store);
        return NULL;
    }
    store->head = 0;
    store->bytes = sizeof(*store) + (size_t)capacity * sizeof(ts_slot) + bitmap_size;
    store->capacity = capacity;
    store->first = TS_NONE;
    store->last = TS_NONE;
    return store;
}

ts_store *ts_create_indexed(uint32_t capacity)
{
    ts_store *store = ts_create(capacity);
    if (!store)
        return NULL;

    /* At most capacity leaves, hence at most capacity-1 binary branches. */
    size_t count = capacity - 1u;
    if (sizeof(ts_index) > SIZE_MAX - store->bytes ||
        count > (SIZE_MAX - store->bytes - sizeof(ts_index)) / sizeof(ts_branch)) {
        ts_destroy(store);
        return NULL;
    }
    size_t bytes = sizeof(ts_index) + count * sizeof(ts_branch);
    /* The recent ring is optional to the algorithm. Its power-of-two size
     * permits addressing by a mask on the write path. */
    uint32_t ring = 1;
    while (ring <= capacity / 2)
        ring *= 2;
    if (ring > (SIZE_MAX - store->bytes - bytes) / sizeof(uint32_t)) {
        ts_destroy(store);
        return NULL;
    }
    ts_index *index = malloc(bytes);
    if (!index) {
        ts_destroy(store);
        return NULL;
    }
    index->recent = calloc(ring, sizeof(uint32_t));
    if (!index->recent) {
        free(index);
        ts_destroy(store);
        return NULL;
    }
    index->recent_mask = ring - 1u;
    index->root = (ts_edge){ TS_NONE, 1 };
    index->used = 0;
    index->free_id = TS_NONE;
    store->index = index;
    store->bytes += bytes + (size_t)ring * sizeof(uint32_t);
    return store;
}

void ts_destroy(ts_store *store)
{
    if (store) {
        if (store->index)
            free(store->index->recent);
        free(store->index);
        free(store->deleted);
        free(store->slots);
        free(store);
    }
}

uint64_t ts_head(const ts_store *store)
{
    return store ? store->head : 0;
}

size_t ts_store_bytes(const ts_store *store)
{
    return store ? store->bytes : 0;
}

static uint64_t epoch_of(uint64_t seq)
{
    return (seq - 1) >> 6;
}

/* Return the single most significant set bit. Value is nonzero here. */
static uint64_t highest_bit(uint64_t value)
{
    value |= value >> 1;
    value |= value >> 2;
    value |= value >> 4;
    value |= value >> 8;
    value |= value >> 16;
    value |= value >> 32;
    return value - (value >> 1);
}

/* The greatest epoch only increases at insertion. Splice one branch on
 * the right spine at the highest bit where new and previous maxima differ. */
static void append_epoch(ts_store *store, uint64_t epoch, uint32_t key)
{
    ts_index *index = store->index;
    ts_edge leaf = { key, 1 };
    if (index->root.id == TS_NONE) {
        index->root = leaf;
        return;
    }

    ts_edge last = index->root;
    while (!last.leaf)
        last = index->branches[last.id].high;
    uint64_t bit = highest_bit(epoch_of(store->slots[last.id].seq) ^ epoch);
    ts_edge *edge = &index->root;
    while (!edge->leaf) {
        ts_branch *branch = &index->branches[edge->id];
        uint64_t split_bit = branch->split & (0u - branch->split);
        if (split_bit < bit)
            break;
        edge = &branch->high;
    }

    uint32_t id = index->free_id;
    if (id == TS_NONE)
        id = index->used++;
    else
        index->free_id = index->branches[id].low.id;
    index->branches[id] = (ts_branch){ epoch & ~(bit - 1), *edge, leaf };
    *edge = (ts_edge){ id, 0 };
}

/* The old representative moves to the tail. Replace it with its successor
 * within the epoch, or splice out the leaf and its parent if none survive. */
static void move_epoch_start(ts_store *store, uint64_t epoch, uint32_t replacement)
{
    ts_index *index = store->index;
    ts_edge *edge = &index->root;
    ts_edge *parent = NULL;
    ts_edge sibling = { TS_NONE, 1 };
    while (!edge->leaf) {
        ts_branch *branch = &index->branches[edge->id];
        parent = edge;
        if (epoch < branch->split) {
            edge = &branch->low;
            sibling = branch->high;
        } else {
            edge = &branch->high;
            sibling = branch->low;
        }
    }

    if (replacement != TS_NONE || !parent) {
        *edge = (ts_edge){ replacement, 1 };
    } else {
        uint32_t removed = parent->id;
        *parent = sibling;
        index->branches[removed].low.id = index->free_id;
        index->free_id = removed;
    }
}

static void update_index(ts_store *store, uint32_t key)
{
    ts_slot *slot = &store->slots[key];
    uint64_t new_epoch = store->head >> 6;
    store->index->recent[store->head & store->index->recent_mask] = key;
    if (slot->seq) {
        uint64_t old_epoch = epoch_of(slot->seq);
        if (slot->prev == TS_NONE ||
            epoch_of(store->slots[slot->prev].seq) != old_epoch) {
            uint32_t successor = slot->next;
            if (successor != TS_NONE &&
                epoch_of(store->slots[successor].seq) == old_epoch)
                move_epoch_start(store, old_epoch, successor);
            else if (old_epoch != new_epoch)
                move_epoch_start(store, old_epoch, TS_NONE);
        }
    }
    if ((store->head & 63u) == 0)
        append_epoch(store, new_epoch, key);
}

/* Numeric lower bound needs one descent and, when a compressed prefix
 * skips the requested epoch, one descent into the nearest right sibling. */
static uint32_t seek_index(const ts_store *store, uint64_t cursor)
{
    const ts_index *index = store->index;
    /* Each entry identifies the writer of one recent sequence. A slot that
     * no longer carries that sequence has been superseded. Probing four
     * consecutive positions is sufficient to establish the earliest match
     * on a hit. The tree supplies the exact result on every miss. */
    uint64_t lag = store->head - cursor;
    if (lag - 1 <= index->recent_mask) {
        for (unsigned i = 0; i < 4 && i < lag; ++i) {
            uint64_t pos = cursor + i;
            uint32_t key = index->recent[pos & index->recent_mask];
            if (store->slots[key].seq == pos + 1)
                return key;
        }
    }
    uint64_t target = cursor >> 6;
    ts_edge edge = index->root;
    ts_edge successor = { TS_NONE, 1 };
    while (!edge.leaf) {
        const ts_branch *branch = &index->branches[edge.id];
        if (target < branch->split) {
            successor = branch->high;
            edge = branch->low;
        } else {
            edge = branch->high;
        }
    }

    if (epoch_of(store->slots[edge.id].seq) < target) {
        edge = successor;
        while (!edge.leaf)
            edge = index->branches[edge.id].low;
    }
    uint32_t key = edge.id;
    while (key != TS_NONE && store->slots[key].seq <= cursor)
        key = store->slots[key].next;
    return key;
}

static int write_change(ts_store *store, uint32_t key, uint64_t value, int deleted)
{
    if (!store)
        return TS_INVALID;
    if (key >= store->capacity)
        return TS_RANGE;
    if (store->head == UINT64_MAX)
        return TS_OVERFLOW;

    ts_slot *slot = &store->slots[key];
    if (store->index)
        update_index(store, key);
    if (store->last != key) {
        if (slot->seq != 0) {
            /* A previously written non-tail key always has a successor. */
            store->slots[slot->next].prev = slot->prev;
            if (slot->prev == TS_NONE)
                store->first = slot->next;
            else
                store->slots[slot->prev].next = slot->next;
        }
        slot->prev = store->last;
        slot->next = TS_NONE;
        if (store->last == TS_NONE)
            store->first = key;
        else
            store->slots[store->last].next = key;
        store->last = key;
    }

    slot->seq = ++store->head;
    slot->value = value;
    unsigned char mask = (unsigned char)(1u << (key % 8));
    if (deleted)
        store->deleted[key / 8] |= mask;
    else
        store->deleted[key / 8] &= (unsigned char)~mask;
    return TS_OK;
}

int ts_put(ts_store *store, uint32_t key, uint64_t value)
{
    return write_change(store, key, value, 0);
}

int ts_delete(ts_store *store, uint32_t key)
{
    return write_change(store, key, 0, 1);
}

static void read_change(const ts_store *store, uint32_t key, ts_change *out)
{
    const ts_slot *slot = &store->slots[key];
    out->key = key;
    out->seq = slot->seq;
    out->value = slot->value;
    out->deleted = (store->deleted[key / 8] >> (key % 8)) & 1;
}

int ts_get(const ts_store *store, uint32_t key, ts_change *out)
{
    if (!store || !out)
        return TS_INVALID;
    if (key >= store->capacity)
        return TS_RANGE;
    if (store->slots[key].seq == 0)
        return TS_DONE;
    read_change(store, key, out);
    return TS_ROW;
}

int ts_begin(const ts_store *store, uint64_t cursor, ts_iter *iter)
{
    if (!store || !iter)
        return TS_INVALID;
    if (cursor > store->head)
        return TS_RANGE;

    uint32_t first = store->first;
    if (cursor == store->head) {
        first = TS_NONE;
    } else if (cursor != 0 && cursor >= store->slots[first].seq) {
        if (store->index && store->head - cursor > 64) {
            first = seek_index(store, cursor);
        } else {
            uint32_t key = store->last;
            /* There is at least one key with seq <= cursor (the first). */
            while (store->slots[key].seq > cursor)
                key = store->slots[key].prev;
            first = store->slots[key].next;
        }
    }
    iter->store = store;
    iter->version = store->head;
    iter->next_key = first;
    return TS_OK;
}

int ts_resume(const ts_store *store, uint64_t cursor, uint32_t last_key, ts_iter *iter)
{
    if (!store || !iter)
        return TS_INVALID;
    if (cursor != 0 && last_key < store->capacity &&
        store->slots[last_key].seq == cursor) {
        iter->store = store;
        iter->version = store->head;
        iter->next_key = store->slots[last_key].next;
        return TS_OK;
    }
    return ts_begin(store, cursor, iter);
}

int ts_next(ts_iter *iter, ts_change *out)
{
    if (!iter || !out || !iter->store)
        return TS_INVALID;
    const ts_store *store = iter->store;
    if (iter->version != store->head)
        return TS_STALE;
    uint32_t key = iter->next_key;
    if (key == TS_NONE)
        return TS_DONE;
    read_change(store, key, out);
    iter->next_key = store->slots[key].next;
    return TS_ROW;
}

int ts_since(const ts_store *store, uint64_t cursor, ts_visit visit, void *context)
{
    if (!visit)
        return TS_INVALID;
    ts_iter iter;
    int status = ts_begin(store, cursor, &iter);
    if (status != TS_OK)
        return status;
    ts_change change;
    while ((status = ts_next(&iter, &change)) == TS_ROW)
        visit(&change, context);
    return status;
}
