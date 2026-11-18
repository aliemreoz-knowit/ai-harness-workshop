/*
 * tinystream v2 -- hidden evaluation test: tests/unit/test_net_viewer_commands_eval.c
 * Daemon control channel as a TCP byte stream (PROTOCOL.md 5): TLV
 * boundaries and read boundaries are unrelated. The daemon is driven through
 * the fake ts_io with a virtual clock; a client writes a long stream of
 * HELLO / SET_RATE / KEYFRAME_REQ in chunks of pseudo-random size (1..13
 * bytes, one daemon tick per chunk), several TLVs glued into one write, and
 * a TLV header split byte by byte. Every TLV must take effect exactly once
 * and as soon as its last byte arrived, with no protocol error and no
 * dropped connection (ticket and metadata: ../../meta.yaml).
 */
#include <string.h>
#include "unity.h"
#include "ts_config.h"
#include "ts_proto.h"
#include "ts_io.h"
#include "ts_daemon.h"
#include "ts_pool.h"

#define D_IP       0x0A000001U   /* 10.0.0.1 */
#define C_IP       0x0A000020U   /* 10.0.0.32 */
#define C_UDP      6000U
#define T0         1000U
#define N_RATES    12U
#define STREAM_CAP 512U

static ts_io_fake_t io_d;
static ts_io_fake_t io_c;
static ts_io_handle_t hc;
static ts_tlv_acc_t acc_c;
static bool closed_c;
static tsd_cfg_t cfg;
static tsd_t *d;
static uint32_t now;
static uint32_t welcomes;

void setUp(void)
{
    ts_addr_t a = { D_IP, (uint16_t)TS_DEFAULT_UDP_PORT };
    ts_addr_t ca = { C_IP, (uint16_t)C_UDP };
    ts_pool_init(ts_pool_get());
    ts_io_fake_init(&io_d, a);
    ts_io_fake_init(&io_c, ca);
    ts_io_fake_link_tcp(&io_c, &io_d);
    hc = TS_IO_INVALID;
    ts_tlv_acc_init(&acc_c);
    closed_c = false;
    welcomes = 0U;
    tsd_cfg_default(&cfg);
    cfg.stream_id = 0xC0FFEE05U;
    cfg.capture.fps = 5U;          /* keep the data path quiet */
    cfg.fps = 5U;
    cfg.capture.scene_cut_ms = 600000U;
    now = T0;
    d = NULL;
}

void tearDown(void)
{
    if (d != NULL) {
        tsd_stop(d);
        d = NULL;
    }
}

/* Read what the daemon wrote on TCP and count WELCOMEs. */
static void cli_pump(void)
{
    uint8_t tmp[512];
    int32_t n;
    ts_tlv_t t;
    do {
        n = ts_io_tcp_recv(&io_c.io, hc, tmp, sizeof(tmp));
        if (n > 0) {
            TEST_ASSERT_EQUAL(TS_OK, ts_tlv_acc_push(&acc_c, tmp, (size_t)n));
        } else if (n == TS_IO_CLOSED) {
            closed_c = true;
        } else {
            /* nothing more */
        }
    } while (n > 0);
    while (ts_tlv_acc_next(&acc_c, &t) == TS_OK) {
        if (t.type == (uint8_t)TS_TLV_WELCOME) {
            welcomes++;
        }
    }
}

static void step(void)
{
    ts_io_dgram_t g;
    ts_io_fake_set_time(&io_d, now);
    (void)tsd_tick(d, now);
    while (ts_io_fake_pop_udp(&io_d, &g)) {
        /* data is not looked at here */
    }
    cli_pump();
    now++;
}

static void run_ms(uint32_t ms)
{
    for (uint32_t i = 0U; i < ms; i++) {
        step();
    }
}

static void start(void)
{
    ts_addr_t srv = { D_IP, (uint16_t)TS_DEFAULT_TCP_PORT };
    ts_io_fake_set_time(&io_d, now);
    d = tsd_create(&cfg, &io_d.io, ts_pool_get());
    TEST_ASSERT_NOT_NULL_MESSAGE(d, "tsd_create failed");
    run_ms(3U);
    hc = ts_io_tcp_connect(&io_c.io, &srv);
    TEST_ASSERT_TRUE(hc != TS_IO_INVALID);
}

/* Write all of p[0..n) (the fake accepts everything without a send limit). */
static void cli_write(const uint8_t *p, size_t n)
{
    size_t off = 0U;
    while (off < n) {
        int32_t w = ts_io_tcp_send(&io_c.io, hc, &p[off], n - off);
        TEST_ASSERT_TRUE(w > 0);
        off += (size_t)w;
    }
}

static ts_client_t *dclient(void)
{
    ts_addr_t a = { C_IP, (uint16_t)C_UDP };
    return ts_client_find(&d->clients, &a);
}

static size_t put_hello(uint8_t *out, size_t cap, uint32_t kbps)
{
    ts_tlv_hello_t h;
    size_t len = 0U;
    h.udp_port = (uint16_t)C_UDP;
    h.kbps = kbps;
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack_hello(&h, out, cap, &len));
    return len;
}

static size_t put_rate(uint8_t *out, size_t cap, uint32_t kbps)
{
    ts_tlv_set_rate_t r;
    size_t len = 0U;
    r.kbps = kbps;
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack_set_rate(&r, out, cap, &len));
    return len;
}

static size_t put_keyreq(uint8_t *out, size_t cap)
{
    size_t len = 0U;
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack_keyframe_req(out, cap, &len));
    return len;
}

static uint32_t xs32(uint32_t *s)
{
    uint32_t x = *s;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    *s = x;
    return x;
}

/*
 * HELLO, then 12 SET_RATE (each followed by a KEYFRAME_REQ every second time),
 * written in chunks of 1..13 bytes, one tick per chunk. After each tick the
 * client's rate must be the one of the last SET_RATE that is complete on the
 * wire, and every KEYFRAME_REQ complete so far must have been counted.
 */
static void test_tlv_stream_in_random_chunks(void)
{
    uint8_t s[STREAM_CAP];
    size_t hello_end;
    size_t rate_end[N_RATES];
    uint32_t rate_kbps[N_RATES];
    size_t key_end[N_RATES];
    uint32_t keys = 0U;
    size_t len;
    size_t sent = 0U;
    uint32_t seed = 0x105A11CEU;
    uint32_t chunks = 0U;
    ts_client_t *c;

    start();
    len = put_hello(s, sizeof(s), 1500U);
    hello_end = len;
    for (uint32_t k = 0U; k < N_RATES; k++) {
        rate_kbps[k] = 1000U + (250U * k);
        len += put_rate(&s[len], sizeof(s) - len, rate_kbps[k]);
        rate_end[k] = len;
        if ((k % 2U) == 1U) {
            len += put_keyreq(&s[len], sizeof(s) - len);
            key_end[keys] = len;
            keys++;
        }
    }

    while (sent < len) {
        size_t n = (size_t)(1U + (xs32(&seed) % 13U));
        uint32_t want_kbps = 1500U;
        uint32_t want_keys = 0U;
        if (n > (len - sent)) {
            n = len - sent;
        }
        cli_write(&s[sent], n);
        sent += n;
        chunks++;
        step();
        c = dclient();
        if (sent < hello_end) {
            TEST_ASSERT_NULL_MESSAGE(c, "client active before its HELLO was complete");
            continue;
        }
        TEST_ASSERT_NOT_NULL_MESSAGE(c, "HELLO complete on the wire but no active client");
        for (uint32_t k = 0U; k < N_RATES; k++) {
            if (rate_end[k] <= sent) {
                want_kbps = rate_kbps[k];
            }
        }
        for (uint32_t k = 0U; k < keys; k++) {
            if (key_end[k] <= sent) {
                want_keys++;
            }
        }
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(want_kbps, c->kbps, "rate of the last complete SET_RATE not applied");
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(want_keys, c->ctr.keyframe_reqs, "KEYFRAME_REQ lost or counted twice");
    }
    run_ms(5U);
    c = dclient();
    TEST_ASSERT_TRUE(chunks > 10U);
    TEST_ASSERT_NOT_NULL(c);
    TEST_ASSERT_EQUAL_UINT32(rate_kbps[N_RATES - 1U], c->kbps);
    TEST_ASSERT_EQUAL_UINT32(rate_kbps[N_RATES - 1U], c->bucket.kbps);
    TEST_ASSERT_EQUAL_UINT32(keys, c->ctr.keyframe_reqs);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(1U, welcomes, "exactly one WELCOME for one HELLO");
    TEST_ASSERT_FALSE_MESSAGE(closed_c, "daemon closed the control connection");
    TEST_ASSERT_EQUAL_UINT32(0U, d->stats.ctrl_errors);
}

/* Six TLVs in one write after the handshake: all of them in one tick. */
static void test_glued_burst_one_tick(void)
{
    uint8_t s[STREAM_CAP];
    size_t len;
    ts_client_t *c;

    start();
    len = put_hello(s, sizeof(s), 0U);
    cli_write(s, len);
    step();
    c = dclient();
    TEST_ASSERT_NOT_NULL(c);
    TEST_ASSERT_EQUAL_UINT32(1U, welcomes);

    len = put_rate(s, sizeof(s), 1200U);
    len += put_keyreq(&s[len], sizeof(s) - len);
    len += put_rate(&s[len], sizeof(s) - len, 2400U);
    len += put_rate(&s[len], sizeof(s) - len, 3600U);
    len += put_keyreq(&s[len], sizeof(s) - len);
    len += put_rate(&s[len], sizeof(s) - len, 4321U);
    cli_write(s, len);
    step();
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(4321U, c->kbps, "not every TLV of one read was handled");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(4321U, c->bucket.kbps, "bucket rate");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(2U, c->ctr.keyframe_reqs, "KEYFRAME_REQs of one read");
    run_ms(3U);
    TEST_ASSERT_FALSE(closed_c);
    TEST_ASSERT_EQUAL_UINT32(0U, d->stats.ctrl_errors);
}

/*
 * A TLV header split across reads (1 byte, 1 byte, rest), the rest glued to
 * the next TLV, and a HELLO whose last byte comes with a SET_RATE.
 */
static void test_header_split_and_tail_glued(void)
{
    uint8_t s[STREAM_CAP];
    size_t a;
    size_t len;
    ts_client_t *c;

    start();
    a = put_hello(s, sizeof(s), 800U);
    len = a + put_rate(&s[a], sizeof(s) - a, 900U);
    cli_write(s, a - 1U);
    step();
    TEST_ASSERT_NULL(dclient());
    TEST_ASSERT_EQUAL_UINT32(0U, welcomes);
    cli_write(&s[a - 1U], len - (a - 1U));   /* last HELLO byte + the whole SET_RATE */
    step();
    c = dclient();
    TEST_ASSERT_NOT_NULL_MESSAGE(c, "HELLO completed by a later read was not handled");
    TEST_ASSERT_EQUAL_UINT32(1U, welcomes);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(900U, c->kbps, "SET_RATE glued to the HELLO tail");

    len = put_rate(s, sizeof(s), 2500U);
    len += put_keyreq(&s[len], sizeof(s) - len);
    cli_write(&s[0], 1U);
    step();
    cli_write(&s[1], 1U);
    step();
    TEST_ASSERT_EQUAL_UINT32(900U, c->kbps);
    cli_write(&s[2], len - 2U);
    step();
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(2500U, c->kbps, "SET_RATE with a split header");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(1U, c->ctr.keyframe_reqs, "KEYFRAME_REQ after a split TLV");
    run_ms(3U);
    TEST_ASSERT_FALSE(closed_c);
    TEST_ASSERT_EQUAL_UINT32(0U, d->stats.ctrl_errors);
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_tlv_stream_in_random_chunks);
    RUN_TEST(test_glued_burst_one_tick);
    RUN_TEST(test_header_split_and_tail_glued);
    return UNITY_END();
}
