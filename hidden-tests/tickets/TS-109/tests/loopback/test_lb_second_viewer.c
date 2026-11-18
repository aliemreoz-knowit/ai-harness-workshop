/*
 * tinystream v2 -- hidden evaluation test: tests/loopback/test_lb_second_viewer.c
 * Two viewers on one laptop (same IP, two UDP ports) on a lossy link: each
 * viewer's NACKs are answered to that viewer, both keep the frame rate,
 * nobody freezes; then one viewer leaves and the other keeps streaming.
 * See meta.yaml for what this test evaluates.
 */
#include "lb_common.h"

#define LOSS_PCT     3U
#define MIN_FPS_1S   25U

void setUp(void)
{
}

void tearDown(void)
{
}

static void start_same_ip(uint8_t loss_pct, uint32_t seed)
{
    ts_loop_cfg_t c;
    lb_cfg(&c);
    c.receivers = 2U;
    c.netem.loss_pct = loss_pct;
    c.netem.seed = seed;
    lb_start(&c);
    TEST_ASSERT_NOT_NULL(g_L.rx[1]);
    /* both viewers on one laptop: same IP, different UDP ports (before the first tick connects) */
    g_L.io_r[1].self.ip = g_L.io_r[0].self.ip;
    TEST_ASSERT_TRUE(g_L.io_r[1].self.port != g_L.io_r[0].self.port);
}

static void test_same_ip_viewers_under_loss(void)
{
    ts_lb_window_t w;
    start_same_ip((uint8_t)LOSS_PCT, 11U);
    ts_lb_run(&g_lb, 2000U);
    ts_lb_window(&g_lb, 6000U, &w);
    lb_print_window("same-ip/loss", &w);
    lb_print_rx("same-ip/loss", 0U);
    lb_print_rx("same-ip/loss", 1U);
    lb_print_netem("same-ip/loss");

    lb_assert_invariants(2U);
    TEST_ASSERT_EQUAL_UINT8_MESSAGE(2U, tsd_stats(g_L.d)->client_count, "daemon does not count two viewers");
    for (uint8_t i = 0U; i < 2U; i++) {
        const tsr_metrics_t *m = tsr_metrics(g_L.rx[i]);
        TEST_ASSERT_TRUE_MESSAGE(m->nacks_sent > 0U, "a viewer sent no NACKs under loss");
        TEST_ASSERT_TRUE_MESSAGE(m->retx_rx > 0U, "a viewer's NACKs were never answered to it");
        TEST_ASSERT_TRUE_MESSAGE(w.min_fps_1s[i] >= MIN_FPS_1S, "a same-IP viewer dropped below 25 fps in a second");
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, m->freezes, "a same-IP viewer froze");
    }
    lb_stop();
}

static void test_same_ip_viewer_leaves_other_continues(void)
{
    ts_lb_window_t w;
    uint32_t freezes0;
    start_same_ip(0U, 5U);
    ts_lb_run(&g_lb, 2500U);
    TEST_ASSERT_EQUAL_UINT8(2U, tsd_stats(g_L.d)->client_count);

    /* viewer 1 quits: BYE, then it goes silent */
    TEST_ASSERT_EQUAL(TS_OK, tsr_send_bye(g_L.rx[1]));
    ts_lb_step(&g_lb);
    ts_lb_step(&g_lb);
    g_L.rx[1] = NULL;
    freezes0 = tsr_metrics(g_L.rx[0])->freezes;
    ts_lb_run(&g_lb, 1000U);
    ts_lb_window(&g_lb, 4000U, &w);
    lb_print_window("same-ip/leave", &w);
    lb_print_rx("same-ip/leave", 0U);

    TEST_ASSERT_EQUAL_UINT8_MESSAGE(1U, ts_clients_active(&g_L.d->clients), "the leaving viewer is still active");
    TEST_ASSERT_EQUAL_INT_MESSAGE(TSD_RUNNING, g_L.d->state, "daemon not running");
    TEST_ASSERT_TRUE_MESSAGE(w.min_fps_1s[0] >= 29U, "remaining viewer below 29 fps after its neighbour left");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(freezes0, tsr_metrics(g_L.rx[0])->freezes, "remaining viewer froze");
    TEST_ASSERT_EQUAL_UINT32(g_lb.rx[0].compared, g_lb.rx[0].exact_recon);
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_same_ip_viewers_under_loss);
    RUN_TEST(test_same_ip_viewer_leaves_other_continues);
    return UNITY_END();
}
