/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- src/pool/pool.c
 * The static storage pool of the daemon (.bss): the daemon state with its
 * history ring, codec reference and buffers, the POSIX I/O object and the
 * receive scratch. The encoded frame buffer is a separate static object in
 * src/net/daemon.c. The sizes below are checked at compile time so a change
 * that grows a buffer past its protocol bound or shrinks the raw frame double
 * buffer fails the build instead of corrupting memory at run time.
 *
 * Raw frame double buffer: the capture HAL stages bytes in
 * pool->daemon.cap.stage and publishes a complete frame into
 * pool->daemon.frame, the only buffer the encoder reads.
 *
 * After ts_pool_seal() / ts_init_done() the daemon binary's malloc shim
 * (src/malloc_guard.c, linked with -Wl,--wrap=malloc,calloc,realloc,free by
 * CMakeLists.txt) aborts on any allocation.
 */
#include <stddef.h>
#include <string.h>
#include "ts_pool.h"

static ts_pool_t g_pool;

/* ---- raw frame double buffer ---------------------------------------------- */
_Static_assert(sizeof(g_pool.daemon.cap.stage) == TS_FRAME_BYTES, "capture staging buffer = one gray8 frame");
_Static_assert(sizeof(g_pool.daemon.frame) == TS_FRAME_BYTES, "published raw frame = one gray8 frame");
_Static_assert(sizeof(g_pool.daemon.enc.ref) == TS_FRAME_BYTES, "encoder reference = one gray8 frame");

/* ---- packet buffers (protocol bounds) ------------------------------------------ */
_Static_assert(sizeof(g_pool.daemon.pkt_buf) >= TS_PKT_MAX, "retransmit scratch holds one DATA packet");
_Static_assert(sizeof(g_pool.daemon.pkt_buf) >= TS_NACK_PKT_MAX, "receive scratch holds one NACK packet");
_Static_assert(sizeof(g_pool.daemon.ctl_buf) >= (TS_TLV_HDR_LEN + TS_TLV_MAX_LEN), "TLV send scratch holds one max TLV");
_Static_assert(sizeof(g_pool.daemon.ctl_buf) >= (TS_TLV_HDR_LEN + TS_STATS_MAX_LEN), "TLV send scratch holds a full STATS");
_Static_assert(sizeof(g_pool.rx_buf) >= TS_PKT_MAX, "UDP receive scratch holds one DATA packet");
_Static_assert(sizeof(g_pool.rx_buf) >= TS_NACK_PKT_MAX, "UDP receive scratch holds one NACK packet");
_Static_assert(sizeof(g_pool.ctl_rx) >= TS_TLV_ACC_CAP, "TCP receive scratch covers one accumulate buffer");
_Static_assert(sizeof(g_pool.daemon.txq.slots[0].pkt) >= TS_PKT_MAX, "ring slot holds one DATA packet");

/* ---- tables -------------------------------------------------------------------- */
_Static_assert((sizeof(g_pool.daemon.txq.slots) / sizeof(g_pool.daemon.txq.slots[0])) == TS_RING_SLOTS, "ring slot count");
_Static_assert((sizeof(g_pool.daemon.clients.c) / sizeof(g_pool.daemon.clients.c[0])) == TS_MAX_CLIENTS, "client table size");
_Static_assert((sizeof(g_pool.io.tcp_fd) / sizeof(g_pool.io.tcp_fd[0])) >= (TS_MAX_CLIENTS + 1U), "a refused 5th client can still be accepted and closed");

/* ---- .bss budget (gate budgets) ------------------------------------------------------ */
#define TS_POOL_BUDGET_BYTES (1536U * 1024U)
_Static_assert(sizeof(ts_pool_t) <= TS_POOL_BUDGET_BYTES, "daemon pool stays under 1.5 MiB");

ts_pool_t *ts_pool_get(void)
{
    return &g_pool;
}

void ts_pool_init(ts_pool_t *p)
{
    if (p != NULL) {
        (void)memset(p, 0, sizeof(*p));
        p->magic = TS_POOL_MAGIC;
        p->sealed = false;
    }
}

void ts_pool_seal(ts_pool_t *p)
{
    if (p != NULL) {
        p->sealed = true;
    }
}

bool ts_pool_sealed(const ts_pool_t *p)
{
    return (p != NULL) ? p->sealed : false;
}

size_t ts_pool_size(void)
{
    return sizeof(ts_pool_t);
}

void ts_init_done(void)
{
    ts_pool_seal(&g_pool);
}
