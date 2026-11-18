/*
 * tinystream v2 -- HIDDEN eval test (copy into tests/loopback/ of the branch under evaluation)
 * loopback sequence wrap, long hidden variant: --seq-start 65000 so frame_seq / pkt_seq wrap within the
 * first seconds, then at least 100 000 fresh packets (pkt_seq wraps twice)
 * with zero freezes (the ticket says what is wrong).
 * 2 ms virtual ticks keep the run short; the link is lightly lossy so the
 * NACK / ring lookup path is exercised across the wrap too, and at every
 * wrap the fresh copy of the last pkt_seq before it (65535) is dropped, so
 * each wrap needs a repair whatever the random loss pattern does.
 */
#include "lb_common.h"

#define WRAP_PKTS       100000U
#define WRAP_MAX_MS     1800000U   /* virtual-time cap (synth at ~5.5 pkts/frame needs ~600 s) */

static uint32_t g_wrap_drops;

void setUp(void)
{
}

void tearDown(void)
{
}

/* Drop fresh DATA packets with pkt_seq 65535 (retransmissions pass). */
static bool wrap_drop_fn(void *ctx, const ts_io_dgram_t *d)
{
    bool pass = true;
    (void)ctx;
    if ((d->len >= TS_HDR_LEN) && (ts_rd16(&d->data[0]) == (uint16_t)TS_MAGIC_DATA) &&
        (((uint32_t)d->data[3] & TS_F_RETRANSMIT) == 0U) && (ts_rd16(&d->data[6]) == 65535U)) {
        pass = false;
        g_wrap_drops++;
    }
    return pass;
}

static void test_seq_wrap_100k_packets_no_freeze(void)
{
    ts_loop_cfg_t c;
    const tsr_metrics_t *m;
    uint32_t elapsed = 0U;
    uint32_t pkts = 0U;
    uint32_t last_decoded = 0U;
    uint32_t stalled_s = 0U;

    lb_cfg(&c);
    c.tick_ms = 2U;
    c.daemon.seq_start = 65000U;
    c.daemon.kbps_client = TS_GLOBAL_KBPS;   /* as many packets per second as the global cap allows */
    c.netem.loss_pct = 1U;
    c.netem.seed = 0xFFFFU;
    g_wrap_drops = 0U;
    lb_start(&c);
    ts_loop_set_tap(&g_L, wrap_drop_fn, NULL);

    while ((pkts < WRAP_PKTS) && (elapsed < WRAP_MAX_MS) && (stalled_s < 3U)) {
        uint32_t d;
        ts_lb_run(&g_lb, 1000U);
        elapsed += 1000U;
        pkts = tsd_stats(g_L.d)->pkts_sent;
        d = ts_lb_decoded(&g_lb, 0U);
        stalled_s = (d == last_decoded) ? (stalled_s + 1U) : 0U;
        last_decoded = d;
    }
    m = tsr_metrics(g_L.rx[0]);
    (void)printf("  [seqwrap] %lu fresh packets in %lu virtual ms, last frame_seq %u, dropped at the wrap %lu, too_old %lu\n",
                 (unsigned long)pkts, (unsigned long)elapsed, (unsigned)g_lb.rx[0].last_seq,
                 (unsigned long)g_wrap_drops, (unsigned long)tsd_stats(g_L.d)->nack_too_old);
    lb_print_rx("seqwrap", 0U);
    lb_print_netem("seqwrap");

    lb_assert_invariants(1U);
    TEST_ASSERT_TRUE_MESSAGE(stalled_s < 3U, "permanent freeze (no frame decoded for 3 s)");
    TEST_ASSERT_TRUE_MESSAGE(pkts >= WRAP_PKTS, "fewer than 100k fresh packets within the virtual-time cap");
    TEST_ASSERT_TRUE_MESSAGE(m->frames_decoded > 600U, "frame_seq never wrapped (fewer than 600 frames)");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, m->freezes, "freeze across the sequence wrap");
    TEST_ASSERT_TRUE_MESSAGE(g_lb.rx[0].max_gap_ms < TS_FREEZE_MS, "decode gap >= TS_FREEZE_MS across the wrap");
    /* checked last: a stalled run never reaches the second wrap, and the stall is the finding */
    TEST_ASSERT_TRUE_MESSAGE(g_wrap_drops >= 2U, "setup: pkt_seq did not wrap twice");
    lb_stop();
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_seq_wrap_100k_packets_no_freeze);
    return UNITY_END();
}
