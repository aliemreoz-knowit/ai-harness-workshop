/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/unit/test_rate.c
 * Token buckets (incl. global cap, retransmit charging and the per-tick
 * retransmit cap), loss adaptation and the keyframe scheduler, all driven by
 * a virtual clock (plain u32 milliseconds, no sleeps).
 */
#include <string.h>
#include "unity.h"
#include "ts_config.h"
#include "ts_rate.h"
#include "ts_clients.h"

void setUp(void)
{
}

void tearDown(void)
{
}

/* ---- token bucket ---------------------------------------------------------- */
static void test_bucket_init_take_refuse(void)
{
    ts_bucket_t b;
    ts_bucket_init(&b, 8000U, 5000U, 100U);
    TEST_ASSERT_EQUAL_UINT32(5000U, ts_bucket_tokens(&b));
    TEST_ASSERT_TRUE(ts_bucket_take(&b, 3000U));
    TEST_ASSERT_TRUE(ts_bucket_take(&b, 2000U));
    TEST_ASSERT_FALSE(ts_bucket_take(&b, 1U));
    TEST_ASSERT_EQUAL_UINT32(0U, ts_bucket_tokens(&b));
    TEST_ASSERT_EQUAL_UINT32(5000U, b.taken);
    TEST_ASSERT_EQUAL_UINT32(1U, b.refused);
    TEST_ASSERT_TRUE(ts_bucket_take(&b, 0U));
    TEST_ASSERT_FALSE(ts_bucket_take(NULL, 1U));
    TEST_ASSERT_EQUAL_UINT32(0U, ts_bucket_tokens(NULL));
}

static void test_bucket_refill_kbps_units(void)
{
    /* 8000 kbps = 8000 bit/ms = 1000 B/ms (kilobits, not kilobytes, not bits/s) */
    ts_bucket_t b;
    ts_bucket_init(&b, 8000U, 100000U, 0U);
    TEST_ASSERT_TRUE(ts_bucket_take(&b, 100000U));
    ts_bucket_refill(&b, 1U);
    TEST_ASSERT_EQUAL_UINT32(1000U, ts_bucket_tokens(&b));
    ts_bucket_refill(&b, 11U);
    TEST_ASSERT_EQUAL_UINT32(11000U, ts_bucket_tokens(&b));
    ts_bucket_refill(&b, 11U);                       /* no time, no tokens */
    TEST_ASSERT_EQUAL_UINT32(11000U, ts_bucket_tokens(&b));
    ts_bucket_refill(&b, 1000U);                     /* capped at burst */
    TEST_ASSERT_EQUAL_UINT32(100000U, ts_bucket_tokens(&b));
    /* the default rate: 4000 kbps = 500 kB/s */
    ts_bucket_init(&b, TS_DEFAULT_KBPS, 1000000U, 0U);
    TEST_ASSERT_TRUE(ts_bucket_take(&b, 1000000U));
    ts_bucket_refill(&b, 1000U);
    TEST_ASSERT_EQUAL_UINT32(500000U, ts_bucket_tokens(&b));
}

static void test_bucket_slow_rate_fine_ticks(void)
{
    /* 4 kbps = 0.5 B/ms refilled every ms must not round down to zero forever */
    ts_bucket_t b;
    ts_bucket_init(&b, 4U, 100U, 0U);
    TEST_ASSERT_TRUE(ts_bucket_take(&b, 100U));
    for (uint32_t t = 1U; t <= 1000U; t++) {
        ts_bucket_refill(&b, t);
    }
    TEST_ASSERT_EQUAL_UINT32(100U, ts_bucket_tokens(&b));  /* 500 B earned, capped */
    TEST_ASSERT_TRUE(ts_bucket_take(&b, 100U));
    for (uint32_t t = 1001U; t <= 1100U; t++) {
        ts_bucket_refill(&b, t);
    }
    TEST_ASSERT_EQUAL_UINT32(50U, ts_bucket_tokens(&b));
}

static void test_bucket_clock_wrap(void)
{
    ts_bucket_t b;
    ts_bucket_init(&b, 8000U, 50000U, 0xFFFFFFF0U);
    TEST_ASSERT_TRUE(ts_bucket_take(&b, 50000U));
    ts_bucket_refill(&b, 0x00000010U);               /* 32 ms across the wrap */
    TEST_ASSERT_EQUAL_UINT32(32000U, ts_bucket_tokens(&b));
}

static void test_bucket_unlimited_and_set_rate(void)
{
    ts_bucket_t b;
    ts_bucket_init(&b, 0U, 10U, 0U);
    TEST_ASSERT_TRUE(ts_bucket_take(&b, 1000000U));   /* kbps 0 = unlimited */
    TEST_ASSERT_EQUAL_UINT32(0U, ts_bucket_wait_ms(&b, 1000000U));
    ts_bucket_refill(&b, 5U);
    TEST_ASSERT_EQUAL_UINT32(10U, ts_bucket_tokens(&b));
    ts_bucket_set_rate(&b, 800U);                     /* 100 B/ms */
    TEST_ASSERT_TRUE(ts_bucket_take(&b, 10U));
    TEST_ASSERT_FALSE(ts_bucket_take(&b, 1U));
    ts_bucket_refill(&b, 6U);
    TEST_ASSERT_EQUAL_UINT32(10U, ts_bucket_tokens(&b));
    ts_bucket_set_rate(NULL, 1U);
    ts_bucket_refill(NULL, 1U);
    ts_bucket_init(NULL, 1U, 1U, 1U);
}

static void test_bucket_wait_ms(void)
{
    ts_bucket_t b;
    ts_bucket_init(&b, 8U, 100U, 0U);                 /* 1 B/ms */
    TEST_ASSERT_EQUAL_UINT32(0U, ts_bucket_wait_ms(&b, 100U));
    TEST_ASSERT_TRUE(ts_bucket_take(&b, 100U));
    TEST_ASSERT_EQUAL_UINT32(10U, ts_bucket_wait_ms(&b, 10U));
    TEST_ASSERT_EQUAL_UINT32(UINT32_MAX, ts_bucket_wait_ms(&b, 101U));  /* above capacity */
    ts_bucket_init(&b, 3U, 100U, 0U);                 /* 0.375 B/ms: rounds up */
    TEST_ASSERT_TRUE(ts_bucket_take(&b, 100U));
    TEST_ASSERT_EQUAL_UINT32(3U, ts_bucket_wait_ms(&b, 1U));
    TEST_ASSERT_EQUAL_UINT32(0U, ts_bucket_wait_ms(NULL, 1U));
}

static void test_bucket_global_cap_pair(void)
{
    ts_bucket_t c1;
    ts_bucket_t c2;
    ts_bucket_t g;
    ts_bucket_init(&c1, 8000U, 3000U, 0U);
    ts_bucket_init(&c2, 8000U, 3000U, 0U);
    ts_bucket_init(&g, 8000U, 4000U, 0U);             /* global cap below the sum */
    TEST_ASSERT_TRUE(ts_bucket_take_pair(&c1, &g, 2500U));
    TEST_ASSERT_FALSE(ts_bucket_take_pair(&c2, &g, 2500U));   /* global lacks: neither charged */
    TEST_ASSERT_EQUAL_UINT32(3000U, ts_bucket_tokens(&c2));
    TEST_ASSERT_EQUAL_UINT32(1500U, ts_bucket_tokens(&g));
    TEST_ASSERT_EQUAL_UINT32(1U, g.refused);
    TEST_ASSERT_EQUAL_UINT32(0U, c2.refused);
    TEST_ASSERT_FALSE(ts_bucket_take_pair(&c1, &g, 1000U));   /* client lacks: global kept */
    TEST_ASSERT_EQUAL_UINT32(1500U, ts_bucket_tokens(&g));
    TEST_ASSERT_EQUAL_UINT32(1U, c1.refused);
    TEST_ASSERT_TRUE(ts_bucket_take_pair(&c2, &g, 1500U));
    TEST_ASSERT_EQUAL_UINT32(0U, ts_bucket_tokens(&g));
    TEST_ASSERT_TRUE(ts_bucket_take_pair(&c2, NULL, 1500U));  /* no global cap */
    TEST_ASSERT_FALSE(ts_bucket_take_pair(NULL, &g, 1U));
    ts_bucket_refill(&g, 1U);
    TEST_ASSERT_EQUAL_UINT32(1000U, ts_bucket_tokens(&g));
    {
        /* both short: both count the refusal */
        TEST_ASSERT_FALSE(ts_bucket_take_pair(&c2, &g, 2000U));
        TEST_ASSERT_EQUAL_UINT32(1U, c2.refused);
        TEST_ASSERT_EQUAL_UINT32(2U, g.refused);
    }
}

/* ---- retransmit charging + per-tick cap (client bucket) --------------------- */
static void client_for_rate(ts_client_t *c, uint32_t kbps, uint32_t burst)
{
    (void)memset(c, 0, sizeof(*c));
    c->state = TS_CL_ACTIVE;
    c->kbps = kbps;
    ts_bucket_init(&c->bucket, kbps, burst, 0U);
}

static void test_retx_charged_to_client_bucket(void)
{
    ts_client_t c;
    ts_bucket_t g;
    client_for_rate(&c, 8000U, 3000U);
    ts_bucket_init(&g, 0U, 0U, 0U);                   /* unlimited global */
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_retx_charge(&c, &g, 1424U, 0U));
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_retx_charge(&c, &g, 1424U, 0U));
    TEST_ASSERT_EQUAL_UINT32(3000U - 2848U, ts_bucket_tokens(&c.bucket));
    TEST_ASSERT_EQUAL_INT(TS_E_AGAIN, ts_client_retx_charge(&c, &g, 1424U, 0U));  /* bucket empty */
    TEST_ASSERT_EQUAL_UINT32(2U, c.ctr.retx_sent);
    TEST_ASSERT_EQUAL_UINT32(1U, c.ctr.retx_dropped);
    TEST_ASSERT_EQUAL_UINT32(2848U, c.ctr.bytes_sent);
    TEST_ASSERT_EQUAL_UINT8(2U, c.retx_this_tick);
    /* the global cap also limits retransmissions */
    client_for_rate(&c, 0U, 0U);
    ts_bucket_init(&g, 8000U, 1000U, 0U);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_retx_charge(&c, &g, 600U, 0U));
    TEST_ASSERT_EQUAL_INT(TS_E_AGAIN, ts_client_retx_charge(&c, &g, 600U, 0U));
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, ts_client_retx_charge(NULL, &g, 600U, 0U));
}

static void test_retx_per_tick_cap(void)
{
    ts_clients_t t;
    ts_client_t *c;
    ts_clients_init(&t);
    c = &t.c[0];
    client_for_rate(c, 0U, 0U);                       /* unlimited: only the cap bites */
    for (uint32_t i = 0U; i < TS_RETX_PER_TICK; i++) {
        TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_retx_charge(c, NULL, 100U, 0U));
    }
    TEST_ASSERT_EQUAL_INT(TS_E_FULL, ts_client_retx_charge(c, NULL, 100U, 0U));
    TEST_ASSERT_EQUAL_UINT32(TS_RETX_PER_TICK, c->ctr.retx_sent);
    TEST_ASSERT_EQUAL_UINT32(1U, c->ctr.retx_dropped);
    ts_clients_tick(&t, 1U);                          /* next tick: budget restored */
    TEST_ASSERT_EQUAL_UINT8(0U, c->retx_this_tick);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_retx_charge(c, NULL, 100U, 0U));
    /* explicit cap */
    ts_clients_tick(&t, 2U);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_retx_charge(c, NULL, 100U, 2U));
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_retx_charge(c, NULL, 100U, 2U));
    TEST_ASSERT_EQUAL_INT(TS_E_FULL, ts_client_retx_charge(c, NULL, 100U, 2U));
}

static void test_retx_and_fresh_share_the_bucket(void)
{
    /* retransmissions consume the same tokens fresh packets need (no bypass) */
    ts_clients_t t;
    ts_client_t *c;
    ts_clients_init(&t);
    c = &t.c[0];
    client_for_rate(c, 8000U, 2000U);                 /* 1000 B/ms */
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_retx_charge(c, NULL, 1500U, 0U));
    TEST_ASSERT_FALSE(ts_bucket_take_pair(&c->bucket, NULL, 1000U));
    ts_clients_tick(&t, 1U);
    TEST_ASSERT_EQUAL_UINT32(1500U, ts_bucket_tokens(&c->bucket));
    TEST_ASSERT_TRUE(ts_bucket_take_pair(&c->bucket, NULL, 1000U));
}

/* ---- loss adaptation -------------------------------------------------------- */
static void test_loss_ratio(void)
{
    ts_loss_t l;
    ts_loss_init(&l, 0U);
    TEST_ASSERT_EQUAL_UINT32(0U, ts_loss_ratio_pct(&l));
    ts_loss_on_sent(&l, 200U);
    ts_loss_on_nack(&l, 25U);
    TEST_ASSERT_EQUAL_UINT32(12U, ts_loss_ratio_pct(&l));
    ts_loss_on_nack(&l, 1000U);
    TEST_ASSERT_EQUAL_UINT32(100U, ts_loss_ratio_pct(&l));   /* capped */
    ts_loss_init(&l, 0U);
    ts_loss_on_nack(&l, 1U);
    TEST_ASSERT_EQUAL_UINT32(100U, ts_loss_ratio_pct(&l));   /* NACKs, nothing sent */
    TEST_ASSERT_EQUAL_UINT32(0U, ts_loss_ratio_pct(NULL));
    ts_loss_on_sent(NULL, 1U);
    ts_loss_on_nack(NULL, 1U);
    ts_loss_init(NULL, 0U);
    TEST_ASSERT_EQUAL_UINT8(1U, ts_loss_update(NULL, 0U));
}

static void test_loss_steps_down_fast_up_slow(void)
{
    ts_loss_t l;
    uint32_t now = 5000U;
    ts_loss_init(&l, now);
    TEST_ASSERT_EQUAL_UINT8(1U, l.quality);
    /* 11 % inside the window: nothing until the window closes */
    ts_loss_on_sent(&l, 100U);
    ts_loss_on_nack(&l, 11U);
    TEST_ASSERT_EQUAL_UINT8(1U, ts_loss_update(&l, now + 999U));
    TEST_ASSERT_EQUAL_UINT8(2U, ts_loss_update(&l, now + 1000U));
    now += 1000U;
    TEST_ASSERT_EQUAL_UINT32(0U, l.pkts);             /* new window */
    ts_loss_on_sent(&l, 100U);
    ts_loss_on_nack(&l, 50U);
    TEST_ASSERT_EQUAL_UINT8(4U, ts_loss_update(&l, now + 1000U));
    now += 1000U;
    ts_loss_on_sent(&l, 100U);
    ts_loss_on_nack(&l, 50U);
    TEST_ASSERT_EQUAL_UINT8(4U, ts_loss_update(&l, now + 1000U));   /* floor */
    now += 1000U;
    TEST_ASSERT_EQUAL_UINT32(2U, l.steps_down);
    /* exactly 10 %: hold (strictly above steps down) */
    ts_loss_on_sent(&l, 100U);
    ts_loss_on_nack(&l, 10U);
    TEST_ASSERT_EQUAL_UINT8(4U, ts_loss_update(&l, now + 1000U));
    now += 1000U;
    /* clean windows step up one level each */
    ts_loss_on_sent(&l, 100U);
    TEST_ASSERT_EQUAL_UINT8(2U, ts_loss_update(&l, now + 1000U));
    now += 1000U;
    ts_loss_on_sent(&l, 100U);
    TEST_ASSERT_EQUAL_UINT8(2U, ts_loss_update(&l, now + 500U));    /* mid-window */
    TEST_ASSERT_EQUAL_UINT8(1U, ts_loss_update(&l, now + 1000U));
    now += 1000U;
    TEST_ASSERT_EQUAL_UINT8(1U, ts_loss_update(&l, now + 1000U));   /* ceiling */
    TEST_ASSERT_EQUAL_UINT32(2U, l.steps_up);
}

static void test_loss_window_across_clock_wrap(void)
{
    ts_loss_t l;
    ts_loss_init(&l, 0xFFFFFF00U);
    ts_loss_on_sent(&l, 10U);
    ts_loss_on_nack(&l, 5U);
    TEST_ASSERT_EQUAL_UINT8(1U, ts_loss_update(&l, 0x00000100U));   /* 512 ms */
    TEST_ASSERT_EQUAL_UINT8(2U, ts_loss_update(&l, 0x000002E8U));   /* 1000 ms */
}

/* ---- keyframe scheduler ---------------------------------------------------- */
static void test_keysched_first_frame_forced_then_periodic(void)
{
    ts_keysched_t k;
    ts_keysched_init(&k, 1000U);
    TEST_ASSERT_EQUAL_INT(TS_KEY_FORCED, ts_keysched_decide(&k, 1000U, 0U));
    ts_keysched_sent(&k, 1000U);
    TEST_ASSERT_EQUAL_INT(TS_KEY_NONE, ts_keysched_decide(&k, 1033U, 0U));
    TEST_ASSERT_EQUAL_INT(TS_KEY_NONE, ts_keysched_decide(&k, 10999U, 0U));
    TEST_ASSERT_EQUAL_INT(TS_KEY_PERIODIC, ts_keysched_decide(&k, 11000U, 0U));
    TEST_ASSERT_EQUAL_INT(TS_KEY_PERIODIC, k.last_reason);
    TEST_ASSERT_EQUAL_INT(TS_KEY_PERIODIC, ts_keysched_decide(&k, 11033U, 0U)); /* until sent */
    ts_keysched_sent(&k, 11033U);
    TEST_ASSERT_EQUAL_INT(TS_KEY_NONE, ts_keysched_decide(&k, 21032U, 0U));
    TEST_ASSERT_EQUAL_INT(TS_KEY_PERIODIC, ts_keysched_decide(&k, 21033U, 0U));
    ts_keysched_force(&k);
    TEST_ASSERT_EQUAL_INT(TS_KEY_FORCED, ts_keysched_decide(&k, 21033U, 0U));
    ts_keysched_request(&k, TS_KEY_FORCED);
    TEST_ASSERT_TRUE(k.forced);
    TEST_ASSERT_EQUAL_INT(TS_KEY_NONE, ts_keysched_decide(NULL, 0U, 0U));
}

static void test_keysched_request_cooldown_deferred_not_lost(void)
{
    ts_keysched_t k;
    ts_keysched_init(&k, 0U);
    ts_keysched_sent(&k, 0U);
    ts_keysched_request(&k, TS_KEY_REQUEST);
    TEST_ASSERT_EQUAL_UINT32(1U, k.requests_total);
    TEST_ASSERT_EQUAL_INT(TS_KEY_NONE, ts_keysched_decide(&k, 500U, 0U));     /* in cooldown */
    TEST_ASSERT_EQUAL_INT(TS_KEY_NONE, ts_keysched_decide(&k, 999U, 0U));
    TEST_ASSERT_EQUAL_INT(TS_KEY_REQUEST, ts_keysched_decide(&k, 1000U, 0U)); /* deferred, served */
    ts_keysched_sent(&k, 1000U);
    TEST_ASSERT_FALSE(k.requested);
    TEST_ASSERT_EQUAL_INT(TS_KEY_NONE, ts_keysched_decide(&k, 1500U, 0U));
    /* outside the cooldown a request is served on the very next frame */
    ts_keysched_request(&k, TS_KEY_REQUEST);
    TEST_ASSERT_EQUAL_INT(TS_KEY_REQUEST, ts_keysched_decide(&k, 2000U, 0U));
    ts_keysched_sent(&k, 2000U);
    /* a burst of requests within the cooldown coalesces into one keyframe */
    ts_keysched_request(&k, TS_KEY_REQUEST);
    ts_keysched_request(&k, TS_KEY_REQUEST);
    ts_keysched_request(&k, TS_KEY_TOO_OLD);
    TEST_ASSERT_EQUAL_UINT32(5U, k.requests_total);
    TEST_ASSERT_EQUAL_UINT32(2U, k.requests_dropped);
    TEST_ASSERT_EQUAL_INT(TS_KEY_NONE, ts_keysched_decide(&k, 2100U, 0U));
    TEST_ASSERT_EQUAL_INT(TS_KEY_REQUEST, ts_keysched_decide(&k, 3000U, 0U));
    ts_keysched_request(&k, TS_KEY_NONE);             /* ignored */
    TEST_ASSERT_EQUAL_UINT32(5U, k.requests_total);
    ts_keysched_request(NULL, TS_KEY_REQUEST);
    ts_keysched_force(NULL);
    ts_keysched_sent(NULL, 0U);
    ts_keysched_init(NULL, 0U);
}

static void test_keysched_too_old_reason(void)
{
    ts_keysched_t k;
    ts_keysched_init(&k, 0U);
    ts_keysched_sent(&k, 0U);
    ts_keysched_request(&k, TS_KEY_TOO_OLD);
    TEST_ASSERT_EQUAL_INT(TS_KEY_NONE, ts_keysched_decide(&k, 10U, 0U));
    TEST_ASSERT_EQUAL_INT(TS_KEY_TOO_OLD, ts_keysched_decide(&k, 1200U, 0U));
    ts_keysched_sent(&k, 1200U);
    TEST_ASSERT_EQUAL_INT(TS_KEY_NONE, ts_keysched_decide(&k, 2400U, 0U));
}

static void test_keysched_scene_change(void)
{
    ts_keysched_t k;
    ts_keysched_init(&k, 0U);
    ts_keysched_sent(&k, 0U);
    /* > 60 % of 300 tiles = more than 180 */
    TEST_ASSERT_EQUAL_INT(TS_KEY_NONE, ts_keysched_decide(&k, 2000U, 180U));
    TEST_ASSERT_EQUAL_INT(TS_KEY_SCENE, ts_keysched_decide(&k, 2000U, 181U));
    ts_keysched_sent(&k, 2000U);
    /* 1 s cooldown after any keyframe */
    TEST_ASSERT_EQUAL_INT(TS_KEY_NONE, ts_keysched_decide(&k, 2999U, 300U));
    TEST_ASSERT_EQUAL_INT(TS_KEY_SCENE, ts_keysched_decide(&k, 3000U, 300U));
    ts_keysched_sent(&k, 3000U);
    /* a pending request wins over a scene change */
    ts_keysched_request(&k, TS_KEY_REQUEST);
    TEST_ASSERT_EQUAL_INT(TS_KEY_REQUEST, ts_keysched_decide(&k, 4000U, 300U));
}

static void test_keysched_clock_wrap(void)
{
    ts_keysched_t k;
    ts_keysched_init(&k, 0xFFFFF000U);
    ts_keysched_sent(&k, 0xFFFFF000U);
    TEST_ASSERT_EQUAL_INT(TS_KEY_NONE, ts_keysched_decide(&k, 0x00000010U, 0U));
    TEST_ASSERT_EQUAL_INT(TS_KEY_PERIODIC, ts_keysched_decide(&k, 0xFFFFF000U + 10000U, 0U));
}

/* ---- congestion control ------------------------------------------------------ */
/* One window of `frames` frames at the given P-frame size (wire bytes), 30 fps. */
static void cc_window(ts_cc_t *cc, uint32_t t0, uint32_t p_bytes, uint32_t frames, uint32_t backlog_ms)
{
    for (uint32_t i = 0U; i < frames; i++) {
        ts_cc_on_frame(cc, p_bytes, false);
        ts_cc_on_backlog(cc, backlog_ms);
    }
    (void)t0;
}

static void test_cc_backlog_ms(void)
{
    TEST_ASSERT_EQUAL_UINT32(0U, ts_cc_backlog_ms(0U, 1000U));
    TEST_ASSERT_EQUAL_UINT32(0U, ts_cc_backlog_ms(10U, 0U));             /* unlimited */
    /* 10 x 1424 B x 8 bit = 113920 bit at 1000 bit/ms -> 114 ms (rounded up) */
    TEST_ASSERT_EQUAL_UINT32(114U, ts_cc_backlog_ms(10U, 1000U));
    TEST_ASSERT_EQUAL_UINT32(UINT32_MAX, ts_cc_backlog_ms(UINT32_MAX, 1U));
}

static void test_cc_steps_down_when_stream_exceeds_limit(void)
{
    ts_cc_t cc;
    ts_cc_init(&cc, 0U);
    TEST_ASSERT_EQUAL_UINT8(1U, cc.quality);
    /* 30 P-frames of 6800 B in 1 s = 1632 kbit/s against a 1000 kbit/s viewer */
    cc_window(&cc, 0U, 6800U, 30U, 0U);
    TEST_ASSERT_EQUAL_UINT8(1U, ts_cc_update(&cc, 999U, 1000U));        /* window not closed yet */
    TEST_ASSERT_EQUAL_UINT8(2U, ts_cc_update(&cc, 1000U, 1000U));
    TEST_ASSERT_EQUAL_UINT32(1632U, cc.est_kbps);
    TEST_ASSERT_EQUAL_UINT32(1U, cc.steps_down);
    /* at q2 the stream is ~816 kbit/s: inside the band (<= 90 %, 2x > 85 %): hold */
    cc_window(&cc, 1000U, 3400U, 30U, 0U);
    TEST_ASSERT_EQUAL_UINT8(2U, ts_cc_update(&cc, 2000U, 1000U));
    TEST_ASSERT_EQUAL_UINT32(816U, cc.est_kbps);
    /* still too big at q2 -> q4, and q4 is the floor */
    cc_window(&cc, 2000U, 4000U, 30U, 0U);
    TEST_ASSERT_EQUAL_UINT8(4U, ts_cc_update(&cc, 3000U, 1000U));
    cc_window(&cc, 3000U, 8000U, 30U, 0U);
    TEST_ASSERT_EQUAL_UINT8(4U, ts_cc_update(&cc, 4000U, 1000U));
    TEST_ASSERT_EQUAL_UINT32(2U, cc.steps_down);
}

static void test_cc_keyframes_do_not_count_as_stream_rate(void)
{
    ts_cc_t cc;
    ts_cc_init(&cc, 0U);
    ts_cc_on_frame(&cc, 77000U, true);                 /* one big keyframe ... */
    cc_window(&cc, 0U, 2000U, 29U, 0U);                /* ... and small P-frames */
    TEST_ASSERT_EQUAL_UINT8(1U, ts_cc_update(&cc, 1000U, 1000U));
    TEST_ASSERT_EQUAL_UINT32(480U, cc.est_kbps);       /* 2000 B x 30 frames x 8 / 1000 ms */
}

static void test_cc_standing_queue_steps_down(void)
{
    ts_cc_t cc;
    ts_cc_init(&cc, 0U);
    /* the estimate fits, but the backlog never drained below 300 ms in the whole window */
    cc_window(&cc, 0U, 1000U, 30U, 300U);
    TEST_ASSERT_EQUAL_UINT8(2U, ts_cc_update(&cc, 1000U, 4000U));
    /* a burst that drains within the window is not a standing queue */
    ts_cc_on_backlog(&cc, 900U);
    cc_window(&cc, 1000U, 1000U, 30U, 0U);
    TEST_ASSERT_EQUAL_UINT8(1U, ts_cc_update(&cc, 2000U, 4000U));       /* 2 x 240 fits: back up */
}

static void test_cc_steps_up_only_when_better_quality_fits(void)
{
    ts_cc_t cc;
    ts_cc_init(&cc, 0U);
    cc.quality = 4U;
    /* 400 kbit/s at q4 -> ~800 at q2: does not fit 85 % of 900 */
    cc_window(&cc, 0U, 1667U, 30U, 0U);
    TEST_ASSERT_EQUAL_UINT8(4U, ts_cc_update(&cc, 1000U, 900U));
    /* fits 85 % of 1000: one step up per window */
    cc_window(&cc, 1000U, 1667U, 30U, 0U);
    TEST_ASSERT_EQUAL_UINT8(2U, ts_cc_update(&cc, 2000U, 1000U));
    TEST_ASSERT_EQUAL_UINT32(1U, cc.steps_up);
    /* no viewer (limit 0): step up even without frames */
    TEST_ASSERT_EQUAL_UINT8(1U, ts_cc_update(&cc, 3000U, 0U));
    TEST_ASSERT_EQUAL_UINT8(1U, ts_cc_update(&cc, 4000U, 0U));
    /* a window without P-frames holds */
    cc.quality = 2U;
    ts_cc_on_frame(&cc, 50000U, true);
    TEST_ASSERT_EQUAL_UINT8(2U, ts_cc_update(&cc, 5000U, 1000U));
    /* NULL-safe */
    ts_cc_init(NULL, 0U);
    ts_cc_on_frame(NULL, 1U, false);
    ts_cc_on_backlog(NULL, 1U);
    TEST_ASSERT_EQUAL_UINT8(1U, ts_cc_update(NULL, 0U, 0U));
}

static void test_cc_clock_wrap(void)
{
    ts_cc_t cc;
    ts_cc_init(&cc, 0xFFFFFE00U);
    cc_window(&cc, 0U, 6800U, 30U, 0U);
    TEST_ASSERT_EQUAL_UINT8(1U, ts_cc_update(&cc, 0xFFFFFFFFU, 1000U));  /* 511 ms */
    TEST_ASSERT_EQUAL_UINT8(2U, ts_cc_update(&cc, 0x000001E8U, 1000U));  /* 1000 ms across the wrap */
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_bucket_init_take_refuse);
    RUN_TEST(test_bucket_refill_kbps_units);
    RUN_TEST(test_bucket_slow_rate_fine_ticks);
    RUN_TEST(test_bucket_clock_wrap);
    RUN_TEST(test_bucket_unlimited_and_set_rate);
    RUN_TEST(test_bucket_wait_ms);
    RUN_TEST(test_bucket_global_cap_pair);
    RUN_TEST(test_retx_charged_to_client_bucket);
    RUN_TEST(test_retx_per_tick_cap);
    RUN_TEST(test_retx_and_fresh_share_the_bucket);
    RUN_TEST(test_loss_ratio);
    RUN_TEST(test_loss_steps_down_fast_up_slow);
    RUN_TEST(test_loss_window_across_clock_wrap);
    RUN_TEST(test_keysched_first_frame_forced_then_periodic);
    RUN_TEST(test_keysched_request_cooldown_deferred_not_lost);
    RUN_TEST(test_keysched_too_old_reason);
    RUN_TEST(test_keysched_scene_change);
    RUN_TEST(test_keysched_clock_wrap);
    RUN_TEST(test_cc_backlog_ms);
    RUN_TEST(test_cc_steps_down_when_stream_exceeds_limit);
    RUN_TEST(test_cc_keyframes_do_not_count_as_stream_rate);
    RUN_TEST(test_cc_standing_queue_steps_down);
    RUN_TEST(test_cc_steps_up_only_when_better_quality_fits);
    RUN_TEST(test_cc_clock_wrap);
    return UNITY_END();
}
