/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/support/ts_loop.c
 * Loopback harness glue (final): wires fake endpoints, netem shim, daemon and
 * receivers under one virtual clock. Contains no protocol logic.
 */
#include <stddef.h>
#include <string.h>
#include "ts_loop.h"

#define TS_LOOP_DAEMON_IP   0x0A000001U   /* 10.0.0.1 */
#define TS_LOOP_RX_IP_BASE  0x0A000020U   /* 10.0.0.32 + i */
#define TS_LOOP_RX_UDP_BASE 6000U

static void loop_step(ts_loop_t *L);

void ts_loop_cfg_default(ts_loop_cfg_t *c)
{
    if (c != NULL) {
        (void)memset(c, 0, sizeof(*c));
        tsd_cfg_default(&c->daemon);
        tsr_cfg_default(&c->receiver);
        c->receivers = 1U;
        c->tick_ms = 1U;
        c->start_ms = 1000U;
        c->netem.seed = 1U;
    }
}

static void loop_setup(ts_loop_t *L, const ts_loop_cfg_t *c);

void ts_loop_init(ts_loop_t *L, const ts_loop_cfg_t *c)
{
    if ((L != NULL) && (c != NULL)) {
        loop_setup(L, c);
    }
}

static void loop_setup(ts_loop_t *L, const ts_loop_cfg_t *c)
{
    (void)memset(L, 0, sizeof(*L));
    L->cfg = *c;
    if (L->cfg.receivers == 0U) {
        L->cfg.receivers = 1U;
    }
    if (L->cfg.receivers > TS_LOOP_MAX_RX) {
        L->cfg.receivers = TS_LOOP_MAX_RX;
    }
    if (L->cfg.tick_ms == 0U) {
        L->cfg.tick_ms = 1U;
    }
    L->now_ms = L->cfg.start_ms;

    /* endpoints */
    {
        ts_addr_t a = { TS_LOOP_DAEMON_IP, L->cfg.daemon.udp_port };
        ts_io_fake_init(&L->io_d, a);
        ts_io_fake_set_time(&L->io_d, L->now_ms);
    }
    for (uint8_t i = 0U; i < L->cfg.receivers; i++) {
        ts_addr_t a = { TS_LOOP_RX_IP_BASE + i, (uint16_t)(TS_LOOP_RX_UDP_BASE + i) };
        ts_io_fake_init(&L->io_r[i], a);
        ts_io_fake_set_time(&L->io_r[i], L->now_ms);
        ts_io_fake_link_tcp(&L->io_r[i], &L->io_d);
    }
    ts_netem_init(&L->netem, &L->cfg.netem, L->now_ms);

    /* daemon in the static pool */
    L->pool = ts_pool_get();
    ts_pool_init(L->pool);
    L->d = tsd_create(&L->cfg.daemon, &L->io_d.io, L->pool);
    L->ok = (L->d != NULL);

    /* receivers in caller storage (no heap) */
    for (uint8_t i = 0U; i < L->cfg.receivers; i++) {
        tsr_cfg_t rc = L->cfg.receiver;
        rc.server.ip = TS_LOOP_DAEMON_IP;
        rc.server.port = L->cfg.daemon.tcp_port;
        rc.udp_port = L->io_r[i].self.port;
        if (tsr_init(&L->rx_storage[i], &rc, &L->io_r[i].io) == TS_OK) {
            L->rx[i] = &L->rx_storage[i];
        } else {
            L->rx[i] = NULL;
            L->ok = false;
        }
    }
}

void ts_loop_step(ts_loop_t *L)
{
    if (L != NULL) {
        loop_step(L);
    }
}

/* Offer every queued daemon datagram to the tap once, in order; keep the ones it passes. */
static void loop_tap(ts_loop_t *L)
{
    static ts_io_dgram_t dg;   /* 1.5 KB scratch, kept off the stack */
    ts_io_dq_t *q = &L->io_d.udp_out;
    uint16_t n = q->count;
    for (uint16_t i = 0U; i < n; i++) {
        if (ts_io_fake_pop_udp(&L->io_d, &dg)) {
            if (L->tap(L->tap_ctx, &dg)) {
                q->q[((uint32_t)q->head + (uint32_t)q->count) % TS_IO_FAKE_QDEPTH] = dg;
                q->count++;
            } else {
                L->tap_dropped++;
            }
        }
    }
}

void ts_loop_set_tap(ts_loop_t *L, ts_loop_tap_fn_t fn, void *ctx)
{
    if (L != NULL) {
        L->tap = fn;
        L->tap_ctx = ctx;
    }
}

static void loop_step(ts_loop_t *L)
{
    ts_io_fake_t *leaves[TS_LOOP_MAX_RX];
    ts_io_fake_set_time(&L->io_d, L->now_ms);
    for (uint8_t i = 0U; i < L->cfg.receivers; i++) {
        ts_io_fake_set_time(&L->io_r[i], L->now_ms);
        leaves[i] = &L->io_r[i];
    }
    if (L->d != NULL) {
        (void)tsd_tick(L->d, L->now_ms);
    }
    if (L->tap != NULL) {
        loop_tap(L);
    }
    ts_netem_pump_star(&L->netem, &L->io_d, leaves, L->cfg.receivers, L->now_ms);
    for (uint8_t i = 0U; i < L->cfg.receivers; i++) {
        if (L->rx[i] != NULL) {
            (void)tsr_tick(L->rx[i], L->now_ms);
        }
    }
    ts_netem_pump_star(&L->netem, &L->io_d, leaves, L->cfg.receivers, L->now_ms);
    L->now_ms += L->cfg.tick_ms;
    L->steps++;
}

void ts_loop_run(ts_loop_t *L, uint32_t ms)
{
    if (L != NULL) {
        uint32_t remaining = ms;
        while (remaining > 0U) {
            uint32_t step = (L->cfg.tick_ms < remaining) ? L->cfg.tick_ms : remaining;
            ts_loop_step(L);
            remaining -= step;
        }
    }
}

void ts_loop_set_netem(ts_loop_t *L, const ts_netem_cfg_t *cfg)
{
    if ((L != NULL) && (cfg != NULL)) {
        L->cfg.netem = *cfg;
        ts_netem_set(&L->netem, cfg, L->now_ms);
    }
}

void ts_loop_close(ts_loop_t *L)
{
    if (L != NULL) {
        for (uint8_t i = 0U; i < L->cfg.receivers; i++) {
            if (L->rx[i] != NULL) {
                (void)tsr_send_bye(L->rx[i]);
                (void)tsr_tick(L->rx[i], L->now_ms);
            }
        }
        if (L->d != NULL) {
            tsd_stop(L->d);
        }
    }
}
