/*
 * tinystream v2 -- hidden evaluation test: tests/unit/test_clients_stats_eval.c
 * Per-viewer STATS blocks (PROTOCOL.md 5.1) driven only through the client
 * API: fresh packets, NACK retransmissions (admitted and refused), keyframe
 * requests, SET_RATE and liveness are counted per client, independently of
 * the other viewers, and ts_clients_stats() reports them in slot order.
 * Virtual clock only; see meta.yaml for what this test evaluates.
 */
#include <string.h>
#include "unity.h"
#include "ts_config.h"
#include "ts_proto.h"
#include "ts_txq.h"
#include "ts_rate.h"
#include "ts_clients.h"

#define IP_A  0x0A000040U   /* 10.0.0.64 */
#define IP_B  0x0A000041U   /* 10.0.0.65 */
#define FRAG_PAYLOAD 1400U

static ts_clients_t T;
static ts_txq_t Q;
static uint8_t enc[TS_ENC_MAX];

void setUp(void)
{
    ts_clients_init(&T);
    ts_txq_init(&Q, 100U);
}

void tearDown(void)
{
}

static ts_addr_t addr(uint32_t ip, uint16_t port)
{
    ts_addr_t a;
    a.ip = ip;
    a.port = port;
    return a;
}

static ts_client_t *viewer(ts_io_handle_t h, uint32_t ip, uint16_t udp_port, uint32_t kbps, uint32_t now)
{
    ts_addr_t peer = addr(ip, (uint16_t)(41000U + (uint32_t)h));
    ts_client_t *c = ts_client_accept(&T, h, &peer, now);
    TEST_ASSERT_NOT_NULL(c);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_hello(&T, c, udp_port, kbps, &Q, now));
    return c;
}

static void push(uint16_t frame_seq, uint32_t frags)
{
    uint8_t n = 0U;
    uint32_t len = frags * FRAG_PAYLOAD;
    (void)memset(enc, (int)(0x30U + (frame_seq & 0x0FU)), len);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_push_frame(&Q, frame_seq, 0U, 0U, enc, len, &n));
    TEST_ASSERT_EQUAL_UINT8((uint8_t)frags, n);
}

/* Draw up to `max` fresh packets for `c`; returns the bytes of the packets handed out. */
static uint32_t draw(ts_client_t *c, uint32_t max, uint32_t *count)
{
    uint32_t bytes = 0U;
    uint32_t n = 0U;
    const ts_txq_slot_t *s;
    while ((n < max) && ((s = ts_client_next_fresh(c, &Q, NULL)) != NULL)) {
        bytes += (uint32_t)s->len;
        n++;
    }
    *count = n;
    return bytes;
}

static void nack3(ts_nack_t *n, uint16_t frame_seq)
{
    (void)memset(n, 0, sizeof(*n));
    n->count = 3U;
    for (uint8_t i = 0U; i < 3U; i++) {
        n->e[i].frame_seq = frame_seq;
        n->e[i].frag_idx = i;
    }
}

static const ts_stats_client_t *block_for(const ts_stats_client_t *s, uint8_t n, uint8_t id)
{
    const ts_stats_client_t *r = NULL;
    for (uint8_t i = 0U; i < n; i++) {
        if (s[i].id == id) {
            r = &s[i];
        }
    }
    return r;
}

static void test_stats_count_each_viewer_independently(void)
{
    ts_stats_client_t s[TS_MAX_CLIENTS];
    ts_retx_batch_t out;
    ts_nack_t n;
    uint32_t a_fresh = 0U;
    uint32_t b_fresh = 0U;
    uint32_t a_bytes;
    uint32_t b_bytes;
    ts_client_t *a = viewer(1, IP_A, 6100U, 0U, 100U);
    ts_client_t *b = viewer(2, IP_B, 6200U, 0U, 100U);
    const ts_stats_client_t *sa;
    const ts_stats_client_t *sb;

    push(7U, 10U);
    a_bytes = draw(a, 10U, &a_fresh);
    b_bytes = draw(b, 4U, &b_fresh);
    TEST_ASSERT_EQUAL_UINT32(10U, a_fresh);
    TEST_ASSERT_EQUAL_UINT32(4U, b_fresh);

    /* a: three admitted retransmissions */
    nack3(&n, 7U);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_nack(a, &n, &Q, NULL, 0U, NULL, 500U, &out));
    TEST_ASSERT_EQUAL_UINT8(3U, out.count);
    for (uint8_t i = 0U; i < out.count; i++) {
        a_bytes += (uint32_t)out.slot[i]->len;
    }

    /* b: two keyframe requests, a rate change, then a NACK under a per-tick cap of 1 */
    TEST_ASSERT_TRUE(ts_client_keyframe_req(b, NULL, 200U));
    (void)ts_client_keyframe_req(b, NULL, 300U);          /* inside the 500 ms limit: still counted */
    ts_client_set_rate(b, 2500U, 320U);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_nack(b, &n, &Q, NULL, 1U, NULL, 350U, &out));
    TEST_ASSERT_EQUAL_UINT8(1U, out.count);
    b_bytes += (uint32_t)out.slot[0]->len;

    TEST_ASSERT_EQUAL_UINT8_MESSAGE(2U, ts_clients_stats(&T, 1000U, s), "per-viewer STATS blocks");
    sa = block_for(s, 2U, a->id);
    sb = block_for(s, 2U, b->id);
    TEST_ASSERT_NOT_NULL_MESSAGE(sa, "no STATS block for viewer A");
    TEST_ASSERT_NOT_NULL_MESSAGE(sb, "no STATS block for viewer B");
    TEST_ASSERT_TRUE_MESSAGE(s[0].id < s[1].id, "STATS blocks not in slot order");

    TEST_ASSERT_EQUAL_UINT8(1U, sa->state);
    TEST_ASSERT_EQUAL_UINT16(6100U, sa->udp_port);
    TEST_ASSERT_EQUAL_UINT32(TS_DEFAULT_KBPS, sa->kbps);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(10U, sa->pkts_sent, "A pkts_sent");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(a_bytes, sa->bytes_sent, "A bytes_sent (fresh + retransmits)");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(3U, sa->nacks_rx, "A nacks_rx");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(3U, sa->retx_sent, "A retx_sent");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, sa->retx_dropped, "A retx_dropped");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, sa->keyframe_reqs, "A keyframe_reqs");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(500U, sa->last_seen_ms, "A last_seen_ms");

    TEST_ASSERT_EQUAL_UINT8(1U, sb->state);
    TEST_ASSERT_EQUAL_UINT16(6200U, sb->udp_port);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(2500U, sb->kbps, "B kbps after SET_RATE");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(4U, sb->pkts_sent, "B pkts_sent");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(b_bytes, sb->bytes_sent, "B bytes_sent (fresh + retransmits)");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(3U, sb->nacks_rx, "B nacks_rx");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(1U, sb->retx_sent, "B retx_sent");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(2U, sb->retx_dropped, "B retx_dropped (per-tick cap)");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(2U, sb->keyframe_reqs, "B keyframe_reqs");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(650U, sb->last_seen_ms, "B last_seen_ms");
}

static void test_stats_reports_only_viewers_in_slot_order(void)
{
    ts_stats_client_t s[TS_MAX_CLIENTS];
    ts_addr_t peer = addr(IP_B, 42000U);
    ts_client_t *a = viewer(1, IP_A, 6100U, 0U, 0U);
    ts_client_t *b = viewer(2, IP_A, 6101U, 3000U, 0U);
    ts_client_t *c = viewer(3, IP_B, 6102U, 0U, 0U);
    TEST_ASSERT_NOT_NULL(ts_client_accept(&T, 4, &peer, 0U));   /* PENDING: no block */

    (void)memset(s, 0xAB, sizeof(s));
    TEST_ASSERT_EQUAL_UINT8_MESSAGE(3U, ts_clients_stats(&T, 10U, s), "three ACTIVE viewers");
    TEST_ASSERT_EQUAL_UINT8(a->id, s[0].id);
    TEST_ASSERT_EQUAL_UINT8(b->id, s[1].id);
    TEST_ASSERT_EQUAL_UINT8(c->id, s[2].id);
    TEST_ASSERT_EQUAL_UINT16(6101U, s[1].udp_port);
    TEST_ASSERT_EQUAL_UINT32(3000U, s[1].kbps);
    TEST_ASSERT_EQUAL_UINT8_MESSAGE(0xABU, s[3].id, "a block written past the returned count");

    ts_client_close(&T, a);
    ts_client_free(&T, a);
    TEST_ASSERT_EQUAL_UINT8(2U, ts_clients_stats(&T, 20U, s));
    TEST_ASSERT_EQUAL_UINT8(b->id, s[0].id);
    TEST_ASSERT_EQUAL_UINT8(c->id, s[1].id);
    TEST_ASSERT_EQUAL_UINT8(0U, ts_clients_stats(&T, 20U, NULL));
    TEST_ASSERT_EQUAL_UINT8(0U, ts_clients_stats(NULL, 20U, s));
}

static void test_stats_new_viewer_in_reused_slot_starts_at_zero(void)
{
    ts_stats_client_t s[TS_MAX_CLIENTS];
    uint32_t k = 0U;
    ts_client_t *a = viewer(1, IP_A, 6100U, 0U, 0U);
    uint8_t slot = a->id;
    push(9U, 6U);
    (void)draw(a, 6U, &k);
    TEST_ASSERT_EQUAL_UINT32(6U, k);
    (void)ts_client_keyframe_req(a, NULL, 10U);
    TEST_ASSERT_EQUAL_UINT8(1U, ts_clients_stats(&T, 20U, s));
    TEST_ASSERT_EQUAL_UINT32(6U, s[0].pkts_sent);

    ts_client_close(&T, a);
    ts_client_free(&T, a);
    a = viewer(5, IP_B, 6300U, 0U, 30U);
    TEST_ASSERT_EQUAL_UINT8(slot, a->id);
    TEST_ASSERT_EQUAL_UINT8(1U, ts_clients_stats(&T, 40U, s));
    TEST_ASSERT_EQUAL_UINT16(6300U, s[0].udp_port);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, s[0].pkts_sent, "counters of the previous viewer leaked");
    TEST_ASSERT_EQUAL_UINT32(0U, s[0].bytes_sent);
    TEST_ASSERT_EQUAL_UINT32(0U, s[0].keyframe_reqs);
    TEST_ASSERT_EQUAL_UINT32(10U, s[0].last_seen_ms);
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_stats_count_each_viewer_independently);
    RUN_TEST(test_stats_reports_only_viewers_in_slot_order);
    RUN_TEST(test_stats_new_viewer_in_reused_slot_starts_at_zero);
    return UNITY_END();
}
