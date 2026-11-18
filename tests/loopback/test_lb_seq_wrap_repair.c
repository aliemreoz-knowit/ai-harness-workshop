/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/loopback/test_lb_seq_wrap_repair.c
 * loopback sequence wrap with repair (PROTOCOL.md 3.2, 4, 7): the stream crosses
 * the u16 pkt_seq wrap (and, in a second run, the frame_seq wrap) under 2 %
 * random loss in both directions. The viewer asked for a keyframe just
 * before the wrap, and fresh fragments on both sides of the wrap are lost.
 * They must be repaired by NACK + retransmission across the wrap exactly as
 * anywhere else in the sequence space: no freeze, no long decode gap, no
 * "too old" answer for a fragment the ring still holds, delivery continues at
 * the full frame rate and loss detection keeps working after the wrap.
 *
 * Each run is ~2 s of warm-up (the wrap comes after the first keyframe's
 * cooldown) plus a 2.8 s measured tail; 1 ms virtual ticks.
 */
#include "lb_common.h"

#define WRAP_LOSS_PCT     2U
#define WRAP_AFTER_MS     2500U     /* measured window after the wrap */
#define WRAP_SETTLE_MS    300U      /* repairs of the wrap losses, before the window */
#define WRAP_PRE_MAX_MS   6000U     /* setup must reach the wrap within this */
#define WRAP_ASK_AFTER_MS 1500U     /* first keyframe cooled down */
#define WRAP_MIN_FPS      25U       /* every whole second of the window */
#define WRAP_MAX_GAP_MS   250U      /* longest decode gap of the whole run */
#define WRAP_KEY_AGE_MS   400U      /* the requested keyframe precedes the wrap losses by less */
#define WRAP_PKT_START    65186U    /* pkt_seq wraps ~2.2 s after start */
#define WRAP_FRAME_START  65476U    /* frame_seq wraps after 60 frames (2 s) */
#define WRAP_KEY_LEAD     24U       /* pkt mode: ask for the keyframe within this many packets of the wrap */

typedef struct wrap_tap {
    bool     pkt_mode;          /* true: losses around the pkt_seq wrap; false: around the frame_seq wrap */
    uint32_t dropped;
    bool     pre;               /* a fragment before the wrap was dropped */
    bool     post;              /* a fragment after the wrap was dropped */
    uint32_t key_age_ms;        /* age of the newest keyframe at the first drop */
} wrap_tap_t;

static wrap_tap_t g_tap;

void setUp(void)
{
}

void tearDown(void)
{
}

/*
 * Fresh DATA packets only (retransmissions pass). pkt mode: pkt_seq 65534,
 * 65535 and 0. frame mode: the last fragment of frame 65535 and the first
 * fragment of frame 0.
 */
static bool wrap_tap_fn(void *ctx, const ts_io_dgram_t *d)
{
    wrap_tap_t *t = (wrap_tap_t *)ctx;
    bool pass = true;
    if ((d->len >= TS_HDR_LEN) && (ts_rd16(&d->data[0]) == (uint16_t)TS_MAGIC_DATA) &&
        (((uint32_t)d->data[3] & TS_F_RETRANSMIT) == 0U)) {
        uint16_t frame_seq = ts_rd16(&d->data[4]);
        uint16_t pkt_seq = ts_rd16(&d->data[6]);
        uint8_t idx = d->data[8];
        uint8_t cnt = d->data[9];
        bool pre;
        bool post;
        if (t->pkt_mode) {
            pre = (pkt_seq == 65534U) || (pkt_seq == 65535U);
            post = (pkt_seq == 0U);
        } else {
            pre = (frame_seq == 65535U) && (((uint32_t)idx + 1U) == (uint32_t)cnt);
            post = (frame_seq == 0U) && (idx == 0U);
        }
        if (pre || post) {
            if (t->dropped == 0U) {
                t->key_age_ms = g_L.now_ms - g_L.d->keysched.last_key_ms;
            }
            pass = false;
            t->dropped++;
            t->pre = t->pre || pre;
            t->post = t->post || post;
        }
    }
    return pass;
}

/*
 * Warm up, then ask for a keyframe just before the wrap: in pkt mode when a
 * frame was just pushed and the wrap is less than WRAP_KEY_LEAD packets away,
 * in frame mode right after frame 65534 (so frame 65535 is the keyframe).
 * Returns once the wrap losses happened.
 */
static void wrap_reach(bool pkt_mode)
{
    bool asked = false;
    uint32_t t0 = g_L.now_ms;
    uint32_t seen = g_L.d->txq.frames;
    while (((g_L.now_ms - t0) < WRAP_PRE_MAX_MS) && !(asked && g_tap.pre && g_tap.post)) {
        ts_lb_step(&g_lb);
        if (!asked && (g_L.d->txq.frames != seen) && ((g_L.now_ms - t0) > WRAP_ASK_AFTER_MS)) {
            uint16_t to_wrap = (uint16_t)(0U - (uint32_t)g_L.d->txq.pkt_seq_next);
            bool now = pkt_mode ? ((to_wrap > 2U) && (to_wrap <= WRAP_KEY_LEAD))
                                : (g_L.d->frame_seq_next == 65535U);
            if (now) {
                TEST_ASSERT_EQUAL(TS_OK, tsr_send_keyframe_req(g_L.rx[0]));
                asked = true;
            }
        }
        seen = g_L.d->txq.frames;
    }
    TEST_ASSERT_TRUE_MESSAGE(asked, "setup: the wrap was not reached in time");
    TEST_ASSERT_TRUE_MESSAGE(g_tap.pre && g_tap.post, "setup: no loss on both sides of the wrap");
    TEST_ASSERT_TRUE_MESSAGE(g_tap.key_age_ms < WRAP_KEY_AGE_MS, "setup: the requested keyframe was not sent before the wrap");
}

static void wrap_run(bool pkt_mode, uint16_t seq_start, const char *tag)
{
    ts_loop_cfg_t c;
    ts_lb_window_t w;
    const tsr_metrics_t *m;
    uint32_t missing_at_wrap;

    lb_cfg(&c);
    c.daemon.seq_start = seq_start;
    c.netem.loss_pct = (uint8_t)WRAP_LOSS_PCT;
    c.netem.seed = 0x5EEDF00DU;
    (void)memset(&g_tap, 0, sizeof(g_tap));
    g_tap.pkt_mode = pkt_mode;
    lb_start(&c);
    ts_loop_set_tap(&g_L, wrap_tap_fn, &g_tap);

    wrap_reach(pkt_mode);
    missing_at_wrap = tsr_metrics(g_L.rx[0])->pkts_missing;
    ts_lb_run(&g_lb, WRAP_SETTLE_MS);
    ts_lb_window(&g_lb, WRAP_AFTER_MS, &w);
    m = tsr_metrics(g_L.rx[0]);
    (void)printf("  [%s] dropped %lu at the wrap (keyframe age %lu ms), next pkt_seq %u frame_seq %u, too_old %lu\n",
                 tag, (unsigned long)g_tap.dropped, (unsigned long)g_tap.key_age_ms,
                 (unsigned)g_L.d->txq.pkt_seq_next, (unsigned)g_L.d->frame_seq_next,
                 (unsigned long)tsd_stats(g_L.d)->nack_too_old);
    lb_print_window(tag, &w);
    lb_print_rx(tag, 0U);
    lb_print_netem(tag);

    lb_assert_invariants(1U);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, m->freezes, "freeze across the wrap");
    TEST_ASSERT_TRUE_MESSAGE(g_lb.rx[0].max_gap_ms < WRAP_MAX_GAP_MS, "long decode gap: losses at the wrap not repaired");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, tsd_stats(g_L.d)->nack_too_old, "a NACK for a fragment still in the ring was answered as too old");
    TEST_ASSERT_TRUE_MESSAGE(w.min_fps_1s[0] >= WRAP_MIN_FPS, "delivery not continuous after the wrap");
    TEST_ASSERT_TRUE_MESSAGE(m->pkts_missing > missing_at_wrap, "loss detection stopped after the wrap");
    TEST_ASSERT_TRUE_MESSAGE(m->retx_rx > 0U, "no retransmission received");
    lb_stop();
}

static void test_pkt_seq_wrap_losses_repaired(void)
{
    wrap_run(true, (uint16_t)WRAP_PKT_START, "wrap/pkt");
}

static void test_frame_seq_wrap_losses_repaired(void)
{
    wrap_run(false, (uint16_t)WRAP_FRAME_START, "wrap/frame");
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_pkt_seq_wrap_losses_repaired);
    RUN_TEST(test_frame_seq_wrap_losses_repaired);
    return UNITY_END();
}
