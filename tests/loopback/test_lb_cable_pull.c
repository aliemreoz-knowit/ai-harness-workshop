/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/loopback/test_lb_cable_pull.c
 * loopback cable pull: UDP blacked out for 2 s (scenario timeline "t=3s blackout
 * 2s", the same text tsnetem --scenario takes). After the cable is back a
 * fresh, exactly reconstructed frame must be on screen within 1 s, long
 * before the next periodic keyframe (10 s).
 */
#include "lb_common.h"

#define PULL_AT_MS    3000U
#define PULL_FOR_MS   2000U
#define RECOVERY_MS   1000U

static ts_netem_script_t g_script;

void setUp(void)
{
}

void tearDown(void)
{
}

static void test_cable_pull_2s_recovers_within_1s(void)
{
    ts_loop_cfg_t c;
    ts_lb_window_t w;
    const ts_netem_scenario_t *sc;
    uint32_t t0;
    uint32_t restore_ms;
    uint16_t seq_before;
    uint32_t decoded_before;
    bool recovered = false;
    uint32_t recovery_ms = 0xFFFFFFFFU;

    lb_cfg(&c);
    lb_start(&c);
    ts_netem_script_init(&g_script, &g_L.cfg.netem);
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_script_line(&g_script, "t=3s blackout 2s"));
    sc = ts_netem_script_finish(&g_script);
    TEST_ASSERT_EQUAL_UINT8(2U, sc->count);
    t0 = g_L.now_ms;
    ts_netem_scenario_start(&g_L.netem, sc, t0);

    ts_lb_run(&g_lb, PULL_AT_MS);
    TEST_ASSERT_TRUE_MESSAGE(g_lb.rx[0].have_last, "nothing decoded before the pull");
    seq_before = g_lb.rx[0].last_seq;
    ts_lb_run(&g_lb, PULL_FOR_MS);
    TEST_ASSERT_TRUE_MESSAGE(ts_netem_stats(&g_L.netem)->dropped_cable > 0U, "cable pull dropped nothing");
    decoded_before = ts_lb_decoded(&g_lb, 0U);
    restore_ms = t0 + PULL_AT_MS + PULL_FOR_MS;

    /* step until a NEW frame (after the pull) is decoded and matches the encoder exactly */
    while (!recovered && ((g_L.now_ms - restore_ms) < (3U * RECOVERY_MS))) {
        uint32_t step_ms = g_L.now_ms;
        uint32_t exact_before = g_lb.rx[0].exact_recon;
        ts_lb_step(&g_lb);
        if ((ts_lb_decoded(&g_lb, 0U) != decoded_before) && (g_lb.rx[0].exact_recon != exact_before) &&
            ts_seq_after(g_lb.rx[0].last_seq, seq_before)) {
            const ts_lb_truth_t *t = ts_lb_truth(&g_lb, g_lb.rx[0].last_seq);
            if ((t != NULL) && ts_seq_after(t->frame_seq, seq_before)) {
                recovered = true;
                recovery_ms = step_ms - restore_ms;
            }
        }
    }
    (void)printf("  [cable] recovery %lu ms after the cable came back\n", (unsigned long)recovery_ms);
    ts_lb_window(&g_lb, 2000U, &w);
    lb_print_window("cable", &w);
    lb_print_rx("cable", 0U);
    lb_print_netem("cable");

    lb_assert_invariants(1U);
    TEST_ASSERT_TRUE_MESSAGE(recovered, "no fresh exact frame within 3 s of the cable coming back");
    TEST_ASSERT_TRUE_MESSAGE(recovery_ms <= RECOVERY_MS, "recovery after a 2 s cable pull took more than 1 s");
    TEST_ASSERT_TRUE_MESSAGE(w.fps_x100[0] >= 2500U, "fps < 25 after recovery");
    TEST_ASSERT_FALSE_MESSAGE(g_L.netem.cfg.cable_pulled, "scenario did not restore the cable");
    lb_stop();
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_cable_pull_2s_recovers_within_1s);
    return UNITY_END();
}
