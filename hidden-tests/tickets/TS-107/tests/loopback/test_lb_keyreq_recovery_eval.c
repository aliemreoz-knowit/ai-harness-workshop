/*
 * tinystream v2 -- hidden evaluation test: tests/loopback/test_lb_keyreq_recovery_eval.c
 * (ticket metadata: meta.yaml next to this file)
 *
 * Short outages must heal long before the next periodic keyframe (10 s):
 *   1. cable pull of 1 s and of 0.5 s (shorter than every client / silence
 *      timeout, so no reconnect hides the problem): a fresh frame, exactly
 *      the encoder's reconstruction, is on screen within 1 s of the cable
 *      coming back, and the following 2 s run at >= 25 fps;
 *   2. a viewer that asks for a keyframe on the control channel
 *      (tsr_send_keyframe_req, the 'k' key) gets one within 200 ms when the
 *      last keyframe is older than the cooldown;
 *   3. a 50-datagram burst (twice, 2 s apart) loses several whole frames
 *      whose sizes the viewer cannot know: the viewer asks for a keyframe BY
 *      ITSELF within 100 ms of the burst's last drop (the test never calls
 *      tsr_send_keyframe_req here), the daemon sends it within 100 ms of the
 *      request (cooldown long passed), a fresh exact frame is on screen
 *      within 150 ms of the burst, at most one request per burst, the
 *      following second runs at >= 25 fps;
 *   4. 50 % loss in both directions during the first 150 ms of a session
 *      (data, NACKs and retransmissions alike): fragments of the first
 *      keyframe are given up after their NACK rounds. The viewer asks for a
 *      keyframe by itself no later than 50 ms after the first give-up; the
 *      daemon honours the 1 s cooldown after the session's first keyframe
 *      and answers within cooldown + 50 ms of the request; the answer is
 *      decoded within 100 ms; at most two requests in 1.5 s;
 *   5. request rate limits on the daemon: three explicit requests at +0,
 *      +100 ms and +600 ms -> all counted for the client, the second dropped
 *      by the 500 ms per-client limit (two reach the keyframe scheduler); the
 *      first is answered within 100 ms, the third is deferred to the 1 s
 *      cooldown and answered at cooldown .. cooldown + 50 ms after the first
 *      keyframe, no keyframe in between.
 * Frozen API only (ts_loop / ts_lbcheck / tsd_t / tsd_stats / tsr_*); losses
 * come from netem scenario lines; virtual clock, fixed seeds.
 */
#include "lb_common.h"

#define PULL_AT_MS     3000U
#define RECOVERY_MS    1000U
#define AFTER_MS       2000U
#define AFTER_MIN_FPS  25U
#define ASK_AFTER_MS   3000U    /* well past the first keyframe's cooldown, before the 10 s period */
#define ASK_ANSWER_MS  200U

#define BURST_LEN          50U
#define BURST_REQ_MS       100U   /* burst end -> the viewer's own request */
#define BURST_ANSWER_MS    100U   /* request -> keyframe sent (cooldown long passed) */
#define BURST_FRESH_MS     150U   /* burst end -> fresh exact frame decoded */
#define BURST_WATCH_MS     1000U
#define START_GIVEUP_MAX   400U   /* the first give-up must happen in this start window */
#define START_REQ_MS       50U    /* first give-up -> the viewer's own request */
#define START_WATCH_MS     1500U
#define START_MAX_REQS     2U
#define KEY_MARGIN_MS      50U    /* scheduling slack on top of the cooldown */
#define KEY_DECODE_MS      100U
#define RATE_SECOND_MS     100U   /* inside the per-client limit */
#define RATE_THIRD_MS      600U   /* past the per-client limit, inside the cooldown */
#define NONE               0xFFFFFFFFU

static ts_netem_script_t g_script;

void setUp(void)
{
}

void tearDown(void)
{
}

static void start_script(const char *const *lines)
{
    ts_netem_script_init(&g_script, &g_L.cfg.netem);
    for (const char *const *l = lines; *l != NULL; l++) {
        TEST_ASSERT_EQUAL(TS_OK, ts_netem_script_line(&g_script, *l));
    }
    ts_netem_scenario_start(&g_L.netem, ts_netem_script_finish(&g_script), g_L.now_ms);
}

static ts_client_t *active_client(void)
{
    ts_client_t *found = NULL;
    for (uint32_t i = 0U; (i < (uint32_t)TS_MAX_CLIENTS) && (found == NULL); i++) {
        if (g_L.d->clients.c[i].state == TS_CL_ACTIVE) {
            found = &g_L.d->clients.c[i];
        }
    }
    TEST_ASSERT_NOT_NULL_MESSAGE(found, "no active client on the daemon");
    return found;
}

static uint32_t rx_kreqs(void)
{
    return tsr_metrics(g_L.rx[0])->keyframe_reqs;
}

static uint32_t keys_sent(void)
{
    return tsd_stats(g_L.d)->keyframes_sent;
}

static void run_pull(const char *tag, const char *line, uint32_t pull_ms)
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
    c.netem.seed = 11U;
    lb_start(&c);
    ts_netem_script_init(&g_script, &g_L.cfg.netem);
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_script_line(&g_script, line));
    sc = ts_netem_script_finish(&g_script);
    t0 = g_L.now_ms;
    ts_netem_scenario_start(&g_L.netem, sc, t0);

    ts_lb_run(&g_lb, PULL_AT_MS);
    TEST_ASSERT_TRUE_MESSAGE(g_lb.rx[0].have_last, "nothing decoded before the pull");
    seq_before = g_lb.rx[0].last_seq;
    ts_lb_run(&g_lb, pull_ms);
    TEST_ASSERT_TRUE_MESSAGE(ts_netem_stats(&g_L.netem)->dropped_cable > 0U, "cable pull dropped nothing");
    decoded_before = ts_lb_decoded(&g_lb, 0U);
    restore_ms = t0 + PULL_AT_MS + pull_ms;

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
    (void)printf("  [%s] recovery %lu ms after the cable came back\n", tag, (unsigned long)recovery_ms);
    ts_lb_window(&g_lb, AFTER_MS, &w);
    lb_print_window(tag, &w);
    lb_print_rx(tag, 0U);
    lb_print_netem(tag);

    lb_assert_invariants(1U);
    TEST_ASSERT_FALSE_MESSAGE(g_L.netem.cfg.cable_pulled, "scenario did not restore the cable");
    TEST_ASSERT_TRUE_MESSAGE(recovered, "no fresh exact frame within 3 s of the cable coming back");
    TEST_ASSERT_TRUE_MESSAGE(recovery_ms <= RECOVERY_MS, "recovery after a short cable pull took more than 1 s");
    TEST_ASSERT_TRUE_MESSAGE(w.min_fps_1s[0] >= AFTER_MIN_FPS, "fps < 25 in the 2 s after recovery");
    lb_stop();
}

static void test_cable_pull_1s_recovers_within_1s(void)
{
    run_pull("pull1s", "t=3s blackout 1s", 1000U);
}

static void test_cable_pull_500ms_recovers_within_1s(void)
{
    run_pull("pull500", "t=3s blackout 500ms", 500U);
}

static void test_viewer_request_gets_a_keyframe(void)
{
    ts_loop_cfg_t c;
    uint32_t keys_before;
    uint32_t rx_keys_before;
    uint32_t waited = 0U;

    lb_cfg(&c);
    lb_start(&c);
    ts_lb_run(&g_lb, ASK_AFTER_MS);
    TEST_ASSERT_TRUE_MESSAGE(ts_lb_decoded(&g_lb, 0U) > 0U, "nothing decoded before the request");
    keys_before = tsd_stats(g_L.d)->keyframes_sent;
    rx_keys_before = tsr_metrics(g_L.rx[0])->keyframes;
    TEST_ASSERT_EQUAL(TS_OK, tsr_send_keyframe_req(g_L.rx[0]));
    while ((tsd_stats(g_L.d)->keyframes_sent == keys_before) && (waited < ASK_ANSWER_MS)) {
        ts_lb_step(&g_lb);
        waited++;
    }
    (void)printf("  [ask] keyframes_sent %lu -> %lu after %lu ms\n", (unsigned long)keys_before,
                 (unsigned long)tsd_stats(g_L.d)->keyframes_sent, (unsigned long)waited);
    TEST_ASSERT_TRUE_MESSAGE(tsd_stats(g_L.d)->keyframes_sent > keys_before,
                             "no keyframe within 200 ms of the viewer's request");
    ts_lb_run(&g_lb, 300U);
    TEST_ASSERT_TRUE_MESSAGE(tsr_metrics(g_L.rx[0])->keyframes > rx_keys_before, "requested keyframe not decoded");
    lb_assert_invariants(1U);
    lb_stop();
}

/* ---- the viewer asks by itself: a burst it cannot name ---------------------------- */
typedef struct burst_obs {
    uint32_t end_ms;     /* step in which the burst's last datagram was dropped */
    uint32_t req_ms;     /* first step with a new receiver KEYFRAME_REQ */
    uint32_t key_ms;     /* first step with a new keyframe sent by the daemon */
    uint32_t fresh_ms;   /* first step with a fresh exact frame decoded */
    uint32_t reqs;       /* receiver requests within BURST_WATCH_MS of the burst end */
} burst_obs_t;

static void watch_burst(uint32_t burst_no, burst_obs_t *o)
{
    uint32_t reqs0;
    uint32_t keys0;
    uint32_t decoded0;
    uint16_t seq0;
    uint32_t guard = 0U;

    TEST_ASSERT_TRUE_MESSAGE(g_lb.rx[0].have_last, "nothing decoded before the burst");
    o->end_ms = NONE;
    o->req_ms = NONE;
    o->key_ms = NONE;
    o->fresh_ms = NONE;
    reqs0 = rx_kreqs();
    keys0 = keys_sent();
    seq0 = g_lb.rx[0].last_seq;
    decoded0 = ts_lb_decoded(&g_lb, 0U);
    while (((o->end_ms == NONE) && (guard < 3000U)) ||
           ((o->end_ms != NONE) && ((g_L.now_ms - o->end_ms) < BURST_WATCH_MS))) {
        uint32_t step_ms = g_L.now_ms;
        uint32_t exact_before = g_lb.rx[0].exact_recon;
        ts_lb_step(&g_lb);
        guard++;
        if ((o->end_ms == NONE) && (ts_netem_stats(&g_L.netem)->dropped_burst >= (burst_no * BURST_LEN))) {
            o->end_ms = step_ms;
            seq0 = g_lb.rx[0].last_seq;            /* nothing gets through a burst */
            decoded0 = ts_lb_decoded(&g_lb, 0U);
        }
        if ((o->req_ms == NONE) && (rx_kreqs() != reqs0)) {
            o->req_ms = step_ms;
        }
        if ((o->key_ms == NONE) && (o->req_ms != NONE) && (keys_sent() != keys0)) {
            o->key_ms = step_ms;
        }
        if ((o->fresh_ms == NONE) && (o->end_ms != NONE) && (o->end_ms != step_ms) && (ts_lb_decoded(&g_lb, 0U) != decoded0) &&
            (g_lb.rx[0].exact_recon != exact_before) && ts_seq_after(g_lb.rx[0].last_seq, seq0)) {
            o->fresh_ms = step_ms;
        }
    }
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(burst_no * BURST_LEN, ts_netem_stats(&g_L.netem)->dropped_burst,
                                     "burst not applied");
    o->reqs = rx_kreqs() - reqs0;
}

/** For the log lines: `t - from`, -1 when `t` never happened. */
static long rel(uint32_t t, uint32_t from)
{
    return ((t == NONE) || (from == NONE)) ? -1L : (long)(t - from);
}

static void test_burst_viewer_requests_keyframe_by_itself(void)
{
    /* the middle line ends the first one-shot burst so the third re-arms it */
    static const char *const lines[] = { "t=3s burst=50", "t=4s burst=0", "t=5s burst=50", NULL };
    ts_loop_cfg_t c;
    ts_lb_window_t w;
    burst_obs_t o[2];
    ts_client_t *cl;
    uint32_t cl_reqs0;

    lb_cfg(&c);
    c.netem.seed = 11U;
    lb_start(&c);
    start_script(lines);
    ts_lb_run(&g_lb, 2900U);
    cl = active_client();
    cl_reqs0 = cl->ctr.keyframe_reqs;
    for (uint32_t b = 0U; b < 2U; b++) {
        watch_burst(b + 1U, &o[b]);
        (void)printf("  [burst%lu] end t=%lu: request +%ld ms, keyframe +%ld ms after the request, "
                     "fresh frame +%ld ms, requests %lu\n",
                     (unsigned long)(b + 1U), (unsigned long)o[b].end_ms, rel(o[b].req_ms, o[b].end_ms),
                     rel(o[b].key_ms, o[b].req_ms), rel(o[b].fresh_ms, o[b].end_ms),
                     (unsigned long)o[b].reqs);
    }
    ts_lb_window(&g_lb, 1000U, &w);
    lb_print_window("burst", &w);
    lb_print_rx("burst", 0U);
    lb_print_netem("burst");

    lb_assert_invariants(1U);
    for (uint32_t b = 0U; b < 2U; b++) {
        TEST_ASSERT_TRUE_MESSAGE(o[b].req_ms != NONE, "the viewer never asked for a keyframe after the burst");
        TEST_ASSERT_TRUE_MESSAGE(o[b].req_ms <= (o[b].end_ms + BURST_REQ_MS),
                                 "the viewer asked for a keyframe more than 100 ms after an unrepairable burst");
        TEST_ASSERT_TRUE_MESSAGE(o[b].key_ms != NONE, "no keyframe sent after the viewer's request");
        TEST_ASSERT_TRUE_MESSAGE((o[b].key_ms - o[b].req_ms) <= BURST_ANSWER_MS,
                                 "daemon answered the viewer's request more than 100 ms late (cooldown long passed)");
        TEST_ASSERT_TRUE_MESSAGE(o[b].fresh_ms != NONE, "no fresh frame within 1 s of the burst");
        TEST_ASSERT_TRUE_MESSAGE(o[b].fresh_ms <= (o[b].end_ms + BURST_FRESH_MS),
                                 "picture back more than 150 ms after the burst");
        TEST_ASSERT_TRUE_MESSAGE(o[b].reqs <= 1U, "more than one keyframe request per burst (cooldown)");
    }
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(rx_kreqs(), cl->ctr.keyframe_reqs - cl_reqs0,
                                     "the daemon did not see every request of the viewer");
    TEST_ASSERT_TRUE_MESSAGE(w.min_fps_1s[0] >= AFTER_MIN_FPS, "fps < 25 in the second after the bursts");
    lb_stop();
}

/* ---- the viewer asks by itself: NACK rounds exhausted at session start ------------ */
static void run_start_loss(uint32_t seed)
{
    static const char *const lines[] = { "t=0 loss=50%", "t=150ms loss=0", NULL };
    ts_loop_cfg_t c;
    uint32_t t0;
    uint32_t giveup_ms = NONE;
    uint32_t req_ms = NONE;
    uint32_t prev_key_ms = NONE;   /* last keyframe before the request */
    uint32_t key_ms = NONE;        /* first keyframe after the request */
    uint32_t key_rxk = 0U;         /* receiver keyframes before the answer */
    uint32_t dec_ms = NONE;
    uint32_t keys = 0U;
    char tag[16];

    (void)snprintf(tag, sizeof(tag), "start%lu", (unsigned long)seed);
    lb_cfg(&c);
    c.netem.seed = seed;
    lb_start(&c);
    start_script(lines);
    t0 = g_L.now_ms;
    keys = keys_sent();
    while ((g_L.now_ms - t0) < START_WATCH_MS) {
        uint32_t step_ms = g_L.now_ms;
        uint32_t rxk = tsr_metrics(g_L.rx[0])->keyframes;
        ts_lb_step(&g_lb);
        if ((giveup_ms == NONE) && (tsr_metrics(g_L.rx[0])->nack_giveups > 0U)) {
            giveup_ms = step_ms;
        }
        if ((req_ms == NONE) && (rx_kreqs() > 0U)) {
            req_ms = step_ms;
        }
        if (keys_sent() != keys) {
            keys = keys_sent();
            if (req_ms == NONE) {
                prev_key_ms = step_ms;
            } else if (key_ms == NONE) {
                key_ms = step_ms;
                key_rxk = rxk;
            } else {
                /* later keyframes: not watched */
            }
        }
        if ((dec_ms == NONE) && (key_ms != NONE) && (tsr_metrics(g_L.rx[0])->keyframes > key_rxk)) {
            dec_ms = step_ms;
        }
    }
    (void)printf("  [%s] first give-up +%ld ms, request +%ld ms, previous keyframe +%ld ms, answer +%ld ms "
                 "(%ld ms after the request), decoded +%ld ms, requests %lu\n",
                 tag, rel(giveup_ms, t0), rel(req_ms, t0), rel(prev_key_ms, t0),
                 rel(key_ms, t0), rel(key_ms, req_ms), rel(dec_ms, t0),
                 (unsigned long)rx_kreqs());
    lb_print_rx(tag, 0U);
    lb_print_netem(tag);

    lb_assert_invariants(1U);
    TEST_ASSERT_TRUE_MESSAGE(giveup_ms != NONE, "scenario invalid: no NACK give-up in the lossy start");
    TEST_ASSERT_TRUE_MESSAGE((giveup_ms - t0) <= START_GIVEUP_MAX, "scenario invalid: first give-up too late");
    TEST_ASSERT_TRUE_MESSAGE(req_ms != NONE, "the viewer never asked for a keyframe after giving packets up");
    TEST_ASSERT_TRUE_MESSAGE(req_ms <= (giveup_ms + START_REQ_MS),
                             "the viewer asked for a keyframe more than 50 ms after giving packets up");
    TEST_ASSERT_TRUE_MESSAGE(prev_key_ms != NONE, "no keyframe sent at session start");
    TEST_ASSERT_TRUE_MESSAGE(key_ms != NONE, "the viewer's request was never answered");
    TEST_ASSERT_TRUE_MESSAGE((key_ms - prev_key_ms) >= (TS_KEYFRAME_COOLDOWN_MS - 2U),
                             "keyframe cooldown not respected");
    TEST_ASSERT_TRUE_MESSAGE((key_ms - req_ms) <= (TS_KEYFRAME_COOLDOWN_MS + KEY_MARGIN_MS),
                             "keyframe sent later than the cooldown after the request");
    TEST_ASSERT_TRUE_MESSAGE(dec_ms != NONE, "requested keyframe not decoded");
    TEST_ASSERT_TRUE_MESSAGE((dec_ms - key_ms) <= KEY_DECODE_MS, "requested keyframe decoded late");
    TEST_ASSERT_TRUE_MESSAGE(rx_kreqs() <= START_MAX_REQS, "keyframe requests not rate-limited");
    lb_stop();
}

static void test_lossy_start_viewer_requests_keyframe_by_itself(void)
{
    run_start_loss(1U);
    run_start_loss(2U);
}

/* ---- daemon-side limits ----------------------------------------------------------------- */
static void test_request_limits_on_the_daemon(void)
{
    ts_loop_cfg_t c;
    ts_client_t *cl;
    uint32_t cl0;
    uint32_t sched0;
    uint32_t keys0;
    uint32_t rx_keys0;
    uint32_t t_ask;
    uint32_t key1_ms = NONE;
    uint32_t key2_ms = NONE;
    uint32_t extra = 0U;

    lb_cfg(&c);
    lb_start(&c);
    ts_lb_run(&g_lb, ASK_AFTER_MS);
    cl = active_client();
    cl0 = cl->ctr.keyframe_reqs;
    sched0 = g_L.d->keysched.requests_total;
    keys0 = keys_sent();
    rx_keys0 = tsr_metrics(g_L.rx[0])->keyframes;
    t_ask = g_L.now_ms;

    TEST_ASSERT_EQUAL(TS_OK, tsr_send_keyframe_req(g_L.rx[0]));
    while ((g_L.now_ms - t_ask) < (RATE_THIRD_MS + TS_KEYFRAME_COOLDOWN_MS)) {
        uint32_t step_ms = g_L.now_ms;
        if ((step_ms - t_ask) == RATE_SECOND_MS) {
            TEST_ASSERT_EQUAL(TS_OK, tsr_send_keyframe_req(g_L.rx[0]));
        }
        if ((step_ms - t_ask) == RATE_THIRD_MS) {
            TEST_ASSERT_EQUAL(TS_OK, tsr_send_keyframe_req(g_L.rx[0]));
        }
        ts_lb_step(&g_lb);
        if (keys_sent() != keys0) {
            if (key1_ms == NONE) {
                key1_ms = step_ms;
            } else if (key2_ms == NONE) {
                key2_ms = step_ms;
            } else {
                extra++;
            }
            keys0 = keys_sent();
        }
    }
    (void)printf("  [limits] client requests %lu, scheduler requests %lu, keyframe 1 +%ld ms, keyframe 2 +%ld ms "
                 "(%ld ms after keyframe 1)\n",
                 (unsigned long)(cl->ctr.keyframe_reqs - cl0), (unsigned long)(g_L.d->keysched.requests_total - sched0),
                 rel(key1_ms, t_ask), rel(key2_ms, t_ask),
                 rel(key2_ms, key1_ms));

    lb_assert_invariants(1U);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(3U, cl->ctr.keyframe_reqs - cl0, "daemon did not count every request");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(2U, g_L.d->keysched.requests_total - sched0,
                                     "per-client 500 ms request limit not applied");
    TEST_ASSERT_TRUE_MESSAGE(key1_ms != NONE, "first request not answered");
    TEST_ASSERT_TRUE_MESSAGE((key1_ms - t_ask) <= BURST_ANSWER_MS, "first request answered more than 100 ms late");
    TEST_ASSERT_TRUE_MESSAGE(key2_ms != NONE, "request after the per-client limit never answered");
    TEST_ASSERT_TRUE_MESSAGE((key2_ms - key1_ms) >= (TS_KEYFRAME_COOLDOWN_MS - 2U), "keyframe cooldown not respected");
    TEST_ASSERT_TRUE_MESSAGE((key2_ms - key1_ms) <= (TS_KEYFRAME_COOLDOWN_MS + KEY_MARGIN_MS),
                             "deferred request answered later than cooldown + 50 ms");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, extra, "more keyframes than requests");
    TEST_ASSERT_TRUE_MESSAGE(tsr_metrics(g_L.rx[0])->keyframes >= (rx_keys0 + 2U), "requested keyframes not decoded");
    lb_stop();
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_cable_pull_1s_recovers_within_1s);
    RUN_TEST(test_cable_pull_500ms_recovers_within_1s);
    RUN_TEST(test_viewer_request_gets_a_keyframe);
    RUN_TEST(test_burst_viewer_requests_keyframe_by_itself);
    RUN_TEST(test_lossy_start_viewer_requests_keyframe_by_itself);
    RUN_TEST(test_request_limits_on_the_daemon);
    return UNITY_END();
}
