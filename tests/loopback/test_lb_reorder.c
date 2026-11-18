/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/loopback/test_lb_reorder.c
 * loopback reorder and duplicates: datagrams held back or delivered twice. The
 * picture must stay exact (exact-match >= 99 %), reassembly must drop
 * duplicates, and a hold shorter than TS_NACK_DELAY_MS must not cause NACKs.
 */
#include "lb_common.h"

void setUp(void)
{
}

void tearDown(void)
{
}

static void run_reorder(uint8_t pct, uint32_t hold_ms, bool expect_no_nacks)
{
    ts_loop_cfg_t c;
    ts_lb_window_t w;
    const tsr_metrics_t *m;
    lb_cfg(&c);
    c.netem.reorder_pct = pct;
    c.netem.reorder_delay_ms = hold_ms;
    c.netem.seed = 0x0DE0U + (uint32_t)hold_ms;
    lb_start(&c);

    ts_lb_run(&g_lb, 1000U);
    ts_lb_window(&g_lb, 5000U, &w);
    m = tsr_metrics(g_L.rx[0]);
    lb_print_window("reorder", &w);
    lb_print_rx("reorder", 0U);
    lb_print_netem("reorder");

    lb_assert_invariants(1U);
    TEST_ASSERT_TRUE_MESSAGE(ts_netem_stats(&g_L.netem)->reordered > 0U, "netem reordered nothing");
    TEST_ASSERT_TRUE_MESSAGE(g_lb.rx[0].compared >= 120U, "too few decoded frames compared");
    TEST_ASSERT_TRUE_MESSAGE(ts_lb_exact_at_least(g_lb.rx[0].exact_raw, g_lb.rx[0].compared, 99U),
                             "exact-match < 99 % under reorder");
    TEST_ASSERT_TRUE_MESSAGE(w.fps_x100[0] >= 2500U, "fps < 25 under reorder");
    if (expect_no_nacks) {
        TEST_ASSERT_TRUE_MESSAGE(((uint64_t)m->nacks_sent * 1000U) < (uint64_t)m->pkts_rx,
                                 "a hold below TS_NACK_DELAY_MS produced NACKs");
    }
    lb_stop();
}

static void test_reorder_short_hold_no_nacks(void)
{
    run_reorder(10U, TS_NETEM_REORDER_DEFAULT_MS, true);
}

static void test_reorder_long_hold_repairs(void)
{
    run_reorder(5U, 40U, false);   /* held past the NACK delay: retransmit + late original (dup) */
}

static void test_duplicates_dropped(void)
{
    ts_loop_cfg_t c;
    ts_lb_window_t w;
    lb_cfg(&c);
    c.netem.dup_pct = 10U;
    c.netem.seed = 0xD0D0U;
    lb_start(&c);

    ts_lb_run(&g_lb, 1000U);
    ts_lb_window(&g_lb, 4000U, &w);
    lb_print_window("dup", &w);
    lb_print_rx("dup", 0U);

    lb_assert_invariants(1U);
    TEST_ASSERT_TRUE_MESSAGE(tsr_metrics(g_L.rx[0])->dups > 0U, "receiver counted no duplicates");
    TEST_ASSERT_TRUE_MESSAGE(ts_lb_exact_at_least(g_lb.rx[0].exact_raw, g_lb.rx[0].compared, 99U),
                             "exact-match < 99 % with duplicates");
    TEST_ASSERT_TRUE_MESSAGE(w.min_fps_1s[0] >= 29U, "fps < 29 with duplicates only");
    lb_stop();
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_reorder_short_hold_no_nacks);
    RUN_TEST(test_reorder_long_hold_repairs);
    RUN_TEST(test_duplicates_dropped);
    return UNITY_END();
}
