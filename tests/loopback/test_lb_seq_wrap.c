/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/loopback/test_lb_seq_wrap.c
 * loopback sequence wrap (short public check): --seq-start 65000, so pkt_seq wraps
 * within the first seconds and frame_seq after 536 frames (~18 s). Every u16
 * counter must be compared with serial arithmetic (PROTOCOL.md 7): the stream
 * runs across both wraps with zero freezes. The link is lightly lossy so the
 * NACK / ring lookup path is exercised across the wrap too; 2 ms virtual
 * ticks keep the run short.
 */
#include "lb_common.h"

#define WRAP_SEQ_START  65000U
#define WRAP_RUN_MS     20000U     /* frame_seq wraps at ~17.9 s (536 frames at 30 fps) */

void setUp(void)
{
}

void tearDown(void)
{
}

static void test_seq_wrap_frame_and_pkt_seq_no_freeze(void)
{
    ts_loop_cfg_t c;
    const tsr_metrics_t *m;
    uint32_t last_decoded = 0U;
    uint32_t stalled_s = 0U;
    uint32_t pkts;

    lb_cfg(&c);
    c.tick_ms = 2U;
    c.daemon.seq_start = (uint16_t)WRAP_SEQ_START;
    c.netem.loss_pct = 1U;
    c.netem.seed = 0xFFFFU;
    lb_start(&c);

    for (uint32_t t = 0U; (t < WRAP_RUN_MS) && (stalled_s < 3U); t += 1000U) {
        uint32_t d;
        ts_lb_run(&g_lb, 1000U);
        d = ts_lb_decoded(&g_lb, 0U);
        stalled_s = (d == last_decoded) ? (stalled_s + 1U) : 0U;
        last_decoded = d;
    }
    m = tsr_metrics(g_L.rx[0]);
    pkts = tsd_stats(g_L.d)->pkts_sent;
    (void)printf("  [seqwrap] %lu fresh packets, next frame_seq %u, last decoded frame_seq %u\n",
                 (unsigned long)pkts, (unsigned)g_L.d->frame_seq_next, (unsigned)g_lb.rx[0].last_seq);
    lb_print_rx("seqwrap", 0U);
    lb_print_netem("seqwrap");

    lb_assert_invariants(1U);
    TEST_ASSERT_TRUE_MESSAGE(stalled_s < 3U, "permanent freeze (no frame decoded for 3 s) across a sequence wrap");
    TEST_ASSERT_TRUE_MESSAGE(pkts > (65536U - WRAP_SEQ_START), "pkt_seq never wrapped");
    TEST_ASSERT_TRUE_MESSAGE(g_lb.rx[0].last_seq < 1000U, "frame_seq never wrapped (or the receiver stopped before it)");
    TEST_ASSERT_TRUE(m->frames_decoded > 560U);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, m->freezes, "freeze across the sequence wrap");
    TEST_ASSERT_TRUE_MESSAGE(g_lb.rx[0].max_gap_ms < TS_FREEZE_MS, "decode gap >= TS_FREEZE_MS across the wrap");
    lb_stop();
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_seq_wrap_frame_and_pkt_seq_no_freeze);
    return UNITY_END();
}
