/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/unit/test_net.c
 * Daemon core (src/net) driven through the fake ts_io with a virtual clock:
 * HELLO/WELCOME, split and glued TLVs, protocol errors, SET_RATE in kbps,
 * KEYFRAME_REQ, BYE, the 5th client, timeouts, STATS, bounded NACK parsing
 * (count 40 is dropped whole), per-client lookup by (ip, port), retransmit
 * cap, NACK_TOO_OLD, seq-start wrap, tsd_stop. No sockets, no sleeps.
 *
 * The daemon needs proto, codec, txq, rate and clients. On a branch where one
 * of them is still a scaffold stub the dependent tests are IGNOREd with a
 * reason instead of failing for somebody else's module.
 */
#include <string.h>
#include "unity.h"
#include "ts_config.h"
#include "ts_proto.h"
#include "ts_io.h"
#include "ts_daemon.h"
#include "ts_pool.h"

#define D_IP        0x0A000001U   /* 10.0.0.1 */
#define C_IP_BASE   0x0A000020U   /* 10.0.0.32 + i */
#define C_UDP_BASE  6000U
#define MAX_CLI     5U
#define T0          1000U

typedef struct cli {
    ts_io_fake_t   *io;
    ts_io_handle_t  h;
    ts_tlv_acc_t    acc;
    bool            closed;
    uint16_t        udp_port;
} cli_t;

static ts_io_fake_t io_d;
static ts_io_fake_t io_c[MAX_CLI];
static cli_t cl[MAX_CLI];
static tsd_cfg_t cfg;
static tsd_t *d;
static uint32_t now;
static uint8_t buf[2048];

/* datagrams the daemon sent, per destination port, drained every step */
static uint32_t data_rx[MAX_CLI];
static uint32_t retx_rx[MAX_CLI];
static uint32_t key_rx[MAX_CLI];
static ts_hdr_t first_hdr[MAX_CLI];
static ts_hdr_t last_hdr[MAX_CLI];
static bool frame_seq_monotonic;
static uint32_t bad_resume[MAX_CLI];

/* end-to-end check for client 0: reassemble (clean, in-order link), decode, compare with synth */
static bool e2e_on;
static uint16_t e2e_seq;
static uint32_t e2e_have;
static uint32_t e2e_ok;
static uint32_t e2e_bad;
static uint32_t e2e_scf;
static uint8_t e2e_enc[TS_ENC_MAX];
static uint8_t e2e_out[TS_FRAME_BYTES];
static uint8_t e2e_truth[TS_FRAME_BYTES];
static ts_dec_t e2e_dec;

static void e2e_fragment(const ts_hdr_t *h, const uint8_t *payload)
{
    if ((e2e_have == 0U) || (h->frame_seq != e2e_seq)) {
        e2e_seq = h->frame_seq;
        e2e_have = 0U;
    }
    (void)memcpy(&e2e_enc[(size_t)h->frag_idx * TS_MTU_PAYLOAD], payload, h->payload_len);
    e2e_have++;
    if (e2e_have == (uint32_t)h->frag_count) {
        ts_frame_info_t info;
        e2e_have = 0U;
        if (ts_dec_decode(&e2e_dec, e2e_enc, h->frame_len, e2e_out, &info) == TS_OK) {
            ts_cap_synth_frame(e2e_truth, (uint32_t)h->frame_seq, cfg.capture.seed, e2e_scf);
            if (memcmp(e2e_truth, e2e_out, TS_FRAME_BYTES) == 0) {
                e2e_ok++;
            } else {
                e2e_bad++;
            }
        } else {
            e2e_bad++;
        }
    }
}

void setUp(void)
{
    ts_addr_t a = { D_IP, (uint16_t)TS_DEFAULT_UDP_PORT };
    ts_pool_init(ts_pool_get());
    ts_io_fake_init(&io_d, a);
    for (uint32_t i = 0U; i < MAX_CLI; i++) {
        ts_addr_t ca = { C_IP_BASE + i, (uint16_t)(C_UDP_BASE + i) };
        ts_io_fake_init(&io_c[i], ca);
        ts_io_fake_link_tcp(&io_c[i], &io_d);
        (void)memset(&cl[i], 0, sizeof(cl[i]));
        cl[i].io = &io_c[i];
        cl[i].h = TS_IO_INVALID;
        cl[i].udp_port = (uint16_t)(C_UDP_BASE + i);
        ts_tlv_acc_init(&cl[i].acc);
        data_rx[i] = 0U;
        retx_rx[i] = 0U;
        key_rx[i] = 0U;
        bad_resume[i] = 0U;
    }
    frame_seq_monotonic = true;
    e2e_on = false;
    e2e_have = 0U;
    e2e_ok = 0U;
    e2e_bad = 0U;
    tsd_cfg_default(&cfg);
    cfg.stream_id = 0xC0FFEE01U;
    cfg.capture.scene_cut_ms = 600000U;   /* one noise burst at 0.5 s, no cut during a test */
    now = T0;
    d = NULL;
}

void tearDown(void)
{
    if (d != NULL) {
        tsd_stop(d);
    }
}

/* ---- dependency probe ---------------------------------------------------------- */

static void require_deps(void)
{
    ts_tlv_hello_t h = { 1U, 0U };
    ts_bucket_t b;
    size_t len = 0U;
    uint8_t frags = 0U;
    ts_codec_hdr_t ch = { (uint8_t)TS_CODEC_KIND_KEY, 1U, 0U, 0U, (uint16_t)TS_HEIGHT };
    ts_addr_t peer = { C_IP_BASE, 1U };
    static ts_clients_t t;
    static ts_txq_t q;
    ts_clients_init(&t);
    ts_txq_init(&q, 0U);
    ts_bucket_init(&b, 1000U, 100U, 0U);
    if ((ts_tlv_pack_hello(&h, buf, sizeof(buf), &len) == TS_E_NOTIMPL) ||
        (ts_codec_hdr_pack(&ch, buf, sizeof(buf)) == TS_E_NOTIMPL) ||
        (ts_txq_push_frame(&q, 0U, 0U, 0U, buf, 16U, &frags) == TS_E_NOTIMPL) ||
        (ts_client_accept(&t, 0, &peer, 0U) == NULL) ||
        !ts_bucket_take(&b, 10U)) {
        TEST_IGNORE_MESSAGE("proto/codec/txq/clients/rate still scaffold stubs on this branch");
    }
}

/* ---- harness ------------------------------------------------------------------- */

static void create(void)
{
    ts_io_fake_set_time(&io_d, now);
    d = tsd_create(&cfg, &io_d.io, ts_pool_get());
    TEST_ASSERT_NOT_NULL(d);
}

static void drain_daemon_udp(void)
{
    ts_io_dgram_t g;
    while (ts_io_fake_pop_udp(&io_d, &g)) {
        for (uint32_t i = 0U; i < MAX_CLI; i++) {
            if ((g.to.ip == io_c[i].self.ip) && (g.to.port == cl[i].udp_port)) {
                ts_hdr_t h;
                const uint8_t *pl = NULL;
                TEST_ASSERT_EQUAL(TS_OK, ts_hdr_parse(g.data, g.len, &h, &pl));
                if ((h.flags & TS_F_RETRANSMIT) != 0U) {
                    retx_rx[i]++;
                } else {
                    if ((data_rx[i] == 0U)) {
                        first_hdr[i] = h;
                    } else if ((h.frame_seq != last_hdr[i].frame_seq) &&
                               (h.frame_seq != (uint16_t)(last_hdr[i].frame_seq + 1U))) {
                        frame_seq_monotonic = false;
                        if (((h.flags & TS_F_KEYFRAME) == 0U) || (h.frag_idx != 0U)) {
                            bad_resume[i]++;   /* a jump must land on the first fragment of a keyframe */
                        }
                    } else {
                        /* same frame or the next one */
                    }
                    last_hdr[i] = h;
                    data_rx[i]++;
                    if (e2e_on && (i == 0U)) {
                        e2e_fragment(&h, pl);
                    }
                    if (((h.flags & TS_F_KEYFRAME) != 0U) && (h.frag_idx == 0U)) {
                        key_rx[i]++;
                    }
                }
            }
        }
    }
}

static void step(void)
{
    ts_io_fake_set_time(&io_d, now);
    (void)tsd_tick(d, now);
    drain_daemon_udp();
    now++;
}

static void run_ms(uint32_t ms)
{
    for (uint32_t i = 0U; i < ms; i++) {
        step();
    }
}

static void cli_connect(uint32_t i)
{
    ts_addr_t srv = { D_IP, (uint16_t)TS_DEFAULT_TCP_PORT };
    cl[i].h = ts_io_tcp_connect(&io_c[i].io, &srv);
    TEST_ASSERT_TRUE(cl[i].h != TS_IO_INVALID);
}

static void cli_send(uint32_t i, const uint8_t *p, size_t n)
{
    size_t off = 0U;
    while (off < n) {
        int32_t w = ts_io_tcp_send(&io_c[i].io, cl[i].h, &p[off], n - off);
        TEST_ASSERT_TRUE(w > 0);
        off += (size_t)w;
    }
}

static void cli_send_hello(uint32_t i, uint32_t kbps)
{
    ts_tlv_hello_t h;
    size_t len = 0U;
    h.udp_port = cl[i].udp_port;
    h.kbps = kbps;
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack_hello(&h, buf, sizeof(buf), &len));
    cli_send(i, buf, len);
}

/* Read everything the daemon wrote to client i into its accumulate buffer. */
static void cli_pump(uint32_t i)
{
    uint8_t tmp[512];
    int32_t n;
    do {
        n = ts_io_tcp_recv(&io_c[i].io, cl[i].h, tmp, sizeof(tmp));
        if (n > 0) {
            TEST_ASSERT_EQUAL(TS_OK, ts_tlv_acc_push(&cl[i].acc, tmp, (size_t)n));
        } else if (n == TS_IO_CLOSED) {
            cl[i].closed = true;
        } else {
            /* nothing more */
        }
    } while (n > 0);
}

/* Next TLV of `type` from client i's stream (skipping others); false when none. */
static bool cli_next(uint32_t i, uint8_t type, ts_tlv_t *out)
{
    bool found = false;
    ts_tlv_t t;
    cli_pump(i);
    while (!found && (ts_tlv_acc_next(&cl[i].acc, &t) == TS_OK)) {
        if (t.type == type) {
            *out = t;
            found = true;
        }
    }
    return found;
}

static ts_client_t *daemon_client(uint32_t i)
{
    ts_addr_t a = { io_c[i].self.ip, cl[i].udp_port };
    return ts_client_find(&d->clients, &a);
}

/* Full handshake for client i: connect, HELLO, WELCOME. */
static void join(uint32_t i, uint32_t kbps)
{
    ts_tlv_t t;
    cli_connect(i);
    cli_send_hello(i, kbps);
    step();
    TEST_ASSERT_TRUE_MESSAGE(cli_next(i, (uint8_t)TS_TLV_WELCOME, &t), "WELCOME after HELLO");
    TEST_ASSERT_NOT_NULL(daemon_client(i));
}

static void push_udp_from(uint32_t i, uint16_t port, const uint8_t *p, size_t n)
{
    ts_io_dgram_t g;
    (void)memset(&g, 0, sizeof(g));
    g.from.ip = io_c[i].self.ip;
    g.from.port = port;
    g.to = io_d.self;
    g.len = (uint16_t)n;
    (void)memcpy(g.data, p, n);
    TEST_ASSERT_EQUAL(TS_OK, ts_io_fake_push_udp(&io_d, &g));
}

static void send_nack(uint32_t i, const ts_nack_entry_t *e, uint8_t count)
{
    ts_nack_t n;
    size_t len = 0U;
    (void)memset(&n, 0, sizeof(n));
    n.count = count;
    for (uint8_t k = 0U; k < count; k++) {
        n.e[k] = e[k];
    }
    TEST_ASSERT_EQUAL(TS_OK, ts_nack_pack(&n, buf, sizeof(buf), &len));
    push_udp_from(i, cl[i].udp_port, buf, len);
}

static void keepalive(uint32_t i)
{
    send_nack(i, NULL, 0U);   /* empty NACK = "still here" */
}

/* ---- tests that need no other module -------------------------------------------- */

static void test_create_seals_pool_and_validates(void)
{
    ts_pool_t *p = ts_pool_get();
    TEST_ASSERT_NULL(tsd_create(NULL, &io_d.io, p));
    TEST_ASSERT_NULL(tsd_create(&cfg, NULL, p));
    TEST_ASSERT_NULL(tsd_create(&cfg, &io_d.io, NULL));
    cfg.capture.kind = TS_CAP_FILE;
    (void)strcpy(cfg.capture.path, "/nonexistent.y8");
    TEST_ASSERT_NULL(tsd_create(&cfg, &io_d.io, p));
    TEST_ASSERT_FALSE(ts_pool_sealed(p));
    tsd_cfg_default(&cfg);
    cfg.seq_start = 65000U;
    cfg.kbps_client = 0U;   /* 0 = default */
    create();
    TEST_ASSERT_EQUAL_PTR(&p->daemon, d);
    TEST_ASSERT_TRUE(ts_pool_sealed(p));
    TEST_ASSERT_EQUAL(TSD_RUNNING, d->state);
    TEST_ASSERT_EQUAL_UINT16(65000U, d->frame_seq_next);
    TEST_ASSERT_EQUAL_UINT32(65000U, d->cap.cfg.idx_start);
    TEST_ASSERT_EQUAL_UINT32(TS_DEFAULT_KBPS, d->cfg.kbps_client);
    TEST_ASSERT_TRUE(d->cfg.stream_id != 0U);
}

static void test_tick_state_errors(void)
{
    TEST_ASSERT_EQUAL(TS_E_INVAL, tsd_tick(NULL, 0U));
    create();
    tsd_stop(d);
    TEST_ASSERT_EQUAL(TSD_STOPPED, d->state);
    TEST_ASSERT_EQUAL(TS_E_STATE, tsd_tick(d, now));
    TEST_ASSERT_NULL(tsd_stats(NULL));
    TEST_ASSERT_EQUAL_PTR(&d->stats, tsd_stats(d));
    d = NULL;
}

static void test_pool_init_done_seals(void)
{
    ts_pool_t *p = ts_pool_get();
    ts_pool_init(p);
    TEST_ASSERT_FALSE(ts_pool_sealed(p));
    ts_init_done();
    TEST_ASSERT_TRUE(ts_pool_sealed(p));
    TEST_ASSERT_TRUE(ts_pool_size() <= (1536U * 1024U));
}

static void test_fake_tcp_split_and_close(void)
{
    /* the fake itself: limit splits writes, close is seen by the peer after the data */
    ts_addr_t srv = { D_IP, 1U };
    ts_addr_t peer;
    ts_io_handle_t hc = ts_io_tcp_connect(&io_c[0].io, &srv);
    ts_io_handle_t hs = ts_io_tcp_accept(&io_d.io, &peer);
    uint8_t out[8];
    TEST_ASSERT_TRUE((hc != TS_IO_INVALID) && (hs != TS_IO_INVALID));
    TEST_ASSERT_EQUAL_HEX32(C_IP_BASE, peer.ip);
    TEST_ASSERT_EQUAL(TS_IO_INVALID, ts_io_tcp_accept(&io_d.io, &peer));
    io_c[0].tcp_send_limit = 3U;
    TEST_ASSERT_EQUAL_INT32(3, ts_io_tcp_send(&io_c[0].io, hc, (const uint8_t *)"abcdef", 6U));
    TEST_ASSERT_EQUAL_INT32(3, ts_io_tcp_send(&io_c[0].io, hc, (const uint8_t *)"def", 3U));
    ts_io_tcp_close(&io_c[0].io, hc);
    TEST_ASSERT_EQUAL_INT32(6, ts_io_tcp_recv(&io_d.io, hs, out, sizeof(out)));
    TEST_ASSERT_EQUAL_MEMORY("abcdef", out, 6U);
    TEST_ASSERT_EQUAL_INT32(TS_IO_CLOSED, ts_io_tcp_recv(&io_d.io, hs, out, sizeof(out)));
    TEST_ASSERT_EQUAL_INT32(TS_IO_ERR, ts_io_tcp_send(&io_d.io, hs, out, 1U));
    ts_io_tcp_close(&io_d.io, hs);
}

/* ---- control channel ------------------------------------------------------------- */

static void test_hello_welcome(void)
{
    ts_tlv_t t;
    ts_tlv_welcome_t w;
    ts_client_t *c;
    require_deps();
    create();
    run_ms(5U);
    cli_connect(0U);
    step();
    TEST_ASSERT_EQUAL_UINT8(0U, ts_clients_active(&d->clients));   /* PENDING until HELLO */
    TEST_ASSERT_FALSE(cli_next(0U, (uint8_t)TS_TLV_WELCOME, &t));
    cli_send_hello(0U, 0U);
    step();
    TEST_ASSERT_TRUE(cli_next(0U, (uint8_t)TS_TLV_WELCOME, &t));
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_parse_welcome(&t, &w));
    TEST_ASSERT_EQUAL_UINT16(TS_WIDTH, w.w);
    TEST_ASSERT_EQUAL_UINT16(TS_HEIGHT, w.h);
    TEST_ASSERT_EQUAL_UINT16(TS_MTU_PAYLOAD, w.mtu);
    TEST_ASSERT_EQUAL_UINT16(TS_RING_SLOTS, w.ring_slots);
    TEST_ASSERT_EQUAL_HEX32(0xC0FFEE01U, w.stream_id);
    TEST_ASSERT_EQUAL_UINT8(1U, ts_clients_active(&d->clients));
    c = daemon_client(0U);
    TEST_ASSERT_NOT_NULL(c);
    TEST_ASSERT_EQUAL_UINT32(TS_DEFAULT_KBPS, c->kbps);   /* HELLO kbps 0 = daemon default */
    /* data flows to (peer ip, HELLO udp_port); the first frame a new client sees is a keyframe */
    run_ms(100U);
    TEST_ASSERT_TRUE(data_rx[0] > 0U);
    TEST_ASSERT_TRUE((first_hdr[0].flags & TS_F_KEYFRAME) != 0U);
    TEST_ASSERT_EQUAL_UINT8(0U, first_hdr[0].frag_idx);
    TEST_ASSERT_TRUE(frame_seq_monotonic);
    TEST_ASSERT_EQUAL_UINT32(0U, d->stats.ctrl_errors);
}

static void test_hello_split_byte_by_byte(void)
{
    ts_tlv_hello_t h = { (uint16_t)C_UDP_BASE, 1234U };
    size_t len = 0U;
    ts_tlv_t t;
    require_deps();
    cfg.capture.fps = 1U;   /* keep the data path quiet */
    cfg.fps = 1U;
    create();
    cli_connect(0U);
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack_hello(&h, buf, sizeof(buf), &len));
    io_c[0].tcp_send_limit = 1U;
    for (size_t k = 0U; k < len; k++) {
        TEST_ASSERT_EQUAL_INT32(1, ts_io_tcp_send(&io_c[0].io, cl[0].h, &buf[k], len - k));
        step();
        if ((k + 1U) < len) {
            TEST_ASSERT_FALSE_MESSAGE(cli_next(0U, (uint8_t)TS_TLV_WELCOME, &t), "WELCOME before the HELLO was complete");
            TEST_ASSERT_NULL(daemon_client(0U));
        }
    }
    TEST_ASSERT_TRUE(cli_next(0U, (uint8_t)TS_TLV_WELCOME, &t));
    TEST_ASSERT_EQUAL_UINT32(1234U, daemon_client(0U)->kbps);
    TEST_ASSERT_EQUAL_UINT32(0U, d->stats.ctrl_errors);
}

static void test_glued_tlvs_and_set_rate_kbps(void)
{
    ts_tlv_hello_t h = { (uint16_t)C_UDP_BASE, 0U };
    ts_tlv_set_rate_t r = { 2000U };
    size_t a = 0U;
    size_t b = 0U;
    size_t c = 0U;
    ts_tlv_t t;
    ts_client_t *dc;
    require_deps();
    create();
    run_ms(2U);
    cli_connect(0U);
    /* HELLO + SET_RATE + KEYFRAME_REQ in one write */
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack_hello(&h, buf, sizeof(buf), &a));
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack_set_rate(&r, &buf[a], sizeof(buf) - a, &b));
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack_keyframe_req(&buf[a + b], sizeof(buf) - a - b, &c));
    cli_send(0U, buf, a + b + c);
    step();
    TEST_ASSERT_TRUE(cli_next(0U, (uint8_t)TS_TLV_WELCOME, &t));
    dc = daemon_client(0U);
    TEST_ASSERT_NOT_NULL(dc);
    /* kilobits per second end to end: 2000 kbps, not 2 000 000 and not 2 */
    TEST_ASSERT_EQUAL_UINT32(2000U, dc->kbps);
    TEST_ASSERT_EQUAL_UINT32(2000U, dc->bucket.kbps);
    TEST_ASSERT_EQUAL_UINT32(1U, dc->ctr.keyframe_reqs);
    /* SET_RATE split in two writes, then 0 = back to the default */
    r.kbps = 0U;
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack_set_rate(&r, buf, sizeof(buf), &b));
    cli_send(0U, buf, 3U);
    step();
    TEST_ASSERT_EQUAL_UINT32(2000U, dc->kbps);
    cli_send(0U, &buf[3], b - 3U);
    step();
    TEST_ASSERT_EQUAL_UINT32(TS_DEFAULT_KBPS, dc->kbps);
    TEST_ASSERT_EQUAL_UINT32(TS_DEFAULT_KBPS, dc->bucket.kbps);
    TEST_ASSERT_EQUAL_UINT32(0U, d->stats.ctrl_errors);
}

static void test_unknown_tlv_closes_connection(void)
{
    const uint8_t shell[] = { 0x63U, 0x00U, 0x02U, 0x00U, 'l', 's' };   /* type 99: never skipped */
    require_deps();
    create();
    join(0U, 0U);
    cli_send(0U, shell, sizeof(shell));
    step();
    step();
    cli_pump(0U);
    TEST_ASSERT_TRUE(cl[0].closed);
    TEST_ASSERT_NULL(daemon_client(0U));
    TEST_ASSERT_EQUAL_UINT32(1U, d->stats.ctrl_errors);
    TEST_ASSERT_EQUAL_UINT8(0U, ts_clients_active(&d->clients));
}

static void test_tlv_too_long_closes_connection(void)
{
    const uint8_t big[] = { (uint8_t)TS_TLV_HELLO, 0x00U, 0x58U, 0x02U };   /* len 600 > 512 */
    require_deps();
    create();
    cli_connect(0U);
    cli_send(0U, big, sizeof(big));
    step();
    step();
    cli_pump(0U);
    TEST_ASSERT_TRUE(cl[0].closed);
    TEST_ASSERT_EQUAL_UINT32(1U, d->stats.ctrl_errors);
}

static void test_bye_frees_client(void)
{
    size_t len = 0U;
    require_deps();
    create();
    join(0U, 0U);
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack_bye(buf, sizeof(buf), &len));
    cli_send(0U, buf, len);
    step();
    step();
    cli_pump(0U);
    TEST_ASSERT_TRUE(cl[0].closed);
    TEST_ASSERT_NULL(daemon_client(0U));
    TEST_ASSERT_EQUAL_UINT32(0U, d->stats.ctrl_errors);
    for (uint32_t i = 0U; i < TS_MAX_CLIENTS; i++) {
        TEST_ASSERT_EQUAL(TS_CL_FREE, d->clients.c[i].state);
    }
}

static void test_fifth_client_refused(void)
{
    ts_tlv_t t;
    require_deps();
    create();
    for (uint32_t i = 0U; i < 4U; i++) {
        join(i, 0U);
    }
    cli_connect(4U);
    cli_send_hello(4U, 0U);
    step();
    step();
    TEST_ASSERT_FALSE(cli_next(4U, (uint8_t)TS_TLV_WELCOME, &t));
    TEST_ASSERT_TRUE(cl[4].closed);
    TEST_ASSERT_EQUAL_UINT8(4U, ts_clients_active(&d->clients));
}

static void test_client_timeout_and_keepalive(void)
{
    require_deps();
    create();
    join(0U, 0U);
    join(1U, 0U);
    /* client 0 sends an empty NACK every 500 ms, client 1 stays silent */
    for (uint32_t k = 0U; k < 6U; k++) {
        keepalive(0U);
        run_ms(500U);
    }
    TEST_ASSERT_NOT_NULL(daemon_client(0U));
    TEST_ASSERT_NULL(daemon_client(1U));
    cli_pump(1U);
    TEST_ASSERT_TRUE(cl[1].closed);
    TEST_ASSERT_EQUAL_UINT8(1U, ts_clients_active(&d->clients));
}

static void test_silent_connection_without_hello_is_dropped(void)
{
    require_deps();
    create();
    cli_connect(0U);
    step();
    TEST_ASSERT_NOT_NULL(ts_client_find_tcp(&d->clients, d->clients.c[0].tcp));
    TEST_ASSERT_EQUAL(TS_CL_PENDING, d->clients.c[0].state);
    run_ms(TS_CLIENT_TIMEOUT_MS + 10U);
    cli_pump(0U);
    TEST_ASSERT_TRUE(cl[0].closed);
    for (uint32_t i = 0U; i < TS_MAX_CLIENTS; i++) {
        TEST_ASSERT_EQUAL(TS_CL_FREE, d->clients.c[i].state);
    }
}

static void test_stats_every_second(void)
{
    ts_tlv_t t;
    ts_tlv_stats_t st;
    uint32_t n = 0U;
    require_deps();
    create();
    join(0U, 0U);
    for (uint32_t k = 0U; k < 4U; k++) {
        keepalive(0U);
        run_ms(500U);
    }
    while (cli_next(0U, (uint8_t)TS_TLV_STATS, &t)) {
        TEST_ASSERT_EQUAL(TS_OK, ts_tlv_parse_stats(&t, &st));
        n++;
    }
    TEST_ASSERT_TRUE((n >= 1U) && (n <= 3U));
    TEST_ASSERT_EQUAL_UINT8(1U, st.g.client_count);
    TEST_ASSERT_EQUAL_UINT16(C_UDP_BASE, st.c[0].udp_port);
    TEST_ASSERT_TRUE(st.g.frames_captured > 30U);
    TEST_ASSERT_TRUE(st.g.pkts_sent > 0U);
    TEST_ASSERT_TRUE(st.c[0].pkts_sent > 0U);
    TEST_ASSERT_TRUE(st.g.uptime_ms >= 1000U);
}

static void test_keyframe_req_produces_keyframe(void)
{
    ts_tlv_t t;
    size_t len = 0U;
    uint32_t before;
    require_deps();
    create();
    join(0U, 0U);
    for (uint32_t k = 0U; k < 6U; k++) {   /* past the first keyframe cooldown and the 0.5 s noise burst */
        keepalive(0U);
        run_ms(500U);
    }
    (void)cli_next(0U, (uint8_t)TS_TLV_STATS, &t);
    before = d->stats.keyframes_sent;
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack_keyframe_req(buf, sizeof(buf), &len));
    cli_send(0U, buf, len);
    keepalive(0U);
    run_ms(500U);
    keepalive(0U);
    run_ms(700U);
    TEST_ASSERT_EQUAL_UINT32(1U, daemon_client(0U)->ctr.keyframe_reqs);
    TEST_ASSERT_TRUE(d->stats.keyframes_sent > before);
}

/* ---- keyframe rate limits: HELLO churn and KEYFRAME_REQ spam ---------------------- */

/* Read and drop everything the daemon wrote to client i (WELCOME floods). */
static void cli_discard(uint32_t i)
{
    uint8_t tmp[512];
    int32_t n;
    if (cl[i].h != TS_IO_INVALID) {
        do {
            n = ts_io_tcp_recv(&io_c[i].io, cl[i].h, tmp, sizeof(tmp));
        } while (n > 0);
    }
}

static void warm_up(void)
{
    for (uint32_t k = 0U; k < 3U; k++) {   /* past the first keyframe cooldown and the 0.5 s noise burst */
        keepalive(0U);
        run_ms(500U);
        cli_discard(0U);
    }
}

static void test_rehello_every_tick_keyframes_rate_limited(void)
{
    uint32_t before;
    uint32_t data_before;
    require_deps();
    cfg.kbps_client = 1000000U;
    cfg.kbps_global = 1000000U;
    create();
    join(0U, 0U);
    warm_up();
    before = d->stats.keyframes_sent;
    data_before = data_rx[0];
    for (uint32_t k = 0U; k < 3000U; k++) {   /* 3 s, a HELLO on the same connection every tick */
        cli_send_hello(0U, 0U);
        step();
        cli_discard(0U);
    }
    /* at most one keyframe per TS_KEYFRAME_COOLDOWN_MS (the same key keeps its cursor: none needed) */
    TEST_ASSERT_TRUE_MESSAGE((d->stats.keyframes_sent - before) <= 3U, "re-HELLO bypasses the keyframe cooldown");
    TEST_ASSERT_EQUAL_UINT32(0U, d->stats.ctrl_errors);
    TEST_ASSERT_EQUAL_UINT8(1U, ts_clients_active(&d->clients));
    TEST_ASSERT_NOT_NULL(daemon_client(0U));
    TEST_ASSERT_TRUE(data_rx[0] > (data_before + 300U));   /* the stream kept flowing */
    TEST_ASSERT_TRUE(frame_seq_monotonic);
}

static void test_rehello_keeps_bucket_tokens(void)
{
    ts_client_t *c;
    uint32_t sent;
    require_deps();
    cfg.kbps_client = 80U;   /* 10 bytes per ms */
    create();
    join(0U, 0U);
    run_ms(10U);
    c = daemon_client(0U);
    TEST_ASSERT_NOT_NULL(c);
    c->bucket.tokens = 0U;   /* the viewer has spent its burst */
    c->bucket.last_ms = now;
    sent = c->ctr.pkts_sent;
    cli_send_hello(0U, 0U);
    step();
    cli_discard(0U);
    TEST_ASSERT_EQUAL_PTR(c, daemon_client(0U));
    TEST_ASSERT_TRUE_MESSAGE(ts_bucket_tokens(&c->bucket) < TS_PKT_MAX, "re-HELLO refilled the bucket burst");
    TEST_ASSERT_EQUAL_UINT32(sent, c->ctr.pkts_sent);
}

static void test_reconnect_churn_keyframes_rate_limited(void)
{
    uint32_t before;
    require_deps();
    cfg.kbps_client = 1000000U;
    cfg.kbps_global = 1000000U;
    create();
    join(0U, 0U);
    warm_up();
    before = d->stats.keyframes_sent;
    for (uint32_t k = 0U; k < 60U; k++) {   /* 3 s: a new connection + HELLO every 50 ms */
        uint32_t i = 1U + (k % 3U);
        if (cl[i].h != TS_IO_INVALID) {
            ts_io_tcp_close(&io_c[i].io, cl[i].h);   /* the viewer quits ... */
            cl[i].h = TS_IO_INVALID;
            step();                                   /* ... the daemon frees its slot ... */
        }
        cli_connect(i);                               /* ... and it comes straight back */
        cli_send_hello(i, 0U);
        keepalive(0U);
        for (uint32_t t = 0U; t < 50U; t++) {
            step();
            for (uint32_t j = 0U; j < 4U; j++) {
                cli_discard(j);
            }
        }
        TEST_ASSERT_NOT_NULL(daemon_client(i));
    }
    TEST_ASSERT_TRUE_MESSAGE((d->stats.keyframes_sent - before) <= 4U, "HELLO churn bypasses the keyframe cooldown");
    TEST_ASSERT_NOT_NULL(daemon_client(0U));
    for (uint32_t i = 1U; i < 4U; i++) {
        TEST_ASSERT_TRUE(data_rx[i] > 0U);
        TEST_ASSERT_TRUE((first_hdr[i].flags & TS_F_KEYFRAME) != 0U);   /* newcomers start on a keyframe */
    }
}

static void test_new_client_starts_on_held_keyframe(void)
{
    uint32_t before;
    require_deps();
    cfg.kbps_client = 1000000U;
    cfg.kbps_global = 1000000U;
    create();
    join(0U, 0U);
    run_ms(300U);   /* inside the cooldown of the first keyframe */
    before = d->stats.keyframes_sent;
    join(1U, 0U);
    run_ms(5U);
    TEST_ASSERT_EQUAL_UINT32(before, d->stats.keyframes_sent);   /* no extra keyframe spent */
    TEST_ASSERT_TRUE(data_rx[1] > 0U);
    TEST_ASSERT_TRUE((first_hdr[1].flags & TS_F_KEYFRAME) != 0U);
    TEST_ASSERT_EQUAL_UINT8(0U, first_hdr[1].frag_idx);
    TEST_ASSERT_EQUAL_UINT16(first_hdr[0].frame_seq, first_hdr[1].frame_seq);
    TEST_ASSERT_TRUE(frame_seq_monotonic);
}

static void test_keyframe_req_spam_limited_per_client(void)
{
    ts_client_t *c;
    uint8_t req[TS_TLV_HDR_LEN];
    size_t len = 0U;
    uint32_t reqs;
    uint32_t keys;
    uint32_t fwd;
    require_deps();
    create();
    join(0U, 0U);
    warm_up();
    c = daemon_client(0U);
    reqs = c->ctr.keyframe_reqs;
    keys = d->stats.keyframes_sent;
    fwd = d->keysched.requests_total;
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack_keyframe_req(req, sizeof(req), &len));
    for (uint32_t k = 0U; k < 30U; k++) {   /* 3 s, one KEYFRAME_REQ every 100 ms */
        cli_send(0U, req, len);
        keepalive(0U);
        run_ms(100U);
        cli_discard(0U);
    }
    TEST_ASSERT_EQUAL_UINT32(reqs + 30U, c->ctr.keyframe_reqs);
    /* TS_KEYREQ_MIN_INTERVAL_MS per client: at most one forwarded request per 500 ms */
    TEST_ASSERT_TRUE((d->keysched.requests_total - fwd) <= 7U);
    TEST_ASSERT_TRUE((d->keysched.requests_total - fwd) >= 5U);
    TEST_ASSERT_TRUE((d->stats.keyframes_sent - keys) <= 3U);
    TEST_ASSERT_TRUE((d->stats.keyframes_sent - keys) >= 2U);
}

/* ---- UDP: NACK ------------------------------------------------------------------- */

static void test_nack_count_40_rejected(void)
{
    ts_client_t *c;
    uint8_t pkt[4U + (4U * 40U)];
    ts_nack_entry_t e;
    require_deps();
    cfg.kbps_client = 1000000U;
    cfg.kbps_global = 1000000U;
    create();
    join(0U, 0U);
    run_ms(60U);
    c = daemon_client(0U);
    TEST_ASSERT_NOT_NULL(c);
    TEST_ASSERT_TRUE(data_rx[0] > 0U);
    /* a NACK that claims 40 entries (and carries them): dropped whole, nothing read past 32 */
    (void)memset(pkt, 0, sizeof(pkt));
    ts_wr16(pkt, (uint16_t)TS_MAGIC_NACK);
    pkt[2] = 40U;
    for (uint32_t k = 0U; k < 40U; k++) {
        ts_wr16(&pkt[4U + (4U * k)], first_hdr[0].frame_seq);
        pkt[6U + (4U * k)] = 0U;
    }
    push_udp_from(0U, cl[0].udp_port, pkt, sizeof(pkt));
    /* the same 40 claim in a 132-byte packet is a length mismatch: dropped too */
    push_udp_from(0U, cl[0].udp_port, pkt, (size_t)TS_NACK_PKT_MAX);
    step();
    TEST_ASSERT_EQUAL_UINT32(0U, d->stats.nacks_rx);
    TEST_ASSERT_EQUAL_UINT32(0U, c->ctr.nacks_rx);
    TEST_ASSERT_EQUAL_UINT32(0U, retx_rx[0]);
    TEST_ASSERT_EQUAL(TS_CL_ACTIVE, c->state);   /* a bad datagram is not a reason to drop the viewer */
    /* a valid NACK for the same fragment is answered with a retransmission */
    e.frame_seq = first_hdr[0].frame_seq;
    e.frag_idx = 0U;
    send_nack(0U, &e, 1U);
    step();
    TEST_ASSERT_EQUAL_UINT32(1U, d->stats.nacks_rx);
    TEST_ASSERT_EQUAL_UINT32(1U, c->ctr.nacks_rx);
    TEST_ASSERT_EQUAL_UINT32(1U, d->stats.retx_sent);
    TEST_ASSERT_EQUAL_UINT32(1U, retx_rx[0]);
}

static void test_nack_matches_ip_and_port(void)
{
    ts_nack_entry_t e;
    ts_nack_t n;
    size_t len = 0U;
    require_deps();
    cfg.kbps_client = 1000000U;
    cfg.kbps_global = 1000000U;
    create();
    join(0U, 0U);
    run_ms(60U);
    e.frame_seq = first_hdr[0].frame_seq;
    e.frag_idx = 0U;
    (void)memset(&n, 0, sizeof(n));
    n.count = 1U;
    n.e[0] = e;
    TEST_ASSERT_EQUAL(TS_OK, ts_nack_pack(&n, buf, sizeof(buf), &len));
    /* same IP, other port: not this client */
    push_udp_from(0U, (uint16_t)(cl[0].udp_port + 1U), buf, len);
    step();
    TEST_ASSERT_EQUAL_UINT32(0U, d->stats.nacks_rx);
    TEST_ASSERT_EQUAL_UINT32(0U, retx_rx[0]);
}

static void test_retransmit_capped_per_tick(void)
{
    ts_nack_entry_t e[TS_NACK_MAX];
    ts_client_t *c;
    uint16_t key_seq = 0U;
    bool have_key = false;
    require_deps();
    cfg.kbps_client = 1000000U;
    cfg.kbps_global = 1000000U;
    create();
    join(0U, 0U);
    run_ms(60U);
    c = daemon_client(0U);
    /* the first frame of a new client is a keyframe; the synth gradient needs > 32 fragments */
    TEST_ASSERT_TRUE((first_hdr[0].flags & TS_F_KEYFRAME) != 0U);
    TEST_ASSERT_TRUE(first_hdr[0].frag_count >= TS_NACK_MAX);
    key_seq = first_hdr[0].frame_seq;
    have_key = true;
    TEST_ASSERT_TRUE(have_key);
    for (uint32_t k = 0U; k < TS_NACK_MAX; k++) {
        e[k].frame_seq = key_seq;
        e[k].frag_idx = (uint8_t)k;
    }
    send_nack(0U, e, (uint8_t)TS_NACK_MAX);
    step();
    TEST_ASSERT_EQUAL_UINT32(TS_NACK_MAX, c->ctr.nacks_rx);
    TEST_ASSERT_EQUAL_UINT32(TS_RETX_PER_TICK, c->ctr.retx_sent);
    TEST_ASSERT_EQUAL_UINT32(TS_NACK_MAX - TS_RETX_PER_TICK, c->ctr.retx_dropped);
    TEST_ASSERT_EQUAL_UINT32(TS_RETX_PER_TICK, retx_rx[0]);
}

static void test_retransmit_charged_to_bucket(void)
{
    ts_nack_entry_t e;
    ts_client_t *c;
    require_deps();
    cfg.kbps_client = 8U;   /* 1 byte per ms: the bucket cannot pay for a retransmit right away */
    create();
    join(0U, 0U);
    c = daemon_client(0U);
    run_ms(3U);
    e.frame_seq = first_hdr[0].frame_seq;
    e.frag_idx = 0U;
    TEST_ASSERT_TRUE(data_rx[0] >= 1U);
    ts_bucket_init(&c->bucket, 8U, (uint32_t)TS_PKT_MAX, now);
    c->bucket.tokens = 0U;
    send_nack(0U, &e, 1U);
    step();
    TEST_ASSERT_EQUAL_UINT32(1U, c->ctr.nacks_rx);
    TEST_ASSERT_EQUAL_UINT32(0U, c->ctr.retx_sent);
    TEST_ASSERT_EQUAL_UINT32(1U, c->ctr.retx_dropped);
    TEST_ASSERT_EQUAL_UINT32(0U, retx_rx[0]);
}

static void test_nack_too_old(void)
{
    ts_nack_entry_t e;
    uint16_t first;
    require_deps();
    cfg.kbps_client = 1000000U;
    cfg.kbps_global = 1000000U;
    create();
    join(0U, 0U);
    run_ms(40U);
    first = first_hdr[0].frame_seq;
    /* run until the first frame has left the 448-slot ring */
    for (uint32_t k = 0U; (k < 60U) && (d->txq.pushed < (2U * TS_RING_SLOTS)); k++) {
        keepalive(0U);
        run_ms(500U);
    }
    TEST_ASSERT_TRUE(d->txq.pushed >= (2U * TS_RING_SLOTS));
    e.frame_seq = first;
    e.frag_idx = 0U;
    send_nack(0U, &e, 1U);
    step();
    TEST_ASSERT_EQUAL_UINT32(1U, d->stats.nacks_rx);
    TEST_ASSERT_EQUAL_UINT32(1U, d->stats.nack_too_old);
    TEST_ASSERT_EQUAL_UINT32(0U, retx_rx[0]);
}

static void test_empty_datagram_does_not_stall_udp_drain(void)
{
    ts_nack_entry_t e;
    uint32_t before;
    require_deps();
    cfg.kbps_client = 1000000U;
    cfg.kbps_global = 1000000U;
    create();
    join(0U, 0U);
    run_ms(60U);
    e.frame_seq = first_hdr[0].frame_seq;
    e.frag_idx = 0U;
    before = d->stats.nacks_rx;
    push_udp_from(0U, cl[0].udp_port, buf, 0U);   /* a zero-length datagram first ... */
    for (uint32_t k = 0U; k < 10U; k++) {          /* ... then 10 valid NACKs */
        send_nack(0U, &e, 1U);
    }
    TEST_ASSERT_EQUAL_UINT16(11U, ts_io_fake_in_count(&io_d));
    step();
    /* the empty datagram must not end the drain: all ten NACKs are served in this tick */
    TEST_ASSERT_EQUAL_UINT16(0U, ts_io_fake_in_count(&io_d));
    TEST_ASSERT_EQUAL_UINT32(before + 10U, d->stats.nacks_rx);
    TEST_ASSERT_EQUAL_UINT32(10U, retx_rx[0]);
}

static void test_stale_backlog_jumps_to_newest_keyframe(void)
{
    ts_client_t *c;
    require_deps();
    cfg.kbps_client = 400U;      /* far below the stream: the backlog grows */
    cfg.loss_adapt = false;      /* no quality adaptation: only the backlog policy acts */
    create();
    join(0U, 0U);
    c = daemon_client(0U);
    TEST_ASSERT_NOT_NULL(c);
    for (uint32_t k = 0U; k < 12U; k++) {
        uint32_t pend;
        keepalive(0U);
        run_ms(500U);
        cli_discard(0U);
        pend = ts_txq_cursor_pending(&d->txq, &c->cursor);
        TEST_ASSERT_TRUE(pend < TS_RING_SLOTS);   /* the ring never overruns the cursor */
    }
    TEST_ASSERT_TRUE_MESSAGE(d->cc.resets >= 1U, "stale backlog never dropped");
    TEST_ASSERT_TRUE(c->cursor.skipped > 0U);
    TEST_ASSERT_TRUE(ts_txq_cursor_pending(&d->txq, &c->cursor) < TS_RING_SLOTS);
    TEST_ASSERT_TRUE(key_rx[0] >= (d->cc.resets + 1U));   /* every jump resumed on a keyframe */
    TEST_ASSERT_EQUAL_UINT32(0U, bad_resume[0]);
}

static void test_low_rate_client_steps_quality_down(void)
{
    require_deps();
    cfg.kbps_client = 1000U;
    create();
    join(0U, 0U);
    for (uint32_t k = 0U; k < 8U; k++) {
        keepalive(0U);
        run_ms(500U);
        cli_discard(0U);
    }
    TEST_ASSERT_TRUE(ts_enc_quality(&d->enc) > 1U);
    TEST_ASSERT_TRUE(d->cc.steps_down >= 1U);
    TEST_ASSERT_TRUE(d->cc.est_kbps > 0U);
}

/* ---- data path ------------------------------------------------------------------- */

static void test_seq_start_wraps_with_serial_order(void)
{
    require_deps();
    cfg.seq_start = 65530U;
    create();
    join(0U, 0U);
    for (uint32_t k = 0U; k < 2U; k++) {
        keepalive(0U);
        run_ms(500U);
    }
    TEST_ASSERT_TRUE(frame_seq_monotonic);
    TEST_ASSERT_TRUE(ts_seq_after(last_hdr[0].frame_seq, 65530U));
    TEST_ASSERT_TRUE(last_hdr[0].frame_seq < 100U);   /* wrapped past 65535 */
    TEST_ASSERT_TRUE(ts_seq_after(last_hdr[0].pkt_seq, 65530U));
    TEST_ASSERT_TRUE(d->stats.frames_sent >= 25U);
}

static void test_two_clients_same_ip_both_served(void)
{
    require_deps();
    create();
    join(0U, 0U);
    /* a second viewer on the same host, other UDP port */
    io_c[1].self.ip = C_IP_BASE;
    cl[1].udp_port = (uint16_t)(C_UDP_BASE + 7U);
    join(1U, 0U);
    for (uint32_t k = 0U; k < 2U; k++) {
        keepalive(0U);
        keepalive(1U);
        run_ms(400U);
    }
    TEST_ASSERT_EQUAL_UINT8(2U, ts_clients_active(&d->clients));
    TEST_ASSERT_TRUE(data_rx[0] > 10U);
    TEST_ASSERT_TRUE(data_rx[1] > 10U);
    TEST_ASSERT_TRUE(key_rx[1] >= 1U);
}

static void test_end_to_end_frames_match_synth(void)
{
    /* the receiver can regenerate every frame from frame_seq alone, across the u16 wrap */
    require_deps();
    cfg.seq_start = 65500U;
    cfg.capture.seed = 5U;
    cfg.loss_adapt = false;
    e2e_scf = ts_cap_scene_cut_frames(cfg.capture.scene_cut_ms, cfg.fps);
    ts_dec_init(&e2e_dec);
    e2e_on = true;
    create();
    join(0U, 0U);
    for (uint32_t k = 0U; k < 5U; k++) {
        keepalive(0U);
        run_ms(500U);
    }
    TEST_ASSERT_EQUAL_UINT32(0U, e2e_bad);
    TEST_ASSERT_TRUE(e2e_ok >= 70U);
    TEST_ASSERT_TRUE(ts_seq_after(last_hdr[0].frame_seq, 65500U) && (last_hdr[0].frame_seq < 65500U));
}

static void test_next_due_and_pacing(void)
{
    uint32_t due;
    require_deps();
    create();
    step();
    due = tsd_next_due_ms(d);
    TEST_ASSERT_TRUE((due - d->last_tick_ms) <= 34U);
    run_ms(1000U);
    TEST_ASSERT_TRUE((d->stats.frames_captured >= 29U) && (d->stats.frames_captured <= 31U));
    TEST_ASSERT_EQUAL_UINT32(d->stats.frames_captured, d->stats.frames_sent);
}

static void test_stop_sends_bye(void)
{
    ts_tlv_t t;
    require_deps();
    create();
    join(0U, 0U);
    tsd_stop(d);
    TEST_ASSERT_TRUE(cli_next(0U, (uint8_t)TS_TLV_BYE, &t));
    cli_pump(0U);
    TEST_ASSERT_TRUE(cl[0].closed);
    TEST_ASSERT_EQUAL(TSD_STOPPED, d->state);
    TEST_ASSERT_EQUAL(TS_E_STATE, tsd_tick(d, now));
    d = NULL;
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_create_seals_pool_and_validates);
    RUN_TEST(test_tick_state_errors);
    RUN_TEST(test_pool_init_done_seals);
    RUN_TEST(test_fake_tcp_split_and_close);
    RUN_TEST(test_hello_welcome);
    RUN_TEST(test_hello_split_byte_by_byte);
    RUN_TEST(test_glued_tlvs_and_set_rate_kbps);
    RUN_TEST(test_unknown_tlv_closes_connection);
    RUN_TEST(test_tlv_too_long_closes_connection);
    RUN_TEST(test_bye_frees_client);
    RUN_TEST(test_fifth_client_refused);
    RUN_TEST(test_client_timeout_and_keepalive);
    RUN_TEST(test_silent_connection_without_hello_is_dropped);
    RUN_TEST(test_stats_every_second);
    RUN_TEST(test_keyframe_req_produces_keyframe);
    RUN_TEST(test_rehello_every_tick_keyframes_rate_limited);
    RUN_TEST(test_rehello_keeps_bucket_tokens);
    RUN_TEST(test_reconnect_churn_keyframes_rate_limited);
    RUN_TEST(test_new_client_starts_on_held_keyframe);
    RUN_TEST(test_keyframe_req_spam_limited_per_client);
    RUN_TEST(test_nack_count_40_rejected);
    RUN_TEST(test_nack_matches_ip_and_port);
    RUN_TEST(test_retransmit_capped_per_tick);
    RUN_TEST(test_retransmit_charged_to_bucket);
    RUN_TEST(test_nack_too_old);
    RUN_TEST(test_empty_datagram_does_not_stall_udp_drain);
    RUN_TEST(test_stale_backlog_jumps_to_newest_keyframe);
    RUN_TEST(test_low_rate_client_steps_quality_down);
    RUN_TEST(test_seq_start_wraps_with_serial_order);
    RUN_TEST(test_two_clients_same_ip_both_served);
    RUN_TEST(test_end_to_end_frames_match_synth);
    RUN_TEST(test_next_due_and_pacing);
    RUN_TEST(test_stop_sends_bye);
    return UNITY_END();
}
