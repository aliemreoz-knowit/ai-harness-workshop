/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/loopback/test_lb_two_clients.c
 * loopback two viewers: both >= 29 fps on a clean link, each gets exact frames.
 * Same-IP / two-port variant: two viewers behind one IP are two clients
 * (PROTOCOL.md 5: a client is keyed by ip AND port).
 */
#include "lb_common.h"

void setUp(void)
{
}

void tearDown(void)
{
}

static void run_two(bool same_ip)
{
    ts_loop_cfg_t c;
    ts_lb_window_t w;
    lb_cfg(&c);
    c.receivers = 2U;
    lb_start(&c);
    TEST_ASSERT_NOT_NULL(g_L.rx[1]);
    if (same_ip) {
        /* both viewers on one laptop: same IP, different UDP ports (before the first tick connects) */
        g_L.io_r[1].self.ip = g_L.io_r[0].self.ip;
        TEST_ASSERT_TRUE(g_L.io_r[1].self.port != g_L.io_r[0].self.port);
    }

    ts_lb_run(&g_lb, 2000U);
    ts_lb_window(&g_lb, 5000U, &w);
    lb_print_window(same_ip ? "two/same-ip" : "two", &w);
    lb_print_rx(same_ip ? "two/same-ip" : "two", 0U);
    lb_print_rx(same_ip ? "two/same-ip" : "two", 1U);

    lb_assert_invariants(2U);
    for (uint8_t i = 0U; i < 2U; i++) {
        const tsr_metrics_t *m = tsr_metrics(g_L.rx[i]);
        TEST_ASSERT_TRUE_MESSAGE(w.min_fps_1s[i] >= 29U, "a viewer below 29 fps with two viewers");
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, m->freezes, "a viewer froze with two viewers");
        TEST_ASSERT_TRUE_MESSAGE(ts_lb_exact_at_least(g_lb.rx[i].exact_raw, g_lb.rx[i].compared, 99U),
                                 "exact-match < 99 % with two viewers");
        TEST_ASSERT_TRUE_MESSAGE(((uint64_t)m->nacks_sent * 1000U) < (uint64_t)m->pkts_rx, "NACKs on a clean link");
    }
    TEST_ASSERT_EQUAL_UINT8_MESSAGE(2U, tsd_stats(g_L.d)->client_count, "daemon does not count two clients");
    lb_stop();
}

static void test_two_clients_two_ips(void)
{
    run_two(false);
}

static void test_two_clients_same_ip_two_ports(void)
{
    run_two(true);
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_two_clients_two_ips);
    RUN_TEST(test_two_clients_same_ip_two_ports);
    return UNITY_END();
}
