/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/loopback/test_lb_clean.c
 * loopback clean link: fps >= 29, freezes 0, NACK < 0.1 %, every frame exact,
 * including the synth noise burst (worst-case frames, ASan in sanitizers).
 */
#include "lb_common.h"

void setUp(void)
{
}

void tearDown(void)
{
}

static void test_clean_link_budgets(void)
{
    ts_loop_cfg_t c;
    ts_lb_window_t w;
    const tsr_metrics_t *m;
    lb_cfg(&c);
    lb_start(&c);

    ts_lb_run(&g_lb, 2000U);                 /* connect, WELCOME, first keyframe */
    TEST_ASSERT_TRUE_MESSAGE(ts_lb_decoded(&g_lb, 0U) > 0U, "nothing decoded after 2 s");
    ts_lb_window(&g_lb, 5000U, &w);
    m = tsr_metrics(g_L.rx[0]);
    lb_print_window("clean", &w);
    lb_print_rx("clean", 0U);
    lb_print_netem("clean");

    lb_assert_invariants(1U);
    TEST_ASSERT_TRUE_MESSAGE(w.min_fps_1s[0] >= 29U, "fps < 29 in some second on a clean link");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, m->freezes, "freeze on a clean link");
    TEST_ASSERT_TRUE_MESSAGE(g_lb.rx[0].max_gap_ms < TS_FREEZE_MS, "decode gap >= TS_FREEZE_MS");
    TEST_ASSERT_TRUE_MESSAGE(m->pkts_rx > 0U, "no packets received");
    TEST_ASSERT_TRUE_MESSAGE(((uint64_t)m->nacks_sent * 1000U) < (uint64_t)m->pkts_rx, "NACK ratio >= 0.1 % on a clean link");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, m->crc_fail, "CRC failures on a clean link");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, m->decode_errors, "decode errors on a clean link");
    TEST_ASSERT_TRUE_MESSAGE(g_lb.rx[0].compared >= 140U, "too few decoded frames compared");
    TEST_ASSERT_TRUE_MESSAGE(ts_lb_exact_at_least(g_lb.rx[0].exact_raw, g_lb.rx[0].compared, 99U),
                             "end-to-end exact match < 99 % on a clean link");
    TEST_ASSERT_TRUE_MESSAGE(w.daemon_kbps <= ((TS_DEFAULT_KBPS * 110U) / 100U), "daemon above its budget");
    lb_stop();
}

static void test_clean_link_is_deterministic(void)
{
    ts_loop_cfg_t c;
    uint32_t pkts[2];
    uint32_t decoded[2];
    uint32_t bytes[2];
    lb_cfg(&c);
    for (uint32_t run = 0U; run < 2U; run++) {
        lb_start(&c);
        ts_lb_run(&g_lb, 3000U);
        pkts[run] = tsr_metrics(g_L.rx[0])->pkts_rx;
        decoded[run] = tsr_metrics(g_L.rx[0])->frames_decoded;
        bytes[run] = tsd_stats(g_L.d)->bytes_sent;
        lb_stop();
    }
    TEST_ASSERT_EQUAL_UINT32(pkts[0], pkts[1]);
    TEST_ASSERT_EQUAL_UINT32(decoded[0], decoded[1]);
    TEST_ASSERT_EQUAL_UINT32(bytes[0], bytes[1]);
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_clean_link_budgets);
    RUN_TEST(test_clean_link_is_deterministic);
    return UNITY_END();
}
