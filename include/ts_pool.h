/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- ts_pool.h
 * The daemon storage pool (one static object, sized with _Static_asserts) and
 * the "no dynamic memory after init" guard. The daemon binary links with
 * -Wl,--wrap=malloc/calloc/realloc/free (src/malloc_guard.c): after
 * ts_pool_seal() any allocation aborts. Gate no-heap additionally runs `nm -u` on
 * the daemon objects and fails on any reference to an allocating or
 * process-creating call.
 *
 * FROZEN API: additive, non-breaking changes only.
 */
#ifndef TS_POOL_H
#define TS_POOL_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "ts_config.h"
#include "ts_daemon.h"
#include "ts_io.h"

#ifdef __cplusplus
extern "C" {
#endif

/** The pool: daemon + I/O object + scratch. One instance, static, in src/pool/pool.c. */
typedef struct ts_pool {
    tsd_t        daemon;
    ts_io_posix_t io;                  /**< used by main.c; tests pass their own io */
    uint8_t      rx_buf[TS_IO_DGRAM_MAX];
    uint8_t      ctl_rx[TS_TLV_ACC_CAP];
    uint32_t     magic;                /**< TS_POOL_MAGIC after ts_pool_init */
    bool         sealed;
} ts_pool_t;

#define TS_POOL_MAGIC 0x504F4F4CU

/** The single static instance. */
ts_pool_t *ts_pool_get(void);
/** Zero everything, set magic. */
void   ts_pool_init(ts_pool_t *p);
/** Seal: malloc/calloc/realloc/free abort from now on (only when linked with the wrap). */
void   ts_pool_seal(ts_pool_t *p);
bool   ts_pool_sealed(const ts_pool_t *p);
/** sizeof(ts_pool_t), for the budgets .bss budget. */
size_t ts_pool_size(void);
/**
 * Additive (v2.0.1): end of initialisation for the daemon binary. Seals the
 * static pool (ts_pool_seal(ts_pool_get())); from here on the --wrap malloc
 * shim aborts on any allocation. tsd_create() calls ts_pool_seal() itself.
 */
void   ts_init_done(void);

/* Budget checks */
_Static_assert(sizeof(ts_txq_slot_t) <= 1456U, "ring slot stays compact");
_Static_assert(TS_RING_SLOTS * sizeof(ts_txq_slot_t) <= 700000U, "ring under 700 KB");

#ifdef __cplusplus
}
#endif

#endif /* TS_POOL_H */
