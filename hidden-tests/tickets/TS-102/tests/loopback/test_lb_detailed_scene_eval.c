/*
 * tinystream v2 -- hidden evaluation test: tests/loopback/test_lb_detailed_scene_eval.c
 * (evaluation companion of the public test_lb_enc_max.c, independent cadence
 * and seed)
 *
 * The RLE worst case is larger than the raw frame (PROTOCOL.md 6.4), so a
 * synthetic noise burst encodes to more than TS_FRAME_BYTES bytes. Drive the
 * full daemon + receiver loop at quality 1 (budget well above the stream, so
 * no quality step-down) through repeated noise bursts: every encode buffer on
 * the daemon path must hold TS_ENC_MAX, the decoded picture must stay
 * bit-exact against the encoder's reconstructed reference and the daemon must
 * keep running. Under ASan (sanitizers) an undersized daemon encode buffer is a global
 * buffer overflow on the first noise frame; on the host it shows as a wrong
 * picture or a dead daemon/receiver.
 *
 * The defect: see the ticket.
 */
#include "lb_common.h"

#define ENC_WARM_MS     1500U
#define ENC_WINDOW_MS   5000U
#define ENC_CUT_MS      1000U     /* a noise frame 15 frames after each cut -> ~1 per second */

void setUp(void)
{
}

void tearDown(void)
{
}

static void test_noise_bursts_never_overflow_the_encode_buffer(void)
{
    ts_loop_cfg_t c;
    ts_lb_window_t w;
    const tsr_metrics_t *m;

    lb_cfg(&c);
    /* budget far above the stream: quality stays 1, so a noise frame is the
     * full worst-case keyframe (> TS_FRAME_BYTES, close to TS_ENC_MAX). */
    c.daemon.kbps_client = TS_GLOBAL_KBPS;
    c.daemon.kbps_global = TS_GLOBAL_KBPS;
    c.daemon.capture.scene_cut_ms = ENC_CUT_MS;
    c.netem.seed = 0x5A1AD7U;
    lb_start(&c);

    ts_lb_run(&g_lb, ENC_WARM_MS);
    ts_lb_window(&g_lb, ENC_WINDOW_MS, &w);
    m = tsr_metrics(g_L.rx[0]);
    lb_print_window("enc-overflow", &w);
    lb_print_rx("enc-overflow", 0U);

    lb_assert_invariants(1U);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, m->decode_errors, "decode errors across the noise bursts");
    TEST_ASSERT_TRUE_MESSAGE(g_lb.rx[0].compared >= 150U, "too few frames decoded with noise bursts");
    TEST_ASSERT_TRUE_MESSAGE(w.min_fps_1s[0] > 0U, "a whole second without a decoded frame");
    /* the stream must actually have carried worst-case keyframes */
    TEST_ASSERT_TRUE_MESSAGE(tsd_stats(g_L.d)->keyframes_sent >= 3U, "no worst-case keyframes were sent");
    lb_stop();
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_noise_bursts_never_overflow_the_encode_buffer);
    return UNITY_END();
}
