/*
 * tinystream v2 -- hidden evaluation test: tests/loopback/test_lb_viewer_keys_eval.c
 * Control channel end to end (PROTOCOL.md 5): the viewer queues a SET_RATE
 * and a KEYFRAME_REQ in the same tick (in both orders, alternating), so the
 * two TLVs reach the daemon in one TCP read. Every one of them must take
 * effect: the daemon's rate follows each SET_RATE (and STATS reports it),
 * each KEYFRAME_REQ is counted, the viewer never reconnects and neither side
 * reports a control error, while the stream keeps decoding at full rate.
 * The second run cuts every viewer write into 3-byte pieces as well
 * (io_fake tcp_send_limit). Ticket and metadata: ../../meta.yaml.
 */
#include "lb_common.h"

#define JOIN_MS       500U
#define ROUNDS        6U
#define ROUND_MS      1200U     /* > TS_KEYREQ_MIN_INTERVAL_MS and one STATS period */
#define RATE_A        1500U
#define RATE_B        3000U

void setUp(void)
{
}

void tearDown(void)
{
}

static void run_ctrl(uint32_t split)
{
    ts_loop_cfg_t c;
    const tsr_metrics_t *m;
    const ts_client_t *dc;
    uint32_t want = 0U;
    uint32_t dec0;

    lb_cfg(&c);
    lb_start(&c);
    g_L.io_r[0].tcp_send_limit = split;

    ts_lb_run(&g_lb, JOIN_MS);
    lb_print_rx("ctrl/join", 0U);
    TEST_ASSERT_EQUAL_INT_MESSAGE(TSR_STREAMING, tsr_state(g_L.rx[0]), "viewer not streaming 500 ms after connect");
    TEST_ASSERT_TRUE_MESSAGE(ts_lb_decoded(&g_lb, 0U) > 0U, "nothing decoded after the handshake");
    dc = &g_L.d->clients.c[0];
    TEST_ASSERT_EQUAL_INT_MESSAGE(TS_CL_ACTIVE, dc->state, "daemon client not active");
    dec0 = ts_lb_decoded(&g_lb, 0U);

    for (uint32_t k = 0U; k < ROUNDS; k++) {
        want = ((k % 2U) == 0U) ? RATE_A : RATE_B;
        if ((k % 2U) == 0U) {
            TEST_ASSERT_EQUAL(TS_OK, tsr_send_set_rate(g_L.rx[0], want));
            TEST_ASSERT_EQUAL(TS_OK, tsr_send_keyframe_req(g_L.rx[0]));
        } else {
            TEST_ASSERT_EQUAL(TS_OK, tsr_send_keyframe_req(g_L.rx[0]));
            TEST_ASSERT_EQUAL(TS_OK, tsr_send_set_rate(g_L.rx[0], want));
        }
        ts_lb_run(&g_lb, ROUND_MS);
        (void)printf("  [ctrl/round %lu] want %lu kbps, daemon %lu kbps, keyreqs %lu, stats kbps %lu\n",
                     (unsigned long)k, (unsigned long)want, (unsigned long)dc->kbps,
                     (unsigned long)dc->ctr.keyframe_reqs, (unsigned long)tsr_metrics(g_L.rx[0])->last_stats.c[0].kbps);
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(want, dc->kbps, "SET_RATE did not reach the daemon");
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(k + 1U, dc->ctr.keyframe_reqs, "KEYFRAME_REQ did not reach the daemon");
    }
    m = tsr_metrics(g_L.rx[0]);
    lb_print_rx("ctrl/end", 0U);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(want, m->last_stats.c[0].kbps, "STATS kbps != last SET_RATE");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, m->reconnects, "viewer reconnected (re-HELLO)");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, m->ctrl_errors, "viewer control errors");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, tsd_stats(g_L.d)->ctrl_errors, "daemon control errors");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, m->freezes, "freeze");
    TEST_ASSERT_TRUE_MESSAGE((ts_lb_decoded(&g_lb, 0U) - dec0) > ((ROUNDS * ROUND_MS * 25U) / 1000U),
                             "frame rate below 25 fps during the control exchange");
    lb_assert_invariants(1U);
    lb_stop();
}

static void test_ctrl_pairs_whole_writes(void)
{
    run_ctrl(0U);
}

static void test_ctrl_pairs_3_byte_writes(void)
{
    run_ctrl(3U);
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_ctrl_pairs_whole_writes);
    RUN_TEST(test_ctrl_pairs_3_byte_writes);
    return UNITY_END();
}
