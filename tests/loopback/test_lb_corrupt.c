/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/loopback/test_lb_corrupt.c
 * loopback corruption: bit flips in headers / payloads and truncated datagrams in
 * both directions (DATA and NACK). Nothing may crash (ASan in sanitizers), every
 * corrupted datagram is rejected (CRC / bounds) so no wrong picture is ever
 * shown, and the stream keeps going.
 */
#include "lb_common.h"

void setUp(void)
{
}

void tearDown(void)
{
}

static void run_corrupt(const char *tag, uint8_t flip_pct, uint8_t scope, uint8_t trunc_pct)
{
    ts_loop_cfg_t c;
    ts_lb_window_t w;
    const tsr_metrics_t *m;
    lb_cfg(&c);
    c.netem.bitflip_pct = flip_pct;
    c.netem.bitflip_scope = scope;
    c.netem.truncate_pct = trunc_pct;
    c.netem.seed = 0xC0FFEEU + (uint32_t)scope;
    lb_start(&c);

    ts_lb_run(&g_lb, 2000U);
    ts_lb_window(&g_lb, 6000U, &w);
    m = tsr_metrics(g_L.rx[0]);
    lb_print_window(tag, &w);
    lb_print_rx(tag, 0U);
    lb_print_netem(tag);

    lb_assert_invariants(1U);
    TEST_ASSERT_TRUE_MESSAGE((ts_netem_stats(&g_L.netem)->flipped + ts_netem_stats(&g_L.netem)->truncated) > 0U,
                             "netem corrupted nothing");
    TEST_ASSERT_TRUE_MESSAGE((m->crc_fail + m->parse_fail) > 0U, "receiver rejected no corrupted datagram");
    TEST_ASSERT_TRUE_MESSAGE(w.min_fps_1s[0] > 0U, "a whole second without a decoded frame under corruption");
    TEST_ASSERT_TRUE_MESSAGE(w.fps_x100[0] >= 1500U, "fps < 15 under 2 % corruption");
    lb_stop();
}

static void test_bitflip_anywhere_and_truncation(void)
{
    run_corrupt("flip-any", 2U, (uint8_t)TS_NETEM_FLIP_ANY, 1U);
}

static void test_bitflip_headers(void)
{
    run_corrupt("flip-hdr", 2U, (uint8_t)TS_NETEM_FLIP_HEADER, 0U);
}

static void test_bitflip_payloads(void)
{
    run_corrupt("flip-payload", 2U, (uint8_t)TS_NETEM_FLIP_PAYLOAD, 0U);
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_bitflip_anywhere_and_truncation);
    RUN_TEST(test_bitflip_headers);
    RUN_TEST(test_bitflip_payloads);
    return UNITY_END();
}
