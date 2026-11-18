/*
 * tinystream v2 -- hidden evaluation test: tests/loopback/test_lb_per_client_stats.c
 * Two viewers, STATS over the control channel (PROTOCOL.md 5.1): every viewer
 * receives one block per active viewer, finds its own block by its UDP port,
 * and the block matches what that viewer did (rate, keyframe requests, NACKs).
 * The per-viewer counters add up to the global block of the same STATS.
 * See meta.yaml for what this test evaluates.
 */
#include "lb_common.h"

#define RATE_RX1_KBPS   3000U
#define LOSS_PCT        5U

void setUp(void)
{
}

void tearDown(void)
{
}

static const ts_stats_client_t *own_block(const ts_tlv_stats_t *st, uint16_t udp_port)
{
    const ts_stats_client_t *b = NULL;
    for (uint8_t i = 0U; (i < st->g.client_count) && (i < (uint8_t)TS_MAX_CLIENTS); i++) {
        if (st->c[i].udp_port == udp_port) {
            b = &st->c[i];
        }
    }
    return b;
}

static void print_stats(const char *tag, const ts_tlv_stats_t *st)
{
    (void)printf("  [%s] global pkts=%lu bytes=%lu nacks=%lu retx=%lu count=%u\n", tag,
                 (unsigned long)st->g.pkts_sent, (unsigned long)st->g.bytes_sent, (unsigned long)st->g.nacks_rx,
                 (unsigned long)st->g.retx_sent, (unsigned)st->g.client_count);
    for (uint8_t i = 0U; (i < st->g.client_count) && (i < (uint8_t)TS_MAX_CLIENTS); i++) {
        const ts_stats_client_t *c = &st->c[i];
        (void)printf("  [%s] client id=%u state=%u port=%u kbps=%lu pkts=%lu bytes=%lu nacks=%lu retx=%lu "
                     "retx_drop=%lu keyreq=%lu last_seen=%lu\n", tag,
                     (unsigned)c->id, (unsigned)c->state, (unsigned)c->udp_port, (unsigned long)c->kbps,
                     (unsigned long)c->pkts_sent, (unsigned long)c->bytes_sent, (unsigned long)c->nacks_rx,
                     (unsigned long)c->retx_sent, (unsigned long)c->retx_dropped, (unsigned long)c->keyframe_reqs,
                     (unsigned long)c->last_seen_ms);
    }
}

/* The blocks of one STATS add up to its global block. */
static void assert_sums(const ts_tlv_stats_t *st)
{
    uint32_t pkts = 0U;
    uint32_t bytes = 0U;
    uint32_t nacks = 0U;
    uint32_t retx = 0U;
    for (uint8_t i = 0U; (i < st->g.client_count) && (i < (uint8_t)TS_MAX_CLIENTS); i++) {
        pkts += st->c[i].pkts_sent;
        bytes += st->c[i].bytes_sent;
        nacks += st->c[i].nacks_rx;
        retx += st->c[i].retx_sent;
    }
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(st->g.pkts_sent, pkts, "per-viewer pkts_sent do not add up to the global count");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(st->g.bytes_sent, bytes, "per-viewer bytes_sent do not add up to the global count");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(st->g.nacks_rx, nacks, "per-viewer nacks_rx do not add up to the global count");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(st->g.retx_sent, retx, "per-viewer retx_sent do not add up to the global count");
}

static void check_viewer(uint8_t i, uint32_t kbps, bool lossy)
{
    const tsr_metrics_t *m = tsr_metrics(g_L.rx[i]);
    const ts_tlv_stats_t *st = &m->last_stats;
    const ts_stats_client_t *own = own_block(st, g_L.io_r[i].self.port);
    const ts_stats_client_t *other = own_block(st, g_L.io_r[1U - i].self.port);

    TEST_ASSERT_TRUE_MESSAGE(m->stats_rx >= 3U, "viewer received no STATS");
    TEST_ASSERT_EQUAL_UINT8_MESSAGE(2U, st->g.client_count, "STATS does not carry one block per viewer");
    TEST_ASSERT_NOT_NULL_MESSAGE(own, "viewer cannot find its own STATS block (by UDP port)");
    TEST_ASSERT_NOT_NULL_MESSAGE(other, "viewer cannot see the other viewer's STATS block");
    TEST_ASSERT_TRUE_MESSAGE(own->id != other->id, "two viewers share one block id");
    TEST_ASSERT_EQUAL_UINT8_MESSAGE(1U, own->state, "block state is not 'active'");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(kbps, own->kbps, "block kbps is not the viewer's rate");
    TEST_ASSERT_TRUE_MESSAGE(own->pkts_sent > 0U, "block pkts_sent is zero");
    TEST_ASSERT_TRUE_MESSAGE(own->bytes_sent >= (own->pkts_sent * TS_HDR_LEN), "block bytes_sent below pkts_sent headers");
    TEST_ASSERT_TRUE_MESSAGE(own->pkts_sent <= m->pkts_rx + m->pkts_lost + 1U,
                             "block pkts_sent above what the viewer received");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(m->keyframe_reqs, own->keyframe_reqs,
                                     "block keyframe_reqs differ from the viewer's requests");
    TEST_ASSERT_TRUE_MESSAGE(own->last_seen_ms < TS_CLIENT_TIMEOUT_MS, "block last_seen_ms too old");
    TEST_ASSERT_TRUE_MESSAGE(own->nacks_rx <= m->nacks_sent, "block nacks_rx above the viewer's NACK entries");
    TEST_ASSERT_TRUE_MESSAGE(own->retx_sent <= own->nacks_rx, "block retx_sent above its nacks_rx");
    if (lossy) {
        TEST_ASSERT_TRUE_MESSAGE(own->nacks_rx > 0U, "block nacks_rx is zero under loss");
        TEST_ASSERT_TRUE_MESSAGE(own->retx_sent > 0U, "block retx_sent is zero under loss");
    } else {
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, own->nacks_rx, "block nacks_rx on a clean link");
    }
    assert_sums(st);
}

static void test_per_viewer_stats_blocks(void)
{
    ts_loop_cfg_t c;
    ts_netem_cfg_t lossy;
    ts_netem_cfg_t clean;

    lb_cfg(&c);
    c.receivers = 2U;
    lb_start(&c);
    TEST_ASSERT_NOT_NULL(g_L.rx[1]);

    /* phase 1, clean link: viewer 1 changes its rate, viewer 0 asks for keyframes */
    ts_lb_run(&g_lb, 1500U);
    TEST_ASSERT_EQUAL(TS_OK, tsr_send_set_rate(g_L.rx[1], RATE_RX1_KBPS));
    for (uint32_t k = 0U; k < 3U; k++) {
        TEST_ASSERT_EQUAL(TS_OK, tsr_send_keyframe_req(g_L.rx[0]));
        ts_lb_run(&g_lb, 600U);
    }
    ts_lb_run(&g_lb, 2200U);          /* two more STATS periods */
    print_stats("stats/clean rx0", &tsr_metrics(g_L.rx[0])->last_stats);
    print_stats("stats/clean rx1", &tsr_metrics(g_L.rx[1])->last_stats);
    lb_print_rx("stats/clean", 0U);
    lb_print_rx("stats/clean", 1U);
    TEST_ASSERT_TRUE_MESSAGE(tsr_metrics(g_L.rx[0])->keyframe_reqs >= 3U, "viewer 0 sent no keyframe requests");
    check_viewer(0U, TS_DEFAULT_KBPS, false);
    check_viewer(1U, RATE_RX1_KBPS, false);

    /* phase 2: loss on the link, then clean again until the counters settle */
    (void)memset(&lossy, 0, sizeof(lossy));
    lossy.seed = 7U;
    lossy.loss_pct = (uint8_t)LOSS_PCT;
    ts_loop_set_netem(&g_L, &lossy);
    ts_lb_run(&g_lb, 3000U);
    (void)memset(&clean, 0, sizeof(clean));
    clean.seed = 7U;
    ts_loop_set_netem(&g_L, &clean);
    ts_lb_run(&g_lb, 2500U);
    print_stats("stats/loss rx0", &tsr_metrics(g_L.rx[0])->last_stats);
    lb_print_rx("stats/loss", 0U);
    lb_print_rx("stats/loss", 1U);
    check_viewer(0U, TS_DEFAULT_KBPS, true);
    check_viewer(1U, RATE_RX1_KBPS, true);

    lb_assert_invariants(2U);
    lb_stop();
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_per_viewer_stats_blocks);
    return UNITY_END();
}
