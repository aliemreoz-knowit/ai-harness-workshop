/*
 * tinystream v2 -- hidden evaluation test: tests/loopback/test_lb_set_rate_eval.c
 * (evaluation variant of the public SET_RATE scenario: other rates, several
 * steps, frame rate budget per step)
 * A viewer changes its rate several times with SET_RATE (PROTOCOL.md 5:
 * kbps, 1 kbit = 1000 bit). After each step the daemon reports the new rate
 * in STATS, sends at most rate + 10 %, does not crawl far below it, and the
 * picture keeps moving (quality adapts instead of the frame rate).
 * See meta.yaml for what this test evaluates.
 */
#include "lb_common.h"

#define BASE_KBPS   6000U
#define SETTLE_MS   3500U
#define WINDOW_MS   4000U
#define MIN_FPS_1S  20U

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

static void step_to(uint32_t kbps, uint32_t base_kbps)
{
    ts_lb_window_t w;
    char tag[32];
    const tsr_metrics_t *m;
    uint32_t freezes0;

    TEST_ASSERT_EQUAL(TS_OK, tsr_send_set_rate(g_L.rx[0], kbps));
    ts_lb_run(&g_lb, SETTLE_MS);
    freezes0 = tsr_metrics(g_L.rx[0])->freezes;
    ts_lb_window(&g_lb, WINDOW_MS, &w);
    m = tsr_metrics(g_L.rx[0]);
    (void)snprintf(tag, sizeof(tag), "set-rate/%lu", (unsigned long)kbps);
    lb_print_window(tag, &w);
    lb_print_rx(tag, 0U);

    TEST_ASSERT_EQUAL_UINT8_MESSAGE(1U, m->last_stats.g.client_count, "STATS client_count");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(kbps, m->last_stats.c[0].kbps, "STATS kbps != SET_RATE kbps");
    TEST_ASSERT_TRUE_MESSAGE(w.daemon_kbps <= ((kbps * 110U) / 100U), "bitrate above SET_RATE + 10 %");
    TEST_ASSERT_TRUE_MESSAGE(w.daemon_kbps >= (min_u32(kbps, base_kbps) / 2U), "bitrate far below SET_RATE");
    TEST_ASSERT_TRUE_MESSAGE(w.min_fps_1s[0] >= MIN_FPS_1S, "a second below 20 fps after SET_RATE");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(freezes0, m->freezes, "freeze after SET_RATE");
}

static void test_set_rate_steps(void)
{
    ts_loop_cfg_t c;
    ts_lb_window_t base;

    lb_cfg(&c);
    c.daemon.kbps_client = BASE_KBPS;
    lb_start(&c);
    ts_lb_run(&g_lb, 2000U);
    ts_lb_window(&g_lb, 3000U, &base);
    lb_print_window("set-rate/base", &base);
    TEST_ASSERT_TRUE_MESSAGE(base.min_fps_1s[0] >= 29U, "base stream below 29 fps");

    step_to(2500U, base.daemon_kbps);
    step_to(1200U, base.daemon_kbps);
    step_to(4000U, base.daemon_kbps);

    lb_assert_invariants(1U);
    lb_stop();
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_set_rate_steps);
    return UNITY_END();
}
