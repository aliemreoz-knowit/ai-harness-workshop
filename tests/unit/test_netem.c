/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/unit/test_netem.c
 * Chaos filter, shim and scenario timeline (tools/tsnetem/lib, include/ts_netem.h).
 * Virtual time only, fake endpoints only: no sockets, no sleeps.
 */
#include <string.h>
#include "unity.h"
#include "../../tools/tsnetem/relay_peer.h"
#include "ts_config.h"
#include "ts_io.h"
#include "ts_netem.h"

static ts_netem_t      g_n;
static ts_netem_t      g_n2;
static ts_io_fake_t    g_a;
static ts_io_fake_t    g_b;
static ts_io_fake_t    g_c;
static ts_netem_script_t g_s;

static const ts_addr_t A_ADDR = { 0x0A000001U, 5600U };
static const ts_addr_t B_ADDR = { 0x0A000020U, 6000U };
static const ts_addr_t C_ADDR = { 0x0A000021U, 6001U };

void setUp(void)
{
    ts_io_fake_init(&g_a, A_ADDR);
    ts_io_fake_init(&g_b, B_ADDR);
    ts_io_fake_init(&g_c, C_ADDR);
}

void tearDown(void)
{
}

static ts_netem_cfg_t clean_cfg(void)
{
    ts_netem_cfg_t c;
    (void)memset(&c, 0, sizeof(c));
    c.seed = 1U;
    return c;
}

static ts_netem_verdict_t filt(ts_netem_t *n, uint32_t now, uint32_t *delay)
{
    uint8_t pkt[100];
    (void)memset(pkt, 0xA5, sizeof(pkt));
    return ts_netem_filter(n, pkt, (uint16_t)sizeof(pkt), now, delay);
}

static void send_tag(ts_io_fake_t *from, const ts_addr_t *to, uint8_t tag, size_t len)
{
    uint8_t buf[TS_IO_DGRAM_MAX];
    (void)memset(buf, tag, len);
    TEST_ASSERT_EQUAL_INT32((int32_t)len, ts_io_udp_send(&from->io, to, buf, len));
}

/** Receive one datagram; returns its first byte (tag) or -1 when none. */
static int32_t recv_tag(ts_io_fake_t *at, ts_addr_t *from)
{
    uint8_t buf[TS_IO_DGRAM_MAX];
    int32_t n = ts_io_udp_recv(&at->io, from, buf, sizeof(buf));
    return (n > 0) ? (int32_t)buf[0] : -1;
}

static uint32_t popcount8(uint8_t v)
{
    uint32_t c = 0U;
    for (uint8_t b = v; b != 0U; b = (uint8_t)(b & (uint8_t)(b - 1U))) {
        c++;
    }
    return c;
}

/* ---- PRNG ------------------------------------------------------------------ */

static void test_rand_xorshift32_reference(void)
{
    uint32_t s = 1U;
    TEST_ASSERT_EQUAL_HEX32(270369U, ts_netem_rand(&s));   /* 1 -> 0x00042021 */
    TEST_ASSERT_EQUAL_HEX32(270369U, s);
    s = 0U;                                                 /* 0 behaves as 1 */
    TEST_ASSERT_EQUAL_HEX32(270369U, ts_netem_rand(&s));
}

/* ---- filter ------------------------------------------------------------------ */

static void test_clean_link_passes_and_draws_nothing(void)
{
    ts_netem_cfg_t c = clean_cfg();
    uint32_t delay = 99U;
    ts_netem_init(&g_n, &c, 0U);
    for (uint32_t i = 0U; i < 1000U; i++) {
        TEST_ASSERT_EQUAL(TS_NETEM_PASS, filt(&g_n, i, &delay));
        TEST_ASSERT_EQUAL_UINT32(0U, delay);
    }
    TEST_ASSERT_EQUAL_UINT32(1U, g_n.rng);      /* no feature enabled: no draws */
    TEST_ASSERT_EQUAL_UINT32(1000U, ts_netem_stats(&g_n)->seen);
    TEST_ASSERT_EQUAL_UINT32(1000U, ts_netem_stats(&g_n)->passed);
    TEST_ASSERT_EQUAL_UINT32(0U, ts_netem_stats(&g_n)->dropped);
}

static void test_null_filter_is_clean_link(void)
{
    uint32_t delay = 5U;
    uint16_t len = 10U;
    TEST_ASSERT_EQUAL(TS_NETEM_PASS, ts_netem_filter(NULL, NULL, 0U, 0U, &delay));
    TEST_ASSERT_EQUAL_UINT32(0U, delay);
    TEST_ASSERT_EQUAL(TS_NETEM_PASS, ts_netem_filter_len(NULL, NULL, &len, 0U, &delay));
    TEST_ASSERT_NULL(ts_netem_stats(NULL));
}

static void test_loss_is_predictable_from_the_seed(void)
{
    ts_netem_cfg_t c = clean_cfg();
    uint32_t shadow = 12345U;
    uint32_t drops = 0U;
    c.loss_pct = 5U;
    c.seed = 12345U;
    ts_netem_init(&g_n, &c, 0U);
    for (uint32_t i = 0U; i < 100000U; i++) {
        bool expect_drop = (ts_netem_rand(&shadow) % 100U) < 5U;
        ts_netem_verdict_t v = filt(&g_n, i, NULL);
        TEST_ASSERT_EQUAL(expect_drop ? TS_NETEM_DROP : TS_NETEM_PASS, v);
        drops += expect_drop ? 1U : 0U;
    }
    TEST_ASSERT_EQUAL_UINT32(drops, ts_netem_stats(&g_n)->dropped);
    TEST_ASSERT_TRUE(drops > 4500U);
    TEST_ASSERT_TRUE(drops < 5500U);
}

static void test_same_seed_same_decisions_other_seed_differs(void)
{
    ts_netem_cfg_t c = clean_cfg();
    uint32_t differ = 0U;
    c.loss_pct = 20U;
    c.dup_pct = 10U;
    c.reorder_pct = 10U;
    c.bitflip_pct = 10U;
    c.seed = 7U;
    ts_netem_init(&g_n, &c, 0U);
    ts_netem_init(&g_n2, &c, 0U);
    for (uint32_t i = 0U; i < 2000U; i++) {
        uint32_t d1 = 0U;
        uint32_t d2 = 0U;
        TEST_ASSERT_EQUAL(filt(&g_n, i, &d1), filt(&g_n2, i, &d2));
        TEST_ASSERT_EQUAL_UINT32(d1, d2);
    }
    c.seed = 8U;
    ts_netem_init(&g_n2, &c, 0U);
    ts_netem_init(&g_n, &c, 0U);
    g_n.rng = 7U;
    for (uint32_t i = 0U; i < 2000U; i++) {
        differ += (filt(&g_n, i, NULL) != filt(&g_n2, i, NULL)) ? 1U : 0U;
    }
    TEST_ASSERT_TRUE(differ > 100U);
}

static void test_full_loss_and_cable_pull(void)
{
    ts_netem_cfg_t c = clean_cfg();
    c.loss_pct = 100U;
    ts_netem_init(&g_n, &c, 0U);
    for (uint32_t i = 0U; i < 100U; i++) {
        TEST_ASSERT_EQUAL(TS_NETEM_DROP, filt(&g_n, i, NULL));
    }
    c = clean_cfg();
    c.cable_pulled = true;
    c.dup_pct = 100U;
    ts_netem_init(&g_n, &c, 0U);
    for (uint32_t i = 0U; i < 100U; i++) {
        TEST_ASSERT_EQUAL(TS_NETEM_DROP, filt(&g_n, i, NULL));
    }
    TEST_ASSERT_EQUAL_UINT32(100U, ts_netem_stats(&g_n)->dropped_cable);
    TEST_ASSERT_EQUAL_UINT32(1U, g_n.rng);   /* cable pull never draws */
    c.cable_pulled = false;
    c.dup_pct = 0U;
    ts_netem_set(&g_n, &c, 200U);
    TEST_ASSERT_EQUAL(TS_NETEM_PASS, filt(&g_n, 200U, NULL));
    TEST_ASSERT_EQUAL_UINT32(100U, ts_netem_stats(&g_n)->seen - 1U);   /* stats kept by set */
}

static void test_burst_once_at_activation(void)
{
    ts_netem_cfg_t c = clean_cfg();
    ts_netem_init(&g_n, &c, 0U);
    TEST_ASSERT_EQUAL(TS_NETEM_PASS, filt(&g_n, 0U, NULL));
    c.burst_len = 50U;
    ts_netem_set(&g_n, &c, 10U);
    for (uint32_t i = 0U; i < 50U; i++) {
        TEST_ASSERT_EQUAL(TS_NETEM_DROP, filt(&g_n, 10U + i, NULL));
    }
    for (uint32_t i = 0U; i < 1000U; i++) {
        TEST_ASSERT_EQUAL(TS_NETEM_PASS, filt(&g_n, 100U + i, NULL));
    }
    TEST_ASSERT_EQUAL_UINT32(50U, ts_netem_stats(&g_n)->dropped_burst);
    /* same burst config again: not re-armed */
    ts_netem_set(&g_n, &c, 5000U);
    TEST_ASSERT_EQUAL(TS_NETEM_PASS, filt(&g_n, 5000U, NULL));
    /* explicit live burst */
    ts_netem_burst_now(&g_n, 3U);
    TEST_ASSERT_EQUAL(TS_NETEM_DROP, filt(&g_n, 5001U, NULL));
    TEST_ASSERT_EQUAL(TS_NETEM_DROP, filt(&g_n, 5001U, NULL));
    TEST_ASSERT_EQUAL(TS_NETEM_DROP, filt(&g_n, 5001U, NULL));
    TEST_ASSERT_EQUAL(TS_NETEM_PASS, filt(&g_n, 5001U, NULL));
    TEST_ASSERT_EQUAL_UINT32(53U, ts_netem_stats(&g_n)->dropped_burst);
    /* burst at init time */
    ts_netem_init(&g_n, &c, 0U);
    TEST_ASSERT_EQUAL(TS_NETEM_DROP, filt(&g_n, 0U, NULL));
}

static void test_burst_periodic(void)
{
    ts_netem_cfg_t c = clean_cfg();
    c.burst_len = 3U;
    c.burst_period_ms = 100U;
    ts_netem_init(&g_n, &c, 1000U);
    for (uint32_t i = 0U; i < 3U; i++) {
        TEST_ASSERT_EQUAL(TS_NETEM_DROP, filt(&g_n, 1000U, NULL));
    }
    TEST_ASSERT_EQUAL(TS_NETEM_PASS, filt(&g_n, 1050U, NULL));
    TEST_ASSERT_EQUAL(TS_NETEM_PASS, filt(&g_n, 1099U, NULL));
    for (uint32_t i = 0U; i < 3U; i++) {
        TEST_ASSERT_EQUAL(TS_NETEM_DROP, filt(&g_n, 1100U, NULL));
    }
    TEST_ASSERT_EQUAL(TS_NETEM_PASS, filt(&g_n, 1100U, NULL));
    /* a long silence re-arms once, not once per missed period */
    for (uint32_t i = 0U; i < 3U; i++) {
        TEST_ASSERT_EQUAL(TS_NETEM_DROP, filt(&g_n, 5000U, NULL));
    }
    TEST_ASSERT_EQUAL(TS_NETEM_PASS, filt(&g_n, 5000U, NULL));
    TEST_ASSERT_EQUAL_UINT32(9U, ts_netem_stats(&g_n)->dropped_burst);
}

static void test_reorder_delay_and_dup(void)
{
    ts_netem_cfg_t c = clean_cfg();
    uint32_t delay = 0U;
    c.reorder_pct = 100U;
    c.reorder_delay_ms = 15U;
    ts_netem_init(&g_n, &c, 0U);
    TEST_ASSERT_EQUAL(TS_NETEM_PASS, filt(&g_n, 0U, &delay));
    TEST_ASSERT_EQUAL_UINT32(15U, delay);
    c.reorder_delay_ms = 0U;
    c.delay_ms = 7U;
    ts_netem_set(&g_n, &c, 0U);
    TEST_ASSERT_EQUAL(TS_NETEM_PASS, filt(&g_n, 0U, &delay));
    TEST_ASSERT_EQUAL_UINT32(7U + TS_NETEM_REORDER_DEFAULT_MS, delay);
    TEST_ASSERT_TRUE(TS_NETEM_REORDER_DEFAULT_MS < TS_NACK_DELAY_MS);
    TEST_ASSERT_EQUAL_UINT32(2U, ts_netem_stats(&g_n)->reordered);
    c = clean_cfg();
    c.dup_pct = 100U;
    ts_netem_set(&g_n, &c, 0U);
    TEST_ASSERT_EQUAL(TS_NETEM_DUP, filt(&g_n, 0U, &delay));
    TEST_ASSERT_EQUAL_UINT32(0U, delay);
    TEST_ASSERT_EQUAL_UINT32(1U, ts_netem_stats(&g_n)->duplicated);
}

static void check_flip_scope(uint8_t scope, uint16_t len, uint32_t lo, uint32_t hi)
{
    ts_netem_cfg_t c = clean_cfg();
    c.bitflip_pct = 100U;
    c.bitflip_scope = scope;
    c.seed = 99U;
    ts_netem_init(&g_n, &c, 0U);
    for (uint32_t k = 0U; k < 500U; k++) {
        uint8_t pkt[200];
        uint32_t bits = 0U;
        (void)memset(pkt, 0, sizeof(pkt));
        TEST_ASSERT_EQUAL(TS_NETEM_PASS, ts_netem_filter(&g_n, pkt, len, k, NULL));
        for (uint32_t i = 0U; i < sizeof(pkt); i++) {
            if (pkt[i] != 0U) {
                TEST_ASSERT_TRUE(i >= lo);
                TEST_ASSERT_TRUE(i < hi);
            }
            bits += popcount8(pkt[i]);
        }
        TEST_ASSERT_EQUAL_UINT32(1U, bits);
    }
    TEST_ASSERT_EQUAL_UINT32(500U, ts_netem_stats(&g_n)->flipped);
}

static void test_bitflip_scopes(void)
{
    check_flip_scope((uint8_t)TS_NETEM_FLIP_ANY, 100U, 0U, 100U);
    check_flip_scope((uint8_t)TS_NETEM_FLIP_HEADER, 100U, 0U, TS_HDR_LEN);
    check_flip_scope((uint8_t)TS_NETEM_FLIP_PAYLOAD, 100U, TS_HDR_LEN, 100U);
    check_flip_scope((uint8_t)TS_NETEM_FLIP_PAYLOAD, 20U, 0U, 20U);   /* too short: anywhere */
    check_flip_scope((uint8_t)TS_NETEM_FLIP_HEADER, 10U, 0U, 10U);
}

static void test_truncate_only_through_filter_len(void)
{
    ts_netem_cfg_t c = clean_cfg();
    uint8_t pkt[300];
    c.truncate_pct = 100U;
    ts_netem_init(&g_n, &c, 0U);
    for (uint32_t k = 0U; k < 1000U; k++) {
        uint16_t len = 300U;
        TEST_ASSERT_EQUAL(TS_NETEM_PASS, ts_netem_filter_len(&g_n, pkt, &len, k, NULL));
        TEST_ASSERT_TRUE(len >= 1U);
        TEST_ASSERT_TRUE(len < 300U);
    }
    TEST_ASSERT_EQUAL_UINT32(1000U, ts_netem_stats(&g_n)->truncated);
    {
        uint16_t one = 1U;   /* nothing left to cut */
        TEST_ASSERT_EQUAL(TS_NETEM_PASS, ts_netem_filter_len(&g_n, pkt, &one, 0U, NULL));
        TEST_ASSERT_EQUAL_UINT16(1U, one);
    }
    TEST_ASSERT_EQUAL(TS_NETEM_PASS, ts_netem_filter(&g_n, pkt, 300U, 0U, NULL));
    TEST_ASSERT_EQUAL_UINT32(1000U, ts_netem_stats(&g_n)->truncated);
}

static void test_throttle_token_bucket(void)
{
    ts_netem_cfg_t c = clean_cfg();
    uint8_t pkt[1000];
    uint32_t pass = 0U;
    c.throttle_kbps = 800U;     /* 100 bytes/ms, bucket = 100 ms = 10000 bytes */
    ts_netem_init(&g_n, &c, 0U);
    for (uint32_t i = 0U; i < 11U; i++) {
        pass += (ts_netem_filter(&g_n, pkt, 1000U, 0U, NULL) == TS_NETEM_PASS) ? 1U : 0U;
    }
    TEST_ASSERT_EQUAL_UINT32(10U, pass);
    TEST_ASSERT_EQUAL_UINT32(1U, ts_netem_stats(&g_n)->dropped_throttle);
    TEST_ASSERT_EQUAL(TS_NETEM_DROP, ts_netem_filter(&g_n, pkt, 1000U, 9U, NULL));
    TEST_ASSERT_EQUAL(TS_NETEM_PASS, ts_netem_filter(&g_n, pkt, 1000U, 10U, NULL));
    /* offered 1 MB/s for one second: ~100 KB/s get through */
    pass = 0U;
    for (uint32_t t = 1000U; t < 2000U; t++) {
        pass += (ts_netem_filter(&g_n, pkt, 1000U, t, NULL) == TS_NETEM_PASS) ? 1U : 0U;
    }
    TEST_ASSERT_UINT32_WITHIN(12U, 110U, pass);
    /* unlimited again */
    c.throttle_kbps = 0U;
    ts_netem_set(&g_n, &c, 2000U);
    for (uint32_t i = 0U; i < 100U; i++) {
        TEST_ASSERT_EQUAL(TS_NETEM_PASS, ts_netem_filter(&g_n, pkt, 1000U, 2000U, NULL));
    }
}

/* ---- shim -------------------------------------------------------------------- */

static void test_pump_clean_both_directions(void)
{
    ts_netem_cfg_t c = clean_cfg();
    ts_addr_t from;
    ts_netem_init(&g_n, &c, 0U);
    send_tag(&g_a, &B_ADDR, 1U, 1424U);
    send_tag(&g_b, &A_ADDR, 2U, 132U);
    ts_netem_pump(&g_n, &g_a, &g_b, 0U);
    TEST_ASSERT_EQUAL_INT32(1, recv_tag(&g_b, &from));
    TEST_ASSERT_TRUE(ts_addr_eq(&from, &A_ADDR));
    TEST_ASSERT_EQUAL_INT32(2, recv_tag(&g_a, &from));
    TEST_ASSERT_TRUE(ts_addr_eq(&from, &B_ADDR));
    TEST_ASSERT_EQUAL_INT32(-1, recv_tag(&g_b, &from));
    TEST_ASSERT_EQUAL_UINT32(2U, ts_netem_stats(&g_n)->passed);
}

static void test_pump_misrouted(void)
{
    ts_netem_cfg_t c = clean_cfg();
    ts_netem_init(&g_n, &c, 0U);
    send_tag(&g_a, &C_ADDR, 1U, 10U);
    ts_netem_pump(&g_n, &g_a, &g_b, 0U);
    TEST_ASSERT_EQUAL_UINT16(0U, ts_io_fake_in_count(&g_b));
    TEST_ASSERT_EQUAL_UINT32(1U, ts_netem_stats(&g_n)->misrouted);
    TEST_ASSERT_EQUAL_UINT32(0U, ts_netem_stats(&g_n)->seen);
}

static void test_pump_fixed_delay_keeps_order(void)
{
    ts_netem_cfg_t c = clean_cfg();
    c.delay_ms = 5U;
    ts_netem_init(&g_n, &c, 100U);
    for (uint8_t i = 1U; i <= 3U; i++) {
        send_tag(&g_a, &B_ADDR, i, 50U);
    }
    ts_netem_pump(&g_n, &g_a, &g_b, 100U);
    ts_netem_pump(&g_n, &g_a, &g_b, 104U);
    TEST_ASSERT_EQUAL_UINT16(0U, ts_io_fake_in_count(&g_b));
    ts_netem_pump(&g_n, &g_a, &g_b, 105U);
    TEST_ASSERT_EQUAL_UINT16(3U, ts_io_fake_in_count(&g_b));
    TEST_ASSERT_EQUAL_INT32(1, recv_tag(&g_b, NULL));
    TEST_ASSERT_EQUAL_INT32(2, recv_tag(&g_b, NULL));
    TEST_ASSERT_EQUAL_INT32(3, recv_tag(&g_b, NULL));
}

static void test_pump_reorder_hold(void)
{
    ts_netem_cfg_t c = clean_cfg();
    ts_netem_init(&g_n, &c, 0U);
    send_tag(&g_a, &B_ADDR, 1U, 50U);
    ts_netem_pump(&g_n, &g_a, &g_b, 0U);
    c.reorder_pct = 100U;
    c.reorder_delay_ms = 3U;
    ts_netem_set(&g_n, &c, 0U);
    send_tag(&g_a, &B_ADDR, 2U, 50U);
    ts_netem_pump(&g_n, &g_a, &g_b, 1U);
    c.reorder_pct = 0U;
    ts_netem_set(&g_n, &c, 1U);
    send_tag(&g_a, &B_ADDR, 3U, 50U);
    ts_netem_pump(&g_n, &g_a, &g_b, 2U);
    ts_netem_pump(&g_n, &g_a, &g_b, 4U);
    TEST_ASSERT_EQUAL_INT32(1, recv_tag(&g_b, NULL));
    TEST_ASSERT_EQUAL_INT32(3, recv_tag(&g_b, NULL));
    TEST_ASSERT_EQUAL_INT32(2, recv_tag(&g_b, NULL));
    TEST_ASSERT_EQUAL_INT32(-1, recv_tag(&g_b, NULL));
}

static void test_pump_dup_and_loss(void)
{
    ts_netem_cfg_t c = clean_cfg();
    c.dup_pct = 100U;
    ts_netem_init(&g_n, &c, 0U);
    send_tag(&g_a, &B_ADDR, 7U, 50U);
    ts_netem_pump(&g_n, &g_a, &g_b, 0U);
    TEST_ASSERT_EQUAL_UINT16(2U, ts_io_fake_in_count(&g_b));
    c = clean_cfg();
    c.loss_pct = 100U;
    ts_netem_set(&g_n, &c, 1U);
    send_tag(&g_a, &B_ADDR, 8U, 50U);
    send_tag(&g_b, &A_ADDR, 9U, 50U);
    ts_netem_pump(&g_n, &g_a, &g_b, 1U);
    TEST_ASSERT_EQUAL_UINT16(2U, ts_io_fake_in_count(&g_b));
    TEST_ASSERT_EQUAL_UINT16(0U, ts_io_fake_in_count(&g_a));
    TEST_ASSERT_EQUAL_UINT32(2U, ts_netem_stats(&g_n)->dropped);
}

static void test_pump_truncates_datagrams(void)
{
    ts_netem_cfg_t c = clean_cfg();
    uint8_t buf[TS_IO_DGRAM_MAX];
    c.truncate_pct = 100U;
    ts_netem_init(&g_n, &c, 0U);
    send_tag(&g_a, &B_ADDR, 1U, 1000U);
    ts_netem_pump(&g_n, &g_a, &g_b, 0U);
    {
        int32_t n = ts_io_udp_recv(&g_b.io, NULL, buf, sizeof(buf));
        TEST_ASSERT_TRUE(n >= 1);
        TEST_ASSERT_TRUE(n < 1000);
    }
}

static void test_pump_hold_overflow(void)
{
    ts_netem_cfg_t c = clean_cfg();
    c.delay_ms = 10U;
    ts_netem_init(&g_n, &c, 0U);
    for (uint32_t i = 0U; i < TS_NETEM_HOLD + 6U; i++) {
        send_tag(&g_a, &B_ADDR, (uint8_t)i, 20U);
    }
    ts_netem_pump(&g_n, &g_a, &g_b, 0U);
    TEST_ASSERT_EQUAL_UINT32(6U, ts_netem_stats(&g_n)->held_overflow);
    ts_netem_pump(&g_n, &g_a, &g_b, 10U);
    TEST_ASSERT_EQUAL_UINT16((uint16_t)TS_NETEM_HOLD, ts_io_fake_in_count(&g_b));
    TEST_ASSERT_EQUAL_INT32(0, recv_tag(&g_b, NULL));
    /* table empty again */
    send_tag(&g_a, &B_ADDR, 0xEEU, 20U);
    ts_netem_pump(&g_n, &g_a, &g_b, 11U);
    ts_netem_pump(&g_n, &g_a, &g_b, 21U);
    TEST_ASSERT_EQUAL_UINT16((uint16_t)TS_NETEM_HOLD, ts_io_fake_in_count(&g_b));
}

static void test_pump_star_routes_by_destination(void)
{
    ts_netem_cfg_t c = clean_cfg();
    ts_io_fake_t *leaves[2] = { &g_b, &g_c };
    ts_addr_t from;
    ts_addr_t odd = { 0x0A000099U, 1U };
    ts_netem_init(&g_n, &c, 0U);
    send_tag(&g_a, &B_ADDR, 1U, 30U);
    send_tag(&g_a, &C_ADDR, 2U, 30U);
    send_tag(&g_a, &odd, 3U, 30U);
    send_tag(&g_b, &A_ADDR, 4U, 30U);
    send_tag(&g_c, &odd, 5U, 30U);     /* leaves always reach the hub */
    ts_netem_pump_star(&g_n, &g_a, leaves, 2U, 0U);
    TEST_ASSERT_EQUAL_INT32(1, recv_tag(&g_b, NULL));
    TEST_ASSERT_EQUAL_INT32(2, recv_tag(&g_c, NULL));
    TEST_ASSERT_EQUAL_INT32(4, recv_tag(&g_a, &from));
    TEST_ASSERT_TRUE(ts_addr_eq(&from, &B_ADDR));
    TEST_ASSERT_EQUAL_INT32(5, recv_tag(&g_a, &from));
    TEST_ASSERT_TRUE(ts_addr_eq(&from, &C_ADDR));
    TEST_ASSERT_EQUAL_UINT32(1U, ts_netem_stats(&g_n)->misrouted);
    /* same ip, two ports: routed by port */
    {
        ts_addr_t c2 = { B_ADDR.ip, 6001U };
        g_c.self = c2;
        send_tag(&g_a, &c2, 6U, 30U);
        send_tag(&g_a, &B_ADDR, 7U, 30U);
        ts_netem_pump_star(&g_n, &g_a, leaves, 2U, 1U);
        TEST_ASSERT_EQUAL_INT32(7, recv_tag(&g_b, NULL));
        TEST_ASSERT_EQUAL_INT32(6, recv_tag(&g_c, NULL));
    }
}

/* ---- scenario ---------------------------------------------------------------- */

static void test_scenario_steps_applied_by_tick(void)
{
    static ts_netem_step_t steps[3];
    ts_netem_scenario_t sc;
    ts_netem_cfg_t c = clean_cfg();
    (void)memset(steps, 0, sizeof(steps));
    steps[0].at_ms = 0U;
    steps[0].cfg.loss_pct = 0U;
    steps[1].at_ms = 100U;
    steps[1].cfg.cable_pulled = true;
    steps[2].at_ms = 300U;
    sc.steps = steps;
    sc.count = 3U;
    ts_netem_init(&g_n, &c, 0U);
    ts_netem_scenario_start(&g_n, &sc, 1000U);
    ts_netem_tick(&g_n, 1050U);
    TEST_ASSERT_FALSE(g_n.cfg.cable_pulled);
    TEST_ASSERT_EQUAL_UINT8(1U, g_n.scenario_step);
    ts_netem_tick(&g_n, 1100U);
    TEST_ASSERT_TRUE(g_n.cfg.cable_pulled);
    TEST_ASSERT_EQUAL(TS_NETEM_DROP, filt(&g_n, 1200U, NULL));
    ts_netem_tick(&g_n, 1299U);
    TEST_ASSERT_TRUE(g_n.cfg.cable_pulled);
    ts_netem_tick(&g_n, 1300U);
    TEST_ASSERT_FALSE(g_n.cfg.cable_pulled);
    TEST_ASSERT_EQUAL_UINT8(3U, g_n.scenario_step);
    ts_netem_tick(&g_n, 5000U);
    TEST_ASSERT_EQUAL(TS_NETEM_PASS, filt(&g_n, 5000U, NULL));
}

static void test_scenario_through_pump(void)
{
    const ts_netem_scenario_t *sc;
    ts_netem_cfg_t c = clean_cfg();
    ts_netem_script_init(&g_s, NULL);
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_script_line(&g_s, "t=10ms blackout 20ms"));
    sc = ts_netem_script_finish(&g_s);
    ts_netem_init(&g_n, &c, 0U);
    ts_netem_scenario_start(&g_n, sc, 0U);
    for (uint32_t t = 0U; t < 40U; t++) {
        send_tag(&g_a, &B_ADDR, (uint8_t)t, 10U);
        ts_netem_pump(&g_n, &g_a, &g_b, t);
    }
    TEST_ASSERT_EQUAL_UINT16(20U, ts_io_fake_in_count(&g_b));
    TEST_ASSERT_EQUAL_UINT32(20U, ts_netem_stats(&g_n)->dropped_cable);
}

/* ---- text: specs --------------------------------------------------------------- */

static void test_cfg_parse_documented_example(void)
{
    ts_netem_cfg_t c;
    (void)memset(&c, 0xFF, sizeof(c));
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_cfg_parse("loss=5,burst=50,reorder=2,delay=10,cable=1", &c));
    TEST_ASSERT_EQUAL_UINT8(5U, c.loss_pct);
    TEST_ASSERT_EQUAL_UINT16(50U, c.burst_len);
    TEST_ASSERT_EQUAL_UINT8(2U, c.reorder_pct);
    TEST_ASSERT_EQUAL_UINT32(10U, c.delay_ms);
    TEST_ASSERT_TRUE(c.cable_pulled);
    TEST_ASSERT_EQUAL_UINT8(0U, c.dup_pct);       /* untouched keys reset */
    TEST_ASSERT_EQUAL_UINT32(0U, c.throttle_kbps);
    TEST_ASSERT_EQUAL_UINT32(0U, c.seed);
}

static void test_cfg_parse_units_and_aliases(void)
{
    ts_netem_cfg_t c;
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_cfg_parse(" loss=5% ; dup=1 hold=1.5s flip=3 scope=header trunc=2 "
                                                "throttle=2mbps period=2s seed=42 ", &c));
    TEST_ASSERT_EQUAL_UINT8(5U, c.loss_pct);
    TEST_ASSERT_EQUAL_UINT8(1U, c.dup_pct);
    TEST_ASSERT_EQUAL_UINT32(1500U, c.reorder_delay_ms);
    TEST_ASSERT_EQUAL_UINT8(3U, c.bitflip_pct);
    TEST_ASSERT_EQUAL_UINT8((uint8_t)TS_NETEM_FLIP_HEADER, c.bitflip_scope);
    TEST_ASSERT_EQUAL_UINT8(2U, c.truncate_pct);
    TEST_ASSERT_EQUAL_UINT32(2000U, c.throttle_kbps);
    TEST_ASSERT_EQUAL_UINT32(2000U, c.burst_period_ms);
    TEST_ASSERT_EQUAL_UINT32(42U, c.seed);
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_cfg_parse("kbps=1.5m,delay=250ms,scope=payload,cable=off", &c));
    TEST_ASSERT_EQUAL_UINT32(1500U, c.throttle_kbps);
    TEST_ASSERT_EQUAL_UINT32(250U, c.delay_ms);
    TEST_ASSERT_EQUAL_UINT8((uint8_t)TS_NETEM_FLIP_PAYLOAD, c.bitflip_scope);
    TEST_ASSERT_FALSE(c.cable_pulled);
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_cfg_parse("", &c));
    TEST_ASSERT_EQUAL_UINT8(0U, c.loss_pct);
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_cfg_parse("LOSS=7,Cable", &c));
    TEST_ASSERT_EQUAL_UINT8(7U, c.loss_pct);
    TEST_ASSERT_TRUE(c.cable_pulled);
}

static void test_cfg_parse_rejects_garbage(void)
{
    static const char *const bad[] = {
        "loss=101", "loss=-1", "loss=abc", "loss=", "loss=5.5", "foo=1", "burst=70000",
        "delay=1.5", "delay=2h", "hold=1.2345s", "throttle=1.5", "throttle=5gbps", "cable=maybe",
        "scope=middle", "seed=4294967296", "bogus", "loss=5=6"
    };
    ts_netem_cfg_t c = clean_cfg();
    c.loss_pct = 9U;
    for (size_t i = 0U; i < sizeof(bad) / sizeof(bad[0]); i++) {
        TEST_ASSERT_EQUAL_MESSAGE(TS_E_INVAL, ts_netem_cfg_parse(bad[i], &c), bad[i]);
        TEST_ASSERT_EQUAL_MESSAGE(TS_E_INVAL, ts_netem_cfg_apply(bad[i], &c), bad[i]);
    }
    TEST_ASSERT_EQUAL_UINT8(9U, c.loss_pct);     /* unchanged on error */
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_netem_cfg_parse(NULL, &c));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_netem_cfg_parse("loss=1", NULL));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_netem_cfg_apply("loss=1,nope=2", &c));
    TEST_ASSERT_EQUAL_UINT8(9U, c.loss_pct);     /* atomic: first key not applied either */
}

static void test_cfg_apply_is_incremental(void)
{
    ts_netem_cfg_t c = clean_cfg();
    c.seed = 5U;
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_cfg_apply("loss=5,dup=2", &c));
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_cfg_apply("reorder=3", &c));
    TEST_ASSERT_EQUAL_UINT8(5U, c.loss_pct);
    TEST_ASSERT_EQUAL_UINT8(2U, c.dup_pct);
    TEST_ASSERT_EQUAL_UINT8(3U, c.reorder_pct);
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_cfg_apply("pull", &c));
    TEST_ASSERT_TRUE(c.cable_pulled);
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_cfg_apply("plug", &c));
    TEST_ASSERT_FALSE(c.cable_pulled);
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_cfg_apply("clean", &c));
    TEST_ASSERT_EQUAL_UINT8(0U, c.loss_pct);
    TEST_ASSERT_EQUAL_UINT8(0U, c.reorder_pct);
    TEST_ASSERT_EQUAL_UINT32(5U, c.seed);         /* clean keeps the seed */
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_cfg_apply("loss=3 # comment loss=9", &c));
    TEST_ASSERT_EQUAL_UINT8(3U, c.loss_pct);
}

/* ---- text: scenario timeline ------------------------------------------------------ */

static void test_script_loss_and_blackout(void)
{
    const ts_netem_scenario_t *sc;
    ts_netem_script_init(&g_s, NULL);
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_script_line(&g_s, "# warm-up on a clean link\n"));
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_script_line(&g_s, "t=5s loss=5%\n"));
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_script_line(&g_s, "   \n"));
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_script_line(&g_s, "t=20s blackout 2s\n"));
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_script_line(&g_s, "t=30s clean"));
    sc = ts_netem_script_finish(&g_s);
    TEST_ASSERT_NOT_NULL(sc);
    TEST_ASSERT_EQUAL_UINT8(4U, sc->count);
    TEST_ASSERT_EQUAL_UINT32(5000U, sc->steps[0].at_ms);
    TEST_ASSERT_EQUAL_UINT8(5U, sc->steps[0].cfg.loss_pct);
    TEST_ASSERT_FALSE(sc->steps[0].cfg.cable_pulled);
    TEST_ASSERT_EQUAL_UINT32(20000U, sc->steps[1].at_ms);
    TEST_ASSERT_EQUAL_UINT8(5U, sc->steps[1].cfg.loss_pct);
    TEST_ASSERT_TRUE(sc->steps[1].cfg.cable_pulled);
    TEST_ASSERT_EQUAL_UINT32(22000U, sc->steps[2].at_ms);
    TEST_ASSERT_EQUAL_UINT8(5U, sc->steps[2].cfg.loss_pct);
    TEST_ASSERT_FALSE(sc->steps[2].cfg.cable_pulled);
    TEST_ASSERT_EQUAL_UINT32(30000U, sc->steps[3].at_ms);
    TEST_ASSERT_EQUAL_UINT8(0U, sc->steps[3].cfg.loss_pct);
}

static void test_script_blackout_overlaps_and_finish(void)
{
    const ts_netem_scenario_t *sc;
    ts_netem_script_init(&g_s, NULL);
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_script_line(&g_s, "t=10s blackout=5s"));
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_script_line(&g_s, "t=12s loss=3"));
    sc = ts_netem_script_finish(&g_s);
    TEST_ASSERT_EQUAL_UINT8(3U, sc->count);
    TEST_ASSERT_TRUE(sc->steps[1].cfg.cable_pulled);        /* still inside the blackout */
    TEST_ASSERT_EQUAL_UINT8(3U, sc->steps[1].cfg.loss_pct);
    TEST_ASSERT_EQUAL_UINT32(15000U, sc->steps[2].at_ms);
    TEST_ASSERT_FALSE(sc->steps[2].cfg.cable_pulled);
    TEST_ASSERT_EQUAL_UINT8(3U, sc->steps[2].cfg.loss_pct);
}

static void test_script_one_shot_burst_and_base(void)
{
    const ts_netem_scenario_t *sc;
    ts_netem_cfg_t base = clean_cfg();
    base.dup_pct = 1U;
    ts_netem_script_init(&g_s, &base);
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_script_line(&g_s, "t=1s burst=50"));
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_script_line(&g_s, "t=2s loss=1"));
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_script_line(&g_s, "t=3s burst=50"));
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_script_line(&g_s, "t=3s loss=2"));    /* same time: merged */
    sc = ts_netem_script_finish(&g_s);
    TEST_ASSERT_EQUAL_UINT8(3U, sc->count);
    TEST_ASSERT_EQUAL_UINT16(50U, sc->steps[0].cfg.burst_len);
    TEST_ASSERT_EQUAL_UINT16(0U, sc->steps[1].cfg.burst_len);
    TEST_ASSERT_EQUAL_UINT8(1U, sc->steps[1].cfg.dup_pct);
    TEST_ASSERT_EQUAL_UINT8(2U, sc->steps[2].cfg.loss_pct);
    TEST_ASSERT_EQUAL_UINT16(50U, sc->steps[2].cfg.burst_len);   /* same-time lines merge */
    /* through the filter: the burst fires at 1 s and again at 3 s */
    {
        ts_netem_cfg_t c = clean_cfg();
        ts_netem_init(&g_n, &c, 0U);
        ts_netem_scenario_start(&g_n, sc, 0U);
        for (uint32_t t = 0U; t < 4000U; t++) {
            ts_netem_tick(&g_n, t);
            (void)filt(&g_n, t, NULL);
        }
        TEST_ASSERT_EQUAL_UINT32(100U, ts_netem_stats(&g_n)->dropped_burst);
    }
}

static void test_script_errors(void)
{
    ts_netem_script_init(&g_s, NULL);
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_netem_script_line(&g_s, "loss=5"));          /* no t= */
    TEST_ASSERT_EQUAL(TS_OK, ts_netem_script_line(&g_s, "t=5s loss=5"));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_netem_script_line(&g_s, "t=4s loss=6"));      /* backwards */
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_netem_script_line(&g_s, "t=6s blackout"));    /* no duration */
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_netem_script_line(&g_s, "t=6s blackout=0s"));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_netem_script_line(&g_s, "t=6s wobble=1"));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_netem_script_line(&g_s, "t=xs"));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_netem_script_line(NULL, "t=1s"));
    TEST_ASSERT_EQUAL_UINT8(1U, g_s.count);                                         /* unchanged */
    TEST_ASSERT_EQUAL_UINT8(5U, g_s.cur.loss_pct);
    ts_netem_script_init(&g_s, NULL);
    for (uint32_t i = 0U; i < TS_NETEM_SCRIPT_STEPS; i++) {
        char line[32];
        line[0] = 't';
        line[1] = '=';
        line[2] = (char)('0' + (char)(i / 10U));
        line[3] = (char)('0' + (char)(i % 10U));
        line[4] = 's';
        line[5] = '\0';
        TEST_ASSERT_EQUAL(TS_OK, ts_netem_script_line(&g_s, line));
    }
    TEST_ASSERT_EQUAL(TS_E_FULL, ts_netem_script_line(&g_s, "t=99s loss=1"));
    TEST_ASSERT_EQUAL_UINT8((uint8_t)TS_NETEM_SCRIPT_STEPS, ts_netem_script_finish(&g_s)->count);
}

/* ---- tsnetem relay: upstream peer policy (tools/tsnetem/relay_peer.h) ------------ */
static void test_relay_peer_learned_from_first_datagram_only(void)
{
    /* no --peer: the first datagram teaches the peer ... */
    TEST_ASSERT_EQUAL_INT(RELAY_PEER_LEARN, relay_peer_action(false, false, false));
    /* ... and nobody else replaces it afterwards (counted foreign, still relayed) */
    TEST_ASSERT_EQUAL_INT(RELAY_PEER_FOREIGN, relay_peer_action(false, true, false));
    TEST_ASSERT_EQUAL_INT(RELAY_PEER_KEEP, relay_peer_action(false, true, true));
    /* --peer: never learned, other senders are foreign */
    TEST_ASSERT_EQUAL_INT(RELAY_PEER_FOREIGN, relay_peer_action(true, true, false));
    TEST_ASSERT_EQUAL_INT(RELAY_PEER_KEEP, relay_peer_action(true, true, true));
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_rand_xorshift32_reference);
    RUN_TEST(test_clean_link_passes_and_draws_nothing);
    RUN_TEST(test_null_filter_is_clean_link);
    RUN_TEST(test_loss_is_predictable_from_the_seed);
    RUN_TEST(test_same_seed_same_decisions_other_seed_differs);
    RUN_TEST(test_full_loss_and_cable_pull);
    RUN_TEST(test_burst_once_at_activation);
    RUN_TEST(test_burst_periodic);
    RUN_TEST(test_reorder_delay_and_dup);
    RUN_TEST(test_bitflip_scopes);
    RUN_TEST(test_truncate_only_through_filter_len);
    RUN_TEST(test_throttle_token_bucket);
    RUN_TEST(test_pump_clean_both_directions);
    RUN_TEST(test_pump_misrouted);
    RUN_TEST(test_pump_fixed_delay_keeps_order);
    RUN_TEST(test_pump_reorder_hold);
    RUN_TEST(test_pump_dup_and_loss);
    RUN_TEST(test_pump_truncates_datagrams);
    RUN_TEST(test_pump_hold_overflow);
    RUN_TEST(test_pump_star_routes_by_destination);
    RUN_TEST(test_scenario_steps_applied_by_tick);
    RUN_TEST(test_scenario_through_pump);
    RUN_TEST(test_cfg_parse_documented_example);
    RUN_TEST(test_cfg_parse_units_and_aliases);
    RUN_TEST(test_cfg_parse_rejects_garbage);
    RUN_TEST(test_cfg_apply_is_incremental);
    RUN_TEST(test_script_loss_and_blackout);
    RUN_TEST(test_script_blackout_overlaps_and_finish);
    RUN_TEST(test_script_one_shot_burst_and_base);
    RUN_TEST(test_script_errors);
    RUN_TEST(test_relay_peer_learned_from_first_datagram_only);
    return UNITY_END();
}
