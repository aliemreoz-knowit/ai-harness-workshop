/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- src/net/daemon.c
 * Daemon core (include/ts_daemon.h). One tsd_tick():
 *
 *   1. refill buckets, reset per-tick retransmit budgets
 *   2. accept TCP connections (a 5th client is closed without WELCOME)
 *   3. per client: recv() -> TLV accumulate buffer -> complete TLVs only
 *      (HELLO/KEYFRAME_REQ/SET_RATE/BYE; anything else closes the connection)
 *   4. UDP: bounded NACK parse (never more than TS_NACK_MAX entries), client
 *      lookup by (ip, port), retransmit from the ring charged to the client
 *      bucket and the global cap, at most retx_per_tick per client per tick
 *   5. client backlogs (stale backlog dropped), loss adaptation and
 *      congestion control (stream vs slowest viewer) -> quality step
 *   6. capture -> keyframe decision -> encode -> fragment into the ring
 *   7. fresh packets to every ACTIVE client, round-robin, bucket paced
 *   8. expire silent clients, close and free CLOSING ones
 *   9. STATS every TS_STATS_PERIOD_MS to every ACTIVE client
 *
 * Network only through ts_io_t, time only through `now_ms`, every u16 counter
 * compared with ts_seq_*, every u32 timer with a wrapping difference. No heap
 * (all state lives in the pool), no stdio (TS_LOG is compiled out here).
 */
#include <stddef.h>
#include <string.h>
#include "ts_daemon.h"
#include "ts_pool.h"

#define TSD_ACCEPT_PER_TICK   (TS_IO_MAX_TCP + 1U)   /* bounded accept loop */
#define TSD_TCP_RX_PER_TICK   4U                     /* recv() calls per client per tick */
#define TSD_UDP_RX_PER_TICK   64U                    /* datagrams per tick */
#define TSD_SEND_RETRIES      4U                     /* would-block retries for one control TLV */
#define TSD_POLL_MS           2U                     /* fd sources (pipe, v4l2) are polled this often */
#define TSD_MAX_FPS           1000U
#define TSD_GLOBAL_BURST      (TS_MAX_FRAGS * TS_PKT_MAX)   /* one worst-case frame, 156640 bytes */
#define TSD_STREAM_ID_SALT    0x7473646DU                                    /* "tsdm" */
#define TSD_ENC_BUF_BYTES     TS_ENC_MAX            /* encoded frame buffer */

/* daemon event codes for the (compiled-out) ring log */
#define TSD_EV_PROTO_ERR      1U
#define TSD_EV_CAPTURE_ERR    2U
#define TSD_EV_ENCODE_ERR     3U
#define TSD_EV_REFUSED        4U

_Static_assert(TSD_GLOBAL_BURST >= TS_PKT_MAX, "global bucket can always pass one packet");
_Static_assert(TS_NACK_MAX <= 255U, "NACK count is u8");

/* Encoded frame before fragmentation (static storage, a separate object). Only
 * used inside one tsd_encode_push() call, so daemons in one process can share it. */
static uint8_t g_tsd_enc_buf[TSD_ENC_BUF_BYTES];
static const uint32_t g_tsd_enc_max = TS_ENC_MAX;   /* largest encoded frame (PROTOCOL.md 6.4) */

/* ---- small helpers -------------------------------------------------------- */

/* true when u32 time `now` is at or after `due` (wrapping difference). */
static bool tsd_time_reached(uint32_t now, uint32_t due)
{
    return (uint32_t)(now - due) < 0x80000000U;
}

static uint32_t tsd_mix32(uint32_t v)
{
    uint32_t h = v ^ TSD_STREAM_ID_SALT;
    h ^= h >> 16;
    h *= 0x7FEB352DU;
    h ^= h >> 15;
    h *= 0x846CA68BU;
    h ^= h >> 16;
    return h;
}

static bool tsd_client_live(const ts_client_t *c)
{
    return (c->state == TS_CL_PENDING) || (c->state == TS_CL_ACTIVE);
}

/*
 * A viewer needs a keyframe (new cursor at the ring end, backlog reset). The
 * keyframe is forced only when the cooldown since the last keyframe has
 * passed; otherwise it is requested and the scheduler defers it until the
 * cooldown ends, so HELLO churn (re-HELLO or reconnect loops) cannot produce
 * more than one keyframe per TS_KEYFRAME_COOLDOWN_MS.
 */
static void tsd_want_keyframe(tsd_t *d, uint32_t now_ms)
{
    if (d->keysched.forced || d->keysched.requested) {
        /* one is already on its way */
    } else if ((uint32_t)(now_ms - d->keysched.last_key_ms) >= d->keysched.cooldown_ms) {
        ts_keysched_force(&d->keysched);
    } else {
        ts_keysched_request(&d->keysched, TS_KEY_REQUEST);
    }
}

/* ---- control channel: send side ------------------------------------------ */

typedef enum tsd_ctl_res {
    TSD_CTL_SENT = 0,     /* the whole TLV is on its way */
    TSD_CTL_SKIPPED,      /* not a single byte accepted: dropping it keeps the stream in sync */
    TSD_CTL_BROKEN        /* torn TLV or socket error: the connection must be closed */
} tsd_ctl_res_t;

/*
 * Write one complete TLV, looping over partial writes (a TCP send may take
 * fewer bytes than offered; the fake splits on purpose with tcp_send_limit).
 */
static tsd_ctl_res_t tsd_ctl_write(tsd_t *d, const ts_client_t *c, const uint8_t *buf, size_t len)
{
    size_t sent = 0U;
    uint32_t stalls = 0U;
    bool err = false;
    tsd_ctl_res_t res;
    /* every productive call writes >= 1 byte, so len + retries bounds the loop */
    for (size_t iter = 0U; (sent < len) && !err && (stalls < TSD_SEND_RETRIES) && (iter < (len + TSD_SEND_RETRIES)); iter++) {
        int32_t n = ts_io_tcp_send(d->io, c->tcp, &buf[sent], len - sent);
        if (n > 0) {
            sent += (size_t)n;
        } else if (n == 0) {
            stalls++;
        } else {
            err = true;
        }
    }
    if (err) {
        res = TSD_CTL_BROKEN;
    } else if (sent == len) {
        res = TSD_CTL_SENT;
    } else if (sent == 0U) {
        res = TSD_CTL_SKIPPED;
    } else {
        res = TSD_CTL_BROKEN;
    }
    return res;
}

static void tsd_close_client(tsd_t *d, ts_client_t *c, bool protocol_error)
{
    if (protocol_error) {
        d->stats.ctrl_errors++;
        TS_LOG(&d->log, d->last_tick_ms, TS_LOG_WARN, TS_MOD_NET, (uint16_t)TSD_EV_PROTO_ERR, (uint32_t)c->id, 0U);
    }
    ts_client_close(&d->clients, c);
}

/* Send a TLV that must arrive whole (WELCOME); a short write closes the client. */
static void tsd_ctl_send_required(tsd_t *d, ts_client_t *c, const uint8_t *buf, size_t len)
{
    if (tsd_ctl_write(d, c, buf, len) != TSD_CTL_SENT) {
        tsd_close_client(d, c, false);
    }
}

/* Send a TLV that may be skipped when the socket is full (STATS). */
static void tsd_ctl_send_optional(tsd_t *d, ts_client_t *c, const uint8_t *buf, size_t len)
{
    if (tsd_ctl_write(d, c, buf, len) == TSD_CTL_BROKEN) {
        tsd_close_client(d, c, false);
    }
}

static void tsd_send_welcome(tsd_t *d, ts_client_t *c)
{
    ts_tlv_welcome_t w;
    size_t len = 0U;
    w.w = (uint16_t)TS_WIDTH;
    w.h = (uint16_t)TS_HEIGHT;
    w.mtu = (uint16_t)TS_MTU_PAYLOAD;
    w.stream_id = d->cfg.stream_id;
    w.ring_slots = (uint16_t)TS_RING_SLOTS;
    if (ts_tlv_pack_welcome(&w, d->ctl_buf, sizeof(d->ctl_buf), &len) == TS_OK) {
        tsd_ctl_send_required(d, c, d->ctl_buf, len);
    } else {
        tsd_close_client(d, c, false);
    }
}

/* ---- control channel: receive side ---------------------------------------- */

/* True when the newest keyframe's first fragment is still held in the ring. */
static bool tsd_key_held(const tsd_t *d)
{
    bool ok = false;
    if (d->have_key_push) {
        ts_txq_cursor_t k;
        uint32_t behind = d->txq.pushed - d->key_push_idx;   /* u32 serial distance */
        k.next = d->key_push_idx;
        k.skipped = 0U;
        /* pending() is clipped to what the ring holds: equal means nothing was overwritten */
        ok = (behind < 0x80000000U) && (ts_txq_cursor_pending(&d->txq, &k) == behind);
    }
    return ok;
}

/*
 * A cursor that was just (re)started at the ring end needs a keyframe. When
 * the newest keyframe is younger than the cooldown and still in the ring, the
 * cursor is moved back to its first fragment (the viewer decodes at once and
 * no extra keyframe is spent); otherwise a keyframe is wanted (forced when
 * cooled down, deferred otherwise).
 */
static void tsd_start_cursor(tsd_t *d, ts_client_t *c, uint32_t now_ms)
{
    bool cooled = ((uint32_t)(now_ms - d->keysched.last_key_ms) >= d->keysched.cooldown_ms);
    if ((!cooled) && tsd_key_held(d)) {
        c->cursor.next = d->key_push_idx;
    } else {
        tsd_want_keyframe(d, now_ms);
    }
}

static void tsd_on_hello(tsd_t *d, ts_client_t *c, const ts_tlv_t *tlv, uint32_t now_ms)
{
    ts_tlv_hello_t h;
    if (ts_tlv_parse_hello(tlv, &h) != TS_OK) {
        tsd_close_client(d, c, true);
    } else {
        uint32_t kbps = (h.kbps != 0U) ? h.kbps : d->cfg.kbps_client;
        bool was_active = (c->state == TS_CL_ACTIVE);
        ts_addr_t old_key = c->udp;
        if (ts_client_hello(&d->clients, c, h.udp_port, kbps, &d->txq, now_ms) != TS_OK) {
            tsd_close_client(d, c, true);
        } else {
            tsd_send_welcome(d, c);
            if ((!was_active) || (!ts_addr_eq(&old_key, &c->udp))) {
                /* new cursor at the ring end: its first frame must be a keyframe */
                tsd_start_cursor(d, c, now_ms);
            }
            /* a repeated HELLO on the same key keeps the cursor: no keyframe needed */
        }
    }
}

static void tsd_on_set_rate(tsd_t *d, ts_client_t *c, const ts_tlv_t *tlv, uint32_t now_ms)
{
    ts_tlv_set_rate_t r;
    if ((c->state != TS_CL_ACTIVE) || (ts_tlv_parse_set_rate(tlv, &r) != TS_OK)) {
        tsd_close_client(d, c, true);
    } else {
        /* kilobits per second (1 kbit = 1000 bit), PROTOCOL.md 5; 0 = daemon default;
         * tokens earned at the old rate are kept */
        uint32_t kbps = (r.kbps != 0U) ? r.kbps : d->cfg.kbps_client;
        ts_client_set_rate(c, kbps, now_ms);
    }
}

static void tsd_on_keyframe_req(tsd_t *d, ts_client_t *c, const ts_tlv_t *tlv, uint32_t now_ms)
{
    if ((c->state != TS_CL_ACTIVE) || (tlv->len != 0U)) {
        tsd_close_client(d, c, true);
    } else {
        /* per-client TS_KEYREQ_MIN_INTERVAL_MS limit, then the scheduler cooldown */
        (void)ts_client_keyframe_req(c, &d->keysched, now_ms);
    }
}

static void tsd_dispatch(tsd_t *d, ts_client_t *c, const ts_tlv_t *tlv, uint32_t now_ms)
{
    switch (tlv->type) {
    case (uint8_t)TS_TLV_HELLO:
        tsd_on_hello(d, c, tlv, now_ms);
        break;
    case (uint8_t)TS_TLV_KEYFRAME_REQ:
        tsd_on_keyframe_req(d, c, tlv, now_ms);
        break;
    case (uint8_t)TS_TLV_SET_RATE:
        tsd_on_set_rate(d, c, tlv, now_ms);
        break;
    case (uint8_t)TS_TLV_BYE:
        tsd_close_client(d, c, false);
        break;
    default:
        /* WELCOME / STATS travel the other way; unknown types are never skipped */
        tsd_close_client(d, c, true);
        break;
    }
}

/* Pop and handle every complete TLV buffered for `c`. */
static void tsd_drain_tlvs(tsd_t *d, ts_client_t *c, uint32_t now_ms)
{
    bool more = true;
    while (more && tsd_client_live(c)) {
        ts_tlv_t tlv;
        ts_err_t r = ts_tlv_acc_next(&c->acc, &tlv);
        if (r == TS_OK) {
            tsd_dispatch(d, c, &tlv, now_ms);
        } else if (r == TS_E_AGAIN) {
            more = false;
        } else {
            tsd_close_client(d, c, true);   /* TS_E_TOOLONG: len > TS_TLV_MAX_LEN */
            more = false;
        }
    }
}

static void tsd_client_rx(tsd_t *d, ts_client_t *c, uint32_t now_ms)
{
    bool more = true;
    for (uint32_t i = 0U; (i < TSD_TCP_RX_PER_TICK) && more && tsd_client_live(c); i++) {
        /* never read more than the accumulate buffer can take (ctl_rx is TS_TLV_ACC_CAP, see pool.c) */
        size_t pending = (size_t)ts_tlv_acc_pending(&c->acc);
        size_t room = (pending < (size_t)TS_TLV_ACC_CAP) ? ((size_t)TS_TLV_ACC_CAP - pending) : 0U;
        int32_t n;
        if (room == 0U) {
            /* cannot happen with a correct accumulate parser (max TLV < capacity) */
            tsd_close_client(d, c, true);
            n = 0;
        } else {
            n = ts_io_tcp_recv(d->io, c->tcp, d->pool->ctl_rx, room);
        }
        if (n > 0) {
            ts_client_touch(c, now_ms);
            if (ts_tlv_acc_push(&c->acc, d->pool->ctl_rx, (size_t)n) != TS_OK) {
                tsd_close_client(d, c, true);
            } else {
                tsd_drain_tlvs(d, c, now_ms);
            }
        } else if (n == 0) {
            more = false;
        } else {
            tsd_close_client(d, c, false);   /* TS_IO_CLOSED / TS_IO_ERR */
            more = false;
        }
    }
}

static void tsd_accept(tsd_t *d, uint32_t now_ms)
{
    bool more = true;
    for (uint32_t i = 0U; (i < TSD_ACCEPT_PER_TICK) && more; i++) {
        ts_addr_t peer = { 0U, 0U };
        ts_io_handle_t h = ts_io_tcp_accept(d->io, &peer);
        if (h == TS_IO_INVALID) {
            more = false;
        } else if (ts_client_accept(&d->clients, h, &peer, now_ms) == NULL) {
            /* table full: refuse by closing without WELCOME (PROTOCOL.md 5) */
            TS_LOG(&d->log, now_ms, TS_LOG_WARN, TS_MOD_NET, (uint16_t)TSD_EV_REFUSED, peer.ip, (uint32_t)peer.port);
            ts_io_tcp_close(d->io, h);
        } else {
            /* PENDING until HELLO */
        }
    }
}

/* ---- UDP: NACK in, retransmit out ------------------------------------------ */

/* Send one admitted retransmission. ts_client_nack() already charged the
 * buckets; a re-pack or socket failure is a drop (the tokens stay spent, the
 * receiver asks again). */
static void tsd_retransmit(tsd_t *d, const ts_client_t *c, const ts_txq_slot_t *slot)
{
    size_t len = 0U;
    if (ts_txq_make_retransmit(slot, d->pkt_buf, sizeof(d->pkt_buf), &len) == TS_OK) {
        int32_t n = ts_io_udp_send(d->io, &c->udp, d->pkt_buf, len);
        if ((n > 0) && ((size_t)n == len)) {
            d->stats.retx_sent++;
            d->stats.bytes_sent += (uint32_t)len;
        }
    }
}

static void tsd_on_datagram(tsd_t *d, const ts_addr_t *from, const uint8_t *buf, size_t len, uint32_t now_ms)
{
    ts_client_t *c = ts_client_find(&d->clients, from);   /* ACTIVE, ip AND port */
    if (c != NULL) {
        ts_nack_t nack;
        ts_client_touch(c, now_ms);   /* any packet from the client keeps it alive */
        /* bounded parse: count > TS_NACK_MAX, bad magic or bad length is dropped whole */
        if (ts_nack_parse(buf, len, &nack) == TS_OK) {
            ts_retx_batch_t batch;
            /* lookup, per-tick cap and bucket charge per entry; too-old entries
             * request a TS_KEY_TOO_OLD keyframe (cooldown applies) */
            if (ts_client_nack(c, &nack, &d->txq, &d->global_bucket, d->cfg.retx_per_tick,
                               &d->keysched, now_ms, &batch) == TS_OK) {
                uint32_t count = ((uint32_t)nack.count <= TS_NACK_MAX) ? (uint32_t)nack.count : TS_NACK_MAX;
                d->stats.nacks_rx += count;
                d->stats.nack_too_old += (uint32_t)batch.too_old;
                ts_loss_on_nack(&d->loss, count);
                for (uint32_t i = 0U; (i < (uint32_t)batch.count) && (i < TS_NACK_MAX); i++) {
                    tsd_retransmit(d, c, batch.slot[i]);
                }
            }
        }
    }
}

static void tsd_udp_rx(tsd_t *d, uint32_t now_ms)
{
    bool more = true;
    for (uint32_t i = 0U; (i < TSD_UDP_RX_PER_TICK) && more; i++) {
        ts_addr_t from = { 0U, 0U };
        int32_t n = ts_io_udp_recv(d->io, &from, d->pool->rx_buf, sizeof(d->pool->rx_buf));
        if (n > 0) {
            tsd_on_datagram(d, &from, d->pool->rx_buf, (size_t)n, now_ms);
        } else {
            more = false;   /* nothing more now (0) or a socket error (retried next tick) */
        }
    }
}

/* ---- congestion control: client backlog ------------------------------------------ */

/*
 * Per tick: the time every ACTIVE client's cursor needs to drain at its rate.
 * A backlog above TS_CC_RESET_MS is stale video: the cursor jumps to the
 * newest keyframe when one is ahead of it (the viewer resumes on a decodable
 * frame instead of the ring overrunning the cursor mid-frame), otherwise a
 * keyframe is wanted so there will be one to jump to. The worst remaining
 * backlog feeds the congestion controller (standing-queue detection).
 */
static void tsd_backlog(tsd_t *d, uint32_t now_ms)
{
    bool any = false;
    uint32_t worst = 0U;
    for (uint32_t i = 0U; i < TS_MAX_CLIENTS; i++) {
        ts_client_t *c = &d->clients.c[i];
        if (c->state == TS_CL_ACTIVE) {
            uint32_t bl = ts_cc_backlog_ms(ts_txq_cursor_pending(&d->txq, &c->cursor), c->kbps);
            if (bl > TS_CC_RESET_MS) {
                uint32_t ahead = d->key_push_idx - c->cursor.next;   /* u32 serial distance */
                if (tsd_key_held(d) && (ahead > 0U) && (ahead < 0x80000000U)) {
                    c->cursor.skipped += ahead;
                    c->cursor.next = d->key_push_idx;
                    d->cc.resets++;
                    bl = ts_cc_backlog_ms(ts_txq_cursor_pending(&d->txq, &c->cursor), c->kbps);
                } else {
                    tsd_want_keyframe(d, now_ms);
                }
            }
            if (bl > worst) {
                worst = bl;
            }
            any = true;
        }
    }
    if (any) {
        ts_cc_on_backlog(&d->cc, worst);
    }
}

/* The rate the stream must fit: the slowest ACTIVE client, or its share of the global cap. */
static uint32_t tsd_cc_limit(const tsd_t *d)
{
    uint32_t limit = 0U;
    uint32_t n = (uint32_t)ts_clients_active(&d->clients);
    for (uint32_t i = 0U; i < TS_MAX_CLIENTS; i++) {
        const ts_client_t *c = &d->clients.c[i];
        if ((c->state == TS_CL_ACTIVE) && (c->kbps != 0U) && ((limit == 0U) || (c->kbps < limit))) {
            limit = c->kbps;
        }
    }
    if ((n > 0U) && (d->cfg.kbps_global != 0U)) {
        uint32_t share = d->cfg.kbps_global / n;
        if ((limit == 0U) || (share < limit)) {
            limit = share;
        }
    }
    return limit;
}

/* ---- capture -> encode -> ring ---------------------------------------------- */

static void tsd_encode_push(tsd_t *d, uint16_t frame_seq, uint32_t now_ms)
{
    ts_frame_info_t info;
    uint16_t changed = (uint16_t)TS_TILES;
    ts_key_reason_t why;
    (void)memset(&info, 0, sizeof(info));
    if (d->enc.have_ref) {
        /* the frame as it will be reconstructed at the current quality (PROTOCOL.md 6.6) */
        changed = ts_codec_changed_tiles_q(d->frame, d->enc.ref, ts_enc_quality(&d->enc), NULL);
    }
    why = ts_keysched_decide(&d->keysched, now_ms, changed);
    if (why != TS_KEY_NONE) {
        ts_enc_force_keyframe(&d->enc);
    }
    if (ts_enc_encode(&d->enc, d->frame, frame_seq, g_tsd_enc_buf, (size_t)g_tsd_enc_max, &info) != TS_OK) {
        TS_LOG(&d->log, now_ms, TS_LOG_ERR, TS_MOD_DAEMON, (uint16_t)TSD_EV_ENCODE_ERR, (uint32_t)frame_seq, 0U);
    } else {
        uint8_t frags = 0U;
        uint8_t flags = info.keyframe ? (uint8_t)TS_F_KEYFRAME : 0U;
        uint32_t first_idx = d->txq.pushed;
        if (info.keyframe) {
            ts_keysched_sent(&d->keysched, now_ms);
            if (ts_clients_active(&d->clients) == 0U) {
                /* nobody received it: the cooldown protects viewers' bandwidth, so a
                 * keyframe pushed with no viewer does not delay the first viewer's one */
                d->keysched.last_key_ms = now_ms - d->keysched.cooldown_ms;
            }
            d->stats.keyframes_sent++;
        }
        if (ts_txq_push_frame(&d->txq, frame_seq, flags, (uint16_t)(now_ms & 0xFFFFU),
                              g_tsd_enc_buf, (size_t)info.enc_len, &frags) == TS_OK) {
            d->stats.frames_sent++;
            ts_cc_on_frame(&d->cc, info.enc_len + ((uint32_t)frags * TS_HDR_LEN), info.keyframe);
            if (info.keyframe) {
                d->key_push_idx = first_idx;
                d->have_key_push = true;
            }
        }
    }
}

static ts_err_t tsd_capture(tsd_t *d, uint32_t now_ms)
{
    ts_err_t rc = TS_OK;
    ts_err_t r = ts_cap_read(&d->cap, d->frame, now_ms);
    if (r == TS_OK) {
        /* every captured frame consumes one frame_seq (synth: frame_seq == pattern index) */
        uint16_t seq = d->frame_seq_next;
        d->frame_seq_next = (uint16_t)(d->frame_seq_next + 1U);
        d->stats.frames_captured++;
        tsd_encode_push(d, seq, now_ms);
    } else if (r == TS_E_AGAIN) {
        rc = TS_OK;
    } else {
        /* pipe EOF, read error, device gone: fatal for the daemon (systemd restarts it) */
        TS_LOG(&d->log, now_ms, TS_LOG_ERR, TS_MOD_CAPTURE, (uint16_t)TSD_EV_CAPTURE_ERR, (uint32_t)r, 0U);
        rc = TS_E_IO;
    }
    return rc;
}

/* ---- fresh packets ------------------------------------------------------------ */

static void tsd_send_fresh(tsd_t *d)
{
    bool progress = true;
    bool blocked = false;
    const uint32_t max_passes = ((uint32_t)TS_RING_SLOTS * (uint32_t)TS_MAX_CLIENTS) + 1U;
    for (uint32_t pass = 0U; progress && !blocked && (pass < max_passes); pass++) {
        progress = false;
        /* one packet per client per pass: clients share the global cap fairly */
        for (uint32_t i = 0U; (i < TS_MAX_CLIENTS) && !blocked; i++) {
            ts_client_t *c = &d->clients.c[i];
            /* bucket-paced: charges the client bucket and the global cap, advances the cursor */
            const ts_txq_slot_t *slot = ts_client_next_fresh(c, &d->txq, &d->global_bucket);
            if (slot != NULL) {
                uint32_t len = (uint32_t)slot->len;
                int32_t n = ts_io_udp_send(d->io, &c->udp, slot->pkt, (size_t)len);
                if ((n > 0) && ((uint32_t)n == len)) {
                    d->stats.pkts_sent++;
                    d->stats.bytes_sent += len;
                    ts_loss_on_sent(&d->loss, 1U);
                    progress = true;
                } else {
                    /* socket full (0) or unroutable (< 0): the packet is dropped like
                     * on the wire and the receiver NACKs it; it is not counted as sent */
                    if (n == 0) {
                        blocked = true;   /* stop for this tick */
                    } else {
                        progress = true;
                    }
                }
            }
        }
    }
}

/* ---- lifecycle ---------------------------------------------------------------- */

static void tsd_reap(tsd_t *d)
{
    for (uint32_t i = 0U; i < TS_MAX_CLIENTS; i++) {
        ts_client_t *c = &d->clients.c[i];
        if (c->state == TS_CL_CLOSING) {
            if (c->tcp != TS_IO_INVALID) {
                ts_io_tcp_close(d->io, c->tcp);
                c->tcp = TS_IO_INVALID;
            }
            ts_client_free(&d->clients, c);
        }
    }
}

static void tsd_refresh_stats(tsd_t *d, uint32_t now_ms)
{
    d->stats.uptime_ms = now_ms - d->start_ms;
    d->stats.client_count = ts_clients_active(&d->clients);
}

static void tsd_send_stats(tsd_t *d, uint32_t now_ms)
{
    ts_tlv_stats_t st;
    size_t len = 0U;
    (void)memset(&st, 0, sizeof(st));
    tsd_refresh_stats(d, now_ms);
    st.g = d->stats;
    st.g.client_count = ts_clients_stats(&d->clients, now_ms, st.c);
    if (st.g.client_count > (uint8_t)TS_MAX_CLIENTS) {
        st.g.client_count = (uint8_t)TS_MAX_CLIENTS;
    }
    if (ts_tlv_pack_stats(&st, d->ctl_buf, sizeof(d->ctl_buf), &len) == TS_OK) {
        for (uint32_t i = 0U; i < TS_MAX_CLIENTS; i++) {
            ts_client_t *c = &d->clients.c[i];
            if (c->state == TS_CL_ACTIVE) {
                tsd_ctl_send_optional(d, c, d->ctl_buf, len);
                c->last_stats_ms = now_ms;
            }
        }
    }
}

/* ---- public API -------------------------------------------------------------- */

void tsd_cfg_default(tsd_cfg_t *cfg)
{
    if (cfg != NULL) {
        (void)memset(cfg, 0, sizeof(*cfg));
        cfg->udp_port = (uint16_t)TS_DEFAULT_UDP_PORT;
        cfg->tcp_port = (uint16_t)TS_DEFAULT_TCP_PORT;
        cfg->fps = TS_DEFAULT_FPS;
        cfg->kbps_global = TS_GLOBAL_KBPS;
        cfg->kbps_client = TS_DEFAULT_KBPS;
        cfg->keyframe_period_ms = TS_KEYFRAME_PERIOD_MS;
        cfg->client_timeout_ms = TS_CLIENT_TIMEOUT_MS;
        cfg->retx_per_tick = (uint8_t)TS_RETX_PER_TICK;
        cfg->loss_adapt = true;
        cfg->capture.kind = TS_CAP_SYNTH;
        cfg->capture.fd = -1;
        cfg->capture.fps = TS_DEFAULT_FPS;
    }
}

/* Fill the zero ("use the default") fields of a configuration. */
static void tsd_cfg_resolve(tsd_cfg_t *c, uint32_t now_ms)
{
    if (c->fps == 0U) {
        c->fps = TS_DEFAULT_FPS;
    }
    if (c->kbps_global == 0U) {
        c->kbps_global = TS_GLOBAL_KBPS;
    }
    if (c->kbps_client == 0U) {
        c->kbps_client = TS_DEFAULT_KBPS;
    }
    if (c->keyframe_period_ms == 0U) {
        c->keyframe_period_ms = TS_KEYFRAME_PERIOD_MS;
    }
    if (c->client_timeout_ms == 0U) {
        c->client_timeout_ms = TS_CLIENT_TIMEOUT_MS;
    }
    if (c->retx_per_tick == 0U) {
        c->retx_per_tick = (uint8_t)TS_RETX_PER_TICK;
    }
    if (c->stream_id == 0U) {
        c->stream_id = tsd_mix32(now_ms);
        if (c->stream_id == 0U) {
            c->stream_id = TSD_STREAM_ID_SALT;
        }
    }
    if (c->capture.fps == 0U) {
        c->capture.fps = c->fps;
    }
    /* synth pattern index == frame_seq, so the receiver can regenerate every frame */
    c->capture.idx_start = (uint32_t)c->seq_start;
}

tsd_t *tsd_create(const tsd_cfg_t *cfg, ts_io_t *io, struct ts_pool *pool)
{
    tsd_t *res = NULL;
    if ((cfg != NULL) && (io != NULL) && (io->vt != NULL) && (pool != NULL) &&
        (cfg->fps <= TSD_MAX_FPS) && (cfg->capture.fps <= TSD_MAX_FPS)) {
        tsd_t *d = &pool->daemon;
        uint32_t now = ts_io_now_ms(io);
        if (pool->magic != TS_POOL_MAGIC) {
            ts_pool_init(pool);
        }
        (void)memset(d, 0, sizeof(*d));
        d->cfg = *cfg;
        tsd_cfg_resolve(&d->cfg, now);
        d->state = TSD_INIT;
        d->io = io;
        d->pool = pool;
        d->start_ms = now;
        d->last_tick_ms = now;
        d->last_stats_ms = now;
        d->last_loss_ms = now;
        d->frame_seq_next = d->cfg.seq_start;
        ts_log_init(&d->log, TS_LOG_INFO);
        ts_enc_init(&d->enc);
        ts_txq_init(&d->txq, d->cfg.seq_start);
        ts_clients_init(&d->clients);
        ts_bucket_init(&d->global_bucket, d->cfg.kbps_global, TSD_GLOBAL_BURST, now);
        ts_loss_init(&d->loss, now);
        ts_cc_init(&d->cc, now);
        ts_keysched_init(&d->keysched, now);
        d->keysched.period_ms = d->cfg.keyframe_period_ms;
        ts_keysched_force(&d->keysched);   /* the first frame is a keyframe */
        if (ts_cap_open(&d->cap, &d->cfg.capture) == TS_OK) {
            d->state = TSD_RUNNING;
            ts_pool_seal(pool);   /* no allocation from here on */
            res = d;
        } else {
            d->state = TSD_STOPPED;
        }
    }
    return res;
}

ts_err_t tsd_tick(tsd_t *d, uint32_t now_ms)
{
    ts_err_t rc = TS_E_INVAL;
    if (d != NULL) {
        if (d->state != TSD_RUNNING) {
            rc = TS_E_STATE;
        } else {
            d->last_tick_ms = now_ms;

            /* 1. budgets */
            ts_bucket_refill(&d->global_bucket, now_ms);
            ts_clients_tick(&d->clients, now_ms);

            /* 2-3. control channel */
            tsd_accept(d, now_ms);
            for (uint32_t i = 0U; i < TS_MAX_CLIENTS; i++) {
                ts_client_t *c = &d->clients.c[i];
                if (tsd_client_live(c) && (c->tcp != TS_IO_INVALID)) {
                    tsd_client_rx(d, c, now_ms);
                }
            }

            /* 4. NACKs */
            tsd_udp_rx(d, now_ms);

            /* 5. client backlogs, loss adaptation and congestion control -> quality step */
            tsd_backlog(d, now_ms);
            if (d->cfg.loss_adapt) {
                uint8_t q = ts_loss_update(&d->loss, now_ms);
                uint8_t qc = ts_cc_update(&d->cc, now_ms, tsd_cc_limit(d));
                if (qc > q) {
                    q = qc;   /* the coarser of the two wins */
                }
                if (q != ts_enc_quality(&d->enc)) {
                    (void)ts_enc_set_quality(&d->enc, q);
                }
                d->last_loss_ms = now_ms;
            }

            /* 6. capture -> encode -> ring */
            rc = tsd_capture(d, now_ms);

            /* 7. fresh packets */
            tsd_send_fresh(d);

            /* 8. lifecycle (a connection that never says HELLO must not hold a slot either) */
            (void)ts_clients_expire(&d->clients, now_ms, d->cfg.client_timeout_ms);
            for (uint32_t i = 0U; i < TS_MAX_CLIENTS; i++) {
                ts_client_t *c = &d->clients.c[i];
                if ((c->state == TS_CL_PENDING) &&
                    ((uint32_t)(now_ms - c->last_seen_ms) > d->cfg.client_timeout_ms)) {
                    ts_client_close(&d->clients, c);
                }
            }
            tsd_reap(d);

            /* 9. STATS */
            if (tsd_time_reached(now_ms, d->last_stats_ms + TS_STATS_PERIOD_MS)) {
                d->last_stats_ms = now_ms;
                tsd_send_stats(d, now_ms);
                tsd_reap(d);   /* a torn STATS write closed someone */
            }
            tsd_refresh_stats(d, now_ms);
        }
    }
    return rc;
}

const ts_stats_global_t *tsd_stats(const tsd_t *d)
{
    return (d != NULL) ? &d->stats : NULL;
}

void tsd_stop(tsd_t *d)
{
    if ((d != NULL) && (d->state != TSD_STOPPED)) {
        size_t len = 0U;
        bool have_bye = (ts_tlv_pack_bye(d->ctl_buf, sizeof(d->ctl_buf), &len) == TS_OK);
        for (uint32_t i = 0U; i < TS_MAX_CLIENTS; i++) {
            ts_client_t *c = &d->clients.c[i];
            if (c->state != TS_CL_FREE) {
                if ((c->state == TS_CL_ACTIVE) && have_bye && (c->tcp != TS_IO_INVALID)) {
                    (void)tsd_ctl_write(d, c, d->ctl_buf, len);   /* best effort */
                }
                ts_client_close(&d->clients, c);
            }
        }
        tsd_reap(d);
        ts_cap_close(&d->cap);
        d->stats.client_count = 0U;
        d->state = TSD_STOPPED;
    }
}

/* Distance from `now` to `due` in ms, 0 when `due` is already past. */
static uint32_t tsd_dist(uint32_t due, uint32_t now)
{
    uint32_t dist = due - now;
    return (dist >= 0x80000000U) ? 0U : dist;
}

uint32_t tsd_next_due_ms(const tsd_t *d)
{
    uint32_t due = 0U;
    if (d != NULL) {
        uint32_t now = d->last_tick_ms;
        uint32_t best = tsd_dist(d->last_stats_ms + TS_STATS_PERIOD_MS, now);
        if (d->state != TSD_RUNNING) {
            best = TS_STATS_PERIOD_MS;
        } else {
            uint32_t frame = TSD_POLL_MS;   /* fd sources (pipe, v4l2): poll */
            if ((d->cap.cfg.kind == TS_CAP_SYNTH) || (d->cap.cfg.kind == TS_CAP_FILE)) {
                frame = (d->cap.frame_idx == 0U) ? 0U : tsd_dist(d->cap.next_ms, now);
            }
            if (frame < best) {
                best = frame;
            }
            for (uint32_t i = 0U; i < TS_MAX_CLIENTS; i++) {
                const ts_client_t *c = &d->clients.c[i];
                if ((c->state == TS_CL_ACTIVE) && (ts_txq_cursor_pending(&d->txq, &c->cursor) > 0U)) {
                    uint32_t w = ts_bucket_wait_ms(&c->bucket, TS_PKT_MAX);
                    w = (w == 0U) ? 1U : w;   /* bucket-paced: come back as soon as tokens exist */
                    if (w < best) {
                        best = w;
                    }
                }
            }
        }
        due = now + best;
    }
    return due;
}
