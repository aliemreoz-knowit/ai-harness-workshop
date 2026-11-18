/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/loopback/test_lb_set_rate.c
 * SET_RATE conformance (PROTOCOL.md 5: kbps, 1 kbit = 1000 bit; buckets count
 * bytes): after SET_RATE N the daemon reports N in STATS and sends at most
 * N kbps + 10 %, but not crawling far below it either. The stream keeps
 * decoding exactly at the low rate, and
 * a rate below the stream bitrate is met by stepping quality down (no backlog).
 */
#include "lb_common.h"

#define HIGH_KBPS 8000U
#define LOW_KBPS  1000U

void setUp(void)
{
}

void tearDown(void)
{
}

static uint32_t min_u32(uint32_t a, uint32_t b)
{
    return (a < b) ? a : b;
}

static void test_set_rate_conformance(void)
{
    ts_loop_cfg_t c;
    ts_lb_window_t base;
    ts_lb_window_t low;
    ts_lb_window_t back;
    const tsr_metrics_t *m;

    lb_cfg(&c);
    c.daemon.kbps_client = HIGH_KBPS;
    lb_start(&c);

    ts_lb_run(&g_lb, 2000U);
    ts_lb_window(&g_lb, 3000U, &base);
    lb_print_window("rate/base", &base);
    TEST_ASSERT_TRUE_MESSAGE(base.daemon_kbps > 0U, "nothing sent before SET_RATE");

    TEST_ASSERT_EQUAL(TS_OK, tsr_send_set_rate(g_L.rx[0], LOW_KBPS));
    ts_lb_run(&g_lb, 2500U);                    /* applied + at least two STATS periods */
    ts_lb_window(&g_lb, 4000U, &low);
    m = tsr_metrics(g_L.rx[0]);
    lb_print_window("rate/low", &low);
    lb_print_rx("rate/low", 0U);
    TEST_ASSERT_TRUE_MESSAGE(m->stats_rx >= 2U, "no STATS received");
    TEST_ASSERT_EQUAL_UINT8_MESSAGE(1U, m->last_stats.g.client_count, "STATS client_count");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(LOW_KBPS, m->last_stats.c[0].kbps, "STATS kbps != SET_RATE kbps");
    TEST_ASSERT_TRUE_MESSAGE(low.daemon_kbps <= ((LOW_KBPS * 110U) / 100U), "bitrate above SET_RATE + 10 %");
    TEST_ASSERT_TRUE_MESSAGE(low.daemon_kbps >= (min_u32(LOW_KBPS, base.daemon_kbps) / 2U),
                             "bitrate far below SET_RATE (unit mix-up?)");
    TEST_ASSERT_TRUE_MESSAGE(low.decoded[0] > 0U, "nothing decoded at the low rate");

    TEST_ASSERT_EQUAL(TS_OK, tsr_send_set_rate(g_L.rx[0], HIGH_KBPS));
    ts_lb_run(&g_lb, 2500U);
    ts_lb_window(&g_lb, 3000U, &back);
    lb_print_window("rate/back", &back);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(HIGH_KBPS, tsr_metrics(g_L.rx[0])->last_stats.c[0].kbps, "STATS kbps after raise");
    TEST_ASSERT_TRUE_MESSAGE(back.daemon_kbps >= (min_u32(HIGH_KBPS, base.daemon_kbps) / 2U), "rate did not come back up");
    TEST_ASSERT_TRUE_MESSAGE(back.daemon_kbps <= ((HIGH_KBPS * 110U) / 100U), "bitrate above SET_RATE + 10 %");

    lb_assert_invariants(1U);
    lb_stop();
}

/*
 * SET_RATE below the stream bitrate (~1.6 Mbit/s synth at quality 1): the
 * daemon must fit the stream to the viewer (quality steps down) instead of
 * letting the backlog grow until the ring overruns the cursor. Measured over
 * 12 s (two scene cuts with their noise bursts): every whole second decodes
 * >= 25 frames, the one-way delay stays < 500 ms, nothing is skipped and the
 * backlog never grows past one second of the client rate.
 */
#define LONG_WINDOW_S   12U

static void test_set_rate_below_stream_adapts_quality(void)
{
    ts_loop_cfg_t c;
    const tsr_metrics_t *m;
    const ts_client_t *cl;
    uint32_t min_fps = UINT32_MAX;
    int32_t max_owd = 0;
    uint32_t max_pending = 0U;
    uint32_t last = 0U;

    lb_cfg(&c);
    c.daemon.kbps_client = HIGH_KBPS;
    lb_start(&c);
    ts_lb_run(&g_lb, 6000U);
    TEST_ASSERT_EQUAL(TS_OK, tsr_send_set_rate(g_L.rx[0], LOW_KBPS));
    ts_lb_run(&g_lb, 4000U);                    /* applied and adapted */
    last = ts_lb_decoded(&g_lb, 0U);
    for (uint32_t s = 0U; s < LONG_WINDOW_S; s++) {
        uint32_t now_dec;
        for (uint32_t k = 0U; k < 20U; k++) {   /* 50 ms samples */
            uint32_t pend;
            ts_lb_run(&g_lb, 50U);
            m = tsr_metrics(g_L.rx[0]);
            if (m->owd_ms > max_owd) {
                max_owd = m->owd_ms;
            }
            pend = ts_txq_cursor_pending(&g_L.d->txq, &g_L.d->clients.c[0].cursor);
            if (pend > max_pending) {
                max_pending = pend;
            }
        }
        now_dec = ts_lb_decoded(&g_lb, 0U);
        if ((now_dec - last) < min_fps) {
            min_fps = now_dec - last;
        }
        last = now_dec;
    }
    m = tsr_metrics(g_L.rx[0]);
    cl = &g_L.d->clients.c[0];
    (void)printf("  [rate/below] min_fps_1s=%lu max_owd=%ldms max_pending=%lu skipped=%lu quality=%u est=%lukbps "
                 "steps_down=%lu steps_up=%lu resets=%lu freezes=%lu\n",
                 (unsigned long)min_fps, (long)max_owd, (unsigned long)max_pending,
                 (unsigned long)cl->cursor.skipped, (unsigned)ts_enc_quality(&g_L.d->enc),
                 (unsigned long)g_L.d->cc.est_kbps, (unsigned long)g_L.d->cc.steps_down,
                 (unsigned long)g_L.d->cc.steps_up, (unsigned long)g_L.d->cc.resets, (unsigned long)m->freezes);
    lb_print_rx("rate/below", 0U);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(LOW_KBPS, cl->kbps, "SET_RATE not applied");
    TEST_ASSERT_TRUE_MESSAGE(ts_enc_quality(&g_L.d->enc) > 1U, "quality did not adapt to the low rate");
    TEST_ASSERT_TRUE_MESSAGE(min_fps >= 25U, "fps collapsed after SET_RATE below the stream bitrate");
    TEST_ASSERT_TRUE_MESSAGE(max_owd < 500, "one-way delay grew (backlog) after SET_RATE below the stream bitrate");
    TEST_ASSERT_TRUE_MESSAGE(ts_cc_backlog_ms(max_pending, LOW_KBPS) <= TS_CC_RESET_MS, "client backlog above 1 s");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, cl->cursor.skipped, "the ring overran the client cursor");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, m->freezes, "freeze at the low rate");
    lb_assert_invariants(1U);
    lb_stop();
}

static void test_hello_rate_is_kbps(void)
{
    ts_loop_cfg_t c;
    ts_lb_window_t w;
    lb_cfg(&c);
    c.daemon.kbps_client = HIGH_KBPS;
    c.receiver.kbps = 2000U;   /* requested in HELLO */
    lb_start(&c);
    ts_lb_run(&g_lb, 3000U);
    ts_lb_window(&g_lb, 4000U, &w);
    lb_print_window("rate/hello", &w);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(2000U, tsr_metrics(g_L.rx[0])->last_stats.c[0].kbps, "HELLO kbps not applied");
    TEST_ASSERT_TRUE_MESSAGE(w.daemon_kbps <= 2200U, "bitrate above the HELLO rate + 10 %");
    TEST_ASSERT_TRUE_MESSAGE(w.decoded[0] > 0U, "nothing decoded at the HELLO rate");
    lb_assert_invariants(1U);
    lb_stop();
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_set_rate_conformance);
    RUN_TEST(test_set_rate_below_stream_adapts_quality);
    RUN_TEST(test_hello_rate_is_kbps);
    return UNITY_END();
}
