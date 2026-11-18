/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- src/clients/clients.c
 * Static table of TS_MAX_CLIENTS viewers keyed by (ip, udp_port), lifecycle
 * and the per-client send path: fresh packets through the ring cursor and
 * NACK retransmissions, both charged to the client token bucket and the
 * global cap, retransmissions capped per tick
 * (include/ts_clients.h, PROTOCOL.md 4, 5, 8).
 *
 * No dynamic memory, no stdio; time differences are u32 serial differences.
 */
#include <stddef.h>
#include <string.h>
#include "ts_clients.h"

/* Client bucket bytes that fresh packets leave to retransmissions, in full DATA
 * packets: a backlog of fresh packets cannot use up the budget of the repairs. */
#define CL_REPAIR_RESERVE_PKTS 8U

static void client_reset(ts_client_t *c, uint8_t id)
{
    (void)memset(c, 0, sizeof(*c));
    c->state = TS_CL_FREE;
    c->id = id;
    c->tcp = TS_IO_INVALID;
    ts_tlv_acc_init(&c->acc);
}

void ts_clients_init(ts_clients_t *t)
{
    if (t != NULL) {
        (void)memset(t, 0, sizeof(*t));
        for (uint8_t i = 0U; i < (uint8_t)TS_MAX_CLIENTS; i++) {
            client_reset(&t->c[i], i);
        }
    }
}

ts_client_t *ts_client_accept(ts_clients_t *t, ts_io_handle_t tcp, const ts_addr_t *tcp_peer, uint32_t now_ms)
{
    ts_client_t *c = NULL;

    if ((t != NULL) && (tcp_peer != NULL) && (tcp != TS_IO_INVALID)) {
        for (uint8_t i = 0U; (i < (uint8_t)TS_MAX_CLIENTS) && (c == NULL); i++) {
            if (t->c[i].state == TS_CL_FREE) {
                c = &t->c[i];
            }
        }
        if (c == NULL) {
            t->refused++;
        } else {
            client_reset(c, c->id);
            c->state = TS_CL_PENDING;
            c->tcp = tcp;
            c->tcp_peer = *tcp_peer;
            c->last_seen_ms = now_ms;
            c->last_stats_ms = now_ms;
            t->accepted++;
        }
    }
    return c;
}

ts_err_t ts_client_hello(ts_clients_t *t, ts_client_t *c, uint16_t udp_port, uint32_t kbps,
                         const ts_txq_t *q, uint32_t now_ms)
{
    ts_err_t rc = TS_OK;

    if ((t == NULL) || (c == NULL) || (q == NULL) || (udp_port == 0U) ||
        (c->id >= (uint8_t)TS_MAX_CLIENTS) || (&t->c[c->id] != c)) {
        rc = TS_E_INVAL;
    } else if ((c->state != TS_CL_PENDING) && (c->state != TS_CL_ACTIVE)) {
        rc = TS_E_STATE;
    } else {
        ts_addr_t key;
        key.ip = c->tcp_peer.ip;
        key.port = udp_port;

        /* Another slot with the same (ip, port): the viewer restarted. */
        for (uint8_t i = 0U; i < (uint8_t)TS_MAX_CLIENTS; i++) {
            ts_client_t *o = &t->c[i];
            if ((o != c) && (o->state == TS_CL_ACTIVE) && ts_addr_eq(&o->udp, &key)) {
                ts_client_close(t, o);
            }
        }
        if (c->state == TS_CL_PENDING) {
            /* first HELLO: fresh full bucket, cursor at the ring end */
            c->state = TS_CL_ACTIVE;
            t->active++;
            c->udp = key;
            c->kbps = (kbps == 0U) ? TS_DEFAULT_KBPS : kbps;
            ts_bucket_init(&c->bucket, c->kbps, TS_BUCKET_BURST_BYTES, now_ms);
            ts_txq_cursor_init(q, &c->cursor);
            c->retx_this_tick = 0U;
            c->keyreq_seen = false;
            c->last_keyreq_ms = 0U;
        } else {
            /* repeated HELLO on an ACTIVE connection: a rate change that keeps
             * the tokens already earned (no burst refill), and the cursor keeps
             * its place unless the viewer moved to another UDP port */
            if (!ts_addr_eq(&c->udp, &key)) {
                ts_txq_cursor_init(q, &c->cursor);
            }
            c->udp = key;
            ts_client_set_rate(c, kbps, now_ms);
        }
        c->last_seen_ms = now_ms;
        c->last_stats_ms = now_ms;
    }
    return rc;
}

ts_client_t *ts_client_find(ts_clients_t *t, const ts_addr_t *udp)
{
    ts_client_t *c = NULL;

    if ((t != NULL) && (udp != NULL)) {
        for (uint8_t i = 0U; (i < (uint8_t)TS_MAX_CLIENTS) && (c == NULL); i++) {
            /* both ip AND port: two viewers behind one ip are two clients */
            if ((t->c[i].state == TS_CL_ACTIVE) && (t->c[i].udp.ip == udp->ip) &&
                (t->c[i].udp.port == udp->port)) {
                c = &t->c[i];
            }
        }
    }
    return c;
}

ts_client_t *ts_client_find_tcp(ts_clients_t *t, ts_io_handle_t tcp)
{
    ts_client_t *c = NULL;

    if ((t != NULL) && (tcp != TS_IO_INVALID)) {
        for (uint8_t i = 0U; (i < (uint8_t)TS_MAX_CLIENTS) && (c == NULL); i++) {
            if ((t->c[i].state != TS_CL_FREE) && (t->c[i].tcp == tcp)) {
                c = &t->c[i];
            }
        }
    }
    return c;
}

void ts_client_close(ts_clients_t *t, ts_client_t *c)
{
    if ((t != NULL) && (c != NULL)) {
        if (c->state == TS_CL_ACTIVE) {
            if (t->active > 0U) {
                t->active--;
            }
            c->state = TS_CL_CLOSING;
        } else if (c->state == TS_CL_PENDING) {
            c->state = TS_CL_CLOSING;
        } else {
            /* FREE or already CLOSING: nothing to do */
        }
    }
}

void ts_client_free(ts_clients_t *t, ts_client_t *c)
{
    if ((t != NULL) && (c != NULL) && (c->state != TS_CL_FREE)) {
        if ((c->state == TS_CL_ACTIVE) && (t->active > 0U)) {
            t->active--;
        }
        client_reset(c, c->id);
    }
}

void ts_client_touch(ts_client_t *c, uint32_t now_ms)
{
    if (c != NULL) {
        c->last_seen_ms = now_ms;
    }
}

uint8_t ts_clients_expire(ts_clients_t *t, uint32_t now_ms, uint32_t timeout_ms)
{
    uint8_t n = 0U;

    if (t != NULL) {
        uint32_t limit = (timeout_ms == 0U) ? TS_CLIENT_TIMEOUT_MS : timeout_ms;
        for (uint8_t i = 0U; i < (uint8_t)TS_MAX_CLIENTS; i++) {
            ts_client_t *c = &t->c[i];
            if (((c->state == TS_CL_ACTIVE) || (c->state == TS_CL_PENDING)) &&
                ((now_ms - c->last_seen_ms) > limit)) {
                ts_client_close(t, c);
                t->expired++;
                n++;
            }
        }
    }
    return n;
}

uint8_t ts_clients_active(const ts_clients_t *t)
{
    return (t != NULL) ? t->active : 0U;
}

uint8_t ts_clients_stats(const ts_clients_t *t, uint32_t now_ms, ts_stats_client_t *out)
{
    (void)t;
    (void)now_ms;
    (void)out;
    return 0U;
}

/* ---- send path ----------------------------------------------------------------- */
void ts_clients_tick(ts_clients_t *t, uint32_t now_ms)
{
    if (t != NULL) {
        for (uint8_t i = 0U; i < (uint8_t)TS_MAX_CLIENTS; i++) {
            ts_client_t *c = &t->c[i];
            if (c->state == TS_CL_ACTIVE) {
                ts_bucket_refill(&c->bucket, now_ms);
                c->retx_this_tick = 0U;
            }
        }
    }
}

void ts_client_set_rate(ts_client_t *c, uint32_t kbps, uint32_t now_ms)
{
    if (c != NULL) {
        ts_bucket_refill(&c->bucket, now_ms);      /* bank what the old rate earned */
        c->kbps = (kbps == 0U) ? TS_DEFAULT_KBPS : kbps;
        ts_bucket_set_rate(&c->bucket, c->kbps);
    }
}

/* A fresh packet of `len` bytes must leave the repair reserve in the client bucket. */
static bool cl_fresh_fits(const ts_client_t *c, uint32_t len)
{
    return (c->bucket.kbps == 0U) ||
           (ts_bucket_tokens(&c->bucket) >= (len + (CL_REPAIR_RESERVE_PKTS * TS_PKT_MAX)));
}

const ts_txq_slot_t *ts_client_next_fresh(ts_client_t *c, const ts_txq_t *q, ts_bucket_t *global)
{
    const ts_txq_slot_t *s = NULL;

    if ((c != NULL) && (q != NULL) && (c->state == TS_CL_ACTIVE)) {
        const ts_txq_slot_t *p = ts_txq_cursor_peek(q, &c->cursor);
        if ((p != NULL) && cl_fresh_fits(c, (uint32_t)p->len) &&
            ts_bucket_take_pair(&c->bucket, global, (uint32_t)p->len)) {
            ts_txq_cursor_advance(&c->cursor);
            s = p;
        }
    }
    return s;
}

ts_err_t ts_client_retx_charge(ts_client_t *c, ts_bucket_t *global, uint32_t bytes, uint8_t cap)
{
    ts_err_t rc = TS_OK;

    if (c == NULL) {
        rc = TS_E_INVAL;
    } else {
        uint8_t limit = (cap == 0U) ? (uint8_t)TS_RETX_PER_TICK : cap;
        if (c->retx_this_tick >= limit) {
            rc = TS_E_FULL;
        } else if (!ts_bucket_take_pair(&c->bucket, global, bytes)) {
            rc = TS_E_AGAIN;
        } else {
            c->retx_this_tick++;
        }
    }
    return rc;
}

ts_err_t ts_client_nack(ts_client_t *c, const ts_nack_t *n, const ts_txq_t *q, ts_bucket_t *global,
                        uint8_t cap, ts_keysched_t *k, uint32_t now_ms, ts_retx_batch_t *out)
{
    ts_err_t rc = TS_OK;

    if ((c == NULL) || (n == NULL) || (q == NULL) || (out == NULL)) {
        rc = TS_E_INVAL;
    } else {
        (void)memset(out, 0, sizeof(*out));
        if (c->state != TS_CL_ACTIVE) {
            rc = TS_E_STATE;
        } else {
            uint8_t count = (n->count > (uint8_t)TS_NACK_MAX) ? (uint8_t)TS_NACK_MAX : n->count;
            ts_client_touch(c, now_ms);
            for (uint8_t i = 0U; i < count; i++) {
                const ts_txq_slot_t *s = NULL;
                ts_err_t lr = ts_txq_lookup(q, n->e[i].frame_seq, n->e[i].frag_idx, &s);
                if ((lr == TS_OK) && (s != NULL)) {
                    if (ts_client_retx_charge(c, global, (uint32_t)s->len, cap) == TS_OK) {
                        out->slot[out->count] = s;
                        out->count++;
                    } else {
                        out->dropped++;
                    }
                } else if (lr == TS_E_TOO_OLD) {
                    out->too_old++;
                } else {
                    out->not_found++;
                }
            }
            if ((out->too_old > 0U) && (k != NULL)) {
                ts_keysched_request(k, TS_KEY_TOO_OLD);
            }
        }
    }
    return rc;
}

bool ts_client_keyframe_req(ts_client_t *c, ts_keysched_t *k, uint32_t now_ms)
{
    bool fwd = false;

    if ((c != NULL) && (c->state == TS_CL_ACTIVE)) {
        ts_client_touch(c, now_ms);
        if ((!c->keyreq_seen) || ((now_ms - c->last_keyreq_ms) >= TS_KEYREQ_MIN_INTERVAL_MS)) {
            c->keyreq_seen = true;
            c->last_keyreq_ms = now_ms;
            if (k != NULL) {
                ts_keysched_request(k, TS_KEY_REQUEST);
            }
            fwd = true;
        }
    }
    return fwd;
}
