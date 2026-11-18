/*
 * tinystream v2 -- hidden evaluation test: tests/loopback/test_lb_clean_link_eval.c
 * (evaluation companion of the public test_lb_clean.c, independent seed)
 *
 * On a clean link (no loss, no corruption) the receiver must accept every
 * packet: no CRC failures, essentially no NACKs and full frame rate. If the
 * sender's stored CRC does not match the receiver's exact-length CRC, the
 * unaligned fragment of each frame is dropped as a CRC failure although the
 * wire is clean, so the receiver NACKs it, the frame completes a round trip
 * late and the frame rate collapses.
 *
 * The defect: see the ticket.
 */
#include "lb_common.h"

void setUp(void)
{
}

void tearDown(void)
{
}

static void test_clean_link_has_no_crc_failures(void)
{
    ts_loop_cfg_t c;
    ts_lb_window_t w;
    const tsr_metrics_t *m;

    lb_cfg(&c);
    c.netem.seed = 0xC106U;          /* clean link: lb_cfg leaves loss/corruption at 0 */
    lb_start(&c);

    ts_lb_run(&g_lb, 2000U);
    ts_lb_window(&g_lb, 5000U, &w);
    m = tsr_metrics(g_L.rx[0]);
    lb_print_window("clean-crc", &w);
    lb_print_rx("clean-crc", 0U);
    lb_print_netem("clean-crc");

    lb_assert_invariants(1U);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, ts_netem_stats(&g_L.netem)->dropped, "the link dropped a packet (not clean)");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, m->crc_fail, "CRC failures on a clean link");
    TEST_ASSERT_TRUE_MESSAGE(m->pkts_rx > 0U, "no packets received");
    TEST_ASSERT_TRUE_MESSAGE(((uint64_t)m->nacks_sent * 1000U) < (uint64_t)m->pkts_rx,
                             "NACK ratio >= 0.1 % on a clean link");
    TEST_ASSERT_TRUE_MESSAGE(w.min_fps_1s[0] >= 29U, "fps < 29 in some second on a clean link");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, m->freezes, "freeze on a clean link");
    lb_stop();
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_clean_link_has_no_crc_failures);
    return UNITY_END();
}
