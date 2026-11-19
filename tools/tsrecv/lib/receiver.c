/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tools/tsrecv/lib/receiver.c
 * Receiver core, public API (include/ts_receiver.h): lifecycle, the tick
 * orchestration and the TCP control client
 *   DISCONNECTED -> (tcp_connect, HELLO queued) CONNECTING -> (HELLO flushed)
 *   HELLO_SENT -> (WELCOME) STREAMING -> (BYE flushed) CLOSED.
 * Control bytes are queued in r->x.tx_buf and flushed with partial-send
 * handling; inbound bytes go through the TLV accumulate parser, so split and
 * glued TLVs are handled (PROTOCOL.md section 5). A protocol error or a
 * closed connection reconnects after cfg.reconnect_ms.
 * Heap: only tsr_create() allocates; tsr_tick() never does.
 */
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include "tsr_internal.h"

static void ctl_close(tsr_t *r, uint32_t now_ms, tsr_state_t next);
static ts_err_t ctl_queue(tsr_t *r, const uint8_t *buf, size_t len);
static void ctl_connect(tsr_t *r, uint32_t now_ms);
static void ctl_flush(tsr_t *r, uint32_t now_ms);
static void ctl_recv(tsr_t *r, uint32_t now_ms);
static void ctl_handle(tsr_t *r, const ts_tlv_t *t, uint32_t now_ms);
static void ctl_service(tsr_t *r, uint32_t now_ms);
static void cfg_normalise(tsr_cfg_t *c);

/* ---- configuration ------------------------------------------------------------ */
void tsr_cfg_default(tsr_cfg_t *cfg)
{
    if (cfg != NULL) {
        (void)memset(cfg, 0, sizeof(*cfg));
        cfg->server.ip = 0x7F000001U;
        cfg->server.port = (uint16_t)TS_DEFAULT_TCP_PORT;
        cfg->udp_port = (uint16_t)(TS_DEFAULT_UDP_PORT + 10U);
        cfg->nack_delay_ms = TS_NACK_DELAY_MS;
        cfg->nack_retry_ms = TS_NACK_RETRY_MS;
        cfg->reconnect_ms = TSR_RECONNECT_DEF_MS;
        cfg->max_nack_per_tick = (uint8_t)TS_NACK_MAX;
        cfg->late_policy = TSR_LATE_DROP;
    }
}

static void cfg_normalise(tsr_cfg_t *c)
{
    if (c->nack_delay_ms == 0U) {
        c->nack_delay_ms = TS_NACK_DELAY_MS;
    }
    if (c->nack_retry_ms == 0U) {
        c->nack_retry_ms = TS_NACK_RETRY_MS;
    }
    if (c->reconnect_ms == 0U) {
        c->reconnect_ms = TSR_RECONNECT_DEF_MS;
    }
    if (c->max_nack_per_tick == 0U) {
        c->max_nack_per_tick = (uint8_t)TS_NACK_MAX;
    }
    if (c->late_policy != TSR_LATE_DECODE) {
        c->late_policy = TSR_LATE_DROP;
    }
}

/* ---- lifecycle ------------------------------------------------------------------ */
ts_err_t tsr_init(tsr_t *r, const tsr_cfg_t *cfg, ts_io_t *io)
{
    ts_err_t rc = TS_E_INVAL;
    if ((r != NULL) && (cfg != NULL) && (io != NULL) && (io->vt != NULL)) {
        (void)memset(r, 0, sizeof(*r));
        r->cfg = *cfg;
        cfg_normalise(&r->cfg);
        r->io = io;
        r->tcp = TS_IO_INVALID;
        r->state = TSR_DISCONNECTED;
        ts_tlv_acc_init(&r->acc);
        ts_dec_init(&r->dec);
        ts_log_init(&r->log, TS_LOG_INFO);
        r->x.rtt_ms = 0U;
        r->m.state = (uint8_t)TSR_DISCONNECTED;
        rc = TS_OK;
    }
    return rc;
}

tsr_t *tsr_create(const tsr_cfg_t *cfg, ts_io_t *io)
{
    /* the one allowed allocation of the receiver tool (see cppcheck-suppressions) */
    tsr_t *r = (tsr_t *)calloc(1U, sizeof(tsr_t));
    if (r != NULL) {
        if (tsr_init(r, cfg, io) != TS_OK) {
            free(r);
            r = NULL;
        }
    }
    return r;
}

void tsr_destroy(tsr_t *r)
{
    if (r != NULL) {
        if (tsr_i_connected(r)) {
            uint8_t buf[TS_TLV_HDR_LEN];
            size_t n = 0U;
            /* best effort: tell the daemon, then close */
            if (ts_tlv_pack_bye(buf, sizeof(buf), &n) == TS_OK) {
                (void)ts_io_tcp_send(r->io, r->tcp, buf, n);
            }
        }
        if (r->tcp != TS_IO_INVALID) {
            ts_io_tcp_close(r->io, r->tcp);
            r->tcp = TS_IO_INVALID;
        }
        free(r);
    }
}

/* ---- control channel --------------------------------------------------------------- */
bool tsr_i_connected(const tsr_t *r)
{
    return (r->tcp != TS_IO_INVALID) && (!r->x.bye_pending) &&
           ((r->state == TSR_CONNECTING) || (r->state == TSR_HELLO_SENT) || (r->state == TSR_STREAMING));
}

static void ctl_close(tsr_t *r, uint32_t now_ms, tsr_state_t next)
{
    if (r->tcp != TS_IO_INVALID) {
        ts_io_tcp_close(r->io, r->tcp);
        r->tcp = TS_IO_INVALID;
        TS_LOG(&r->log, now_ms, TS_LOG_INFO, TS_MOD_RECEIVER, (uint16_t)TSR_EV_DISCONNECT,
               (uint32_t)next, 0U);
    }
    ts_tlv_acc_init(&r->acc);
    r->x.tx_len = 0U;
    r->x.bye_pending = false;
    r->state = next;
    r->state_ms = now_ms;
}

static ts_err_t ctl_queue(tsr_t *r, const uint8_t *buf, size_t len)
{
    ts_err_t rc = TS_E_FULL;
    size_t used = (size_t)r->x.tx_len;
    if ((len <= (size_t)TSR_TX_CAP) && (used <= ((size_t)TSR_TX_CAP - len))) {
        (void)memcpy(&r->x.tx_buf[used], buf, len);
        r->x.tx_len = (uint16_t)(used + len);
        rc = TS_OK;
    }
    return rc;
}

static void ctl_connect(tsr_t *r, uint32_t now_ms)
{
    if ((now_ms - r->state_ms) >= r->cfg.reconnect_ms) {
        ts_io_handle_t h = ts_io_tcp_connect(r->io, &r->cfg.server);
        r->state_ms = now_ms;
        if (h != TS_IO_INVALID) {
            ts_tlv_hello_t hello;
            uint8_t buf[TS_TLV_HDR_LEN + TS_TLV_HELLO_LEN];
            size_t n = 0U;
            if (r->x.ever_connected) {
                r->m.reconnects++;
            }
            r->x.ever_connected = true;
            r->tcp = h;
            ts_tlv_acc_init(&r->acc);
            r->x.tx_len = 0U;
            r->x.bye_pending = false;
            r->state = TSR_CONNECTING;
            hello.udp_port = r->cfg.udp_port;
            hello.kbps = r->cfg.kbps;
            if ((ts_tlv_pack_hello(&hello, buf, sizeof(buf), &n) != TS_OK) ||
                (ctl_queue(r, buf, n) != TS_OK)) {
                r->m.ctrl_errors++;
                ctl_close(r, now_ms, TSR_DISCONNECTED);
            }
            TS_LOG(&r->log, now_ms, TS_LOG_INFO, TS_MOD_RECEIVER, (uint16_t)TSR_EV_CONNECT,
                   r->cfg.server.ip, (uint32_t)r->cfg.server.port);
        }
    }
}

static void ctl_flush(tsr_t *r, uint32_t now_ms)
{
    bool more = (r->tcp != TS_IO_INVALID) && (r->x.tx_len > 0U);
    while (more) {
        int32_t n = ts_io_tcp_send(r->io, r->tcp, r->x.tx_buf, (size_t)r->x.tx_len);
        if (n > 0) {
            uint32_t sent = (uint32_t)n;
            uint32_t left = (sent < (uint32_t)r->x.tx_len) ? ((uint32_t)r->x.tx_len - sent) : 0U;
            if (left > 0U) {
                (void)memmove(r->x.tx_buf, &r->x.tx_buf[sent], (size_t)left);
            }
            r->x.tx_len = (uint16_t)left;
            more = (left > 0U);
        } else if (n == 0) {
            more = false;   /* would block: retry next tick */
        } else {
            r->m.ctrl_errors++;
            ctl_close(r, now_ms, TSR_DISCONNECTED);
            more = false;
        }
    }
    if ((r->tcp != TS_IO_INVALID) && (r->x.tx_len == 0U)) {
        if (r->x.bye_pending) {
            ctl_close(r, now_ms, TSR_CLOSED);
        } else if (r->state == TSR_CONNECTING) {
            r->state = TSR_HELLO_SENT;
        } else {
            /* nothing to do */
        }
    }
}

static void ctl_handle(tsr_t *r, const ts_tlv_t *t, uint32_t now_ms)
{
    switch (t->type) {
    case (uint8_t)TS_TLV_WELCOME: {
        ts_tlv_welcome_t w;
        if (ts_tlv_parse_welcome(t, &w) == TS_OK) {
            if (r->have_welcome && (w.stream_id != r->welcome.stream_id)) {
                /* daemon restarted: sequence numbers and the reference are meaningless now */
                tsr_i_stream_reset(r, now_ms);
            }
            r->welcome = w;
            r->have_welcome = true;
            r->m.stream_id = w.stream_id;
            if ((r->state == TSR_CONNECTING) || (r->state == TSR_HELLO_SENT)) {
                r->state = TSR_STREAMING;
                r->state_ms = now_ms;
                r->x.last_udp_tx_ms = now_ms;
            }
            TS_LOG(&r->log, now_ms, TS_LOG_INFO, TS_MOD_RECEIVER, (uint16_t)TSR_EV_WELCOME,
                   w.stream_id, (uint32_t)w.ring_slots);
        } else {
            r->m.ctrl_errors++;
        }
        break;
    }
    case (uint8_t)TS_TLV_STATS: {
        if (ts_tlv_parse_stats(t, &r->m.last_stats) == TS_OK) {
            r->m.stats_rx++;
        } else {
            r->m.ctrl_errors++;
        }
        break;
    }
    case (uint8_t)TS_TLV_BYE:
        /* daemon is going away: reconnect after the back-off */
        ctl_close(r, now_ms, TSR_DISCONNECTED);
        break;
    case (uint8_t)TS_TLV_HELLO:
    case (uint8_t)TS_TLV_KEYFRAME_REQ:
    case (uint8_t)TS_TLV_SET_RATE:
        /* receiver -> daemon types arriving the wrong way: count, ignore */
        r->m.ctrl_errors++;
        break;
    default:
        /* unknown type: protocol error (PROTOCOL.md 5), the receiver reconnects */
        r->m.ctrl_errors++;
        TS_LOG(&r->log, now_ms, TS_LOG_WARN, TS_MOD_RECEIVER, (uint16_t)TSR_EV_CTRL_ERR,
               (uint32_t)t->type, (uint32_t)t->len);
        ctl_close(r, now_ms, TSR_DISCONNECTED);
        break;
    }
}

static void ctl_recv(tsr_t *r, uint32_t now_ms)
{
    uint32_t rounds = 0U;
    bool more = (r->tcp != TS_IO_INVALID);
    while (more && (rounds < 16U)) {
        int32_t n = ts_io_tcp_recv(r->io, r->tcp, r->ctl_buf, sizeof(r->ctl_buf));
        rounds++;
        if (n > 0) {
            if (ts_tlv_acc_push(&r->acc, r->ctl_buf, (size_t)(uint32_t)n) != TS_OK) {
                r->m.ctrl_errors++;
                ctl_close(r, now_ms, TSR_DISCONNECTED);
                more = false;
            } else {
                bool tlvs = true;
                while (tlvs) {
                    ts_tlv_t t;
                    ts_err_t rc = ts_tlv_acc_next(&r->acc, &t);
                    if (rc == TS_OK) {
                        r->x.last_alive_ms = now_ms;
                        ctl_handle(r, &t, now_ms);
                        tlvs = (r->tcp != TS_IO_INVALID);
                    } else if (rc == TS_E_AGAIN) {
                        tlvs = false;
                    } else {
                        r->m.ctrl_errors++;
                        ctl_close(r, now_ms, TSR_DISCONNECTED);
                        tlvs = false;
                    }
                }
                more = (r->tcp != TS_IO_INVALID);
            }
        } else if (n == 0) {
            more = false;
        } else {
            /* TS_IO_CLOSED or TS_IO_ERR */
            ctl_close(r, now_ms, TSR_DISCONNECTED);
            more = false;
        }
    }
}

static void ctl_service(tsr_t *r, uint32_t now_ms)
{
    if (r->state == TSR_DISCONNECTED) {
        ctl_connect(r, now_ms);
    }
    ctl_flush(r, now_ms);
    if (r->tcp != TS_IO_INVALID) {
        ctl_recv(r, now_ms);
    }
    if (((r->state == TSR_CONNECTING) || (r->state == TSR_HELLO_SENT)) &&
        ((now_ms - r->state_ms) >= TSR_WELCOME_TIMEOUT_MS)) {
        r->m.ctrl_errors++;
        ctl_close(r, now_ms, TSR_DISCONNECTED);
    } else if ((r->state == TSR_STREAMING) && ((now_ms - r->x.last_alive_ms) >= TSR_SILENCE_MS)) {
        /* neither data nor STATS: the daemon dropped this client (e.g. after a cable
         * pull longer than its client timeout) -> reconnect right away */
        ctl_close(r, now_ms, TSR_DISCONNECTED);
        r->state_ms = now_ms - r->cfg.reconnect_ms;
    } else {
        /* healthy */
    }
}

/* ---- tick ------------------------------------------------------------------------- */
ts_err_t tsr_tick(tsr_t *r, uint32_t now_ms)
{
    ts_err_t rc = TS_E_INVAL;
    if ((r != NULL) && (r->io != NULL)) {
        if (!r->x.started) {
            r->x.started = true;
            r->start_ms = now_ms;
            r->window_start_ms = now_ms;
            /* first connection attempt is due immediately */
            r->state_ms = now_ms - r->cfg.reconnect_ms;
        }
        if (r->state != TSR_CLOSED) {
            ctl_service(r, now_ms);
            tsr_i_udp_drain(r, now_ms);
            tsr_i_nack_tick(r, now_ms);
            tsr_i_decode_pass(r, now_ms);
            tsr_i_keepalive(r, now_ms);
            ctl_flush(r, now_ms);
        }
        tsr_i_metrics_tick(r, now_ms);
        rc = TS_OK;
    }
    return rc;
}

/* ---- accessors ---------------------------------------------------------------------- */
const uint8_t *tsr_frame(const tsr_t *r)
{
    return ((r != NULL) && r->have_decoded) ? r->frame : NULL;
}

const tsr_metrics_t *tsr_metrics(const tsr_t *r)
{
    return (r != NULL) ? &r->m : NULL;
}

tsr_state_t tsr_state(const tsr_t *r)
{
    return (r != NULL) ? r->state : TSR_CLOSED;
}

const ts_tlv_welcome_t *tsr_welcome(const tsr_t *r)
{
    return ((r != NULL) && r->have_welcome) ? &r->welcome : NULL;
}

/* ---- outbound control messages --------------------------------------------------------- */
ts_err_t tsr_send_keyframe_req(tsr_t *r)
{
    ts_err_t rc = TS_E_INVAL;
    if (r != NULL) {
        if (!tsr_i_connected(r)) {
            rc = TS_E_STATE;
        } else {
            uint8_t buf[TS_TLV_HDR_LEN];
            size_t n = 0U;
            rc = ts_tlv_pack_keyframe_req(buf, sizeof(buf), &n);
            if (rc == TS_OK) {
                rc = ctl_queue(r, buf, n);
            }
            if (rc == TS_OK) {
                r->m.keyframe_reqs++;
            }
        }
    }
    return rc;
}

ts_err_t tsr_send_set_rate(tsr_t *r, uint32_t kbps)
{
    ts_err_t rc = TS_E_INVAL;
    if (r != NULL) {
        if (!tsr_i_connected(r)) {
            rc = TS_E_STATE;
        } else {
            ts_tlv_set_rate_t v;
            uint8_t buf[TS_TLV_HDR_LEN + TS_TLV_SET_RATE_LEN];
            size_t n = 0U;
            v.kbps = kbps;
            rc = ts_tlv_pack_set_rate(&v, buf, sizeof(buf), &n);
            if (rc == TS_OK) {
                rc = ctl_queue(r, buf, n);
            }
            if (rc == TS_OK) {
                r->cfg.kbps = kbps;   /* re-announced in HELLO after a reconnect */
            }
        }
    }
    return rc;
}

ts_err_t tsr_send_bye(tsr_t *r)
{
    ts_err_t rc = TS_E_INVAL;
    if (r != NULL) {
        if (!tsr_i_connected(r)) {
            rc = TS_E_STATE;
        } else {
            uint8_t buf[TS_TLV_HDR_LEN];
            size_t n = 0U;
            rc = ts_tlv_pack_bye(buf, sizeof(buf), &n);
            if (rc == TS_OK) {
                rc = ctl_queue(r, buf, n);
            }
            if (rc == TS_OK) {
                r->x.bye_pending = true;   /* closed by ctl_flush once written */
            }
        }
    }
    return rc;
}

/* ---- ground truth ------------------------------------------------------------------------ */
void tsr_set_truth(tsr_t *r, const uint8_t *truth)
{
    if ((r != NULL) && (truth != NULL)) {
        (void)memcpy(r->truth, truth, TS_FRAME_BYTES);
        r->have_truth = true;
    }
}

void tsr_set_truth_fn(tsr_t *r, tsr_truth_fn_t fn, void *ctx)
{
    if (r != NULL) {
        r->x.truth_fn = fn;
        r->x.truth_ctx = ctx;
    }
}

void tsr_reset_metrics(tsr_t *r)
{
    if (r != NULL) {
        uint32_t now_ms = ts_io_now_ms(r->io);
        uint32_t stream_id = r->m.stream_id;
        ts_tlv_stats_t last = r->m.last_stats;
        (void)memset(&r->m, 0, sizeof(r->m));
        r->m.stream_id = stream_id;
        r->m.last_stats = last;
        r->m.state = (uint8_t)r->state;
        r->m.rtt_ms = r->x.rtt_ms;
        r->window_start_ms = now_ms;
        r->window_frames = 0U;
        r->window_bytes = 0U;
        r->x.latency_n = 0U;
        r->x.latency_sum_ms = 0U;
        r->x.jitter_x16 = 0U;
        r->x.in_freeze = false;
        if (r->have_decoded) {
            r->last_decoded_ms = now_ms;
        }
    }
}
