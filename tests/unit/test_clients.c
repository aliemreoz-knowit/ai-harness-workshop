/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/unit/test_clients.c
 * Client table: accept / HELLO / BYE / expiry lifecycle, (ip, port) identity
 * incl. two viewers behind one ip, heartbeat-only liveness, STATS blocks,
 * and the per-client send path (fresh packets, NACK retransmissions, keyframe
 * request rate limit). Virtual clock only.
 */
#include <string.h>
#include "unity.h"
#include "ts_config.h"
#include "ts_proto.h"
#include "ts_txq.h"
#include "ts_rate.h"
#include "ts_clients.h"

#define IP_A  0x0A000020U   /* 10.0.0.32 */
#define IP_B  0x0A000021U   /* 10.0.0.33 */
#define RESERVE (8U * TS_PKT_MAX)   /* client bucket bytes fresh packets leave to retransmissions */

static ts_clients_t T;
static ts_txq_t Q;
static uint8_t enc[TS_ENC_MAX];

void setUp(void)
{
    ts_clients_init(&T);
    ts_txq_init(&Q, 0U);
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

static ts_client_t *connect_client(ts_io_handle_t h, uint32_t ip, uint16_t udp_port, uint32_t now)
{
    ts_addr_t peer = addr(ip, (uint16_t)(40000U + (uint32_t)h));
    ts_client_t *c = ts_client_accept(&T, h, &peer, now);
    TEST_ASSERT_NOT_NULL(c);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_hello(&T, c, udp_port, 0U, &Q, now));
    return c;
}

static void push(uint16_t frame_seq, uint32_t len)
{
    uint8_t n = 0U;
    (void)memset(enc, (int)(frame_seq & 0xFFU), len);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_push_frame(&Q, frame_seq, 0U, 0U, enc, len, &n));
}

/* ---- table + lifecycle ----------------------------------------------------- */
static void test_clients_init_and_accept_until_full(void)
{
    ts_addr_t peer = addr(IP_A, 40000U);
    for (uint8_t i = 0U; i < (uint8_t)TS_MAX_CLIENTS; i++) {
        TEST_ASSERT_EQUAL_INT(TS_CL_FREE, T.c[i].state);
        TEST_ASSERT_EQUAL_UINT8(i, T.c[i].id);
        TEST_ASSERT_EQUAL_INT32(TS_IO_INVALID, T.c[i].tcp);
    }
    for (int32_t h = 0; h < (int32_t)TS_MAX_CLIENTS; h++) {
        ts_client_t *c = ts_client_accept(&T, h, &peer, 10U);
        TEST_ASSERT_NOT_NULL(c);
        TEST_ASSERT_EQUAL_INT(TS_CL_PENDING, c->state);
        TEST_ASSERT_EQUAL_INT32(h, c->tcp);
        TEST_ASSERT_EQUAL_UINT32(10U, c->last_seen_ms);
    }
    TEST_ASSERT_NULL(ts_client_accept(&T, 9, &peer, 10U));      /* fifth viewer refused */
    TEST_ASSERT_EQUAL_UINT32(TS_MAX_CLIENTS, T.accepted);
    TEST_ASSERT_EQUAL_UINT32(1U, T.refused);
    TEST_ASSERT_EQUAL_UINT8(0U, ts_clients_active(&T));          /* PENDING is not active */
    TEST_ASSERT_NULL(ts_client_accept(&T, TS_IO_INVALID, &peer, 10U));
    TEST_ASSERT_NULL(ts_client_accept(NULL, 1, &peer, 10U));
    TEST_ASSERT_NULL(ts_client_accept(&T, 1, NULL, 10U));
    TEST_ASSERT_EQUAL_UINT8(0U, ts_clients_active(NULL));
}

static void test_clients_hello_activates(void)
{
    ts_addr_t key = addr(IP_A, 6000U);
    ts_client_t *c;
    push(1U, 3000U);                                  /* ring not empty before HELLO */
    c = connect_client(3, IP_A, 6000U, 100U);
    TEST_ASSERT_EQUAL_INT(TS_CL_ACTIVE, c->state);
    TEST_ASSERT_EQUAL_UINT8(1U, ts_clients_active(&T));
    TEST_ASSERT_EQUAL_UINT32(IP_A, c->udp.ip);
    TEST_ASSERT_EQUAL_UINT16(6000U, c->udp.port);
    TEST_ASSERT_EQUAL_UINT32(TS_DEFAULT_KBPS, c->kbps);         /* 0 -> default */
    TEST_ASSERT_EQUAL_UINT32(TS_DEFAULT_KBPS, c->bucket.kbps);
    TEST_ASSERT_EQUAL_UINT32(TS_BUCKET_BURST_BYTES, ts_bucket_tokens(&c->bucket));
    TEST_ASSERT_EQUAL_UINT32(0U, ts_txq_cursor_pending(&Q, &c->cursor)); /* starts at the ring end */
    TEST_ASSERT_EQUAL_PTR(c, ts_client_find(&T, &key));
    TEST_ASSERT_EQUAL_PTR(c, ts_client_find_tcp(&T, 3));
    TEST_ASSERT_NULL(ts_client_find_tcp(&T, 4));
    TEST_ASSERT_NULL(ts_client_find_tcp(&T, TS_IO_INVALID));
    /* requested rate is honoured */
    {
        ts_addr_t peer = addr(IP_B, 1U);
        ts_client_t *d = ts_client_accept(&T, 4, &peer, 100U);
        TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_hello(&T, d, 7000U, 1234U, &Q, 100U));
        TEST_ASSERT_EQUAL_UINT32(1234U, d->kbps);
        TEST_ASSERT_EQUAL_UINT8(2U, ts_clients_active(&T));
    }
}

static void test_clients_hello_errors(void)
{
    ts_addr_t peer = addr(IP_A, 1U);
    ts_client_t *c = ts_client_accept(&T, 1, &peer, 0U);
    ts_client_t fake;
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, ts_client_hello(&T, c, 0U, 0U, &Q, 0U));     /* port 0 */
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, ts_client_hello(&T, c, 6000U, 0U, NULL, 0U));
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, ts_client_hello(NULL, c, 6000U, 0U, &Q, 0U));
    (void)memset(&fake, 0, sizeof(fake));
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, ts_client_hello(&T, &fake, 6000U, 0U, &Q, 0U)); /* not in table */
    ts_client_close(&T, c);
    TEST_ASSERT_EQUAL_INT(TS_E_STATE, ts_client_hello(&T, c, 6000U, 0U, &Q, 0U));   /* CLOSING */
    TEST_ASSERT_EQUAL_INT(TS_E_STATE, ts_client_hello(&T, &T.c[3], 6000U, 0U, &Q, 0U)); /* FREE */
}

static void test_clients_same_ip_two_ports_are_two_clients(void)
{
    ts_addr_t k0 = addr(IP_A, 6000U);
    ts_addr_t k1 = addr(IP_A, 6001U);
    ts_addr_t k2 = addr(IP_A, 6002U);
    ts_addr_t kb = addr(IP_B, 6000U);
    ts_client_t *a = connect_client(1, IP_A, 6000U, 0U);
    ts_client_t *b = connect_client(2, IP_A, 6001U, 0U);
    ts_nack_t n;
    ts_retx_batch_t out;
    TEST_ASSERT_TRUE(a != b);
    TEST_ASSERT_EQUAL_UINT8(2U, ts_clients_active(&T));
    TEST_ASSERT_EQUAL_PTR(a, ts_client_find(&T, &k0));
    TEST_ASSERT_EQUAL_PTR(b, ts_client_find(&T, &k1));
    TEST_ASSERT_NULL(ts_client_find(&T, &k2));       /* same ip, other port */
    TEST_ASSERT_NULL(ts_client_find(&T, &kb));       /* same port, other ip */
    /* traffic from b keeps only b alive and only charges b */
    push(1U, 100U);
    (void)memset(&n, 0, sizeof(n));
    n.count = 1U;
    n.e[0].frame_seq = 1U;
    n.e[0].frag_idx = 0U;
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_nack(ts_client_find(&T, &k1), &n, &Q, NULL, 0U, NULL, 1500U, &out));
    TEST_ASSERT_EQUAL_UINT32(1U, b->ctr.nacks_rx);
    TEST_ASSERT_EQUAL_UINT32(0U, a->ctr.nacks_rx);
    TEST_ASSERT_EQUAL_UINT8(1U, ts_clients_expire(&T, 2500U, 0U));
    TEST_ASSERT_EQUAL_INT(TS_CL_CLOSING, a->state);
    TEST_ASSERT_EQUAL_INT(TS_CL_ACTIVE, b->state);
    TEST_ASSERT_NULL(ts_client_find(&T, &k0));
    TEST_ASSERT_EQUAL_PTR(b, ts_client_find(&T, &k1));
}

static void test_clients_hello_same_key_restarts(void)
{
    ts_addr_t k = addr(IP_A, 6000U);
    ts_client_t *old = connect_client(1, IP_A, 6000U, 0U);
    ts_client_t *neu;
    push(1U, 3000U);
    TEST_ASSERT_EQUAL_UINT32(3U, ts_txq_cursor_pending(&Q, &old->cursor));
    neu = connect_client(2, IP_A, 6000U, 50U);       /* viewer restarted: new TCP, same key */
    TEST_ASSERT_TRUE(old != neu);
    TEST_ASSERT_EQUAL_INT(TS_CL_CLOSING, old->state);
    TEST_ASSERT_EQUAL_UINT8(1U, ts_clients_active(&T));
    TEST_ASSERT_EQUAL_PTR(neu, ts_client_find(&T, &k));
    TEST_ASSERT_EQUAL_UINT32(0U, ts_txq_cursor_pending(&Q, &neu->cursor));
    /* HELLO again on the same connection and key: a rate change only -- the
     * cursor keeps its place and the bucket is not refilled to the burst */
    push(2U, 100U);
    TEST_ASSERT_EQUAL_UINT32(1U, ts_txq_cursor_pending(&Q, &neu->cursor));
    neu->bucket.tokens = 100U;
    neu->bucket.last_ms = 50U;
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_hello(&T, neu, 6000U, 8000U, &Q, 60U));
    TEST_ASSERT_EQUAL_UINT32(1U, ts_txq_cursor_pending(&Q, &neu->cursor));
    TEST_ASSERT_EQUAL_UINT32(8000U, neu->kbps);
    /* tokens earned at the old rate (10 ms at 4000 kbps = 5000 B) are kept, no burst refill */
    TEST_ASSERT_EQUAL_UINT32(100U + 5000U, ts_bucket_tokens(&neu->bucket));
    TEST_ASSERT_EQUAL_UINT8(1U, ts_clients_active(&T));
    /* HELLO with another udp_port: the viewer moved, the cursor restarts at the ring end */
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_hello(&T, neu, 6001U, 0U, &Q, 70U));
    TEST_ASSERT_EQUAL_UINT32(0U, ts_txq_cursor_pending(&Q, &neu->cursor));
    TEST_ASSERT_NULL(ts_client_find(&T, &k));
    k.port = 6001U;
    TEST_ASSERT_EQUAL_PTR(neu, ts_client_find(&T, &k));
    TEST_ASSERT_EQUAL_UINT8(1U, ts_clients_active(&T));
}

static void test_clients_bye_close_free(void)
{
    ts_client_t *c = connect_client(1, IP_A, 6000U, 0U);
    ts_client_t *p;
    ts_addr_t peer = addr(IP_B, 1U);
    ts_client_close(&T, c);                          /* BYE */
    TEST_ASSERT_EQUAL_INT(TS_CL_CLOSING, c->state);
    TEST_ASSERT_EQUAL_UINT8(0U, ts_clients_active(&T));
    ts_client_close(&T, c);                          /* idempotent */
    TEST_ASSERT_EQUAL_UINT8(0U, ts_clients_active(&T));
    TEST_ASSERT_EQUAL_PTR(c, ts_client_find_tcp(&T, 1)); /* still owns its socket */
    ts_client_free(&T, c);
    TEST_ASSERT_EQUAL_INT(TS_CL_FREE, c->state);
    TEST_ASSERT_EQUAL_INT32(TS_IO_INVALID, c->tcp);
    TEST_ASSERT_EQUAL_UINT8(0U, c->id);
    TEST_ASSERT_NULL(ts_client_find_tcp(&T, 1));
    ts_client_free(&T, c);                           /* free twice: harmless */
    ts_client_close(&T, c);                          /* close a FREE slot: no-op */
    TEST_ASSERT_EQUAL_INT(TS_CL_FREE, c->state);
    /* freeing an ACTIVE slot directly keeps the count right */
    c = connect_client(2, IP_A, 6000U, 0U);
    TEST_ASSERT_EQUAL_UINT8(1U, ts_clients_active(&T));
    ts_client_free(&T, c);
    TEST_ASSERT_EQUAL_UINT8(0U, ts_clients_active(&T));
    /* PENDING -> CLOSING without touching the active count */
    p = ts_client_accept(&T, 3, &peer, 0U);
    ts_client_close(&T, p);
    TEST_ASSERT_EQUAL_INT(TS_CL_CLOSING, p->state);
    TEST_ASSERT_EQUAL_UINT8(0U, ts_clients_active(&T));
    ts_client_close(NULL, p);
    ts_client_free(NULL, p);
    ts_client_touch(NULL, 0U);
    ts_clients_init(NULL);
}

static void test_clients_expire_after_silence(void)
{
    ts_client_t *c = connect_client(1, IP_A, 6000U, 1000U);
    ts_addr_t peer = addr(IP_B, 1U);
    ts_client_t *p = ts_client_accept(&T, 2, &peer, 1000U);    /* never says HELLO */
    TEST_ASSERT_EQUAL_UINT8(0U, ts_clients_expire(&T, 3000U, TS_CLIENT_TIMEOUT_MS)); /* exactly 2 s */
    TEST_ASSERT_EQUAL_INT(TS_CL_ACTIVE, c->state);
    TEST_ASSERT_EQUAL_UINT8(2U, ts_clients_expire(&T, 3001U, TS_CLIENT_TIMEOUT_MS));
    TEST_ASSERT_EQUAL_INT(TS_CL_CLOSING, c->state);
    TEST_ASSERT_EQUAL_INT(TS_CL_CLOSING, p->state);
    TEST_ASSERT_EQUAL_UINT32(2U, T.expired);
    TEST_ASSERT_EQUAL_UINT8(0U, ts_clients_active(&T));
    TEST_ASSERT_EQUAL_UINT8(0U, ts_clients_expire(&T, 9000U, 0U));  /* CLOSING is not re-counted */
    TEST_ASSERT_EQUAL_UINT8(0U, ts_clients_expire(NULL, 0U, 0U));
}

static void test_clients_heartbeat_only_liveness(void)
{
    /* No UDP at all: control traffic (touch), NACKs or KEYFRAME_REQs keep a client alive. */
    ts_client_t *c = connect_client(1, IP_A, 6000U, 0U);
    ts_client_t *d = connect_client(2, IP_A, 6001U, 0U);
    ts_nack_t n;
    ts_retx_batch_t out;
    (void)memset(&n, 0, sizeof(n));
    for (uint32_t now = 0U; now <= 20000U; now += 100U) {
        if ((now % 1500U) == 0U) {
            ts_client_touch(c, now);                  /* e.g. a TLV every 1.5 s */
        }
        if ((now % 1900U) == 0U) {
            if (((now / 1900U) % 2U) == 0U) {
                TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_nack(d, &n, &Q, NULL, 0U, NULL, now, &out));
            } else {
                (void)ts_client_keyframe_req(d, NULL, now);
            }
        }
        TEST_ASSERT_EQUAL_UINT8(0U, ts_clients_expire(&T, now, TS_CLIENT_TIMEOUT_MS));
    }
    TEST_ASSERT_EQUAL_INT(TS_CL_ACTIVE, c->state);
    TEST_ASSERT_EQUAL_INT(TS_CL_ACTIVE, d->state);
    /* then silence */
    TEST_ASSERT_EQUAL_UINT8(2U, ts_clients_expire(&T, 23000U, TS_CLIENT_TIMEOUT_MS));
}

static void test_clients_expire_across_clock_wrap(void)
{
    ts_client_t *c = connect_client(1, IP_A, 6000U, 0xFFFFFC00U);
    TEST_ASSERT_EQUAL_UINT8(0U, ts_clients_expire(&T, 0x00000100U, 0U));   /* 1280 ms */
    TEST_ASSERT_EQUAL_INT(TS_CL_ACTIVE, c->state);
    TEST_ASSERT_EQUAL_UINT8(1U, ts_clients_expire(&T, 0x00000401U, 0U));   /* 2049 ms */
}

static void test_clients_stats_blocks(void)
{
    ts_stats_client_t s[TS_MAX_CLIENTS];
    ts_addr_t peer = addr(IP_B, 1U);
    ts_client_t *a = connect_client(1, IP_A, 6000U, 100U);
    ts_client_t *b = connect_client(2, IP_A, 6001U, 100U);
    (void)ts_client_accept(&T, 3, &peer, 100U);       /* PENDING: not reported */
    a->ctr.pkts_sent = 11U;
    a->ctr.bytes_sent = 12U;
    a->ctr.nacks_rx = 13U;
    a->ctr.retx_sent = 14U;
    a->ctr.retx_dropped = 15U;
    a->ctr.keyframe_reqs = 16U;
    ts_client_set_rate(b, 2000U, 100U);
    ts_client_touch(b, 400U);
    ts_client_close(&T, b);
    TEST_ASSERT_EQUAL_UINT8(2U, ts_clients_stats(&T, 1100U, s));
    TEST_ASSERT_EQUAL_UINT8(0U, s[0].id);
    TEST_ASSERT_EQUAL_UINT8(1U, s[0].state);          /* wire: 1 = active */
    TEST_ASSERT_EQUAL_UINT16(6000U, s[0].udp_port);
    TEST_ASSERT_EQUAL_UINT32(TS_DEFAULT_KBPS, s[0].kbps);
    TEST_ASSERT_EQUAL_UINT32(11U, s[0].pkts_sent);
    TEST_ASSERT_EQUAL_UINT32(12U, s[0].bytes_sent);
    TEST_ASSERT_EQUAL_UINT32(13U, s[0].nacks_rx);
    TEST_ASSERT_EQUAL_UINT32(14U, s[0].retx_sent);
    TEST_ASSERT_EQUAL_UINT32(15U, s[0].retx_dropped);
    TEST_ASSERT_EQUAL_UINT32(16U, s[0].keyframe_reqs);
    TEST_ASSERT_EQUAL_UINT32(1000U, s[0].last_seen_ms); /* ms since last packet */
    TEST_ASSERT_EQUAL_UINT8(1U, s[1].id);
    TEST_ASSERT_EQUAL_UINT8(2U, s[1].state);          /* wire: 2 = closing */
    TEST_ASSERT_EQUAL_UINT16(6001U, s[1].udp_port);
    TEST_ASSERT_EQUAL_UINT32(2000U, s[1].kbps);
    TEST_ASSERT_EQUAL_UINT32(700U, s[1].last_seen_ms);
    TEST_ASSERT_EQUAL_UINT8(0U, ts_clients_stats(&T, 0U, NULL));
    TEST_ASSERT_EQUAL_UINT8(0U, ts_clients_stats(NULL, 0U, s));
}

/* ---- send path ----------------------------------------------------------------- */
static void test_clients_fresh_packets_paced_by_bucket(void)
{
    ts_client_t *c = connect_client(1, IP_A, 6000U, 0U);
    const ts_txq_slot_t *s;
    uint32_t sent = 0U;
    ts_client_set_rate(c, 8000U, 0U);                 /* 1000 B/ms */
    c->bucket.tokens = 3000U + RESERVE;
    push(1U, 7000U);                                  /* 5 x 1424 B */
    while ((s = ts_client_next_fresh(c, &Q, NULL)) != NULL) {
        TEST_ASSERT_EQUAL_UINT8((uint8_t)sent, s->frag_idx);
        sent++;
    }
    TEST_ASSERT_EQUAL_UINT32(2U, sent);               /* 2 x 1424 <= 3000 < 3 x 1424 */
    TEST_ASSERT_EQUAL_UINT32(3U, ts_txq_cursor_pending(&Q, &c->cursor));   /* waits, not lost */
    ts_clients_tick(&T, 3U);                          /* +3000 B: two more packets */
    while ((s = ts_client_next_fresh(c, &Q, NULL)) != NULL) {
        sent++;
    }
    TEST_ASSERT_EQUAL_UINT32(4U, sent);
    ts_clients_tick(&T, 10U);
    TEST_ASSERT_NOT_NULL(ts_client_next_fresh(c, &Q, NULL));
    TEST_ASSERT_NULL(ts_client_next_fresh(c, &Q, NULL));      /* caught up */
    TEST_ASSERT_EQUAL_UINT32(5U, c->ctr.pkts_sent);
    TEST_ASSERT_EQUAL_UINT32(5U * 1424U, c->ctr.bytes_sent);
    TEST_ASSERT_EQUAL_UINT32(0U, c->cursor.skipped);
}

static void test_clients_fresh_leaves_repair_reserve(void)
{
    ts_client_t *c = connect_client(1, IP_A, 6000U, 0U);
    ts_nack_t n;
    ts_retx_batch_t out;
    uint32_t sent = 0U;
    ts_client_set_rate(c, 800U, 0U);                  /* 100 B/ms */
    c->bucket.tokens = RESERVE + 1424U;
    push(1U, 7000U);                                  /* 5 x 1424 B */
    while (ts_client_next_fresh(c, &Q, NULL) != NULL) {
        sent++;
    }
    TEST_ASSERT_EQUAL_UINT32(1U, sent);               /* fresh traffic stops at the reserve */
    TEST_ASSERT_EQUAL_UINT32(RESERVE, ts_bucket_tokens(&c->bucket));
    (void)memset(&n, 0, sizeof(n));
    n.count = 8U;
    for (uint8_t i = 0U; i < 8U; i++) {
        n.e[i].frame_seq = 1U;
        n.e[i].frag_idx = (uint8_t)(i % 5U);
    }
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_nack(c, &n, &Q, NULL, 0U, NULL, 0U, &out));
    TEST_ASSERT_EQUAL_UINT8(8U, out.count);           /* repairs may use the reserve */
    TEST_ASSERT_EQUAL_UINT32(0U, ts_bucket_tokens(&c->bucket));
    TEST_ASSERT_EQUAL_UINT32(8U, c->ctr.retx_sent);
    n.count = 1U;
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_nack(c, &n, &Q, NULL, 0U, NULL, 0U, &out));
    TEST_ASSERT_EQUAL_UINT8(1U, out.dropped);         /* and are still charged */
    TEST_ASSERT_NULL(ts_client_next_fresh(c, &Q, NULL));
}

static void test_clients_fresh_global_cap_shared(void)
{
    ts_client_t *a = connect_client(1, IP_A, 6000U, 0U);
    ts_client_t *b = connect_client(2, IP_A, 6001U, 0U);
    ts_bucket_t g;
    uint32_t na = 0U;
    uint32_t nb = 0U;
    ts_bucket_init(&g, 8000U, 4U * 1424U, 0U);        /* room for 4 packets in total */
    push(1U, 7000U);
    for (uint32_t i = 0U; i < 5U; i++) {
        if (ts_client_next_fresh(a, &Q, &g) != NULL) {
            na++;
        }
        if (ts_client_next_fresh(b, &Q, &g) != NULL) {
            nb++;
        }
    }
    TEST_ASSERT_EQUAL_UINT32(2U, na);
    TEST_ASSERT_EQUAL_UINT32(2U, nb);
    TEST_ASSERT_TRUE(g.refused > 0U);
    /* not active -> nothing */
    ts_client_close(&T, a);
    TEST_ASSERT_NULL(ts_client_next_fresh(a, &Q, NULL));
    TEST_ASSERT_NULL(ts_client_next_fresh(NULL, &Q, NULL));
}

static void test_clients_nack_serves_ring_and_caps(void)
{
    ts_client_t *c = connect_client(1, IP_A, 6000U, 0U);
    ts_keysched_t k;
    ts_nack_t n;
    ts_retx_batch_t out;
    ts_keysched_init(&k, 0U);
    ts_keysched_sent(&k, 0U);
    for (uint16_t f = 0U; f < 5U; f++) {
        push(f, TS_ENC_MAX);                          /* frame 0 fragments 0..101 evicted */
    }
    (void)memset(&n, 0, sizeof(n));
    n.count = 4U;
    n.e[0].frame_seq = 4U;   n.e[0].frag_idx = 7U;    /* hit */
    n.e[1].frame_seq = 0U;   n.e[1].frag_idx = 3U;    /* too old */
    n.e[2].frame_seq = 9U;   n.e[2].frag_idx = 0U;    /* future */
    n.e[3].frame_seq = 0U;   n.e[3].frag_idx = 105U;  /* hit (partial frame) */
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_nack(c, &n, &Q, NULL, 0U, &k, 500U, &out));
    TEST_ASSERT_EQUAL_UINT8(2U, out.count);
    TEST_ASSERT_EQUAL_UINT16(4U, out.slot[0]->frame_seq);
    TEST_ASSERT_EQUAL_UINT8(7U, out.slot[0]->frag_idx);
    TEST_ASSERT_EQUAL_UINT8(105U, out.slot[1]->frag_idx);
    TEST_ASSERT_EQUAL_UINT8(1U, out.too_old);
    TEST_ASSERT_EQUAL_UINT8(1U, out.not_found);
    TEST_ASSERT_EQUAL_UINT8(0U, out.dropped);
    TEST_ASSERT_EQUAL_UINT32(4U, c->ctr.nacks_rx);
    TEST_ASSERT_EQUAL_UINT32(2U, c->ctr.retx_sent);
    TEST_ASSERT_EQUAL_UINT32(500U, c->last_seen_ms);  /* NACK traffic = liveness */
    TEST_ASSERT_TRUE(k.requested);                    /* too-old asks for a keyframe */
    TEST_ASSERT_EQUAL_INT(TS_KEY_TOO_OLD, ts_keysched_decide(&k, 1000U, 0U));

    /* a full 32-entry NACK: only TS_RETX_PER_TICK - 2 more fit this tick */
    n.count = (uint8_t)TS_NACK_MAX;
    for (uint8_t i = 0U; i < (uint8_t)TS_NACK_MAX; i++) {
        n.e[i].frame_seq = 3U;
        n.e[i].frag_idx = i;
    }
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_nack(c, &n, &Q, NULL, 0U, NULL, 501U, &out));
    TEST_ASSERT_EQUAL_UINT8(TS_RETX_PER_TICK - 2U, out.count);
    TEST_ASSERT_EQUAL_UINT8(TS_NACK_MAX - (TS_RETX_PER_TICK - 2U), out.dropped);
    TEST_ASSERT_EQUAL_UINT32(TS_NACK_MAX - (TS_RETX_PER_TICK - 2U), c->ctr.retx_dropped);
    ts_clients_tick(&T, 502U);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_nack(c, &n, &Q, NULL, 4U, NULL, 502U, &out));
    TEST_ASSERT_EQUAL_UINT8(4U, out.count);           /* explicit cap */
    /* the bucket also limits retransmissions */
    ts_clients_tick(&T, 503U);
    c->bucket.tokens = 2U * 1424U;
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_nack(c, &n, &Q, NULL, 0U, NULL, 503U, &out));
    TEST_ASSERT_EQUAL_UINT8(2U, out.count);
    TEST_ASSERT_EQUAL_UINT8(TS_NACK_MAX - 2U, out.dropped);
}

static void test_clients_nack_errors(void)
{
    ts_client_t *c = connect_client(1, IP_A, 6000U, 0U);
    ts_nack_t n;
    ts_retx_batch_t out;
    (void)memset(&n, 0, sizeof(n));
    n.count = 200U;                                   /* never trusted past TS_NACK_MAX */
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_nack(c, &n, &Q, NULL, 0U, NULL, 1U, &out));
    TEST_ASSERT_EQUAL_UINT32(TS_NACK_MAX, c->ctr.nacks_rx);
    TEST_ASSERT_EQUAL_UINT8(TS_NACK_MAX, out.not_found);
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, ts_client_nack(NULL, &n, &Q, NULL, 0U, NULL, 1U, &out));
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, ts_client_nack(c, NULL, &Q, NULL, 0U, NULL, 1U, &out));
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, ts_client_nack(c, &n, NULL, NULL, 0U, NULL, 1U, &out));
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, ts_client_nack(c, &n, &Q, NULL, 0U, NULL, 1U, NULL));
    ts_client_close(&T, c);
    TEST_ASSERT_EQUAL_INT(TS_E_STATE, ts_client_nack(c, &n, &Q, NULL, 0U, NULL, 1U, &out));
    TEST_ASSERT_EQUAL_UINT8(0U, out.count);
}

static void test_clients_keyframe_req_rate_limited(void)
{
    ts_client_t *a = connect_client(1, IP_A, 6000U, 0U);
    ts_client_t *b = connect_client(2, IP_A, 6001U, 0U);
    ts_keysched_t k;
    ts_keysched_init(&k, 0U);
    ts_keysched_sent(&k, 0U);
    TEST_ASSERT_TRUE(ts_client_keyframe_req(a, &k, 0U));      /* first one at t = 0 */
    TEST_ASSERT_FALSE(ts_client_keyframe_req(a, &k, 100U));
    TEST_ASSERT_FALSE(ts_client_keyframe_req(a, &k, 499U));
    TEST_ASSERT_TRUE(ts_client_keyframe_req(b, &k, 300U));    /* per client, not global */
    TEST_ASSERT_TRUE(ts_client_keyframe_req(a, &k, 500U));
    TEST_ASSERT_EQUAL_UINT32(4U, a->ctr.keyframe_reqs);       /* every request is counted */
    TEST_ASSERT_EQUAL_UINT32(1U, b->ctr.keyframe_reqs);
    TEST_ASSERT_EQUAL_UINT32(500U, a->last_seen_ms);
    TEST_ASSERT_EQUAL_UINT32(3U, k.requests_total);           /* only forwarded ones reach the scheduler */
    TEST_ASSERT_EQUAL_INT(TS_KEY_REQUEST, ts_keysched_decide(&k, 1000U, 0U));
    TEST_ASSERT_TRUE(ts_client_keyframe_req(a, NULL, 1000U));
    ts_client_close(&T, a);
    TEST_ASSERT_FALSE(ts_client_keyframe_req(a, &k, 5000U));
    TEST_ASSERT_FALSE(ts_client_keyframe_req(NULL, &k, 5000U));
}

static void test_clients_set_rate_keeps_earned_tokens(void)
{
    ts_client_t *c = connect_client(1, IP_A, 6000U, 0U);
    ts_client_set_rate(c, 8000U, 0U);
    c->bucket.tokens = 0U;
    ts_client_set_rate(c, 16000U, 10U);               /* 10 ms at 1000 B/ms banked first */
    TEST_ASSERT_EQUAL_UINT32(10000U, ts_bucket_tokens(&c->bucket));
    TEST_ASSERT_EQUAL_UINT32(16000U, c->bucket.kbps);
    ts_clients_tick(&T, 11U);
    TEST_ASSERT_EQUAL_UINT32(12000U, ts_bucket_tokens(&c->bucket));
    ts_client_set_rate(c, 0U, 11U);                   /* 0 -> default */
    TEST_ASSERT_EQUAL_UINT32(TS_DEFAULT_KBPS, c->kbps);
    ts_client_set_rate(NULL, 1U, 0U);
    ts_clients_tick(NULL, 0U);
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_clients_init_and_accept_until_full);
    RUN_TEST(test_clients_hello_activates);
    RUN_TEST(test_clients_hello_errors);
    RUN_TEST(test_clients_same_ip_two_ports_are_two_clients);
    RUN_TEST(test_clients_hello_same_key_restarts);
    RUN_TEST(test_clients_bye_close_free);
    RUN_TEST(test_clients_expire_after_silence);
    RUN_TEST(test_clients_heartbeat_only_liveness);
    RUN_TEST(test_clients_expire_across_clock_wrap);
    RUN_TEST(test_clients_stats_blocks);
    RUN_TEST(test_clients_fresh_packets_paced_by_bucket);
    RUN_TEST(test_clients_fresh_leaves_repair_reserve);
    RUN_TEST(test_clients_fresh_global_cap_shared);
    RUN_TEST(test_clients_nack_serves_ring_and_caps);
    RUN_TEST(test_clients_nack_errors);
    RUN_TEST(test_clients_keyframe_req_rate_limited);
    RUN_TEST(test_clients_set_rate_keeps_earned_tokens);
    return UNITY_END();
}
