/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/support/ts_loop.h
 * Loopback harness: one daemon + up to TS_LOOP_MAX_RX receivers wired through
 * fake ts_io endpoints and the netem shim, driven by a virtual clock. No
 * sockets, no sleeps: every tick is deterministic.
 *
 *   daemon (tsd_tick) --udp_out--> [netem filter] --udp_in--> receiver (tsr_tick)
 *   daemon <--udp_in-- [netem filter] <--udp_out-- receiver        (NACKs)
 *   TCP control: fake connections, reliable, not filtered.
 *
 * Usage in a loopback test:
 *   ts_loop_t L; ts_loop_cfg_t c; ts_loop_cfg_default(&c); c.netem.loss_pct = 5;
 *   ts_loop_init(&L, &c);           // daemon lives in the static pool
 *   ts_loop_run(&L, 5000);          // 5 virtual seconds
 *   TEST_ASSERT(tsr_metrics(L.rx[0])->frames_decoded > 100);
 */
#ifndef TS_LOOP_H
#define TS_LOOP_H

#include <stdint.h>
#include <stdbool.h>
#include "ts_config.h"
#include "ts_io.h"
#include "ts_netem.h"
#include "ts_daemon.h"
#include "ts_pool.h"
#include "ts_receiver.h"

#ifdef __cplusplus
extern "C" {
#endif

#define TS_LOOP_MAX_RX 2U

typedef struct ts_loop_cfg {
    tsd_cfg_t      daemon;
    tsr_cfg_t      receiver;        /**< template; udp_port/server are set per receiver */
    ts_netem_cfg_t netem;
    uint8_t        receivers;       /**< 1..TS_LOOP_MAX_RX */
    uint32_t       tick_ms;         /**< virtual time per step (default 1) */
    uint32_t       start_ms;        /**< initial virtual clock (default 1000) */
} ts_loop_cfg_t;

typedef struct ts_loop {
    ts_loop_cfg_t cfg;
    uint32_t      now_ms;
    uint32_t      steps;
    ts_io_fake_t  io_d;                       /**< daemon endpoint 10.0.0.1 */
    ts_io_fake_t  io_r[TS_LOOP_MAX_RX];       /**< receiver endpoints 10.0.0.2x */
    ts_netem_t    netem;
    ts_pool_t    *pool;
    tsd_t        *d;                          /**< NULL when tsd_create failed */
    tsr_t         rx_storage[TS_LOOP_MAX_RX];
    tsr_t        *rx[TS_LOOP_MAX_RX];
    bool          ok;                         /**< everything created */
    /* additive: optional tap on the daemon's outgoing datagrams (ts_loop_set_tap) */
    bool        (*tap)(void *ctx, const ts_io_dgram_t *d);
    void         *tap_ctx;
    uint32_t      tap_dropped;                /**< datagrams the tap removed */
} ts_loop_t;

/**
 * Tap on the daemon side of the link: every datagram the daemon sent in a
 * step is offered to `fn` before it enters the netem shim; `fn` returns false
 * to drop it (a scenario's targeted loss, e.g. chosen fragments), true to let
 * it pass. NULL removes the tap. The tap sees datagrams in send order.
 */
typedef bool (*ts_loop_tap_fn_t)(void *ctx, const ts_io_dgram_t *d);
void ts_loop_set_tap(ts_loop_t *L, ts_loop_tap_fn_t fn, void *ctx);

/** Synth daemon, one receiver, clean link, 1 ms ticks. */
void ts_loop_cfg_default(ts_loop_cfg_t *c);
/** Build endpoints, link TCP, create daemon in the pool and receivers in `rx_storage`. */
void ts_loop_init(ts_loop_t *L, const ts_loop_cfg_t *c);
/** One virtual tick: daemon, shim, receivers, shim, clock += tick_ms. */
void ts_loop_step(ts_loop_t *L);
/** Run for `ms` virtual milliseconds. */
void ts_loop_run(ts_loop_t *L, uint32_t ms);
/** Change the link at run time (scenario steps). */
void ts_loop_set_netem(ts_loop_t *L, const ts_netem_cfg_t *cfg);
/** Stop daemon and receivers. */
void ts_loop_close(ts_loop_t *L);

#ifdef __cplusplus
}
#endif

#endif /* TS_LOOP_H */
