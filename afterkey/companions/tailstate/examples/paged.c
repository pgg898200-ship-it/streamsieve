/* A worker mirrors the latest advertised capacity of each pool, two rows at a
 * time. The worker and publisher run serially here. In a server, serialize each
 * page with writes and retain this checkpoint per worker, scoped to its store. */
#include "tailstate.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>

enum { POOLS = 4, PAGE_ROWS = 2 };

typedef struct {
    uint64_t applied_seq;
    uint32_t last_key;
    uint64_t capacity[POOLS];
} worker;

static int pull_page(const ts_store *publisher, worker *replica)
{
    ts_iter it;
    int status = replica->applied_seq
                     ? ts_resume(publisher, replica->applied_seq, replica->last_key, &it)
                     : ts_begin(publisher, 0, &it);
    if (status != TS_OK)
        return status;
    for (unsigned i = 0; i < PAGE_ROWS; ++i) {
        ts_change row;
        status = ts_next(&it, &row);
        if (status != TS_ROW)
            return status;
        /* Idempotent assignment. In a durable consumer, commit this state and
         * its checkpoint together before acknowledging the page. */
        replica->capacity[row.key] = row.deleted ? 0 : row.value;
        replica->applied_seq = row.seq;
        replica->last_key = row.key;
        printf("seq=%" PRIu64 " pool=%" PRIu32 " available=%" PRIu64 "\n",
               row.seq, row.key, replica->capacity[row.key]);
    }
    return TS_OK;
}

int main(void)
{
    worker replica = {0};
    ts_store *publisher = ts_create(POOLS);
    if (!publisher)
        return EXIT_FAILURE;
    int failed = 1;
    for (uint32_t pool = 0; pool < POOLS; ++pool)
        if (ts_put(publisher, pool, 8) != TS_OK)
            goto done;

    if (pull_page(publisher, &replica) != TS_OK)
        goto done;
    /* Pool 1 was the worker's anchor. It publishes a replacement while the
     * worker is away. Pool 2 withdraws before its old capacity was delivered. */
    if (ts_put(publisher, 1, 16) != TS_OK || ts_delete(publisher, 2) != TS_OK)
        goto done;
    while (replica.applied_seq < ts_head(publisher))
        if (pull_page(publisher, &replica) != TS_OK)
            goto done;
    failed = !(replica.capacity[0] == 8 && replica.capacity[1] == 16 &&
               replica.capacity[2] == 0 && replica.capacity[3] == 8);
done:
    ts_destroy(publisher);
    return failed ? EXIT_FAILURE : EXIT_SUCCESS;
}
