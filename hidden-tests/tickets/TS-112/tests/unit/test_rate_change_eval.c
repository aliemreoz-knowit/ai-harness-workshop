/*
 * tinystream v2 -- hidden evaluation test: tests/unit/test_rate_change_eval.c
 * Rate units end to end (PROTOCOL.md 5 and 8, ts_rate.h): kbps means
 * 1000 bit/s everywhere, buckets count bytes, so a bucket at N kbps earns
 * N / 8 bytes per millisecond no matter whether the rate came from
 * ts_bucket_init(), ts_bucket_set_rate(), HELLO or SET_RATE. The receiver puts
 * the requested kbps on the wire unchanged. Virtual clock and fake I/O only;
 * see meta.yaml for what this test evaluates.
 */
#include <string.h>
#include "unity.h"
#include "ts_config.h"
#include "ts_proto.h"
#include "ts_io.h"
#include "ts_txq.h"
#include "ts_rate.h"
#include "ts_clients.h"
#include "ts_receiver.h"

#define BIG_BURST 10000000U

static ts_clients_t T;
static ts_txq_t Q;

void setUp(void)
{
    ts_clients_init(&T);
    ts_txq_init(&Q, 0U);
}

void tearDown(void)
{
}

/* Bytes a bucket at `kbps` earns in `ms` after its tokens were drained at t = 0. */
static uint32_t earned_after_set(uint32_t init_kbps, uint32_t kbps, uint32_t ms)
{
    ts_bucket_t b;
    ts_bucket_init(&b, init_kbps, BIG_BURST, 0U);
    TEST_ASSERT_TRUE(ts_bucket_take(&b, BIG_BURST));
    ts_bucket_set_rate(&b, kbps);
    ts_bucket_refill(&b, ms);
    return ts_bucket_tokens(&b);
}

static void test_set_rate_earns_kbps_over_8_bytes_per_ms(void)
{
    static const uint32_t rates[] = { 8U, 64U, 800U, 1000U, 2500U, 8000U, 100000U };
    for (uint32_t i = 0U; i < (uint32_t)(sizeof(rates) / sizeof(rates[0])); i++) {
        uint32_t want = (rates[i] * 40U) / 8U;          /* 40 ms */
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(want, earned_after_set(1000U, rates[i], 40U),
                                         "ts_bucket_set_rate(): bytes earned != kbps * ms / 8");
    }
}

static void test_set_rate_matches_init_at_the_same_rate(void)
{
    static const uint32_t rates[] = { 300U, 1200U, 4000U };
    for (uint32_t i = 0U; i < (uint32_t)(sizeof(rates) / sizeof(rates[0])); i++) {
        ts_bucket_t a;
        ts_bucket_init(&a, rates[i], BIG_BURST, 0U);
        TEST_ASSERT_TRUE(ts_bucket_take(&a, BIG_BURST));
        ts_bucket_refill(&a, 25U);
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(ts_bucket_tokens(&a), earned_after_set(9000U, rates[i], 25U),
                                         "a rate set later earns differently from the same rate at init");
    }
}

static void test_set_rate_wait_ms_in_kbps(void)
{
    ts_bucket_t b;
    ts_bucket_init(&b, 4000U, BIG_BURST, 0U);
    TEST_ASSERT_TRUE(ts_bucket_take(&b, BIG_BURST));
    ts_bucket_set_rate(&b, 800U);                       /* 100 B/ms */
    TEST_ASSERT_EQUAL_UINT32(10U, ts_bucket_wait_ms(&b, 1000U));
    ts_bucket_set_rate(&b, 16000U);                     /* 2000 B/ms */
    TEST_ASSERT_EQUAL_UINT32(1U, ts_bucket_wait_ms(&b, 2000U));
}

static ts_client_t *viewer(uint32_t kbps)
{
    ts_addr_t peer;
    ts_client_t *c;
    peer.ip = 0x0A000050U;
    peer.port = 45000U;
    c = ts_client_accept(&T, 1, &peer, 0U);
    TEST_ASSERT_NOT_NULL(c);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_hello(&T, c, 6500U, kbps, &Q, 0U));
    return c;
}

static void test_client_set_rate_and_repeated_hello_in_kbps(void)
{
    ts_client_t *c = viewer(8000U);
    c->bucket.tokens = 0U;
    c->bucket.last_ms = 100U;

    ts_client_set_rate(c, 2000U, 100U);                 /* SET_RATE 2000 kbps = 250 B/ms */
    TEST_ASSERT_EQUAL_UINT32(2000U, c->kbps);
    ts_bucket_refill(&c->bucket, 140U);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(10000U, ts_bucket_tokens(&c->bucket), "SET_RATE 2000: 40 ms must earn 10000 B");

    c->bucket.tokens = 0U;
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_hello(&T, c, 6500U, 640U, &Q, 140U));   /* repeated HELLO: 80 B/ms */
    TEST_ASSERT_EQUAL_UINT32(640U, c->kbps);
    ts_bucket_refill(&c->bucket, 190U);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(4000U, ts_bucket_tokens(&c->bucket), "repeated HELLO 640: 50 ms must earn 4000 B");
}

/* ---- the receiver puts kbps on the wire unchanged ----------------------------- */
static ts_io_fake_t io_srv;
static ts_io_fake_t io_cli;
static tsr_t rx;
static uint8_t wire[4096];

/* Read everything the receiver sent so far; returns the byte count. */
static size_t drain(ts_io_handle_t h, size_t have)
{
    int32_t n;
    while ((have < sizeof(wire)) && ((n = ts_io_tcp_recv(&io_srv.io, h, &wire[have], sizeof(wire) - have)) > 0)) {
        have += (size_t)n;
    }
    return have;
}

/* kbps value of the last TLV of `type` (len 6 HELLO: offset 2, len 4 SET_RATE: offset 0); false if none. */
static bool last_kbps(size_t len, uint8_t type, uint32_t *kbps)
{
    bool found = false;
    size_t p = 0U;
    while ((p + TS_TLV_HDR_LEN) <= len) {
        uint16_t vlen = (uint16_t)((uint16_t)wire[p + 2U] | (uint16_t)((uint16_t)wire[p + 3U] << 8));
        size_t v = p + TS_TLV_HDR_LEN;
        if ((v + vlen) > len) {
            break;
        }
        if (wire[p] == type) {
            size_t o = (type == (uint8_t)TS_TLV_HELLO) ? (v + 2U) : v;
            TEST_ASSERT_EQUAL_UINT16((type == (uint8_t)TS_TLV_HELLO) ? TS_TLV_HELLO_LEN : TS_TLV_SET_RATE_LEN, vlen);
            *kbps = (uint32_t)wire[o] | ((uint32_t)wire[o + 1U] << 8) | ((uint32_t)wire[o + 2U] << 16) |
                    ((uint32_t)wire[o + 3U] << 24);
            found = true;
        }
        p = v + vlen;
    }
    return found;
}

static void test_receiver_sends_kbps_unchanged(void)
{
    ts_addr_t a_srv = { 0x0A000001U, 5601U };
    ts_addr_t a_cli = { 0x0A000060U, 6600U };
    ts_addr_t peer = { 0U, 0U };
    tsr_cfg_t cfg;
    ts_io_handle_t h = TS_IO_INVALID;
    ts_tlv_welcome_t wel;
    uint8_t wbuf[TS_TLV_HDR_LEN + TS_TLV_WELCOME_LEN];
    size_t wlen = 0U;
    size_t got = 0U;
    uint32_t kbps = 0U;
    uint32_t now = 1000U;

    ts_io_fake_init(&io_srv, a_srv);
    ts_io_fake_init(&io_cli, a_cli);
    ts_io_fake_link_tcp(&io_cli, &io_srv);
    tsr_cfg_default(&cfg);
    cfg.server = a_srv;
    cfg.udp_port = a_cli.port;
    cfg.kbps = 777U;
    TEST_ASSERT_EQUAL_INT(TS_OK, tsr_init(&rx, &cfg, &io_cli.io));

    for (uint32_t i = 0U; (i < 50U) && (h == TS_IO_INVALID); i++) {
        ts_io_fake_set_time(&io_cli, now);
        ts_io_fake_set_time(&io_srv, now);
        (void)tsr_tick(&rx, now);
        h = ts_io_tcp_accept(&io_srv.io, &peer);
        now++;
    }
    TEST_ASSERT_TRUE_MESSAGE(h != TS_IO_INVALID, "receiver did not connect");
    (void)memset(&wel, 0, sizeof(wel));
    wel.w = TS_WIDTH;
    wel.h = TS_HEIGHT;
    wel.mtu = TS_MTU_PAYLOAD;
    wel.stream_id = 42U;
    wel.ring_slots = TS_RING_SLOTS;
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_tlv_pack_welcome(&wel, wbuf, sizeof(wbuf), &wlen));
    TEST_ASSERT_EQUAL_INT32((int32_t)wlen, ts_io_tcp_send(&io_srv.io, h, wbuf, wlen));
    for (uint32_t i = 0U; i < 5U; i++) {
        ts_io_fake_set_time(&io_cli, now);
        (void)tsr_tick(&rx, now);
        now++;
    }
    got = drain(h, got);
    TEST_ASSERT_TRUE_MESSAGE(last_kbps(got, (uint8_t)TS_TLV_HELLO, &kbps), "no HELLO on the wire");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(777U, kbps, "HELLO kbps on the wire != requested kbps");

    TEST_ASSERT_EQUAL_INT(TS_OK, tsr_send_set_rate(&rx, 1234U));
    for (uint32_t i = 0U; i < 5U; i++) {
        ts_io_fake_set_time(&io_cli, now);
        (void)tsr_tick(&rx, now);
        now++;
    }
    got = drain(h, got);
    TEST_ASSERT_TRUE_MESSAGE(last_kbps(got, (uint8_t)TS_TLV_SET_RATE, &kbps), "no SET_RATE on the wire");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(1234U, kbps, "SET_RATE kbps on the wire != requested kbps");
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_set_rate_earns_kbps_over_8_bytes_per_ms);
    RUN_TEST(test_set_rate_matches_init_at_the_same_rate);
    RUN_TEST(test_set_rate_wait_ms_in_kbps);
    RUN_TEST(test_client_set_rate_and_repeated_hello_in_kbps);
    RUN_TEST(test_receiver_sends_kbps_unchanged);
    return UNITY_END();
}
