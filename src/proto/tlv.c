/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- src/proto/tlv.c
 * TCP control channel (PROTOCOL.md section 5): generic and typed TLV packers,
 * typed parsers with exact length checks, and the accumulate parser that turns
 * arbitrary recv() chunks into complete TLVs (split and glued TLVs).
 *
 * Accumulator invariants: pos <= len <= TS_TLV_ACC_CAP. Unconsumed bytes are
 * buf[pos..len). Compaction happens only inside ts_tlv_acc_push (so a value
 * pointer returned by ts_tlv_acc_next stays valid until the next acc call),
 * hence the free space for the next push is always
 * TS_TLV_ACC_CAP - ts_tlv_acc_pending(a).
 */
#include <stddef.h>
#include <string.h>
#include "ts_proto.h"

#define TLV_OFF_TYPE   0U
#define TLV_OFF_FLAGS  1U
#define TLV_OFF_LEN    2U

/* STATS global block offsets (PROTOCOL.md 5.1). */
#define SG_UPTIME      0U
#define SG_CAPTURED    4U
#define SG_SENT        8U
#define SG_KEYFRAMES   12U
#define SG_PKTS        16U
#define SG_BYTES       20U
#define SG_NACKS       24U
#define SG_RETX        28U
#define SG_TOO_OLD     32U
#define SG_CTRL_ERR    36U
#define SG_CLIENTS     40U

/* STATS per-client block offsets. */
#define SC_ID          0U
#define SC_STATE       1U
#define SC_PORT        2U
#define SC_KBPS        4U
#define SC_PKTS        8U
#define SC_BYTES       12U
#define SC_NACKS       16U
#define SC_RETX        20U
#define SC_RETX_DROP   24U
#define SC_KEYREQ      28U
#define SC_LAST_SEEN   32U

/* ---- packers ----------------------------------------------------------------- */
ts_err_t ts_tlv_pack(uint8_t type, uint8_t flags, const uint8_t *value, uint16_t len,
                     uint8_t *out, size_t out_cap, size_t *out_len)
{
    ts_err_t err = TS_OK;

    if ((out == NULL) || (out_len == NULL) || ((value == NULL) && (len != 0U))) {
        err = TS_E_INVAL;
    } else if ((uint32_t)len > TS_TLV_MAX_LEN) {
        err = TS_E_TOOLONG;
    } else if (out_cap < ((size_t)TS_TLV_HDR_LEN + (size_t)len)) {
        err = TS_E_NOMEM;
    } else {
        out[TLV_OFF_TYPE] = type;
        out[TLV_OFF_FLAGS] = flags;
        ts_wr16(&out[TLV_OFF_LEN], len);
        if (len != 0U) {
            /* memmove: callers may build the value in place behind the header */
            (void)memmove(&out[TS_TLV_HDR_LEN], value, (size_t)len);
        }
        *out_len = (size_t)TS_TLV_HDR_LEN + (size_t)len;
    }
    return err;
}

ts_err_t ts_tlv_pack_hello(const ts_tlv_hello_t *v, uint8_t *out, size_t out_cap, size_t *out_len)
{
    ts_err_t err = TS_E_INVAL;
    if (v != NULL) {
        uint8_t val[TS_TLV_HELLO_LEN];
        ts_wr16(&val[0], v->udp_port);
        ts_wr32(&val[2], v->kbps);
        err = ts_tlv_pack((uint8_t)TS_TLV_HELLO, 0U, val, (uint16_t)TS_TLV_HELLO_LEN, out, out_cap, out_len);
    }
    return err;
}

ts_err_t ts_tlv_pack_welcome(const ts_tlv_welcome_t *v, uint8_t *out, size_t out_cap, size_t *out_len)
{
    ts_err_t err = TS_E_INVAL;
    if (v != NULL) {
        uint8_t val[TS_TLV_WELCOME_LEN];
        ts_wr16(&val[0], v->w);
        ts_wr16(&val[2], v->h);
        ts_wr16(&val[4], v->mtu);
        ts_wr32(&val[6], v->stream_id);
        ts_wr16(&val[10], v->ring_slots);
        err = ts_tlv_pack((uint8_t)TS_TLV_WELCOME, 0U, val, (uint16_t)TS_TLV_WELCOME_LEN, out, out_cap, out_len);
    }
    return err;
}

ts_err_t ts_tlv_pack_stats(const ts_tlv_stats_t *v, uint8_t *out, size_t out_cap, size_t *out_len)
{
    ts_err_t err = TS_E_INVAL;
    if ((v != NULL) && ((uint32_t)v->g.client_count <= TS_MAX_CLIENTS)) {
        uint8_t val[TS_STATS_MAX_LEN];
        const ts_stats_global_t *g = &v->g;
        const size_t len = (size_t)TS_STATS_GLOBAL_LEN + ((size_t)TS_STATS_CLIENT_LEN * (size_t)g->client_count);
        ts_wr32(&val[SG_UPTIME], g->uptime_ms);
        ts_wr32(&val[SG_CAPTURED], g->frames_captured);
        ts_wr32(&val[SG_SENT], g->frames_sent);
        ts_wr32(&val[SG_KEYFRAMES], g->keyframes_sent);
        ts_wr32(&val[SG_PKTS], g->pkts_sent);
        ts_wr32(&val[SG_BYTES], g->bytes_sent);
        ts_wr32(&val[SG_NACKS], g->nacks_rx);
        ts_wr32(&val[SG_RETX], g->retx_sent);
        ts_wr32(&val[SG_TOO_OLD], g->nack_too_old);
        ts_wr32(&val[SG_CTRL_ERR], g->ctrl_errors);
        val[SG_CLIENTS] = g->client_count;
        val[SG_CLIENTS + 1U] = 0U;
        val[SG_CLIENTS + 2U] = 0U;
        val[SG_CLIENTS + 3U] = 0U;
        for (size_t i = 0U; i < (size_t)g->client_count; i++) {
            const ts_stats_client_t *c = &v->c[i];
            uint8_t *p = &val[(size_t)TS_STATS_GLOBAL_LEN + ((size_t)TS_STATS_CLIENT_LEN * i)];
            p[SC_ID] = c->id;
            p[SC_STATE] = c->state;
            ts_wr16(&p[SC_PORT], c->udp_port);
            ts_wr32(&p[SC_KBPS], c->kbps);
            ts_wr32(&p[SC_PKTS], c->pkts_sent);
            ts_wr32(&p[SC_BYTES], c->bytes_sent);
            ts_wr32(&p[SC_NACKS], c->nacks_rx);
            ts_wr32(&p[SC_RETX], c->retx_sent);
            ts_wr32(&p[SC_RETX_DROP], c->retx_dropped);
            ts_wr32(&p[SC_KEYREQ], c->keyframe_reqs);
            ts_wr32(&p[SC_LAST_SEEN], c->last_seen_ms);
        }
        err = ts_tlv_pack((uint8_t)TS_TLV_STATS, 0U, val, (uint16_t)len, out, out_cap, out_len);
    }
    return err;
}

ts_err_t ts_tlv_pack_set_rate(const ts_tlv_set_rate_t *v, uint8_t *out, size_t out_cap, size_t *out_len)
{
    ts_err_t err = TS_E_INVAL;
    if (v != NULL) {
        uint8_t val[TS_TLV_SET_RATE_LEN];
        ts_wr32(&val[0], v->kbps);   /* kilobits per second, PROTOCOL.md 5 */
        err = ts_tlv_pack((uint8_t)TS_TLV_SET_RATE, 0U, val, (uint16_t)TS_TLV_SET_RATE_LEN, out, out_cap, out_len);
    }
    return err;
}

ts_err_t ts_tlv_pack_keyframe_req(uint8_t *out, size_t out_cap, size_t *out_len)
{
    return ts_tlv_pack((uint8_t)TS_TLV_KEYFRAME_REQ, 0U, NULL, 0U, out, out_cap, out_len);
}

ts_err_t ts_tlv_pack_bye(uint8_t *out, size_t out_cap, size_t *out_len)
{
    return ts_tlv_pack((uint8_t)TS_TLV_BYE, 0U, NULL, 0U, out, out_cap, out_len);
}

/* ---- typed parsers ----------------------------------------------------------- */

/* true when `tlv` is usable, has type `type` and exactly `len` value bytes. */
static bool tlv_is(const ts_tlv_t *tlv, uint8_t type, uint32_t len)
{
    bool ok = false;
    if ((tlv != NULL) && (tlv->type == type) && ((uint32_t)tlv->len == len)) {
        ok = (len == 0U) || (tlv->value != NULL);
    }
    return ok;
}

ts_err_t ts_tlv_parse_hello(const ts_tlv_t *tlv, ts_tlv_hello_t *v)
{
    ts_err_t err = TS_E_INVAL;
    if ((v != NULL) && tlv_is(tlv, (uint8_t)TS_TLV_HELLO, TS_TLV_HELLO_LEN)) {
        v->udp_port = ts_rd16(&tlv->value[0]);
        v->kbps = ts_rd32(&tlv->value[2]);
        err = TS_OK;
    }
    return err;
}

ts_err_t ts_tlv_parse_welcome(const ts_tlv_t *tlv, ts_tlv_welcome_t *v)
{
    ts_err_t err = TS_E_INVAL;
    if ((v != NULL) && tlv_is(tlv, (uint8_t)TS_TLV_WELCOME, TS_TLV_WELCOME_LEN)) {
        v->w = ts_rd16(&tlv->value[0]);
        v->h = ts_rd16(&tlv->value[2]);
        v->mtu = ts_rd16(&tlv->value[4]);
        v->stream_id = ts_rd32(&tlv->value[6]);
        v->ring_slots = ts_rd16(&tlv->value[10]);
        err = TS_OK;
    }
    return err;
}

ts_err_t ts_tlv_parse_stats(const ts_tlv_t *tlv, ts_tlv_stats_t *v)
{
    ts_err_t err = TS_E_INVAL;
    if ((tlv != NULL) && (v != NULL) && (tlv->type == (uint8_t)TS_TLV_STATS) &&
        (tlv->value != NULL) && ((uint32_t)tlv->len >= TS_STATS_GLOBAL_LEN)) {
        const uint8_t *p = tlv->value;
        const uint8_t count = p[SG_CLIENTS];
        const uint32_t need = TS_STATS_GLOBAL_LEN + (TS_STATS_CLIENT_LEN * (uint32_t)count);
        if (((uint32_t)count <= TS_MAX_CLIENTS) && ((uint32_t)tlv->len == need)) {
            ts_stats_global_t *g = &v->g;
            g->uptime_ms = ts_rd32(&p[SG_UPTIME]);
            g->frames_captured = ts_rd32(&p[SG_CAPTURED]);
            g->frames_sent = ts_rd32(&p[SG_SENT]);
            g->keyframes_sent = ts_rd32(&p[SG_KEYFRAMES]);
            g->pkts_sent = ts_rd32(&p[SG_PKTS]);
            g->bytes_sent = ts_rd32(&p[SG_BYTES]);
            g->nacks_rx = ts_rd32(&p[SG_NACKS]);
            g->retx_sent = ts_rd32(&p[SG_RETX]);
            g->nack_too_old = ts_rd32(&p[SG_TOO_OLD]);
            g->ctrl_errors = ts_rd32(&p[SG_CTRL_ERR]);
            g->client_count = count;
            for (size_t i = 0U; i < (size_t)count; i++) {
                const uint8_t *q = &p[(size_t)TS_STATS_GLOBAL_LEN + ((size_t)TS_STATS_CLIENT_LEN * i)];
                ts_stats_client_t *c = &v->c[i];
                c->id = q[SC_ID];
                c->state = q[SC_STATE];
                c->udp_port = ts_rd16(&q[SC_PORT]);
                c->kbps = ts_rd32(&q[SC_KBPS]);
                c->pkts_sent = ts_rd32(&q[SC_PKTS]);
                c->bytes_sent = ts_rd32(&q[SC_BYTES]);
                c->nacks_rx = ts_rd32(&q[SC_NACKS]);
                c->retx_sent = ts_rd32(&q[SC_RETX]);
                c->retx_dropped = ts_rd32(&q[SC_RETX_DROP]);
                c->keyframe_reqs = ts_rd32(&q[SC_KEYREQ]);
                c->last_seen_ms = ts_rd32(&q[SC_LAST_SEEN]);
            }
            err = TS_OK;
        }
    }
    return err;
}

ts_err_t ts_tlv_parse_set_rate(const ts_tlv_t *tlv, ts_tlv_set_rate_t *v)
{
    ts_err_t err = TS_E_INVAL;
    if ((v != NULL) && tlv_is(tlv, (uint8_t)TS_TLV_SET_RATE, TS_TLV_SET_RATE_LEN)) {
        v->kbps = ts_rd32(&tlv->value[0]);
        err = TS_OK;
    }
    return err;
}

/* ---- accumulate parser ------------------------------------------------------- */
void ts_tlv_acc_init(ts_tlv_acc_t *a)
{
    if (a != NULL) {
        a->len = 0U;
        a->pos = 0U;
    }
}

ts_err_t ts_tlv_acc_push(ts_tlv_acc_t *a, const uint8_t *data, size_t len)
{
    ts_err_t err = TS_OK;

    if ((a == NULL) || ((data == NULL) && (len != 0U)) ||
        (a->pos > a->len) || ((uint32_t)a->len > TS_TLV_ACC_CAP)) {
        err = TS_E_INVAL;
    } else {
        const size_t pending = (size_t)a->len - (size_t)a->pos;
        if (len > ((size_t)TS_TLV_ACC_CAP - pending)) {
            err = TS_E_FULL;    /* nothing is appended; protocol error -> close */
        } else if (len != 0U) {
            if (((size_t)a->len + len) > (size_t)TS_TLV_ACC_CAP) {
                /* compact: move the unconsumed tail to the front */
                (void)memmove(&a->buf[0], &a->buf[a->pos], pending);
                a->len = (uint16_t)pending;
                a->pos = 0U;
            }
            (void)memcpy(&a->buf[a->len], data, len);
            a->len = (uint16_t)((size_t)a->len + len);
        } else {
            err = TS_OK;
        }
    }
    return err;
}

ts_err_t ts_tlv_acc_next(ts_tlv_acc_t *a, ts_tlv_t *out)
{
    ts_err_t err = TS_E_AGAIN;

    if ((a == NULL) || (out == NULL) || (a->pos > a->len) || ((uint32_t)a->len > TS_TLV_ACC_CAP)) {
        err = TS_E_INVAL;
    } else {
        const size_t pending = (size_t)a->len - (size_t)a->pos;
        if (pending >= (size_t)TS_TLV_HDR_LEN) {
            const uint8_t *p = &a->buf[a->pos];
            const uint16_t vlen = ts_rd16(&p[TLV_OFF_LEN]);
            if ((uint32_t)vlen > TS_TLV_MAX_LEN) {
                err = TS_E_TOOLONG;   /* state untouched; caller closes the connection */
            } else if (pending >= ((size_t)TS_TLV_HDR_LEN + (size_t)vlen)) {
                out->type = p[TLV_OFF_TYPE];
                out->flags = p[TLV_OFF_FLAGS];
                out->len = vlen;
                out->value = &p[TS_TLV_HDR_LEN];
                a->pos = (uint16_t)((size_t)a->pos + (size_t)TS_TLV_HDR_LEN + (size_t)vlen);
                err = TS_OK;
            } else {
                err = TS_E_AGAIN;
            }
        }
    }
    return err;
}

uint16_t ts_tlv_acc_pending(const ts_tlv_acc_t *a)
{
    uint16_t n = 0U;
    if ((a != NULL) && (a->pos <= a->len)) {
        n = (uint16_t)(a->len - a->pos);
    }
    return n;
}
