/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/unit/test_io_fake.c
 * The in-memory ts_io_t used by every loopback test (src/io/io_fake.c).
 */
#include <string.h>
#include "unity.h"
#include "ts_config.h"
#include "ts_io.h"

static ts_io_fake_t g_srv;
static ts_io_fake_t g_cli;
static ts_io_fake_t g_cli2;

static const ts_addr_t SRV = { 0x0A000001U, 5600U };
static const ts_addr_t CLI = { 0x0A000020U, 6000U };
static const ts_addr_t CLI2 = { 0x0A000020U, 6001U };
static const ts_addr_t SRV_TCP = { 0x0A000001U, 5601U };

void setUp(void)
{
    ts_io_fake_init(&g_srv, SRV);
    ts_io_fake_init(&g_cli, CLI);
    ts_io_fake_init(&g_cli2, CLI2);
}

void tearDown(void)
{
}

static void test_clock_is_injected(void)
{
    TEST_ASSERT_EQUAL_UINT32(0U, ts_io_now_ms(&g_srv.io));
    ts_io_fake_set_time(&g_srv, 0xFFFFFFF0U);
    TEST_ASSERT_EQUAL_UINT32(0xFFFFFFF0U, ts_io_now_ms(&g_srv.io));
}

static void test_udp_send_pump_recv(void)
{
    uint8_t out[TS_PKT_MAX];
    uint8_t in[TS_IO_DGRAM_MAX];
    ts_addr_t from;
    for (size_t i = 0U; i < sizeof(out); i++) {
        out[i] = (uint8_t)i;
    }
    TEST_ASSERT_EQUAL_INT32(0, ts_io_udp_recv(&g_cli.io, &from, in, sizeof(in)));
    TEST_ASSERT_EQUAL_INT32((int32_t)sizeof(out), ts_io_udp_send(&g_srv.io, &CLI, out, sizeof(out)));
    TEST_ASSERT_EQUAL_UINT16(1U, ts_io_fake_out_count(&g_srv));
    TEST_ASSERT_EQUAL_UINT32(1U, g_srv.udp_sent);
    ts_io_fake_pump(&g_srv, &g_cli);
    TEST_ASSERT_EQUAL_UINT16(0U, ts_io_fake_out_count(&g_srv));
    TEST_ASSERT_EQUAL_UINT16(1U, ts_io_fake_in_count(&g_cli));
    TEST_ASSERT_EQUAL_INT32((int32_t)sizeof(out), ts_io_udp_recv(&g_cli.io, &from, in, sizeof(in)));
    TEST_ASSERT_TRUE(ts_addr_eq(&from, &SRV));
    TEST_ASSERT_EQUAL_MEMORY(out, in, sizeof(out));
    TEST_ASSERT_EQUAL_UINT32(1U, g_cli.udp_received);
}

static void test_udp_order_truncation_and_limits(void)
{
    uint8_t b[TS_IO_DGRAM_MAX + 1U];
    ts_io_dgram_t d;
    (void)memset(b, 0, sizeof(b));
    for (uint8_t i = 0U; i < 3U; i++) {
        b[0] = i;
        TEST_ASSERT_EQUAL_INT32(10, ts_io_udp_send(&g_cli.io, &SRV, b, 10U));
    }
    TEST_ASSERT_TRUE(ts_io_fake_pop_udp(&g_cli, &d));
    TEST_ASSERT_EQUAL_UINT8(0U, d.data[0]);
    TEST_ASSERT_TRUE(ts_addr_eq(&d.from, &CLI));
    TEST_ASSERT_TRUE(ts_addr_eq(&d.to, &SRV));
    TEST_ASSERT_EQUAL(TS_OK, ts_io_fake_push_udp(&g_srv, &d));
    ts_io_fake_pump(&g_cli, &g_srv);
    TEST_ASSERT_EQUAL_UINT16(3U, ts_io_fake_in_count(&g_srv));
    TEST_ASSERT_EQUAL_INT32(4, ts_io_udp_recv(&g_srv.io, NULL, b, 4U));     /* silently truncated */
    TEST_ASSERT_EQUAL_INT32(10, ts_io_udp_recv(&g_srv.io, NULL, b, sizeof(b)));
    TEST_ASSERT_EQUAL_UINT8(1U, b[0]);
    TEST_ASSERT_EQUAL_INT32(10, ts_io_udp_recv(&g_srv.io, NULL, b, sizeof(b)));
    TEST_ASSERT_EQUAL_UINT8(2U, b[0]);
    /* oversize and bad args */
    TEST_ASSERT_EQUAL_INT32(TS_IO_ERR, ts_io_udp_send(&g_cli.io, &SRV, b, TS_IO_DGRAM_MAX + 1U));
    TEST_ASSERT_EQUAL_INT32(TS_IO_ERR, ts_io_udp_send(&g_cli.io, NULL, b, 10U));
    d.len = (uint16_t)(TS_IO_DGRAM_MAX + 1U);
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_io_fake_push_udp(&g_srv, &d));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_io_fake_push_udp(&g_srv, NULL));
    TEST_ASSERT_FALSE(ts_io_fake_pop_udp(&g_cli, &d));
}

static void test_udp_empty_datagrams_skipped(void)
{
    uint8_t b[16] = { 0U };
    ts_io_dgram_t d;
    (void)memset(&d, 0, sizeof(d));
    d.from = CLI;
    d.to = SRV;
    /* [empty, 10 bytes]: one call returns the 10-byte datagram */
    d.len = 0U;
    TEST_ASSERT_EQUAL(TS_OK, ts_io_fake_push_udp(&g_srv, &d));
    d.len = 10U;
    d.data[0] = 0xA5U;
    TEST_ASSERT_EQUAL(TS_OK, ts_io_fake_push_udp(&g_srv, &d));
    TEST_ASSERT_EQUAL_INT32(10, ts_io_udp_recv(&g_srv.io, NULL, b, sizeof(b)));
    TEST_ASSERT_EQUAL_HEX8(0xA5U, b[0]);
    TEST_ASSERT_EQUAL_INT32(0, ts_io_udp_recv(&g_srv.io, NULL, b, sizeof(b)));
    /* the skip is bounded: TS_IO_UDP_EMPTY_SKIP empties per call, then 0 */
    d.len = 0U;
    for (uint32_t i = 0U; i < (TS_IO_UDP_EMPTY_SKIP + 1U); i++) {
        TEST_ASSERT_EQUAL(TS_OK, ts_io_fake_push_udp(&g_srv, &d));
    }
    d.len = 3U;
    TEST_ASSERT_EQUAL(TS_OK, ts_io_fake_push_udp(&g_srv, &d));
    TEST_ASSERT_EQUAL_INT32(0, ts_io_udp_recv(&g_srv.io, NULL, b, sizeof(b)));
    TEST_ASSERT_EQUAL_UINT16(2U, ts_io_fake_in_count(&g_srv));
    TEST_ASSERT_EQUAL_INT32(3, ts_io_udp_recv(&g_srv.io, NULL, b, sizeof(b)));
    TEST_ASSERT_EQUAL_UINT16(0U, ts_io_fake_in_count(&g_srv));
}

static void test_udp_queue_full_drops(void)
{
    uint8_t b[16] = { 0U };
    ts_io_dgram_t d;
    (void)memset(&d, 0, sizeof(d));
    d.len = 16U;
    for (uint32_t i = 0U; i < TS_IO_FAKE_QDEPTH; i++) {
        TEST_ASSERT_EQUAL_INT32(16, ts_io_udp_send(&g_cli.io, &SRV, b, sizeof(b)));
    }
    /* full: "would block" (ts_io.h: 0 = would block / nothing now), counted */
    TEST_ASSERT_EQUAL_INT32(0, ts_io_udp_send(&g_cli.io, &SRV, b, sizeof(b)));
    TEST_ASSERT_EQUAL_INT32(0, ts_io_udp_send(&g_cli.io, &SRV, b, sizeof(b)));
    TEST_ASSERT_EQUAL_UINT16((uint16_t)TS_IO_FAKE_QDEPTH, ts_io_fake_out_count(&g_cli));
    TEST_ASSERT_EQUAL_UINT32(2U, g_cli.udp_out.dropped);
    for (uint32_t i = 0U; i < TS_IO_FAKE_QDEPTH; i++) {
        TEST_ASSERT_EQUAL(TS_OK, ts_io_fake_push_udp(&g_srv, &d));
    }
    TEST_ASSERT_EQUAL(TS_E_FULL, ts_io_fake_push_udp(&g_srv, &d));
    TEST_ASSERT_EQUAL_UINT32(1U, g_srv.udp_in.dropped);
    /* ring wraps cleanly */
    for (uint32_t i = 0U; i < 3U * TS_IO_FAKE_QDEPTH; i++) {
        TEST_ASSERT_TRUE(ts_io_udp_recv(&g_srv.io, NULL, b, sizeof(b)) == 16);
        TEST_ASSERT_EQUAL(TS_OK, ts_io_fake_push_udp(&g_srv, &d));
    }
}

static void test_tcp_connect_accept_exchange(void)
{
    uint8_t buf[64];
    ts_addr_t peer;
    ts_io_handle_t c;
    ts_io_handle_t s;
    TEST_ASSERT_EQUAL_INT32(TS_IO_INVALID, ts_io_tcp_connect(&g_cli.io, &SRV_TCP));   /* not linked */
    ts_io_fake_link_tcp(&g_cli, &g_srv);
    TEST_ASSERT_EQUAL_INT32(TS_IO_INVALID, ts_io_tcp_accept(&g_srv.io, &peer));
    c = ts_io_tcp_connect(&g_cli.io, &SRV_TCP);
    TEST_ASSERT_TRUE(c >= 0);
    /* bytes sent before accept are kept */
    TEST_ASSERT_EQUAL_INT32(5, ts_io_tcp_send(&g_cli.io, c, (const uint8_t *)"hello", 5U));
    s = ts_io_tcp_accept(&g_srv.io, &peer);
    TEST_ASSERT_TRUE(s >= 0);
    TEST_ASSERT_TRUE(ts_addr_eq(&peer, &CLI));
    TEST_ASSERT_EQUAL_INT32(TS_IO_INVALID, ts_io_tcp_accept(&g_srv.io, &peer));
    TEST_ASSERT_EQUAL_INT32(3, ts_io_tcp_recv(&g_srv.io, s, buf, 3U));
    TEST_ASSERT_EQUAL_MEMORY("hel", buf, 3U);
    TEST_ASSERT_EQUAL_INT32(2, ts_io_tcp_recv(&g_srv.io, s, buf, sizeof(buf)));
    TEST_ASSERT_EQUAL_MEMORY("lo", buf, 2U);
    TEST_ASSERT_EQUAL_INT32(0, ts_io_tcp_recv(&g_srv.io, s, buf, sizeof(buf)));   /* would block */
    TEST_ASSERT_EQUAL_INT32(4, ts_io_tcp_send(&g_srv.io, s, (const uint8_t *)"ok!!", 4U));
    TEST_ASSERT_EQUAL_INT32(4, ts_io_tcp_recv(&g_cli.io, c, buf, sizeof(buf)));
    TEST_ASSERT_EQUAL_MEMORY("ok!!", buf, 4U);
    TEST_ASSERT_EQUAL_INT32(TS_IO_ERR, ts_io_tcp_recv(&g_cli.io, 7, buf, sizeof(buf)));
    TEST_ASSERT_EQUAL_INT32(TS_IO_ERR, ts_io_tcp_send(&g_cli.io, -1, buf, 1U));
}

static void test_tcp_send_limit_splits_and_buffer_full(void)
{
    uint8_t big[TS_IO_FAKE_TCP_CAP + 100U];
    uint8_t buf[TS_IO_FAKE_TCP_CAP + 100U];
    ts_io_handle_t c;
    ts_io_handle_t s;
    size_t got = 0U;
    for (size_t i = 0U; i < sizeof(big); i++) {
        big[i] = (uint8_t)(i * 7U);
    }
    ts_io_fake_link_tcp(&g_cli, &g_srv);
    c = ts_io_tcp_connect(&g_cli.io, &SRV_TCP);
    s = ts_io_tcp_accept(&g_srv.io, NULL);
    g_cli.tcp_send_limit = 3U;
    TEST_ASSERT_EQUAL_INT32(3, ts_io_tcp_send(&g_cli.io, c, big, 10U));
    TEST_ASSERT_EQUAL_INT32(3, ts_io_tcp_recv(&g_srv.io, s, buf, sizeof(buf)));
    g_cli.tcp_send_limit = 0U;
    TEST_ASSERT_EQUAL_INT32((int32_t)TS_IO_FAKE_TCP_CAP, ts_io_tcp_send(&g_cli.io, c, big, sizeof(big)));
    TEST_ASSERT_EQUAL_INT32(0, ts_io_tcp_send(&g_cli.io, c, big, 1U));             /* full: would block */
    while (got < TS_IO_FAKE_TCP_CAP) {
        size_t want = ((TS_IO_FAKE_TCP_CAP - got) < 1000U) ? (TS_IO_FAKE_TCP_CAP - got) : 1000U;
        int32_t n = ts_io_tcp_recv(&g_srv.io, s, &buf[got], want);
        TEST_ASSERT_TRUE(n > 0);
        got += (size_t)n;
        /* refill while reading: exercises ring wrap */
        if (got == 1000U) {
            TEST_ASSERT_EQUAL_INT32(100, ts_io_tcp_send(&g_cli.io, c, &big[TS_IO_FAKE_TCP_CAP], 100U));
        }
    }
    TEST_ASSERT_EQUAL_MEMORY(big, buf, TS_IO_FAKE_TCP_CAP);
    TEST_ASSERT_EQUAL_INT32(100, ts_io_tcp_recv(&g_srv.io, s, buf, sizeof(buf)));
    TEST_ASSERT_EQUAL_MEMORY(&big[TS_IO_FAKE_TCP_CAP], buf, 100U);
}

static void test_tcp_close_semantics(void)
{
    uint8_t buf[16];
    ts_io_handle_t c;
    ts_io_handle_t s;
    ts_io_fake_link_tcp(&g_cli, &g_srv);
    c = ts_io_tcp_connect(&g_cli.io, &SRV_TCP);
    s = ts_io_tcp_accept(&g_srv.io, NULL);
    TEST_ASSERT_EQUAL_INT32(2, ts_io_tcp_send(&g_cli.io, c, (const uint8_t *)"by", 2U));
    ts_io_tcp_close(&g_cli.io, c);
    TEST_ASSERT_EQUAL_INT32(2, ts_io_tcp_recv(&g_srv.io, s, buf, sizeof(buf)));    /* buffered bytes first */
    TEST_ASSERT_EQUAL_INT32(TS_IO_CLOSED, ts_io_tcp_recv(&g_srv.io, s, buf, sizeof(buf)));
    TEST_ASSERT_EQUAL_INT32(TS_IO_ERR, ts_io_tcp_send(&g_srv.io, s, buf, 1U));
    TEST_ASSERT_EQUAL_INT32(TS_IO_ERR, ts_io_tcp_recv(&g_cli.io, c, buf, sizeof(buf)));  /* own handle gone */
    ts_io_tcp_close(&g_srv.io, s);
    TEST_ASSERT_EQUAL_INT32(TS_IO_ERR, ts_io_tcp_recv(&g_srv.io, s, buf, sizeof(buf)));
    /* server closes first: client sees CLOSED */
    c = ts_io_tcp_connect(&g_cli.io, &SRV_TCP);
    s = ts_io_tcp_accept(&g_srv.io, NULL);
    ts_io_tcp_close(&g_srv.io, s);
    TEST_ASSERT_EQUAL_INT32(TS_IO_CLOSED, ts_io_tcp_recv(&g_cli.io, c, buf, sizeof(buf)));
    ts_io_tcp_close(&g_cli.io, c);
    ts_io_tcp_close(&g_cli.io, c);   /* double close is harmless */
}

static void test_tcp_close_before_accept(void)
{
    uint8_t buf[16];
    ts_io_handle_t c;
    ts_io_handle_t s;
    ts_io_fake_link_tcp(&g_cli, &g_srv);
    c = ts_io_tcp_connect(&g_cli.io, &SRV_TCP);
    TEST_ASSERT_EQUAL_INT32(1, ts_io_tcp_send(&g_cli.io, c, (const uint8_t *)"x", 1U));
    ts_io_tcp_close(&g_cli.io, c);
    s = ts_io_tcp_accept(&g_srv.io, NULL);
    TEST_ASSERT_TRUE(s >= 0);
    TEST_ASSERT_EQUAL_INT32(1, ts_io_tcp_recv(&g_srv.io, s, buf, sizeof(buf)));
    TEST_ASSERT_EQUAL_INT32(TS_IO_CLOSED, ts_io_tcp_recv(&g_srv.io, s, buf, sizeof(buf)));
}

static void test_tcp_two_clients_same_ip_and_slot_limit(void)
{
    uint8_t buf[8];
    ts_addr_t p1;
    ts_addr_t p2;
    ts_io_handle_t c1;
    ts_io_handle_t c2;
    ts_io_handle_t s1;
    ts_io_handle_t s2;
    ts_io_fake_link_tcp(&g_cli, &g_srv);
    ts_io_fake_link_tcp(&g_cli2, &g_srv);
    c1 = ts_io_tcp_connect(&g_cli.io, &SRV_TCP);
    c2 = ts_io_tcp_connect(&g_cli2.io, &SRV_TCP);
    s1 = ts_io_tcp_accept(&g_srv.io, &p1);
    s2 = ts_io_tcp_accept(&g_srv.io, &p2);
    TEST_ASSERT_TRUE((s1 >= 0) && (s2 >= 0) && (s1 != s2));
    TEST_ASSERT_TRUE(ts_addr_eq(&p1, &CLI));
    TEST_ASSERT_TRUE(ts_addr_eq(&p2, &CLI2));
    TEST_ASSERT_EQUAL_INT32(1, ts_io_tcp_send(&g_srv.io, s2, (const uint8_t *)"2", 1U));
    TEST_ASSERT_EQUAL_INT32(0, ts_io_tcp_recv(&g_cli.io, c1, buf, sizeof(buf)));
    TEST_ASSERT_EQUAL_INT32(1, ts_io_tcp_recv(&g_cli2.io, c2, buf, sizeof(buf)));
    TEST_ASSERT_EQUAL_UINT8('2', buf[0]);
    /* the server has TS_IO_FAKE_MAX_CONN slots: 2 used, 6 more fit */
    for (uint32_t i = 0U; i < TS_IO_FAKE_MAX_CONN - 2U; i++) {
        TEST_ASSERT_TRUE(ts_io_tcp_connect(&g_cli.io, &SRV_TCP) >= 0);
    }
    TEST_ASSERT_EQUAL_INT32(TS_IO_INVALID, ts_io_tcp_connect(&g_cli2.io, &SRV_TCP));
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_clock_is_injected);
    RUN_TEST(test_udp_send_pump_recv);
    RUN_TEST(test_udp_order_truncation_and_limits);
    RUN_TEST(test_udp_empty_datagrams_skipped);
    RUN_TEST(test_udp_queue_full_drops);
    RUN_TEST(test_tcp_connect_accept_exchange);
    RUN_TEST(test_tcp_send_limit_splits_and_buffer_full);
    RUN_TEST(test_tcp_close_semantics);
    RUN_TEST(test_tcp_close_before_accept);
    RUN_TEST(test_tcp_two_clients_same_ip_and_slot_limit);
    return UNITY_END();
}
