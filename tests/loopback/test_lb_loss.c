/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/loopback/test_lb_loss.c
 * loopback 5 % independent loss (both directions): fps >= 20 and the daemon stays
 * within its per-client budget + 10 % including retransmissions.
 */
#include "lb_common.h"

#define BUDGET_KBPS 4000U

void setUp(void)
{
}

void tearDown(void)
{
}

static void test_loss_5pct_budgets(void)
{
    ts_loop_cfg_t c;
    ts_lb_window_t w;
    const tsr_metrics_t *m;
    lb_cfg(&c);
    c.daemon.kbps_client = BUDGET_KBPS;
    c.netem.loss_pct = 5U;
    c.netem.seed = 0x5EED0005U;
    lb_start(&c);

    ts_lb_run(&g_lb, 3000U);
    ts_lb_window(&g_lb, 7000U, &w);
    m = tsr_metrics(g_L.rx[0]);
    lb_print_window("loss5", &w);
    lb_print_rx("loss5", 0U);
    lb_print_netem("loss5");

    lb_assert_invariants(1U);
    TEST_ASSERT_TRUE_MESSAGE(ts_netem_stats(&g_L.netem)->dropped > 0U, "netem dropped nothing");
    TEST_ASSERT_TRUE_MESSAGE(w.fps_x100[0] >= 2000U, "fps < 20 at 5 % loss");
    TEST_ASSERT_TRUE_MESSAGE(w.daemon_kbps <= ((BUDGET_KBPS * 110U) / 100U), "daemon bitrate > budget + 10 % at 5 % loss");
    TEST_ASSERT_TRUE_MESSAGE(m->nacks_sent > 0U, "no NACKs at 5 % loss");
    TEST_ASSERT_TRUE_MESSAGE(m->retx_rx > 0U, "no retransmission received at 5 % loss");
    TEST_ASSERT_TRUE_MESSAGE(w.min_fps_1s[0] > 0U, "a whole second without a decoded frame");
    lb_stop();
}

static void test_loss_is_reproducible_with_a_seed(void)
{
    ts_loop_cfg_t c;
    uint32_t nacks[2];
    uint32_t decoded[2];
    uint32_t dropped[2];
    lb_cfg(&c);
    c.netem.loss_pct = 5U;
    c.netem.seed = 77U;
    for (uint32_t run = 0U; run < 2U; run++) {
        lb_start(&c);
        ts_lb_run(&g_lb, 3000U);
        nacks[run] = tsr_metrics(g_L.rx[0])->nacks_sent;
        decoded[run] = tsr_metrics(g_L.rx[0])->frames_decoded;
        dropped[run] = ts_netem_stats(&g_L.netem)->dropped;
        lb_stop();
    }
    TEST_ASSERT_EQUAL_UINT32(dropped[0], dropped[1]);
    TEST_ASSERT_EQUAL_UINT32(nacks[0], nacks[1]);
    TEST_ASSERT_EQUAL_UINT32(decoded[0], decoded[1]);
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_loss_5pct_budgets);
    RUN_TEST(test_loss_is_reproducible_with_a_seed);
    return UNITY_END();
}
