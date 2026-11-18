/*
 * tinystream v2 -- hidden evaluation test: tests/loopback/test_lb_tight_link_eval.c
 * (evaluation variant of the public test_lb_tight_link.c: budget = link, other seed)
 * loopback tight link under loss: the viewer's budget is close to what the stream
 * needs (900 kbit/s), the link is throttled to the budget and loses 5 %
 * of the packets in both directions. The stream must keep its frame rate
 * (fps >= 20 on average and in every whole second) and stay within the budget
 * + 10 % including retransmissions.
 */
#include "lb_common.h"

#define TIGHT_BUDGET_KBPS  900U
#define TIGHT_LINK_KBPS    900U      /* throttle: budget */
#define TIGHT_LOSS_PCT     5U
#define TIGHT_WARM_MS      3000U
#define TIGHT_WINDOW_MS    5000U
#define TIGHT_MIN_FPS_X100 2000U
#define TIGHT_MIN_FPS_1S   20U

void setUp(void)
{
}

void tearDown(void)
{
}

static void test_tight_link_5pct_loss_keeps_frame_rate(void)
{
    ts_loop_cfg_t c;
    ts_lb_window_t w;
    const tsr_metrics_t *m;

    lb_cfg(&c);
    c.daemon.kbps_client = TIGHT_BUDGET_KBPS;
    c.netem.throttle_kbps = TIGHT_LINK_KBPS;
    c.netem.loss_pct = (uint8_t)TIGHT_LOSS_PCT;
    c.netem.seed = 3U;
    lb_start(&c);

    ts_lb_run(&g_lb, TIGHT_WARM_MS);
    ts_lb_window(&g_lb, TIGHT_WINDOW_MS, &w);
    m = tsr_metrics(g_L.rx[0]);
    lb_print_window("tight", &w);
    lb_print_rx("tight", 0U);
    lb_print_netem("tight");

    lb_assert_invariants(1U);
    TEST_ASSERT_TRUE_MESSAGE(ts_netem_stats(&g_L.netem)->dropped > 0U, "netem dropped nothing");
    TEST_ASSERT_TRUE_MESSAGE(m->retx_rx > 0U, "no retransmission received");
    TEST_ASSERT_TRUE_MESSAGE(w.fps_x100[0] >= TIGHT_MIN_FPS_X100, "fps < 20 on a tight link at 5 % loss");
    TEST_ASSERT_TRUE_MESSAGE(w.min_fps_1s[0] >= TIGHT_MIN_FPS_1S, "a second below 20 fps on a tight link at 5 % loss");
    TEST_ASSERT_TRUE_MESSAGE(w.daemon_kbps <= ((TIGHT_BUDGET_KBPS * 110U) / 100U),
                             "daemon bitrate > budget + 10 % on a tight link");
    lb_stop();
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_tight_link_5pct_loss_keeps_frame_rate);
    return UNITY_END();
}
