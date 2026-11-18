/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/unit/test_tsrecv.c
 * Unit tests of the receiver core (libtsr, include/ts_receiver.h).
 *
 * No sockets, no sleeps: the receiver talks to a test-local in-process
 * ts_io_t (queues for UDP in/out, byte buffers for the TCP control
 * connection) and time is injected through tsr_tick(now). Packets are built
 * with tsproto (ts_hdr_pack) and frames with the codec (ts_enc_encode), so
 * the tests exercise the real wire format.
 *
 * Covered: control handshake (HELLO/WELCOME, split and glued TLVs, STATS,
 * KEYFRAME_REQ/SET_RATE/BYE, reconnect), reassembly vectors (in order,
 * shuffled, short last fragment, duplicates, late frames, u32 frame_len >
 * 65535, eviction, inconsistent geometry), pkt_seq gap detection across the
 * u16 wrap, NACK delay / rounds / chunking / whole-frame inference, keyframe
 * request after give-up, decoder reference reset, both late-frame policies,
 * freeze detection, ts_ms wrap for latency and jitter, truth / PSNR, JSON,
 * the tsrecv tear detector (row-pair discontinuities, tools/tsrecv/rx_tear.h).
 *
 * Tests that need tsproto or the codec are IGNORED (not failed) while those
 * modules are still scaffold stubs on a branch, so this file can land before
 * them; once they are merged every test runs.
 */
#include <stddef.h>
#include <string.h>
#include "unity.h"
#include "ts_config.h"
#include "ts_proto.h"
#include "ts_codec.h"
#include "ts_io.h"
#include "ts_receiver.h"
#include "ts_vectors_gen.h"
#include "../../tools/tsrecv/lib/tsr_internal.h"
#include "../../tools/tsrecv/rx_args.h"
#include "../../tools/tsrecv/rx_tear.h"

/* ---- test-local fake ts_io_t ------------------------------------------------------- */
#define TIO_Q        320U
#define TIO_TCP_CAP  8192U
#define DAEMON_IP    0x0A000001U
#define DAEMON_UDP   5600U
#define T0           1000U

typedef struct tio_dgram {
    ts_addr_t addr;
    uint16_t  len;
    uint8_t   d[TS_IO_DGRAM_MAX];
} tio_dgram_t;

typedef struct tio {
    ts_io_t     io;
    uint32_t    now;
    tio_dgram_t in[TIO_Q];
    uint32_t    in_head;
    uint32_t    in_count;
    tio_dgram_t out[TIO_Q];
    uint32_t    out_count;
    bool        refuse;            /* tcp_connect fails */
    bool        open;
    bool        peer_closed;
    uint32_t    connects;
    uint32_t    closes;
    uint8_t     rx[TIO_TCP_CAP];   /* daemon -> receiver bytes */
    uint32_t    rx_len;
    uint32_t    rx_pos;
    uint32_t    rx_chunk;          /* max bytes per tcp_recv (0 = all) */
    uint8_t     tx[TIO_TCP_CAP];   /* receiver -> daemon bytes */
    uint32_t    tx_len;
} tio_t;

static tio_t T;

static tio_t *tio_of(ts_io_t *io)
{
    return (tio_t *)(void *)io;
}

static int32_t tio_udp_send(ts_io_t *io, const ts_addr_t *to, const uint8_t *buf, size_t len)
{
    tio_t *t = tio_of(io);
    int32_t rc = TS_IO_ERR;
    if ((t->out_count < TIO_Q) && (len <= TS_IO_DGRAM_MAX)) {
        tio_dgram_t *d = &t->out[t->out_count];
        d->addr = *to;
        d->len = (uint16_t)len;
        memcpy(d->d, buf, len);
        t->out_count++;
        rc = (int32_t)len;
    }
    return rc;
}

static int32_t tio_udp_recv(ts_io_t *io, ts_addr_t *from, uint8_t *buf, size_t cap)
{
    tio_t *t = tio_of(io);
    int32_t rc = 0;
    if (t->in_count > 0U) {
        tio_dgram_t *d = &t->in[t->in_head];
        size_t n = (d->len < cap) ? d->len : cap;
        memcpy(buf, d->d, n);
        *from = d->addr;
        t->in_head = (t->in_head + 1U) % TIO_Q;
        t->in_count--;
        rc = (int32_t)n;
    }
    return rc;
}

static ts_io_handle_t tio_tcp_accept(ts_io_t *io, ts_addr_t *peer)
{
    (void)io;
    (void)peer;
    return TS_IO_INVALID;
}

static ts_io_handle_t tio_tcp_connect(ts_io_t *io, const ts_addr_t *to)
{
    tio_t *t = tio_of(io);
    ts_io_handle_t h = TS_IO_INVALID;
    (void)to;
    t->connects++;
    if (!t->refuse) {
        t->open = true;
        t->peer_closed = false;
        t->rx_len = 0U;
        t->rx_pos = 0U;
        h = 3;
    }
    return h;
}

static int32_t tio_tcp_recv(ts_io_t *io, ts_io_handle_t h, uint8_t *buf, size_t cap)
{
    tio_t *t = tio_of(io);
    int32_t rc = 0;
    (void)h;
    if (!t->open) {
        rc = TS_IO_ERR;
    } else if (t->rx_pos < t->rx_len) {
        uint32_t n = t->rx_len - t->rx_pos;
        if ((t->rx_chunk != 0U) && (n > t->rx_chunk)) {
            n = t->rx_chunk;
        }
        if (n > cap) {
            n = (uint32_t)cap;
        }
        memcpy(buf, &t->rx[t->rx_pos], n);
        t->rx_pos += n;
        rc = (int32_t)n;
    } else if (t->peer_closed) {
        rc = TS_IO_CLOSED;
    } else {
        rc = 0;
    }
    return rc;
}

static int32_t tio_tcp_send(ts_io_t *io, ts_io_handle_t h, const uint8_t *buf, size_t len)
{
    tio_t *t = tio_of(io);
    int32_t rc = TS_IO_ERR;
    (void)h;
    if (t->open && ((t->tx_len + len) <= TIO_TCP_CAP)) {
        memcpy(&t->tx[t->tx_len], buf, len);
        t->tx_len += (uint32_t)len;
        rc = (int32_t)len;
    }
    return rc;
}

static void tio_tcp_close(ts_io_t *io, ts_io_handle_t h)
{
    tio_t *t = tio_of(io);
    (void)h;
    t->open = false;
    t->closes++;
}

static uint32_t tio_now(ts_io_t *io)
{
    return tio_of(io)->now;
}

static const ts_io_vt_t tio_vt = {
    tio_udp_send, tio_udp_recv, tio_tcp_accept, tio_tcp_connect,
    tio_tcp_recv, tio_tcp_send, tio_tcp_close, tio_now
};

static void tio_reset(void)
{
    memset(&T, 0, sizeof(T));
    T.io.vt = &tio_vt;
}

static void tio_push_rx(const uint8_t *b, size_t n)
{
    TEST_ASSERT_TRUE(T.rx_len + n <= TIO_TCP_CAP);
    memcpy(&T.rx[T.rx_len], b, n);
    T.rx_len += (uint32_t)n;
}

static void tio_push_udp(const uint8_t *b, size_t n)
{
    tio_dgram_t *d;
    TEST_ASSERT_TRUE(T.in_count < TIO_Q);
    d = &T.in[(T.in_head + T.in_count) % TIO_Q];
    d->addr.ip = DAEMON_IP;
    d->addr.port = (uint16_t)DAEMON_UDP;
    d->len = (uint16_t)n;
    memcpy(d->d, b, n);
    T.in_count++;
}

/* ---- availability of the modules this test depends on --------------------------------- */
static bool proto_ok(void)
{
    static const uint8_t chk[9] = { '1', '2', '3', '4', '5', '6', '7', '8', '9' };
    uint8_t buf[TS_PKT_MAX];
    uint8_t pl[4] = { 1U, 2U, 3U, 4U };
    size_t n = 0U;
    ts_hdr_t h;
    ts_tlv_acc_t acc;
    memset(&h, 0, sizeof(h));
    h.frag_count = 1U;
    h.frame_len = 4U;
    h.payload_len = 4U;
    ts_tlv_acc_init(&acc);
    return (ts_crc32(chk, 9U) == 0xCBF43926U) && (ts_seq_diff(1U, 0xFFFFU) == 2) &&
           (ts_hdr_pack(&h, pl, buf, sizeof(buf), &n) == TS_OK) &&
           (ts_tlv_pack_keyframe_req(buf, sizeof(buf), &n) == TS_OK) &&
           (ts_tlv_acc_push(&acc, buf, n) == TS_OK);
}

static ts_enc_t E;
static uint8_t SRC[TS_FRAME_BYTES];

static bool codec_ok(void)
{
    static uint8_t tmp[TS_ENC_MAX];
    ts_frame_info_t info;
    ts_enc_t e;
    ts_enc_init(&e);
    (void)tsv_gen_frame("gradient0", SRC);
    return ts_enc_encode(&e, SRC, 1U, tmp, sizeof(tmp), &info) == TS_OK;
}

#define NEED_PROTO() do { if (!proto_ok()) { TEST_IGNORE_MESSAGE("tsproto is a scaffold stub on this branch"); } } while (0)
#define NEED_CODEC() do { NEED_PROTO(); if (!codec_ok()) { TEST_IGNORE_MESSAGE("codec is a scaffold stub on this branch"); } } while (0)

/* ---- receiver under test ------------------------------------------------------------------- */
static tsr_t R;

static void rx_setup(const tsr_cfg_t *cfg)
{
    tsr_cfg_t c;
    tio_reset();
    if (cfg != NULL) {
        c = *cfg;
    } else {
        tsr_cfg_default(&c);
    }
    c.server.ip = DAEMON_IP;
    c.server.port = 5601U;
    TEST_ASSERT_EQUAL_INT(TS_OK, tsr_init(&R, &c, &T.io));
}

static void tick(uint32_t now)
{
    T.now = now;
    TEST_ASSERT_EQUAL_INT(TS_OK, tsr_tick(&R, now));
}

static void run_to(uint32_t from, uint32_t to)
{
    for (uint32_t t = from; t <= to; t++) {
        tick(t);
    }
}

static void push_welcome(uint32_t stream_id)
{
    ts_tlv_welcome_t w;
    uint8_t b[64];
    size_t n = 0U;
    w.w = (uint16_t)TS_WIDTH;
    w.h = (uint16_t)TS_HEIGHT;
    w.mtu = (uint16_t)TS_MTU_PAYLOAD;
    w.stream_id = stream_id;
    w.ring_slots = (uint16_t)TS_RING_SLOTS;
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_tlv_pack_welcome(&w, b, sizeof(b), &n));
    tio_push_rx(b, n);
}

/* Count TLVs of `type` the receiver wrote to TCP (all when type == 0); optional last value. */
static uint32_t tx_count(uint8_t type, ts_tlv_t *last)
{
    static ts_tlv_acc_t acc;
    static ts_tlv_t keep;
    static uint8_t val[TS_TLV_MAX_LEN];
    uint32_t cnt = 0U;
    uint32_t off = 0U;
    ts_tlv_acc_init(&acc);
    while (off < T.tx_len) {
        uint32_t n = T.tx_len - off;
        ts_tlv_t t;
        if (n > 256U) {
            n = 256U;
        }
        TEST_ASSERT_EQUAL_INT(TS_OK, ts_tlv_acc_push(&acc, &T.tx[off], n));
        off += n;
        while (ts_tlv_acc_next(&acc, &t) == TS_OK) {
            if ((type == 0U) || (t.type == type)) {
                cnt++;
                keep = t;
                if (t.len > 0U) {
                    memcpy(val, t.value, t.len);
                }
                keep.value = val;
            }
        }
    }
    if (last != NULL) {
        *last = keep;
    }
    return cnt;
}

/* Connect, check HELLO, deliver WELCOME: receiver STREAMING at T0 + 1. */
static void handshake(void)
{
    ts_tlv_t hello;
    tick(T0);
    TEST_ASSERT_EQUAL_UINT32(1U, T.connects);
    TEST_ASSERT_EQUAL_UINT32(1U, tx_count((uint8_t)TS_TLV_HELLO, &hello));
    TEST_ASSERT_EQUAL_INT(TSR_HELLO_SENT, tsr_state(&R));
    push_welcome(0x1234U);
    tick(T0 + 1U);
    TEST_ASSERT_EQUAL_INT(TSR_STREAMING, tsr_state(&R));
}

/* ---- packets and frames --------------------------------------------------------------------- */
typedef struct encf {
    uint8_t  d[TS_ENC_MAX];
    uint32_t len;
    bool     key;
    uint16_t fs;
} encf_t;

static encf_t F[6];
static uint8_t BLOB[TS_ENC_MAX];

static uint8_t nfrags(uint32_t len)
{
    return (uint8_t)((len + TS_MTU_PAYLOAD - 1U) / TS_MTU_PAYLOAD);
}

static void push_frag(uint16_t fs, uint16_t pkt, uint8_t idx, uint32_t frame_len, uint8_t flags,
                      uint16_t ts_ms, const uint8_t *data)
{
    uint8_t buf[TS_PKT_MAX];
    size_t n = 0U;
    ts_hdr_t h;
    uint32_t off = (uint32_t)idx * TS_MTU_PAYLOAD;
    uint32_t pl = frame_len - off;
    memset(&h, 0, sizeof(h));
    h.flags = flags;
    h.frame_seq = fs;
    h.pkt_seq = pkt;
    h.frag_idx = idx;
    h.frag_count = nfrags(frame_len);
    h.frame_len = frame_len;
    h.payload_len = (uint16_t)((pl > TS_MTU_PAYLOAD) ? TS_MTU_PAYLOAD : pl);
    h.ts_ms = ts_ms;
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_hdr_pack(&h, &data[off], buf, sizeof(buf), &n));
    tio_push_udp(buf, n);
}

/* Source frame k: gradient background with a 16x16 box moving right (small P-frames). */
static void make_src(uint32_t k, uint8_t *out)
{
    (void)tsv_gen_frame("gradient0", out);
    for (uint32_t y = 64U; y < 80U; y++) {
        for (uint32_t x = 0U; x < 16U; x++) {
            out[(y * TS_WIDTH) + (((k * 16U) + x) % TS_WIDTH)] = (uint8_t)(40U * (k + 1U));
        }
    }
}

static void enc_frame(uint32_t k, uint16_t fs, bool force_key, encf_t *f)
{
    ts_frame_info_t info;
    size_t cap = sizeof(f->d);
    make_src(k, SRC);
    if (force_key) {
        ts_enc_force_keyframe(&E);
    }
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_enc_encode(&E, SRC, fs, f->d, cap, &info));
    f->len = info.enc_len;
    f->key = info.keyframe;
    f->fs = fs;
}

static uint8_t fflags(const encf_t *f)
{
    return f->key ? (uint8_t)TS_F_KEYFRAME : 0U;
}

/* Push every fragment of f in order with fresh pkt_seq from `pkt`; returns the next pkt_seq. */
static uint16_t send_all(const encf_t *f, uint16_t pkt, uint16_t ts_ms)
{
    uint8_t n = nfrags(f->len);
    for (uint8_t i = 0U; i < n; i++) {
        push_frag(f->fs, (uint16_t)(pkt + i), i, f->len, fflags(f), ts_ms, f->d);
    }
    return (uint16_t)(pkt + n);
}

/* NACK entries the receiver sent since the last call (all packets parsed). */
static uint32_t take_nacks(ts_nack_entry_t *out, uint32_t cap, uint32_t *pkts)
{
    uint32_t cnt = 0U;
    for (uint32_t i = 0U; i < T.out_count; i++) {
        ts_nack_t n;
        TEST_ASSERT_EQUAL_UINT32(DAEMON_IP, T.out[i].addr.ip);
        TEST_ASSERT_EQUAL_UINT16(DAEMON_UDP, T.out[i].addr.port);
        TEST_ASSERT_EQUAL_INT(TS_OK, ts_nack_parse(T.out[i].d, T.out[i].len, &n));
        for (uint32_t j = 0U; j < n.count; j++) {
            if (cnt < cap) {
                out[cnt] = n.e[j];
            }
            cnt++;
        }
    }
    if (pkts != NULL) {
        *pkts = T.out_count;
    }
    T.out_count = 0U;
    return cnt;
}

/* optional loss phase for the next model-sender run (reset before every test) */
static uint32_t ms_phase_from;
static uint32_t ms_phase_to;
static uint32_t ms_phase_loss;

void setUp(void)
{
    ms_phase_from = 0U;
    ms_phase_to = 0U;
    ms_phase_loss = 0U;
    ts_enc_init(&E);
    memset(F, 0, sizeof(F));
    for (uint32_t i = 0U; i < TS_ENC_MAX; i++) {
        BLOB[i] = (uint8_t)((i * 7U) + 3U);
    }
}

void tearDown(void)
{
}

/* ======================================================================== control */
static void test_handshake_hello_welcome(void)
{
    ts_tlv_t hello;
    ts_tlv_hello_t hv;
    tsr_cfg_t c;
    NEED_PROTO();
    tsr_cfg_default(&c);
    c.udp_port = 6000U;
    c.kbps = 2500U;
    rx_setup(&c);
    TEST_ASSERT_EQUAL_INT(TSR_DISCONNECTED, tsr_state(&R));
    TEST_ASSERT_NULL(tsr_welcome(&R));
    TEST_ASSERT_NULL(tsr_frame(&R));
    tick(T0);
    TEST_ASSERT_EQUAL_UINT32(1U, tx_count((uint8_t)TS_TLV_HELLO, &hello));
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_tlv_parse_hello(&hello, &hv));
    TEST_ASSERT_EQUAL_UINT16(6000U, hv.udp_port);
    TEST_ASSERT_EQUAL_UINT32(2500U, hv.kbps);
    TEST_ASSERT_EQUAL_INT(TSR_HELLO_SENT, tsr_state(&R));
    push_welcome(0xCAFEU);
    tick(T0 + 1U);
    TEST_ASSERT_EQUAL_INT(TSR_STREAMING, tsr_state(&R));
    TEST_ASSERT_NOT_NULL(tsr_welcome(&R));
    TEST_ASSERT_EQUAL_UINT32(0xCAFEU, tsr_welcome(&R)->stream_id);
    TEST_ASSERT_EQUAL_UINT32(0xCAFEU, tsr_metrics(&R)->stream_id);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->ctrl_errors);
}

static void test_ctrl_split_and_glued_tlvs(void)
{
    ts_tlv_stats_t st;
    uint8_t b[512];
    size_t n = 0U;
    NEED_PROTO();
    rx_setup(NULL);
    tick(T0);
    T.rx_chunk = 1U;                 /* every recv() returns one byte */
    push_welcome(7U);
    memset(&st, 0, sizeof(st));
    st.g.uptime_ms = 4242U;
    st.g.client_count = 1U;
    st.c[0].udp_port = 5610U;
    st.c[0].kbps = 777U;
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_tlv_pack_stats(&st, b, sizeof(b), &n));
    tio_push_rx(b, n);               /* glued behind WELCOME */
    tio_push_rx(b, n);
    for (uint32_t t = 1U; t < 40U; t++) {
        tick(T0 + t);
    }
    TEST_ASSERT_EQUAL_INT(TSR_STREAMING, tsr_state(&R));
    TEST_ASSERT_EQUAL_UINT32(2U, tsr_metrics(&R)->stats_rx);
    TEST_ASSERT_EQUAL_UINT32(4242U, tsr_metrics(&R)->last_stats.g.uptime_ms);
    TEST_ASSERT_EQUAL_UINT32(777U, tsr_metrics(&R)->last_stats.c[0].kbps);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->ctrl_errors);
}

static void test_ctrl_send_messages_and_bye(void)
{
    ts_tlv_t t;
    ts_tlv_set_rate_t sr;
    NEED_PROTO();
    rx_setup(NULL);
    TEST_ASSERT_EQUAL_INT(TS_E_STATE, tsr_send_keyframe_req(&R));
    TEST_ASSERT_EQUAL_INT(TS_E_STATE, tsr_send_set_rate(&R, 100U));
    TEST_ASSERT_EQUAL_INT(TS_E_STATE, tsr_send_bye(&R));
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, tsr_send_bye(NULL));
    handshake();
    T.tx_len = 0U;
    TEST_ASSERT_EQUAL_INT(TS_OK, tsr_send_keyframe_req(&R));
    TEST_ASSERT_EQUAL_INT(TS_OK, tsr_send_set_rate(&R, 1500U));
    tick(T0 + 2U);
    TEST_ASSERT_EQUAL_UINT32(1U, tx_count((uint8_t)TS_TLV_KEYFRAME_REQ, NULL));
    TEST_ASSERT_EQUAL_UINT32(1U, tx_count((uint8_t)TS_TLV_SET_RATE, &t));
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_tlv_parse_set_rate(&t, &sr));
    TEST_ASSERT_EQUAL_UINT32(1500U, sr.kbps);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->keyframe_reqs);
    TEST_ASSERT_EQUAL_INT(TS_OK, tsr_send_bye(&R));
    tick(T0 + 3U);
    TEST_ASSERT_EQUAL_UINT32(1U, tx_count((uint8_t)TS_TLV_BYE, NULL));
    TEST_ASSERT_EQUAL_INT(TSR_CLOSED, tsr_state(&R));
    TEST_ASSERT_FALSE(T.open);
    tick(T0 + 5000U);                /* closed: no reconnect */
    TEST_ASSERT_EQUAL_UINT32(1U, T.connects);
}

static void test_ctrl_reconnect_after_close(void)
{
    tsr_cfg_t c;
    NEED_PROTO();
    tsr_cfg_default(&c);
    c.reconnect_ms = 500U;
    rx_setup(&c);
    handshake();
    T.peer_closed = true;
    tick(T0 + 10U);
    TEST_ASSERT_EQUAL_INT(TSR_DISCONNECTED, tsr_state(&R));
    TEST_ASSERT_EQUAL_INT(TS_E_STATE, tsr_send_keyframe_req(&R));
    tick(T0 + 400U);
    TEST_ASSERT_EQUAL_UINT32(1U, T.connects);
    T.tx_len = 0U;
    tick(T0 + 510U);
    TEST_ASSERT_EQUAL_UINT32(2U, T.connects);
    TEST_ASSERT_EQUAL_UINT32(1U, tx_count((uint8_t)TS_TLV_HELLO, NULL));
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->reconnects);
    push_welcome(0x1234U);
    tick(T0 + 511U);
    TEST_ASSERT_EQUAL_INT(TSR_STREAMING, tsr_state(&R));
    /* refused connection: stays disconnected and retries after the back-off */
    T.peer_closed = true;
    T.refuse = true;
    tick(T0 + 600U);
    tick(T0 + 1200U);
    TEST_ASSERT_EQUAL_INT(TSR_DISCONNECTED, tsr_state(&R));
    TEST_ASSERT_EQUAL_UINT32(3U, T.connects);
}

static void test_ctrl_silence_watchdog_rehello(void)
{
    ts_tlv_stats_t st;
    uint8_t b[512];
    size_t n = 0U;
    NEED_PROTO();
    rx_setup(NULL);
    handshake();                         /* WELCOME seen at T0 + 1 */
    memset(&st, 0, sizeof(st));
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_tlv_pack_stats(&st, b, sizeof(b), &n));
    tio_push_rx(b, n);                   /* STATS keep the client alive without data */
    tick(T0 + 2000U);
    tick(T0 + 2000U + TSR_SILENCE_MS - 1U);
    TEST_ASSERT_EQUAL_INT(TSR_STREAMING, tsr_state(&R));
    tick(T0 + 2000U + TSR_SILENCE_MS);
    TEST_ASSERT_EQUAL_INT(TSR_DISCONNECTED, tsr_state(&R));
    T.tx_len = 0U;
    tick(T0 + 2001U + TSR_SILENCE_MS);   /* no back-off after the watchdog */
    TEST_ASSERT_EQUAL_UINT32(2U, T.connects);
    TEST_ASSERT_EQUAL_UINT32(1U, tx_count((uint8_t)TS_TLV_HELLO, NULL));
}

static void test_ctrl_unknown_tlv_reconnects(void)
{
    uint8_t b[8] = { 0x7EU, 0U, 0U, 0U };   /* CTRL_SHELL-like unknown type, len 0 */
    NEED_PROTO();
    rx_setup(NULL);
    handshake();
    tio_push_rx(b, 4U);
    tick(T0 + 2U);
    TEST_ASSERT_EQUAL_INT(TSR_DISCONNECTED, tsr_state(&R));
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->ctrl_errors);
}

/* ======================================================================== reassembly */
static void test_reasm_in_order_keyframe_decodes(void)
{
    NEED_CODEC();
    rx_setup(NULL);
    handshake();
    enc_frame(0U, 10U, true, &F[0]);
    TEST_ASSERT_TRUE(F[0].key);
    TEST_ASSERT_TRUE(nfrags(F[0].len) > 1U);
    (void)send_all(&F[0], 100U, 500U);
    tick(T0 + 5U);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->frames_complete);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->frames_decoded);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->keyframes);
    TEST_ASSERT_EQUAL_UINT32((uint32_t)nfrags(F[0].len), tsr_metrics(&R)->pkts_rx);
    TEST_ASSERT_NOT_NULL(tsr_frame(&R));
    make_src(0U, SRC);
    TEST_ASSERT_EQUAL_MEMORY(SRC, tsr_frame(&R), TS_FRAME_BYTES);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->pkts_lost);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->keyframe_reqs);
}

static void test_reasm_shuffled_u32_frame_len(void)
{
    static uint8_t noise[TS_FRAME_BYTES];
    ts_frame_info_t info;
    uint8_t order[TS_MAX_FRAGS];
    uint8_t n;
    uint32_t s = 12345U;
    NEED_CODEC();
    rx_setup(NULL);
    handshake();
    TEST_ASSERT_TRUE(tsv_gen_frame("noise7", noise));
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_enc_encode(&E, noise, 0xFFF0U, F[0].d, sizeof(F[0].d), &info));
    F[0].len = info.enc_len;
    TEST_ASSERT_TRUE(F[0].len > 65535U);          /* needs the u32 frame_len */
    n = nfrags(F[0].len);
    TEST_ASSERT_TRUE(n > 47U);
    for (uint8_t i = 0U; i < n; i++) {
        order[i] = i;
    }
    for (uint8_t i = (uint8_t)(n - 1U); i > 0U; i--) {   /* Fisher-Yates, xorshift32 */
        uint8_t j;
        uint8_t tmp;
        s ^= s << 13;
        s ^= s >> 17;
        s ^= s << 5;
        j = (uint8_t)(s % ((uint32_t)i + 1U));
        tmp = order[i];
        order[i] = order[j];
        order[j] = tmp;
    }
    for (uint8_t i = 0U; i < n; i++) {
        push_frag(0xFFF0U, (uint16_t)(65500U + order[i]), order[i], F[0].len, (uint8_t)TS_F_KEYFRAME, 1U, F[0].d);
    }
    tick(T0 + 5U);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->frames_decoded);
    TEST_ASSERT_EQUAL_MEMORY(noise, tsr_frame(&R), TS_FRAME_BYTES);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->dups);
    /* reordering inside one tick is not loss: every gap was filled immediately */
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->pkts_lost);
    run_to(T0 + 6U, T0 + 300U);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->nacks_sent);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->pkts_lost);
}

static void test_reasm_short_last_fragment_first(void)
{
    uint8_t n;
    NEED_CODEC();
    rx_setup(NULL);
    handshake();
    enc_frame(0U, 20U, true, &F[0]);
    n = nfrags(F[0].len);
    TEST_ASSERT_NOT_EQUAL(0U, F[0].len % TS_MTU_PAYLOAD);   /* last fragment is short */
    push_frag(20U, (uint16_t)(300U + n - 1U), (uint8_t)(n - 1U), F[0].len, (uint8_t)TS_F_KEYFRAME, 0U, F[0].d);
    tick(T0 + 2U);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->frames_complete);
    for (uint8_t i = 0U; i < (uint8_t)(n - 1U); i++) {
        push_frag(20U, (uint16_t)(300U + i), i, F[0].len, (uint8_t)TS_F_KEYFRAME, 0U, F[0].d);
    }
    tick(T0 + 3U);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->frames_decoded);
    make_src(0U, SRC);
    TEST_ASSERT_EQUAL_MEMORY(SRC, tsr_frame(&R), TS_FRAME_BYTES);
}

static void test_reasm_duplicate_dropped(void)
{
    NEED_CODEC();
    rx_setup(NULL);
    handshake();
    enc_frame(0U, 1U, true, &F[0]);
    push_frag(1U, 10U, 0U, F[0].len, (uint8_t)TS_F_KEYFRAME, 0U, F[0].d);
    push_frag(1U, 10U, 0U, F[0].len, (uint8_t)TS_F_KEYFRAME, 0U, F[0].d);  /* netem dup */
    tick(T0 + 2U);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->dups);
    (void)send_all(&F[0], 10U, 0U);    /* fragment 0 a third time, then the rest */
    tick(T0 + 3U);
    TEST_ASSERT_EQUAL_UINT32(2U, tsr_metrics(&R)->dups);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->frames_decoded);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->frames_complete);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->pkts_missing);
}

static void test_reasm_late_frame_dropped(void)
{
    uint16_t pkt = 1000U;
    uint16_t p1;
    NEED_CODEC();
    rx_setup(NULL);
    handshake();
    enc_frame(0U, 50U, true, &F[0]);
    enc_frame(1U, 51U, false, &F[1]);
    enc_frame(2U, 52U, false, &F[2]);
    TEST_ASSERT_FALSE(F[1].key);
    pkt = send_all(&F[0], pkt, 0U);
    p1 = pkt;
    pkt = send_all(&F[1], pkt, 33U);
    (void)send_all(&F[2], pkt, 66U);
    tick(T0 + 5U);
    TEST_ASSERT_EQUAL_UINT32(3U, tsr_metrics(&R)->frames_decoded);
    /* frame 51 again (a late retransmit): already played -> dropped, not re-decoded */
    for (uint8_t i = 0U; i < nfrags(F[1].len); i++) {
        push_frag(51U, (uint16_t)(p1 + i), i, F[1].len, (uint8_t)TS_F_RETRANSMIT, 33U, F[1].d);
    }
    tick(T0 + 6U);
    TEST_ASSERT_EQUAL_UINT32(3U, tsr_metrics(&R)->frames_decoded);
    TEST_ASSERT_EQUAL_UINT32((uint32_t)nfrags(F[1].len), tsr_metrics(&R)->late_pkts);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->frames_dropped);
    make_src(2U, SRC);
    TEST_ASSERT_EQUAL_MEMORY(SRC, tsr_frame(&R), TS_FRAME_BYTES);
}

static void test_reasm_window_eviction(void)
{
    const uint32_t len = 3U * TS_MTU_PAYLOAD;
    NEED_PROTO();
    rx_setup(NULL);
    handshake();
    /* TS_REASM_FRAMES + 1 incomplete frames (fragment 0 of 3 each) */
    for (uint16_t f = 0U; f <= (uint16_t)TS_REASM_FRAMES; f++) {
        push_frag((uint16_t)(200U + f), (uint16_t)(f * 3U), 0U, len, (uint8_t)TS_F_KEYFRAME, 0U, BLOB);
    }
    tick(T0 + 2U);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->frames_dropped);
    TEST_ASSERT_EQUAL_INT(-1, tsr_i_find_slot(&R, 200U));
    TEST_ASSERT_TRUE(tsr_i_find_slot(&R, (uint16_t)(200U + TS_REASM_FRAMES)) >= 0);
    /* a fragment of the evicted frame is now late */
    push_frag(200U, 1U, 1U, len, (uint8_t)TS_F_KEYFRAME, 0U, BLOB);
    tick(T0 + 3U);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->late_pkts);
}

static void test_reasm_inconsistent_geometry_rejected(void)
{
    NEED_PROTO();
    rx_setup(NULL);
    handshake();
    push_frag(5U, 1U, 0U, 3U * TS_MTU_PAYLOAD, 0U, 0U, BLOB);
    push_frag(5U, 2U, 1U, 2U * TS_MTU_PAYLOAD, 0U, 0U, BLOB);   /* same frame, other size */
    tick(T0 + 2U);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->parse_fail);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->frames_complete);
}

static void test_bad_crc_and_garbage_counted(void)
{
    uint8_t buf[TS_PKT_MAX];
    size_t n = 0U;
    ts_hdr_t h;
    NEED_PROTO();
    rx_setup(NULL);
    handshake();
    memset(&h, 0, sizeof(h));
    h.frame_seq = 1U;
    h.frag_count = 1U;
    h.frame_len = 10U;
    h.payload_len = 10U;
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_hdr_pack(&h, BLOB, buf, sizeof(buf), &n));
    buf[30] ^= 0x10U;
    tio_push_udp(buf, n);
    tio_push_udp(buf, 7U);
    tick(T0 + 2U);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->crc_fail);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->parse_fail);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->pkts_rx);
}

/* Receiver over the shared fake io: [empty, 10 junk] is drained in one tick. */
static void test_empty_datagram_does_not_stall_rx_drain(void)
{
    static ts_io_fake_t fio;
    static tsr_t R2;
    tsr_cfg_t c;
    ts_io_dgram_t d;
    ts_addr_t self = { 0x0A000021U, 6000U };
    tsr_cfg_default(&c);
    c.server.ip = DAEMON_IP;
    c.server.port = 5601U;
    ts_io_fake_init(&fio, self);
    TEST_ASSERT_EQUAL_INT(TS_OK, tsr_init(&R2, &c, &fio.io));
    (void)memset(&d, 0, sizeof(d));
    d.from.ip = DAEMON_IP;
    d.from.port = (uint16_t)DAEMON_UDP;
    d.to = self;
    d.len = 0U;
    TEST_ASSERT_EQUAL(TS_OK, ts_io_fake_push_udp(&fio, &d));
    d.len = 7U;   /* too short for a DATA header: counted as a parse failure */
    for (uint32_t k = 0U; k < 10U; k++) {
        TEST_ASSERT_EQUAL(TS_OK, ts_io_fake_push_udp(&fio, &d));
    }
    ts_io_fake_set_time(&fio, T0);
    TEST_ASSERT_EQUAL_INT(TS_OK, tsr_tick(&R2, T0));
    TEST_ASSERT_EQUAL_UINT16(0U, ts_io_fake_in_count(&fio));
    TEST_ASSERT_EQUAL_UINT32(10U, tsr_metrics(&R2)->parse_fail);
}

/* ---- tsrecv command line: strict numbers (tools/tsrecv/rx_args.h) ------------------- */
static void test_args_strict_u32_and_seconds_limit(void)
{
    uint32_t v = 7U;
    TEST_ASSERT_TRUE(rx_parse_u32("0", 0U, UINT32_MAX, &v));
    TEST_ASSERT_EQUAL_UINT32(0U, v);
    TEST_ASSERT_TRUE(rx_parse_u32("4294967295", 0U, UINT32_MAX, &v));
    TEST_ASSERT_EQUAL_UINT32(UINT32_MAX, v);
    /* what strtoul() let through */
    TEST_ASSERT_FALSE(rx_parse_u32(" 5", 0U, UINT32_MAX, &v));
    TEST_ASSERT_FALSE(rx_parse_u32("+5", 0U, UINT32_MAX, &v));
    TEST_ASSERT_FALSE(rx_parse_u32("-0", 0U, UINT32_MAX, &v));
    TEST_ASSERT_FALSE(rx_parse_u32("5 ", 0U, UINT32_MAX, &v));
    TEST_ASSERT_FALSE(rx_parse_u32("4294967296", 0U, UINT32_MAX, &v));
    TEST_ASSERT_FALSE(rx_parse_u32("00000000001", 0U, UINT32_MAX, &v));
    TEST_ASSERT_FALSE(rx_parse_u32("", 0U, UINT32_MAX, &v));
    TEST_ASSERT_FALSE(rx_parse_u32(NULL, 0U, UINT32_MAX, &v));
    TEST_ASSERT_FALSE(rx_parse_u32("12", 13U, 20U, &v));
    TEST_ASSERT_EQUAL_UINT32(UINT32_MAX, v);   /* untouched on failure */
    /* --seconds: 4294968 * 1000 wrapped to 704 ms; it is now out of range */
    TEST_ASSERT_TRUE(rx_parse_u32("4294967", 0U, RX_SECONDS_MAX, &v));
    TEST_ASSERT_FALSE(rx_parse_u32("4294968", 0U, RX_SECONDS_MAX, &v));
    TEST_ASSERT_FALSE(rx_seconds_done(704U, 4294968U));         /* no wrap even if it got through */
    TEST_ASSERT_FALSE(rx_seconds_done(UINT32_MAX, 4294968U));
    TEST_ASSERT_TRUE(rx_seconds_done(5000U, 5U));
    TEST_ASSERT_FALSE(rx_seconds_done(4999U, 5U));
    TEST_ASSERT_FALSE(rx_seconds_done(UINT32_MAX, 0U));         /* 0 = unlimited */
}

/* ---- tsrecv tear detector (tools/tsrecv/rx_tear.h) ------------------------------------ */
static uint8_t g_tear_a[TS_FRAME_BYTES];
static uint8_t g_tear_b[TS_FRAME_BYTES];

/* vertical bars: every row equal, no row-pair step at all */
static void tear_bars(uint8_t *f, uint32_t offset)
{
    for (uint32_t y = 0U; y < TS_HEIGHT; y++) {
        for (uint32_t x = 0U; x < TS_WIDTH; x++) {
            f[(y * TS_WIDTH) + x] = (uint8_t)(((x * 7U) + offset) & 0xFFU);
        }
    }
}

static void test_tear_detector_split_frame(void)
{
    rx_tear_t t;
    rx_tear_init(&t);
    tear_bars(g_tear_a, 0U);
    TEST_ASSERT_EQUAL_UINT32(0U, rx_tear_check(&t, g_tear_a));      /* first frame: nothing to compare */
    TEST_ASSERT_EQUAL_UINT32(0U, rx_tear_check(&t, g_tear_a));
    /* rows 0..99 from one picture, rows 100.. from another: one new line below row 99 */
    tear_bars(g_tear_b, 128U);
    (void)memcpy(g_tear_b, g_tear_a, 100U * TS_WIDTH);
    TEST_ASSERT_EQUAL_UINT32(1U, rx_tear_check(&t, g_tear_b));
    TEST_ASSERT_EQUAL_UINT32(1U, t.tear_frames);
    TEST_ASSERT_EQUAL_UINT32(1U, t.tear_rows_total);
    TEST_ASSERT_EQUAL_UINT32(100U, t.last_row);
    /* the same line in the next frame is no longer new (a static edge is not a tear) */
    TEST_ASSERT_EQUAL_UINT32(0U, rx_tear_check(&t, g_tear_b));
    TEST_ASSERT_EQUAL_UINT32(1U, t.tear_frames);
    TEST_ASSERT_EQUAL_UINT32(4U, t.frames_checked);
    TEST_ASSERT_EQUAL_UINT32(0U, t.last_rows);
}

static void test_tear_detector_picture_change_and_edges(void)
{
    rx_tear_t t;
    rx_tear_init(&t);
    tear_bars(g_tear_a, 3U);
    (void)rx_tear_check(&t, g_tear_a);
    /* a new picture: 1-row stripes, every row pair steps by 255 -> not a tear */
    for (uint32_t y = 0U; y < TS_HEIGHT; y++) {
        (void)memset(&g_tear_b[y * TS_WIDTH], ((y & 1U) != 0U) ? 255 : 0, TS_WIDTH);
    }
    TEST_ASSERT_EQUAL_UINT32(RX_TEAR_PAIRS, rx_tear_check(&t, g_tear_b));
    TEST_ASSERT_EQUAL_UINT32(0U, t.tear_frames);
    TEST_ASSERT_EQUAL_UINT32(1U, t.picture_changes);
    /* the striped picture again: its edges are natural, nothing new */
    TEST_ASSERT_EQUAL_UINT32(0U, rx_tear_check(&t, g_tear_b));
    /* a faint line (mean step 10 levels) is below the visibility threshold */
    tear_bars(g_tear_a, 0U);
    (void)rx_tear_check(&t, g_tear_a);
    tear_bars(g_tear_b, 10U);
    (void)memcpy(g_tear_b, g_tear_a, 50U * TS_WIDTH);
    TEST_ASSERT_EQUAL_UINT32(0U, rx_tear_check(&t, g_tear_b));
    TEST_ASSERT_EQUAL_UINT32(0U, t.tear_frames);
}

static void test_tear_detector_rules(void)
{
    const uint32_t min_sum = RX_TEAR_MIN_STEP * TS_WIDTH;
    rx_tear_t t;
    TEST_ASSERT_TRUE(rx_tear_is_new_line(min_sum, 0U));
    TEST_ASSERT_FALSE(rx_tear_is_new_line(min_sum - 1U, 0U));
    TEST_ASSERT_FALSE(rx_tear_is_new_line(min_sum + 10U, 11U));          /* not min_sum above the old step */
    TEST_ASSERT_TRUE(rx_tear_is_new_line(3U * min_sum, min_sum));
    TEST_ASSERT_FALSE(rx_tear_is_new_line(5U * min_sum, 2U * min_sum));   /* less than 3x the old step */
    /* NULL is ignored */
    rx_tear_init(NULL);
    rx_tear_init(&t);
    TEST_ASSERT_EQUAL_UINT32(0U, rx_tear_check(NULL, g_tear_a));
    TEST_ASSERT_EQUAL_UINT32(0U, rx_tear_check(&t, NULL));
    TEST_ASSERT_EQUAL_UINT32(0U, t.frames_checked);
    /* row step: |a - b| summed over the width */
    tear_bars(g_tear_a, 0U);
    (void)memset(&g_tear_a[5U * TS_WIDTH], 200, TS_WIDTH);
    (void)memset(&g_tear_a[6U * TS_WIDTH], 50, TS_WIDTH);
    TEST_ASSERT_EQUAL_UINT32(150U * TS_WIDTH, rx_tear_row_step(g_tear_a, 5U));
    TEST_ASSERT_EQUAL_UINT32(0U, rx_tear_row_step(g_tear_a, 10U));
}

/* ======================================================================== loss / NACK */
static void test_nack_after_delay_not_before(void)
{
    ts_nack_entry_t e[4];
    const uint32_t len = 3U * TS_MTU_PAYLOAD;
    NEED_PROTO();
    rx_setup(NULL);
    handshake();
    push_frag(7U, 100U, 0U, len, 0U, 0U, BLOB);
    push_frag(7U, 102U, 2U, len, 0U, 0U, BLOB);   /* pkt 101 missing */
    tick(T0 + 10U);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->pkts_missing);
    run_to(T0 + 11U, T0 + 10U + TS_NACK_DELAY_MS - 1U);
    TEST_ASSERT_EQUAL_UINT32(0U, take_nacks(e, 4U, NULL));
    tick(T0 + 10U + TS_NACK_DELAY_MS);
    TEST_ASSERT_EQUAL_UINT32(1U, take_nacks(e, 4U, NULL));
    TEST_ASSERT_EQUAL_UINT16(7U, e[0].frame_seq);
    TEST_ASSERT_EQUAL_UINT8(1U, e[0].frag_idx);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->nacks_sent);
}

static void test_nack_reorder_within_delay_no_nack(void)
{
    ts_nack_entry_t e[4];
    const uint32_t len = 3U * TS_MTU_PAYLOAD;
    NEED_PROTO();
    rx_setup(NULL);
    handshake();
    push_frag(7U, 100U, 0U, len, 0U, 0U, BLOB);
    push_frag(7U, 102U, 2U, len, 0U, 0U, BLOB);
    tick(T0 + 10U);
    push_frag(7U, 101U, 1U, len, 0U, 0U, BLOB);   /* reordered, 10 ms late */
    run_to(T0 + 20U, T0 + 300U);
    TEST_ASSERT_EQUAL_UINT32(0U, take_nacks(e, 4U, NULL));
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->pkts_lost);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->pkts_missing);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->frames_complete);
}

static void test_nack_two_rounds_then_keyframe_req(void)
{
    ts_nack_entry_t e[4];
    const uint32_t len = 3U * TS_MTU_PAYLOAD;
    uint32_t total = 0U;
    uint32_t first = 0U;
    uint32_t second = 0U;
    uint32_t kreq_at = 0U;
    NEED_PROTO();
    rx_setup(NULL);
    handshake();
    T.tx_len = 0U;
    push_frag(9U, 400U, 0U, len, 0U, 0U, BLOB);
    push_frag(9U, 402U, 2U, len, 0U, 0U, BLOB);
    for (uint32_t t = T0 + 2U; t < T0 + 600U; t++) {
        uint32_t n;
        tick(t);
        n = take_nacks(e, 4U, NULL);
        if (n > 0U) {
            TEST_ASSERT_EQUAL_UINT32(1U, n);
            TEST_ASSERT_EQUAL_UINT16(9U, e[0].frame_seq);
            TEST_ASSERT_EQUAL_UINT8(1U, e[0].frag_idx);
            if (total == 0U) {
                first = t;
            } else {
                second = t;
            }
            total += n;
        }
        if ((kreq_at == 0U) && (tx_count((uint8_t)TS_TLV_KEYFRAME_REQ, NULL) > 0U)) {
            kreq_at = t;
        }
    }
    TEST_ASSERT_EQUAL_UINT32(2U, total);                       /* max 2 rounds */
    TEST_ASSERT_EQUAL_UINT32(T0 + 2U + TS_NACK_DELAY_MS, first);
    TEST_ASSERT_TRUE(second > first);
    TEST_ASSERT_EQUAL_UINT32(tsr_i_retry_ival(&R), second - first);   /* ~1 RTT estimate */
    TEST_ASSERT_TRUE(kreq_at > second);
    TEST_ASSERT_TRUE((kreq_at - first) <= TS_NACK_RETRY_MS);   /* give-up within the retry budget */
    TEST_ASSERT_EQUAL_UINT32(1U, tx_count((uint8_t)TS_TLV_KEYFRAME_REQ, NULL));
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->nack_giveups);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->pkts_lost);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->keyframe_reqs);
}

static void test_nack_chunked_to_32(void)
{
    static ts_nack_entry_t e[128];
    const uint32_t len = 100U * TS_MTU_PAYLOAD;
    uint32_t pkts = 0U;
    uint32_t n;
    bool seen[100];
    tsr_cfg_t c;
    NEED_PROTO();
    tsr_cfg_default(&c);
    c.max_nack_per_tick = 255U;
    rx_setup(&c);
    handshake();
    push_frag(3U, 1000U, 0U, len, (uint8_t)TS_F_KEYFRAME, 0U, BLOB);
    push_frag(3U, 1099U, 99U, len, (uint8_t)TS_F_KEYFRAME, 0U, BLOB);
    tick(T0 + 2U);
    TEST_ASSERT_EQUAL_UINT32(98U, tsr_metrics(&R)->pkts_missing);
    tick(T0 + 2U + TS_NACK_DELAY_MS);
    n = take_nacks(e, 128U, &pkts);
    TEST_ASSERT_EQUAL_UINT32(98U, n);
    TEST_ASSERT_EQUAL_UINT32(4U, pkts);                  /* 32 + 32 + 32 + 2 */
    memset(seen, 0, sizeof(seen));
    for (uint32_t i = 0U; i < n; i++) {
        TEST_ASSERT_EQUAL_UINT16(3U, e[i].frame_seq);
        TEST_ASSERT_TRUE((e[i].frag_idx >= 1U) && (e[i].frag_idx <= 98U));
        TEST_ASSERT_FALSE(seen[e[i].frag_idx]);
        seen[e[i].frag_idx] = true;
    }
    TEST_ASSERT_EQUAL_UINT32(98U, tsr_metrics(&R)->nacks_sent);
}

static void test_nack_budget_per_tick_default(void)
{
    static ts_nack_entry_t e[128];
    const uint32_t len = 100U * TS_MTU_PAYLOAD;
    uint32_t pkts = 0U;
    NEED_PROTO();
    rx_setup(NULL);                                     /* max_nack_per_tick = 32 */
    handshake();
    push_frag(3U, 1000U, 0U, len, 0U, 0U, BLOB);
    push_frag(3U, 1099U, 99U, len, 0U, 0U, BLOB);
    tick(T0 + 2U);
    tick(T0 + 2U + TS_NACK_DELAY_MS);
    TEST_ASSERT_EQUAL_UINT32(32U, take_nacks(e, 128U, &pkts));
    TEST_ASSERT_EQUAL_UINT32(1U, pkts);
    tick(T0 + 3U + TS_NACK_DELAY_MS);
    TEST_ASSERT_EQUAL_UINT32(32U, take_nacks(e, 128U, &pkts));
    tick(T0 + 4U + TS_NACK_DELAY_MS);
    TEST_ASSERT_EQUAL_UINT32(32U, take_nacks(e, 128U, &pkts));
    tick(T0 + 5U + TS_NACK_DELAY_MS);
    TEST_ASSERT_EQUAL_UINT32(2U, take_nacks(e, 128U, &pkts));
}

static void test_nack_whole_frame_inferred(void)
{
    ts_nack_entry_t e[8];
    const uint32_t len3 = 3U * TS_MTU_PAYLOAD;
    const uint32_t len2 = 2U * TS_MTU_PAYLOAD;
    uint16_t fs = 0U;
    uint8_t fi = 0U;
    NEED_PROTO();
    rx_setup(NULL);
    handshake();
    /* frame 40: pkt 200..202; frame 41: pkt 203..204 lost completely; frame 42: pkt 205..206 */
    for (uint8_t i = 0U; i < 3U; i++) {
        push_frag(40U, (uint16_t)(200U + i), i, len3, 0U, 0U, BLOB);
    }
    push_frag(42U, 205U, 0U, len2, 0U, 0U, BLOB);
    push_frag(42U, 206U, 1U, len2, 0U, 0U, BLOB);
    tick(T0 + 2U);
    TEST_ASSERT_TRUE(tsr_i_map_pkt(&R, 204U, &fs, &fi));
    TEST_ASSERT_EQUAL_UINT16(41U, fs);
    TEST_ASSERT_EQUAL_UINT8(1U, fi);
    tick(T0 + 2U + TS_NACK_DELAY_MS);
    TEST_ASSERT_EQUAL_UINT32(2U, take_nacks(e, 8U, NULL));
    TEST_ASSERT_EQUAL_UINT16(41U, e[0].frame_seq);
    TEST_ASSERT_EQUAL_UINT8(0U, e[0].frag_idx);
    TEST_ASSERT_EQUAL_UINT16(41U, e[1].frame_seq);
    TEST_ASSERT_EQUAL_UINT8(1U, e[1].frag_idx);
    /* two frames missing between known ones: ambiguous, no guess */
    push_frag(45U, 220U, 0U, len2, 0U, 0U, BLOB);
    tick(T0 + 30U);
    TEST_ASSERT_FALSE(tsr_i_map_pkt(&R, 210U, &fs, &fi));
    /* three single-packet frames lost between two single-packet frames: 1:1 */
    push_frag(50U, 300U, 0U, 700U, 0U, 0U, BLOB);
    push_frag(54U, 304U, 0U, 700U, 0U, 0U, BLOB);
    tick(T0 + 31U);
    TEST_ASSERT_TRUE(tsr_i_map_pkt(&R, 302U, &fs, &fi));
    TEST_ASSERT_EQUAL_UINT16(52U, fs);
    TEST_ASSERT_EQUAL_UINT8(0U, fi);
    TEST_ASSERT_TRUE(tsr_i_map_pkt(&R, 303U, &fs, &fi));
    TEST_ASSERT_EQUAL_UINT16(53U, fs);
}

static void test_unmappable_gap_requests_keyframe_early(void)
{
    const uint32_t len2 = 2U * TS_MTU_PAYLOAD;
    const uint32_t len3 = 3U * TS_MTU_PAYLOAD;
    NEED_PROTO();
    rx_setup(NULL);
    handshake();
    T.tx_len = 0U;
    /* frame 60 = pkt 100..102 (102 lost), frames 61 + 62 lost in pkt 103..106 (sizes
     * unknown), frame 63 from pkt 107 */
    push_frag(60U, 100U, 0U, len3, 0U, 0U, BLOB);
    push_frag(60U, 101U, 1U, len3, 0U, 0U, BLOB);
    push_frag(63U, 107U, 0U, len2, 0U, 0U, BLOB);
    tick(T0 + 2U);
    TEST_ASSERT_EQUAL_UINT32(5U, tsr_metrics(&R)->pkts_missing);
    tick(T0 + 1U + TS_NACK_DELAY_MS);
    TEST_ASSERT_EQUAL_UINT32(0U, tx_count((uint8_t)TS_TLV_KEYFRAME_REQ, NULL));
    tick(T0 + 2U + TS_NACK_DELAY_MS);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->nacks_sent);   /* only (60, 2) can be named */
    TEST_ASSERT_EQUAL_UINT32(1U, tx_count((uint8_t)TS_TLV_KEYFRAME_REQ, NULL));
}

static void test_pkt_seq_wrap_gap_and_clean(void)
{
    ts_nack_entry_t e[4];
    const uint32_t len = 4U * TS_MTU_PAYLOAD;
    NEED_PROTO();
    rx_setup(NULL);
    handshake();
    /* clean wrap: 65534, 65535, 0, 1 -> no gap */
    for (uint8_t i = 0U; i < 4U; i++) {
        push_frag(70U, (uint16_t)(65534U + i), i, len, 0U, 0U, BLOB);
    }
    run_to(T0 + 2U, T0 + 100U);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->pkts_missing);
    TEST_ASSERT_EQUAL_UINT32(0U, take_nacks(e, 4U, NULL));
    TEST_ASSERT_EQUAL_UINT32(4U, tsr_metrics(&R)->pkts_expected);
    /* lossy wrap: frame 71 pkts 2..5 is fine; frame 72 crosses nothing; now start a frame at 65534 again */
    rx_setup(NULL);
    handshake();
    push_frag(80U, 65534U, 0U, len, 0U, 0U, BLOB);
    push_frag(80U, 65535U, 1U, len, 0U, 0U, BLOB);
    push_frag(80U, 1U, 3U, len, 0U, 0U, BLOB);        /* pkt 0 (fragment 2) lost across the wrap */
    tick(T0 + 2U);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->pkts_missing);
    tick(T0 + 2U + TS_NACK_DELAY_MS);
    TEST_ASSERT_EQUAL_UINT32(1U, take_nacks(e, 4U, NULL));
    TEST_ASSERT_EQUAL_UINT16(80U, e[0].frame_seq);
    TEST_ASSERT_EQUAL_UINT8(2U, e[0].frag_idx);
    /* the retransmit (same pkt_seq 0) closes the gap */
    push_frag(80U, 0U, 2U, len, (uint8_t)TS_F_RETRANSMIT, 0U, BLOB);
    run_to(T0 + 3U + TS_NACK_DELAY_MS, T0 + 400U);
    TEST_ASSERT_EQUAL_UINT32(0U, take_nacks(e, 4U, NULL));
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->pkts_lost);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->retx_rx);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->frames_complete);
}

static void test_retransmit_repairs_and_decodes(void)
{
    ts_nack_entry_t e[4];
    uint8_t n;
    NEED_CODEC();
    rx_setup(NULL);
    handshake();
    enc_frame(0U, 300U, true, &F[0]);
    n = nfrags(F[0].len);
    for (uint8_t i = 0U; i < n; i++) {
        if (i != 3U) {
            push_frag(300U, (uint16_t)(500U + i), i, F[0].len, (uint8_t)TS_F_KEYFRAME, 0U, F[0].d);
        }
    }
    tick(T0 + 2U);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->frames_decoded);
    tick(T0 + 2U + TS_NACK_DELAY_MS);
    TEST_ASSERT_EQUAL_UINT32(1U, take_nacks(e, 4U, NULL));
    TEST_ASSERT_EQUAL_UINT8(3U, e[0].frag_idx);
    push_frag(300U, 503U, 3U, F[0].len, (uint8_t)(TS_F_KEYFRAME | TS_F_RETRANSMIT), 0U, F[0].d);
    tick(T0 + 30U);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->frames_decoded);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->retx_rx);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->pkts_lost);
    TEST_ASSERT_EQUAL_UINT32(8U, tsr_metrics(&R)->rtt_ms);      /* 30 - 22 */
    make_src(0U, SRC);
    TEST_ASSERT_EQUAL_MEMORY(SRC, tsr_frame(&R), TS_FRAME_BYTES);
}

static void test_keepalive_empty_nack(void)
{
    ts_nack_entry_t e[4];
    uint32_t pkts = 0U;
    NEED_PROTO();
    rx_setup(NULL);
    handshake();
    push_frag(1U, 1U, 0U, 3U * TS_MTU_PAYLOAD, 0U, 0U, BLOB);
    tick(T0 + 2U);
    run_to(T0 + 3U, T0 + 1000U);
    TEST_ASSERT_EQUAL_UINT32(0U, take_nacks(e, 4U, &pkts));
    TEST_ASSERT_EQUAL_UINT32(0U, pkts);
    tick(T0 + 1001U);                                  /* 1 s after WELCOME */
    TEST_ASSERT_EQUAL_UINT32(0U, take_nacks(e, 4U, &pkts));
    TEST_ASSERT_EQUAL_UINT32(1U, pkts);                /* one empty NACK */
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->nacks_sent);
}

/* ======================================================================== decoder / policy */
static void test_pframe_without_reference_requests_keyframe(void)
{
    uint16_t pkt = 10U;
    NEED_CODEC();
    rx_setup(NULL);
    handshake();
    T.tx_len = 0U;
    enc_frame(0U, 1U, true, &F[0]);      /* never delivered: the receiver joins mid-GOP */
    enc_frame(1U, 2U, false, &F[1]);
    enc_frame(2U, 3U, true, &F[2]);
    enc_frame(3U, 4U, false, &F[3]);
    pkt = send_all(&F[1], pkt, 0U);
    tick(T0 + 5U);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->frames_decoded);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->frames_dropped);
    TEST_ASSERT_EQUAL_UINT32(1U, tx_count((uint8_t)TS_TLV_KEYFRAME_REQ, NULL));
    TEST_ASSERT_NULL(tsr_frame(&R));
    tick(T0 + 100U);                     /* rate-limited: no second request yet */
    TEST_ASSERT_EQUAL_UINT32(1U, tx_count((uint8_t)TS_TLV_KEYFRAME_REQ, NULL));
    pkt = send_all(&F[2], pkt, 66U);
    (void)send_all(&F[3], pkt, 99U);
    tick(T0 + 120U);
    TEST_ASSERT_EQUAL_UINT32(2U, tsr_metrics(&R)->frames_decoded);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->keyframes);
    make_src(3U, SRC);
    TEST_ASSERT_EQUAL_MEMORY(SRC, tsr_frame(&R), TS_FRAME_BYTES);
    tick(T0 + 2000U);
    TEST_ASSERT_EQUAL_UINT32(1U, tx_count((uint8_t)TS_TLV_KEYFRAME_REQ, NULL));
}

/* K0 decoded, P1 lost completely, P2 arrives; then (optionally late) P1. */
static void lost_pframe_scenario(tsr_late_policy_t pol, bool deliver_late)
{
    tsr_cfg_t c;
    uint16_t p1;
    uint16_t pkt = 100U;
    tsr_cfg_default(&c);
    c.late_policy = pol;
    c.no_nack = true;                    /* pure timeout path, no repair */
    rx_setup(&c);
    handshake();
    T.tx_len = 0U;
    enc_frame(0U, 10U, true, &F[0]);
    enc_frame(1U, 11U, false, &F[1]);
    enc_frame(2U, 12U, false, &F[2]);
    TEST_ASSERT_EQUAL_UINT8(1U, nfrags(F[1].len));
    pkt = send_all(&F[0], pkt, 0U);
    p1 = pkt;
    pkt++;                               /* P1 lost on the wire */
    (void)send_all(&F[2], pkt, 66U);
    run_to(T0 + 2U, T0 + 400U);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->frames_decoded);
    TEST_ASSERT_EQUAL_UINT32(1U, tx_count((uint8_t)TS_TLV_KEYFRAME_REQ, NULL));
    if (deliver_late) {
        push_frag(11U, p1, 0U, F[1].len, (uint8_t)TS_F_RETRANSMIT, 33U, F[1].d);
        run_to(T0 + 401U, T0 + 410U);
    }
}

static void test_late_policy_drop(void)
{
    NEED_CODEC();
    lost_pframe_scenario(TSR_LATE_DROP, true);
    /* P1 given up -> P2 cannot decode; the late P1 is dropped */
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->frames_decoded);
    TEST_ASSERT_EQUAL_UINT32(2U, tsr_metrics(&R)->frames_dropped);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->late_pkts);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->pkts_lost);
}

static void test_late_policy_decode_restores_chain(void)
{
    NEED_CODEC();
    lost_pframe_scenario(TSR_LATE_DECODE, true);
    /* the awaited reference arrives late and still decodes, then P2 follows */
    TEST_ASSERT_EQUAL_UINT32(3U, tsr_metrics(&R)->frames_decoded);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->frames_dropped);
    make_src(2U, SRC);
    TEST_ASSERT_EQUAL_MEMORY(SRC, tsr_frame(&R), TS_FRAME_BYTES);
}

static void test_keyframe_resets_reference_after_loss(void)
{
    uint16_t pkt = 1000U;
    NEED_CODEC();
    lost_pframe_scenario(TSR_LATE_DROP, false);
    /* the requested keyframe arrives: reference reset, the chain continues */
    enc_frame(3U, 13U, true, &F[3]);
    enc_frame(4U, 14U, false, &F[4]);
    pkt = send_all(&F[3], pkt, 99U);
    (void)send_all(&F[4], pkt, 132U);
    run_to(T0 + 401U, T0 + 405U);
    TEST_ASSERT_EQUAL_UINT32(3U, tsr_metrics(&R)->frames_decoded);
    TEST_ASSERT_EQUAL_UINT32(2U, tsr_metrics(&R)->keyframes);
    make_src(4U, SRC);
    TEST_ASSERT_EQUAL_MEMORY(SRC, tsr_frame(&R), TS_FRAME_BYTES);
    TEST_ASSERT_FALSE(R.need_keyframe);
}

static void test_corrupt_frame_keeps_last_picture(void)
{
    uint16_t pkt = 10U;
    NEED_CODEC();
    rx_setup(NULL);
    handshake();
    enc_frame(0U, 1U, true, &F[0]);
    pkt = send_all(&F[0], pkt, 0U);
    tick(T0 + 3U);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->frames_decoded);
    /* a keyframe whose codec header is garbage (CRC is fine: the sender built it so) */
    memcpy(F[1].d, BLOB, 2000U);
    F[1].d[0] = 0x4BU;
    F[1].d[1] = 3U;                      /* bad quality */
    F[1].len = 2000U;
    F[1].key = true;
    F[1].fs = 2U;
    (void)send_all(&F[1], pkt, 33U);
    tick(T0 + 4U);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->decode_errors);
    make_src(0U, SRC);
    TEST_ASSERT_EQUAL_MEMORY(SRC, tsr_frame(&R), TS_FRAME_BYTES);
    TEST_ASSERT_TRUE(R.need_keyframe);
}

#define HOLE_LAST 0xFFU

/* K0 decoded; a multi-fragment P1 misses fragment `hole`; P2 optional. no_nack: timeouts only. */
static void incomplete_awaited_scenario(uint8_t hole, bool send_p2)
{
    static uint8_t noise[TS_FRAME_BYTES];
    ts_frame_info_t info;
    tsr_cfg_t c;
    uint16_t pkt = 100U;
    uint8_t n;
    tsr_cfg_default(&c);
    c.no_nack = true;
    rx_setup(&c);
    handshake();
    T.tx_len = 0U;
    enc_frame(0U, 10U, true, &F[0]);
    TEST_ASSERT_TRUE(tsv_gen_frame("noise3", noise));
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_enc_encode(&E, noise, 11U, F[1].d, sizeof(F[1].d), &info));
    F[1].len = info.enc_len;
    F[1].key = info.keyframe;
    F[1].fs = 11U;
    TEST_ASSERT_FALSE(F[1].key);
    enc_frame(2U, 12U, false, &F[2]);
    n = nfrags(F[1].len);
    TEST_ASSERT_TRUE(n > 2U);
    if (hole == HOLE_LAST) {
        hole = (uint8_t)(n - 1U);
    }
    TEST_ASSERT_TRUE(hole < n);
    pkt = send_all(&F[0], pkt, 0U);
    for (uint8_t i = 0U; i < n; i++) {
        if (i != hole) {
            push_frag(11U, (uint16_t)(pkt + i), i, F[1].len, 0U, 33U, F[1].d);
        }
    }
    pkt = (uint16_t)(pkt + n);
    if (send_p2) {
        (void)send_all(&F[2], pkt, 66U);
    }
}

static void test_awaited_incomplete_frame_times_out(void)
{
    NEED_CODEC();
    incomplete_awaited_scenario(1U, true);
    tick(T0 + 2U);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->frames_decoded);
    run_to(T0 + 3U, T0 + 2U + tsr_i_frame_timeout(&R) - 1U);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->frames_dropped);
    run_to(T0 + 2U + tsr_i_frame_timeout(&R), T0 + 300U);
    /* P1 given up (a later packet proves the hole is loss), P2 has no reference */
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->frames_decoded);
    TEST_ASSERT_EQUAL_UINT32(2U, tsr_metrics(&R)->frames_dropped);
    TEST_ASSERT_EQUAL_UINT32(1U, tx_count((uint8_t)TS_TLV_KEYFRAME_REQ, NULL));
    TEST_ASSERT_TRUE(R.need_keyframe);
}

static void test_awaited_frame_stalled_dropped_after_stale(void)
{
    NEED_CODEC();
    /* the tail is missing and nothing later arrived: pacing or loss? wait TSR_STALE_MS */
    incomplete_awaited_scenario(HOLE_LAST, false);
    run_to(T0 + 2U, T0 + 2U + TSR_STALE_MS - 1U);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->frames_dropped);
    run_to(T0 + 2U + TSR_STALE_MS, T0 + 4U + TSR_STALE_MS);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->frames_dropped);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->frames_decoded);
}

static void test_create_destroy_heap(void)
{
    tsr_cfg_t c;
    tsr_t *r;
    NEED_PROTO();
    tio_reset();
    tsr_cfg_default(&c);
    TEST_ASSERT_NULL(tsr_create(NULL, &T.io));
    r = tsr_create(&c, &T.io);
    TEST_ASSERT_NOT_NULL(r);
    T.now = T0;
    TEST_ASSERT_EQUAL_INT(TS_OK, tsr_tick(r, T0));
    TEST_ASSERT_EQUAL_INT(TSR_HELLO_SENT, tsr_state(r));
    T.tx_len = 0U;
    tsr_destroy(r);                      /* BYE, then close */
    TEST_ASSERT_EQUAL_UINT32(1U, tx_count((uint8_t)TS_TLV_BYE, NULL));
    TEST_ASSERT_FALSE(T.open);
    tsr_destroy(NULL);
}

static void test_welcome_timeout_and_send_error(void)
{
    NEED_PROTO();
    rx_setup(NULL);
    tick(T0);
    TEST_ASSERT_EQUAL_INT(TSR_HELLO_SENT, tsr_state(&R));
    tick(T0 + TSR_WELCOME_TIMEOUT_MS - 1U);
    TEST_ASSERT_EQUAL_INT(TSR_HELLO_SENT, tsr_state(&R));
    tick(T0 + TSR_WELCOME_TIMEOUT_MS);
    TEST_ASSERT_EQUAL_INT(TSR_DISCONNECTED, tsr_state(&R));
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->ctrl_errors);
    /* reconnect, then the socket breaks while a message is queued */
    tick(T0 + TSR_WELCOME_TIMEOUT_MS + TSR_RECONNECT_DEF_MS);
    TEST_ASSERT_EQUAL_UINT32(2U, T.connects);
    push_welcome(5U);
    tick(T0 + TSR_WELCOME_TIMEOUT_MS + TSR_RECONNECT_DEF_MS + 1U);
    TEST_ASSERT_EQUAL_INT(TSR_STREAMING, tsr_state(&R));
    T.open = false;                      /* tcp_send / tcp_recv now fail */
    TEST_ASSERT_EQUAL_INT(TS_OK, tsr_send_keyframe_req(&R));
    tick(T0 + TSR_WELCOME_TIMEOUT_MS + TSR_RECONNECT_DEF_MS + 2U);
    TEST_ASSERT_EQUAL_INT(TSR_DISCONNECTED, tsr_state(&R));
    TEST_ASSERT_EQUAL_UINT32(2U, tsr_metrics(&R)->ctrl_errors);
}

/* ======================================================================== model sender */
#define MS_RING 16U

typedef struct msender {
    encf_t   ring[MS_RING];
    uint16_t pkt0[MS_RING];
    bool     used[MS_RING];
    uint32_t rng;
    uint32_t loss_pct;
    uint16_t pkt;
    uint32_t frames;
    uint32_t pkts;
    uint32_t retx;
    uint32_t kreq_seen;
    uint32_t phase_from;   /* virtual ms after start: loss switches to phase_loss ... */
    uint32_t phase_to;     /* ... until here, then back to loss_base */
    uint32_t phase_loss;
    uint32_t loss_base;
    uint32_t decoded_at_phase_end;
} msender_t;

static msender_t MS;

static bool ms_lose(void)
{
    MS.rng ^= MS.rng << 13;
    MS.rng ^= MS.rng >> 17;
    MS.rng ^= MS.rng << 5;
    return (MS.rng % 100U) < MS.loss_pct;
}

static void ms_send_frame(uint32_t k, uint16_t fs, uint32_t now)
{
    uint32_t slot = (uint32_t)fs % MS_RING;
    encf_t *f = &MS.ring[slot];
    uint32_t kreq = tx_count((uint8_t)TS_TLV_KEYFRAME_REQ, NULL);
    bool force = (k == 0U) || (kreq != MS.kreq_seen);
    uint8_t n;
    MS.kreq_seen = kreq;
    enc_frame(k, fs, force, f);
    MS.pkt0[slot] = MS.pkt;
    MS.used[slot] = true;
    n = nfrags(f->len);
    for (uint8_t i = 0U; i < n; i++) {
        if (!ms_lose()) {
            push_frag(fs, (uint16_t)(MS.pkt + i), i, f->len, fflags(f), (uint16_t)now, f->d);
        }
        MS.pkts++;
    }
    MS.pkt = (uint16_t)(MS.pkt + n);
    MS.frames++;
}

static void ms_serve_nacks(uint32_t now)
{
    for (uint32_t i = 0U; i < T.out_count; i++) {
        ts_nack_t nk;
        TEST_ASSERT_EQUAL_INT(TS_OK, ts_nack_parse(T.out[i].d, T.out[i].len, &nk));
        for (uint32_t j = 0U; j < nk.count; j++) {
            uint32_t slot = (uint32_t)nk.e[j].frame_seq % MS_RING;
            encf_t *f = &MS.ring[slot];
            if (MS.used[slot] && (f->fs == nk.e[j].frame_seq) && (nk.e[j].frag_idx < nfrags(f->len)) &&
                (T.in_count < (TIO_Q - 1U))) {
                MS.retx++;
                if (!ms_lose()) {
                    push_frag(f->fs, (uint16_t)(MS.pkt0[slot] + nk.e[j].frag_idx), nk.e[j].frag_idx, f->len,
                              (uint8_t)(fflags(f) | TS_F_RETRANSMIT), (uint16_t)now, f->d);
                }
            }
        }
    }
    T.out_count = 0U;
}

/* 30 fps for `ms` virtual ms; frame_seq starts at fs0, pkt_seq at pkt0 (both near the wrap). */
static uint32_t ms_run(uint32_t loss_pct, uint32_t ms, uint16_t fs0, uint16_t pkt0)
{
    uint32_t k = 0U;
    memset(&MS, 0, sizeof(MS));
    MS.rng = 0x9E3779B9U;
    MS.loss_pct = loss_pct;
    MS.loss_base = loss_pct;
    MS.phase_from = ms_phase_from;
    MS.phase_to = ms_phase_to;
    MS.phase_loss = ms_phase_loss;
    MS.pkt = pkt0;
    rx_setup(NULL);
    handshake();
    for (uint32_t t = T0 + 2U; t < (T0 + 2U + ms); t++) {
        uint32_t rel = t - (T0 + 2U);
        MS.loss_pct = ((rel >= MS.phase_from) && (rel < MS.phase_to)) ? MS.phase_loss : MS.loss_base;
        if (rel == MS.phase_to) {
            MS.decoded_at_phase_end = tsr_metrics(&R)->frames_decoded;
        }
        if ((rel % 33U) == 0U) {
            ms_send_frame(k, (uint16_t)(fs0 + k), t);
            k++;
        }
        tick(t);
        ms_serve_nacks(t);
    }
    for (uint32_t t = T0 + 2U + ms; t < (T0 + 2U + ms + 300U); t++) {   /* drain, no new frames */
        tick(t);
        ms_serve_nacks(t);
    }
    return k;
}

static void test_model_sender_clean_link_wrap(void)
{
    uint32_t k;
    NEED_CODEC();
    k = ms_run(0U, 6000U, 65500U, 65300U);
    TEST_ASSERT_EQUAL_UINT32(k, tsr_metrics(&R)->frames_decoded);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->nacks_sent);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->pkts_missing);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->frames_dropped);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->keyframe_reqs);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->freezes);
    TEST_ASSERT_EQUAL_UINT32(MS.pkts, tsr_metrics(&R)->pkts_expected);
    TEST_ASSERT_TRUE((65300U + MS.pkts) > 65536U);   /* pkt_seq wrapped */
    make_src(k - 1U, SRC);
    TEST_ASSERT_EQUAL_MEMORY(SRC, tsr_frame(&R), TS_FRAME_BYTES);
}

static void test_model_sender_5pct_loss_repairs(void)
{
    uint32_t k;
    const tsr_metrics_t *m;
    NEED_CODEC();
    k = ms_run(5U, 10000U, 65400U, 65300U);
    m = tsr_metrics(&R);
    TEST_ASSERT_TRUE(m->pkts_missing > 0U);
    TEST_ASSERT_TRUE(m->nacks_sent >= m->pkts_missing);
    TEST_ASSERT_TRUE(m->retx_rx > 0U);
    /* NACK repair keeps nearly every frame; the rest recovers through keyframes */
    TEST_ASSERT_TRUE(m->frames_decoded >= ((k * 95U) / 100U));
    TEST_ASSERT_TRUE(m->residual_loss_pct_x100 < 100U);       /* < 1 % after repair */
    TEST_ASSERT_TRUE(m->loss_pct_x100 >= 300U);                /* raw link loss is seen */
    TEST_ASSERT_TRUE(m->keyframe_reqs <= 5U);
    TEST_ASSERT_TRUE(m->freezes <= 1U);
    TEST_ASSERT_TRUE(m->rtt_ms <= 5U);
    make_src(k - 1U, SRC);
    TEST_ASSERT_EQUAL_MEMORY(SRC, tsr_frame(&R), TS_FRAME_BYTES);
}

static void test_model_sender_heavy_loss_then_recovers(void)
{
    uint32_t k;
    const tsr_metrics_t *m;
    NEED_CODEC();
    ms_phase_from = 1000U;               /* 30 % loss from 1 s to 5 s, then clean */
    ms_phase_to = 5000U;
    ms_phase_loss = 30U;
    k = ms_run(0U, 8000U, 100U, 65000U);
    m = tsr_metrics(&R);
    TEST_ASSERT_TRUE(m->nack_giveups > 0U);
    TEST_ASSERT_TRUE(m->keyframe_reqs > 0U);
    /* the 3 clean seconds afterwards decode (almost) every frame */
    TEST_ASSERT_TRUE((m->frames_decoded - MS.decoded_at_phase_end) >= 85U);
    make_src(k - 1U, SRC);
    TEST_ASSERT_EQUAL_MEMORY(SRC, tsr_frame(&R), TS_FRAME_BYTES);
    TEST_ASSERT_EQUAL_UINT32(0U, m->freeze_ms_current);
}

static void test_model_sender_cable_pull_recovery(void)
{
    uint32_t k;
    const tsr_metrics_t *m;
    uint32_t before;
    NEED_CODEC();
    ms_phase_from = 2000U;               /* cable pulled for 1.5 s */
    ms_phase_to = 3500U;
    ms_phase_loss = 100U;
    k = ms_run(0U, 6000U, 65530U, 65530U);
    m = tsr_metrics(&R);
    before = MS.decoded_at_phase_end;
    TEST_ASSERT_EQUAL_UINT32(1U, m->freezes);
    /* recovery: a keyframe was requested and the stream resumed well within 1 s */
    TEST_ASSERT_TRUE(m->keyframe_reqs >= 1U);
    TEST_ASSERT_TRUE(m->freeze_ms_total <= (1500U + 300U));
    TEST_ASSERT_TRUE((m->frames_decoded - before) >= 70U);   /* of ~75 sent after the pull */
    make_src(k - 1U, SRC);
    TEST_ASSERT_EQUAL_MEMORY(SRC, tsr_frame(&R), TS_FRAME_BYTES);
}

/* ======================================================================== metrics */
static void test_freeze_detection(void)
{
    uint16_t pkt = 10U;
    NEED_CODEC();
    rx_setup(NULL);
    handshake();
    enc_frame(0U, 1U, true, &F[0]);
    enc_frame(1U, 2U, false, &F[1]);
    pkt = send_all(&F[0], pkt, 0U);
    tick(T0 + 100U);                     /* decoded at T0+100 */
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->frames_decoded);
    run_to(T0 + 101U, T0 + 100U + TS_FREEZE_MS);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->freezes);
    tick(T0 + 101U + TS_FREEZE_MS);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->freezes);
    TEST_ASSERT_EQUAL_UINT32(TS_FREEZE_MS + 1U, tsr_metrics(&R)->freeze_ms_current);
    run_to(T0 + 102U + TS_FREEZE_MS, T0 + 799U);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->freezes);   /* counted once */
    (void)send_all(&F[1], pkt, 33U);
    tick(T0 + 800U);
    TEST_ASSERT_EQUAL_UINT32(2U, tsr_metrics(&R)->frames_decoded);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->freezes);
    TEST_ASSERT_EQUAL_UINT32(700U, tsr_metrics(&R)->freeze_ms_total);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->freeze_ms_current);
}

static void test_freeze_counted_inside_one_tick(void)
{
    uint16_t pkt = 10U;
    NEED_CODEC();
    rx_setup(NULL);
    handshake();
    enc_frame(0U, 1U, true, &F[0]);
    enc_frame(1U, 2U, false, &F[1]);
    pkt = send_all(&F[0], pkt, 0U);
    tick(T0 + 2U);
    (void)send_all(&F[1], pkt, 33U);
    tick(T0 + 2U + 600U);                /* no tick in between saw the gap */
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->freezes);
    TEST_ASSERT_EQUAL_UINT32(600U, tsr_metrics(&R)->freeze_ms_total);
}

static void test_ts_ms_wrap_owd_and_jitter_helpers(void)
{
    uint32_t j = 0U;
    j = tsr_i_jitter_step(j, 16);
    TEST_ASSERT_EQUAL_UINT32(16U, j);                 /* x16: 1 ms */
    j = tsr_i_jitter_step(j, -16);
    TEST_ASSERT_EQUAL_UINT32(31U, j);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_i_jitter_step(0U, 0));
    NEED_PROTO();                                     /* serial arithmetic lives in tsproto */
    TEST_ASSERT_EQUAL_INT32(4, tsr_i_owd_ms(0x00010002U, 0xFFFEU));
    TEST_ASSERT_EQUAL_INT32(11, tsr_i_owd_ms(5U, 65530U));
    TEST_ASSERT_EQUAL_INT32(-3, tsr_i_owd_ms(65533U, 0U));
    TEST_ASSERT_EQUAL_INT32(0, tsr_i_owd_ms(0x12340000U, 0U));
}

static void test_ts_ms_wrap_latency_jitter(void)
{
    uint16_t pkt = 10U;
    /* capture clock 65 470 + 33 k (wraps after k = 2); arrival 7 ms after capture */
    const uint32_t base = 0x0002FFBEU;               /* low 16 bits = 65 470 */
    NEED_CODEC();
    rx_setup(NULL);
    T.now = base - 10U;
    tick(base - 10U);
    push_welcome(1U);
    tick(base - 9U);
    TEST_ASSERT_EQUAL_INT(TSR_STREAMING, tsr_state(&R));
    for (uint32_t k = 0U; k < 5U; k++) {
        uint32_t cap_ms = base + (33U * k);
        enc_frame(k, (uint16_t)(k + 1U), k == 0U, &F[0]);
        pkt = send_all(&F[0], pkt, (uint16_t)(cap_ms & 0xFFFFU));
        tick(cap_ms + 7U);
        TEST_ASSERT_EQUAL_UINT32(k + 1U, tsr_metrics(&R)->frames_decoded);
        TEST_ASSERT_EQUAL_INT32(7, tsr_metrics(&R)->owd_ms);
        TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->latency_ms);
    }
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->jitter_ms);    /* no spurious 65 536 ms step */
    /* one frame 20 ms late: |D| = 20 -> J = 20/16 */
    enc_frame(5U, 6U, false, &F[0]);
    (void)send_all(&F[0], pkt, (uint16_t)((base + 165U) & 0xFFFFU));
    tick(base + 165U + 27U);
    TEST_ASSERT_EQUAL_INT32(27, tsr_metrics(&R)->owd_ms);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->jitter_ms);
}

static void test_psnr_helper(void)
{
    static uint8_t a[TS_FRAME_BYTES];
    static uint8_t b[TS_FRAME_BYTES];
    uint32_t v;
    memset(a, 100, sizeof(a));
    memcpy(b, a, sizeof(b));
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_i_psnr_cdb(a, b));
    b[5] = 110U;                         /* SSE 100: 10 log10(65025 * 76800 / 100) = 76.98 dB */
    v = tsr_i_psnr_cdb(a, b);
    TEST_ASSERT_UINT32_WITHIN(1U, 7698U, v);
    memset(b, 0, sizeof(b));
    memset(a, 255, sizeof(a));
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_i_psnr_cdb(a, b));   /* MSE = 255^2 -> 0 dB */
    memset(a, 16, sizeof(a));            /* MSE 256 -> 10 log10(65025/256) = 24.05 dB */
    TEST_ASSERT_UINT32_WITHIN(1U, 2405U, tsr_i_psnr_cdb(a, b));
}

static uint32_t truth_calls;
static bool truth_cb(void *ctx, uint16_t frame_seq, uint8_t *truth)
{
    (void)ctx;
    truth_calls++;
    make_src((uint32_t)frame_seq - 1U, truth);
    if (frame_seq == 2U) {
        truth[100] ^= 0x20U;             /* frame 2 is "wrong" */
    }
    return true;
}

static void test_truth_exact_match_and_psnr(void)
{
    uint16_t pkt = 10U;
    NEED_CODEC();
    rx_setup(NULL);
    handshake();
    enc_frame(0U, 1U, true, &F[0]);
    make_src(0U, SRC);
    tsr_set_truth(&R, SRC);              /* one-shot truth for the next frame */
    pkt = send_all(&F[0], pkt, 0U);
    tick(T0 + 3U);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->truth_frames);
    TEST_ASSERT_EQUAL_UINT32(100U, tsr_metrics(&R)->exact_match_pct);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->psnr_cdb);
    TEST_ASSERT_EQUAL_UINT8(TSR_PSNR_TRUTH, tsr_metrics(&R)->psnr_src);
    /* per-frame provider regenerates truth from frame_seq */
    tsr_set_truth_fn(&R, truth_cb, NULL);
    enc_frame(1U, 2U, false, &F[1]);
    pkt = send_all(&F[1], pkt, 33U);
    tick(T0 + 4U);
    TEST_ASSERT_EQUAL_UINT32(1U, truth_calls);
    TEST_ASSERT_EQUAL_UINT32(2U, tsr_metrics(&R)->truth_frames);
    TEST_ASSERT_EQUAL_UINT32(50U, tsr_metrics(&R)->exact_match_pct);
    TEST_ASSERT_TRUE(tsr_metrics(&R)->psnr_cdb > 5000U);
    enc_frame(2U, 3U, false, &F[2]);
    (void)send_all(&F[2], pkt, 66U);
    tick(T0 + 5U);
    TEST_ASSERT_EQUAL_UINT32(66U, tsr_metrics(&R)->exact_match_pct);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->psnr_cdb);
    tsr_set_truth_fn(&R, NULL, NULL);
}

static void test_psnr_vs_next_keyframe(void)
{
    uint16_t pkt = 10U;
    NEED_CODEC();
    rx_setup(NULL);
    handshake();
    enc_frame(0U, 1U, true, &F[0]);
    enc_frame(1U, 2U, false, &F[1]);
    enc_frame(1U, 3U, true, &F[2]);      /* keyframe of the same picture */
    pkt = send_all(&F[0], pkt, 0U);
    pkt = send_all(&F[1], pkt, 33U);
    (void)send_all(&F[2], pkt, 66U);
    tick(T0 + 3U);
    TEST_ASSERT_EQUAL_UINT32(3U, tsr_metrics(&R)->frames_decoded);
    TEST_ASSERT_EQUAL_UINT8(TSR_PSNR_KEYFRAME, tsr_metrics(&R)->psnr_src);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->psnr_cdb);   /* no drift: identical */
}

static void test_fps_kbps_window_and_tiles(void)
{
    uint16_t pkt = 10U;
    uint32_t bytes = 0U;
    NEED_CODEC();
    rx_setup(NULL);
    handshake();
    for (uint32_t k = 0U; k < 30U; k++) {
        uint32_t t = T0 + 2U + (k * 33U);
        enc_frame(k, (uint16_t)(k + 1U), k == 0U, &F[0]);
        bytes += F[0].len + ((uint32_t)nfrags(F[0].len) * TS_HDR_LEN);
        pkt = send_all(&F[0], pkt, (uint16_t)t);
        tick(t);
    }
    tick(T0 + 1000U);
    TEST_ASSERT_EQUAL_UINT32(30U, tsr_metrics(&R)->frames_decoded);
    TEST_ASSERT_UINT32_WITHIN(5U, 3000U, tsr_metrics(&R)->fps_x100);
    TEST_ASSERT_UINT32_WITHIN(2U, (bytes * 8U) / 1000U, tsr_metrics(&R)->kbps);
    /* the box moved by one tile column: its old and new tile changed */
    TEST_ASSERT_EQUAL_UINT16(2U, tsr_metrics(&R)->changed_tiles);
    TEST_ASSERT_TRUE(tsr_metrics(&R)->uptime_ms >= 1000U);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->freezes);
}

static void test_metrics_json(void)
{
    static char buf[8192];
    static char small[64];
    static tsr_metrics_t m;
    size_t n;
    memset(&m, 0, sizeof(m));
    m.fps_x100 = 2997U;
    m.loss_pct_x100 = 505U;
    m.owd_ms = -12;
    m.state = (uint8_t)TSR_STREAMING;
    m.tile_bitmap[0] = 0x05U;            /* tiles 0 and 2 */
    m.last_stats.g.client_count = 1U;
    m.last_stats.c[0].kbps = 4000U;
    n = tsr_metrics_json(&m, buf, sizeof(buf));
    TEST_ASSERT_TRUE(n > 0U);
    TEST_ASSERT_EQUAL_size_t(strlen(buf), n);
    TEST_ASSERT_EQUAL_CHAR('{', buf[0]);
    TEST_ASSERT_EQUAL_CHAR('}', buf[n - 1U]);
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"fps\":29.97,"));
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"loss_pct\":5.05,"));
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"owd_ms\":-12,"));
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"state\":\"streaming\""));
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"tiles\":[\"10100000000000000000\",\"0000"));
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"tile_bitmap\":\"0500"));
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"clients\":[{\"id\":0,"));
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"kbps\":4000"));
    TEST_ASSERT_NULL(strstr(buf, ",,"));
    TEST_ASSERT_NULL(strstr(buf, "{,"));
    TEST_ASSERT_EQUAL_size_t(0U, tsr_metrics_json(&m, small, sizeof(small)));
    TEST_ASSERT_EQUAL_CHAR('\0', small[0]);
    TEST_ASSERT_EQUAL_size_t(0U, tsr_metrics_json(NULL, small, sizeof(small)));
}

static void test_reset_metrics_and_stream_change(void)
{
    uint16_t pkt = 10U;
    NEED_CODEC();
    rx_setup(NULL);
    handshake();
    enc_frame(0U, 1U, true, &F[0]);
    (void)send_all(&F[0], pkt, 0U);
    tick(T0 + 3U);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->frames_decoded);
    tsr_reset_metrics(&R);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->frames_decoded);
    TEST_ASSERT_EQUAL_UINT32(0x1234U, tsr_metrics(&R)->stream_id);
    /* daemon restart: new stream_id resets sequence state, frame_seq may go backwards */
    push_welcome(0x9999U);
    tick(T0 + 4U);
    ts_enc_init(&E);
    enc_frame(0U, 0U, true, &F[1]);
    (void)send_all(&F[1], 0U, 0U);
    tick(T0 + 5U);
    TEST_ASSERT_EQUAL_UINT32(1U, tsr_metrics(&R)->frames_decoded);
    TEST_ASSERT_EQUAL_UINT32(0U, tsr_metrics(&R)->late_pkts);
    TEST_ASSERT_EQUAL_UINT32(0x9999U, tsr_metrics(&R)->stream_id);
}

static void test_api_null_and_init_errors(void)
{
    tsr_cfg_t c;
    tsr_cfg_default(&c);
    tio_reset();
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, tsr_init(NULL, &c, &T.io));
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, tsr_init(&R, NULL, &T.io));
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, tsr_init(&R, &c, NULL));
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, tsr_tick(NULL, 0U));
    TEST_ASSERT_NULL(tsr_metrics(NULL));
    TEST_ASSERT_NULL(tsr_frame(NULL));
    TEST_ASSERT_EQUAL_INT(TSR_CLOSED, tsr_state(NULL));
    c.nack_delay_ms = 0U;
    c.max_nack_per_tick = 0U;
    TEST_ASSERT_EQUAL_INT(TS_OK, tsr_init(&R, &c, &T.io));
    TEST_ASSERT_EQUAL_UINT32(TS_NACK_DELAY_MS, R.cfg.nack_delay_ms);
    TEST_ASSERT_EQUAL_UINT8(TS_NACK_MAX, R.cfg.max_nack_per_tick);
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_api_null_and_init_errors);
    RUN_TEST(test_handshake_hello_welcome);
    RUN_TEST(test_ctrl_split_and_glued_tlvs);
    RUN_TEST(test_ctrl_send_messages_and_bye);
    RUN_TEST(test_ctrl_reconnect_after_close);
    RUN_TEST(test_ctrl_silence_watchdog_rehello);
    RUN_TEST(test_ctrl_unknown_tlv_reconnects);
    RUN_TEST(test_reasm_in_order_keyframe_decodes);
    RUN_TEST(test_reasm_shuffled_u32_frame_len);
    RUN_TEST(test_reasm_short_last_fragment_first);
    RUN_TEST(test_reasm_duplicate_dropped);
    RUN_TEST(test_reasm_late_frame_dropped);
    RUN_TEST(test_reasm_window_eviction);
    RUN_TEST(test_reasm_inconsistent_geometry_rejected);
    RUN_TEST(test_args_strict_u32_and_seconds_limit);
    RUN_TEST(test_tear_detector_split_frame);
    RUN_TEST(test_tear_detector_picture_change_and_edges);
    RUN_TEST(test_tear_detector_rules);
    RUN_TEST(test_bad_crc_and_garbage_counted);
    RUN_TEST(test_empty_datagram_does_not_stall_rx_drain);
    RUN_TEST(test_nack_after_delay_not_before);
    RUN_TEST(test_nack_reorder_within_delay_no_nack);
    RUN_TEST(test_nack_two_rounds_then_keyframe_req);
    RUN_TEST(test_nack_chunked_to_32);
    RUN_TEST(test_nack_budget_per_tick_default);
    RUN_TEST(test_nack_whole_frame_inferred);
    RUN_TEST(test_unmappable_gap_requests_keyframe_early);
    RUN_TEST(test_pkt_seq_wrap_gap_and_clean);
    RUN_TEST(test_retransmit_repairs_and_decodes);
    RUN_TEST(test_keepalive_empty_nack);
    RUN_TEST(test_pframe_without_reference_requests_keyframe);
    RUN_TEST(test_late_policy_drop);
    RUN_TEST(test_late_policy_decode_restores_chain);
    RUN_TEST(test_keyframe_resets_reference_after_loss);
    RUN_TEST(test_corrupt_frame_keeps_last_picture);
    RUN_TEST(test_awaited_incomplete_frame_times_out);
    RUN_TEST(test_awaited_frame_stalled_dropped_after_stale);
    RUN_TEST(test_create_destroy_heap);
    RUN_TEST(test_welcome_timeout_and_send_error);
    RUN_TEST(test_model_sender_clean_link_wrap);
    RUN_TEST(test_model_sender_5pct_loss_repairs);
    RUN_TEST(test_model_sender_heavy_loss_then_recovers);
    RUN_TEST(test_model_sender_cable_pull_recovery);
    RUN_TEST(test_freeze_detection);
    RUN_TEST(test_freeze_counted_inside_one_tick);
    RUN_TEST(test_ts_ms_wrap_owd_and_jitter_helpers);
    RUN_TEST(test_ts_ms_wrap_latency_jitter);
    RUN_TEST(test_psnr_helper);
    RUN_TEST(test_truth_exact_match_and_psnr);
    RUN_TEST(test_psnr_vs_next_keyframe);
    RUN_TEST(test_fps_kbps_window_and_tiles);
    RUN_TEST(test_metrics_json);
    RUN_TEST(test_reset_metrics_and_stream_change);
    return UNITY_END();
}
