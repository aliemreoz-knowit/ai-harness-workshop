/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/loopback/test_lb_burst.c
 * loopback burst loss: 50 consecutive datagrams dropped (DATA and NACKs alike).
 * The daemon must not exit, the receiver must not crash, and the stream
 * must come back.
 */
#include "lb_common.h"

void setUp(void)
{
}

void tearDown(void)
{
}

static void test_burst_50_once(void)
{
    ts_loop_cfg_t c;
    ts_netem_cfg_t n;
    ts_lb_window_t w;
    uint32_t before;
    lb_cfg(&c);
    lb_start(&c);

    ts_lb_run(&g_lb, 2000U);
    before = ts_lb_decoded(&g_lb, 0U);
    TEST_ASSERT_TRUE_MESSAGE(before > 0U, "nothing decoded before the burst");
    n = g_L.cfg.netem;
    n.burst_len = 50U;
    ts_loop_set_netem(&g_L, &n);
    ts_lb_run(&g_lb, 2000U);                  /* burst + recovery */
    ts_lb_window(&g_lb, 3000U, &w);
    lb_print_window("burst50", &w);
    lb_print_rx("burst50", 0U);
    lb_print_netem("burst50");

    lb_assert_invariants(1U);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(50U, ts_netem_stats(&g_L.netem)->dropped_burst, "burst not applied");
    TEST_ASSERT_TRUE_MESSAGE(ts_lb_decoded(&g_lb, 0U) > before, "no frame decoded after the burst");
    TEST_ASSERT_TRUE_MESSAGE(w.min_fps_1s[0] >= 25U, "stream did not recover within 2 s of a 50-packet burst");
    lb_stop();
}

static void test_burst_50_every_second(void)
{
    ts_loop_cfg_t c;
    ts_lb_window_t w;
    lb_cfg(&c);
    c.netem.burst_len = 50U;
    c.netem.burst_period_ms = 1000U;
    c.netem.seed = 3U;
    lb_start(&c);

    ts_lb_run(&g_lb, 2000U);
    ts_lb_window(&g_lb, 6000U, &w);
    lb_print_window("burst50/s", &w);
    lb_print_rx("burst50/s", 0U);
    lb_print_netem("burst50/s");

    lb_assert_invariants(1U);
    TEST_ASSERT_TRUE_MESSAGE(ts_netem_stats(&g_L.netem)->dropped_burst >= 350U, "periodic bursts not applied");
    TEST_ASSERT_TRUE_MESSAGE(w.min_fps_1s[0] > 0U, "a whole second without a decoded frame under periodic bursts");
    lb_stop();
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_burst_50_once);
    RUN_TEST(test_burst_50_every_second);
    return UNITY_END();
}
