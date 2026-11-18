/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/loopback/lb_common.h
 * Shared helpers for the loopback scenarios (header only, test code: stdio
 * allowed). Each scenario file is its own ctest executable (label loopback):
 * one daemon and one or two receivers in-process, linked through the netem
 * shim, virtual clock, no sockets, no sleeps.
 */
#ifndef LB_COMMON_H
#define LB_COMMON_H

#include <stdio.h>
#include <string.h>
#include "unity.h"
#include "ts_config.h"
#include "ts_loop.h"
#include "ts_lbcheck.h"
#include "ts_netem.h"

/* One loop + checker per process (several MB: static). */
static ts_loop_t g_L;
static ts_lb_t   g_lb;

/** Defaults every scenario starts from: synth source, explicit budgets, clean link. */
static inline void lb_cfg(ts_loop_cfg_t *c)
{
    ts_loop_cfg_default(c);
    c->daemon.kbps_client = TS_DEFAULT_KBPS;
    c->daemon.kbps_global = TS_GLOBAL_KBPS;
    c->netem.seed = 1U;
}

/** Initialise the loop; fails the test with a clear reason when a module is still a stub. */
static inline void lb_start(const ts_loop_cfg_t *c)
{
    ts_loop_init(&g_L, c);
    TEST_ASSERT_NOT_NULL_MESSAGE(g_L.d, "tsd_create() returned NULL (daemon/capture/pool not implemented yet?)");
    TEST_ASSERT_NOT_NULL_MESSAGE(g_L.rx[0], "tsr_init() failed (receiver not implemented yet?)");
    TEST_ASSERT_TRUE_MESSAGE(g_L.ok, "loop not fully created");
    ts_lb_attach(&g_lb, &g_L);
}

static inline void lb_stop(void)
{
    ts_loop_close(&g_L);
}

static inline void lb_print_rx(const char *tag, uint8_t i)
{
    const tsr_t *r = g_L.rx[i];
    const ts_lb_rx_t *x = &g_lb.rx[i];
    if (r != NULL) {
        const tsr_metrics_t *m = tsr_metrics(r);
        (void)printf("  [%s rx%u] t=%lu decoded=%lu compared=%lu exact_raw=%lu exact_recon=%lu no_truth=%lu "
                     "max_gap=%lums fps_x100=%lu pkts=%lu lost=%lu nacks=%lu retx=%lu dups=%lu crc_fail=%lu "
                     "parse_fail=%lu dec_err=%lu keyreq=%lu freezes=%lu state=%d\n",
                     tag, (unsigned)i, (unsigned long)g_L.now_ms, (unsigned long)m->frames_decoded,
                     (unsigned long)x->compared, (unsigned long)x->exact_raw, (unsigned long)x->exact_recon,
                     (unsigned long)x->no_truth, (unsigned long)x->max_gap_ms, (unsigned long)m->fps_x100,
                     (unsigned long)m->pkts_rx, (unsigned long)m->pkts_lost, (unsigned long)m->nacks_sent,
                     (unsigned long)m->retx_rx, (unsigned long)m->dups, (unsigned long)m->crc_fail,
                     (unsigned long)m->parse_fail, (unsigned long)m->decode_errors,
                     (unsigned long)m->keyframe_reqs, (unsigned long)m->freezes, (int)tsr_state(r));
    }
}

static inline void lb_print_window(const char *tag, const ts_lb_window_t *w)
{
    (void)printf("  [%s window %lums] decoded=%lu/%lu fps_x100=%lu/%lu min1s=%lu/%lu daemon_kbps=%lu pkts=%lu retx=%lu\n",
                 tag, (unsigned long)w->ms, (unsigned long)w->decoded[0], (unsigned long)w->decoded[1],
                 (unsigned long)w->fps_x100[0], (unsigned long)w->fps_x100[1],
                 (unsigned long)w->min_fps_1s[0], (unsigned long)w->min_fps_1s[1],
                 (unsigned long)w->daemon_kbps, (unsigned long)w->daemon_pkts, (unsigned long)w->daemon_retx);
}

static inline void lb_print_netem(const char *tag)
{
    const ts_netem_stats_t *s = ts_netem_stats(&g_L.netem);
    (void)printf("  [%s netem] seen=%lu passed=%lu dropped=%lu burst=%lu thr=%lu cable=%lu dup=%lu reord=%lu "
                 "flip=%lu trunc=%lu held_ovf=%lu misrouted=%lu\n",
                 tag, (unsigned long)s->seen, (unsigned long)s->passed, (unsigned long)s->dropped,
                 (unsigned long)s->dropped_burst, (unsigned long)s->dropped_throttle, (unsigned long)s->dropped_cable,
                 (unsigned long)s->duplicated, (unsigned long)s->reordered, (unsigned long)s->flipped,
                 (unsigned long)s->truncated, (unsigned long)s->held_overflow, (unsigned long)s->misrouted);
}

/** Invariants that hold in every scenario: daemon alive, receivers alive, never a wrong picture. */
static inline void lb_assert_invariants(uint8_t receivers)
{
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, g_lb.daemon_stopped, "daemon stopped (exit) during the scenario");
    TEST_ASSERT_EQUAL_INT_MESSAGE(TSD_RUNNING, g_L.d->state, "daemon not running");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, g_lb.rx_closed, "a receiver closed during the scenario");
    for (uint8_t i = 0U; i < receivers; i++) {
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(g_lb.rx[i].compared, g_lb.rx[i].exact_recon,
                                         "a decoded frame differs from the encoder's reconstructed reference");
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, g_L.netem.st.misrouted, "datagram addressed to nobody");
    }
}

#endif /* LB_COMMON_H */
