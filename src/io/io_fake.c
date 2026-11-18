/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- src/io/io_fake.c
 * In-memory ts_io_t for unit and loopback tests (include/ts_io.h).
 *
 * UDP: udp_send() appends to `udp_out` (the netem shim or the test moves it),
 * udp_recv() pops `udp_in`. A full udp_out makes udp_send() return 0 (would
 * block) and counts the refusal in `dropped`; a full udp_in refuses
 * ts_io_fake_push_udp() with TS_E_FULL (the shim's loss, counted too).
 * TCP: tcp_connect() on a client linked with ts_io_fake_link_tcp() allocates
 * one connection slot on each side and queues the server slot for
 * tcp_accept(); bytes are written straight into the peer's rx ring (reliable,
 * in order, never filtered). `tcp_send_limit` caps the bytes per tcp_send()
 * so tests can split TLVs across recv() calls. Closing one side makes the
 * other side's tcp_recv() return the buffered bytes, then TS_IO_CLOSED.
 * Time is injected with ts_io_fake_set_time(). No dynamic memory.
 */
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include "ts_io.h"

_Static_assert(TS_IO_FAKE_QDEPTH <= 65535U, "queue indices are u16");
_Static_assert(TS_IO_FAKE_TCP_CAP <= 65535U, "rx ring indices are u16");
_Static_assert(TS_IO_FAKE_MAX_CONN <= 255U, "connection index is u8");

/* ---- datagram queues ------------------------------------------------------- */

static bool dq_push(ts_io_dq_t *q, const ts_io_dgram_t *d)
{
    bool ok = false;
    if (q->count < TS_IO_FAKE_QDEPTH) {
        uint32_t idx = ((uint32_t)q->head + (uint32_t)q->count) % TS_IO_FAKE_QDEPTH;
        q->q[idx] = *d;
        q->count++;
        ok = true;
    } else {
        q->dropped++;
    }
    return ok;
}

static bool dq_pop(ts_io_dq_t *q, ts_io_dgram_t *out)
{
    bool ok = false;
    if (q->count > 0U) {
        if (out != NULL) {
            *out = q->q[q->head];
        }
        q->head = (uint16_t)(((uint32_t)q->head + 1U) % TS_IO_FAKE_QDEPTH);
        q->count--;
        ok = true;
    }
    return ok;
}

/* ---- vtable ---------------------------------------------------------------- */

static ts_io_fake_t *fake_of(ts_io_t *io)
{
    /* container-of: ts_io_t is the first member of ts_io_fake_t (vtable pattern) */
    /* cppcheck-suppress misra-c2012-11.3 */
    return (ts_io_fake_t *)io;
}

static int32_t fake_udp_send(ts_io_t *io, const ts_addr_t *to, const uint8_t *buf, size_t len)
{
    int32_t rc = TS_IO_ERR;
    ts_io_fake_t *f = fake_of(io);
    if ((f != NULL) && (to != NULL) && ((buf != NULL) || (len == 0U)) && (len <= TS_IO_DGRAM_MAX)) {
        ts_io_dgram_t d;
        d.from = f->self;
        d.to = *to;
        d.len = (uint16_t)len;
        if (len > 0U) {
            (void)memcpy(d.data, buf, len);
        }
        if (dq_push(&f->udp_out, &d)) {
            f->udp_sent++;
            rc = (int32_t)len;
        } else {
            rc = 0;   /* queue full: would block (counted in udp_out.dropped) */
        }
    }
    return rc;
}

static int32_t fake_udp_recv(ts_io_t *io, ts_addr_t *from, uint8_t *buf, size_t cap)
{
    int32_t rc = 0;
    ts_io_fake_t *f = fake_of(io);
    if ((f == NULL) || ((buf == NULL) && (cap > 0U))) {
        rc = TS_IO_ERR;
    } else {
        /* zero-length datagrams are consumed and skipped like io_posix does
         * (bounded), so 0 means "nothing queued" */
        for (uint32_t k = 0U; (k < TS_IO_UDP_EMPTY_SKIP) && (f->udp_in.count > 0U) &&
                              (f->udp_in.q[f->udp_in.head].len == 0U); k++) {
            (void)dq_pop(&f->udp_in, NULL);
            f->udp_received++;
        }
        if ((f->udp_in.count > 0U) && (f->udp_in.q[f->udp_in.head].len > 0U)) {
            const ts_io_dgram_t *d = &f->udp_in.q[f->udp_in.head];
            size_t n = ((size_t)d->len < cap) ? (size_t)d->len : cap;
            if (n > 0U) {
                (void)memcpy(buf, d->data, n);
            }
            if (from != NULL) {
                *from = d->from;
            }
            (void)dq_pop(&f->udp_in, NULL);
            f->udp_received++;
            rc = (int32_t)n;
        } else {
            /* nothing queued: would block */
        }
    }
    return rc;
}

static int32_t conn_alloc(ts_io_fake_t *f)
{
    int32_t idx = -1;
    for (uint8_t i = 0U; (i < TS_IO_FAKE_MAX_CONN) && (idx < 0); i++) {
        if (!f->conn[i].used) {
            (void)memset(&f->conn[i], 0, sizeof(f->conn[i]));
            f->conn[i].used = true;
            idx = (int32_t)i;
        }
    }
    return idx;
}

static ts_io_conn_t *conn_get(ts_io_fake_t *f, ts_io_handle_t h)
{
    ts_io_conn_t *c = NULL;
    if ((f != NULL) && (h >= 0) && (h < (ts_io_handle_t)TS_IO_FAKE_MAX_CONN)) {
        if (f->conn[h].used) {
            c = &f->conn[h];
        }
    }
    return c;
}

static ts_io_handle_t fake_tcp_accept(ts_io_t *io, ts_addr_t *peer)
{
    ts_io_handle_t h = TS_IO_INVALID;
    ts_io_fake_t *f = fake_of(io);
    if ((f != NULL) && (f->pending_count > 0U)) {
        uint8_t idx = f->pending[0];
        for (uint8_t i = 1U; i < f->pending_count; i++) {
            f->pending[i - 1U] = f->pending[i];
        }
        f->pending_count--;
        if (f->conn[idx].used) {
            if (peer != NULL) {
                *peer = f->conn[idx].peer_addr;
            }
            h = (ts_io_handle_t)idx;
        }
    }
    return h;
}

static ts_io_handle_t fake_tcp_connect(ts_io_t *io, const ts_addr_t *to)
{
    ts_io_handle_t h = TS_IO_INVALID;
    ts_io_fake_t *f = fake_of(io);
    if ((f != NULL) && (to != NULL) && (f->tcp_server != NULL) &&
        (f->tcp_server->pending_count < TS_IO_FAKE_MAX_CONN)) {
        ts_io_fake_t *s = f->tcp_server;
        int32_t ci = conn_alloc(f);
        if (ci >= 0) {
            int32_t si = conn_alloc(s);
            if (si >= 0) {
                f->conn[ci].peer = s;
                f->conn[ci].peer_conn = (uint8_t)si;
                f->conn[ci].peer_addr = *to;
                s->conn[si].peer = f;
                s->conn[si].peer_conn = (uint8_t)ci;
                s->conn[si].peer_addr = f->self;
                s->pending[s->pending_count] = (uint8_t)si;
                s->pending_count++;
                h = (ts_io_handle_t)ci;
            } else {
                f->conn[ci].used = false;
            }
        }
    }
    return h;
}

static int32_t fake_tcp_recv(ts_io_t *io, ts_io_handle_t h, uint8_t *buf, size_t cap)
{
    int32_t rc = 0;
    ts_io_conn_t *c = conn_get(fake_of(io), h);
    if ((c == NULL) || ((buf == NULL) && (cap > 0U))) {
        rc = TS_IO_ERR;
    } else if (c->rx_count > 0U) {
        size_t n = ((size_t)c->rx_count < cap) ? (size_t)c->rx_count : cap;
        for (size_t i = 0U; i < n; i++) {
            buf[i] = c->rx[c->rx_head];
            c->rx_head = (uint16_t)(((uint32_t)c->rx_head + 1U) % TS_IO_FAKE_TCP_CAP);
        }
        c->rx_count = (uint16_t)((size_t)c->rx_count - n);
        rc = (int32_t)n;
    } else if (c->peer_closed) {
        rc = TS_IO_CLOSED;
    } else {
        /* nothing buffered: would block */
    }
    return rc;
}

static int32_t fake_tcp_send(ts_io_t *io, ts_io_handle_t h, const uint8_t *buf, size_t len)
{
    int32_t rc = TS_IO_ERR;
    ts_io_fake_t *f = fake_of(io);
    ts_io_conn_t *c = conn_get(f, h);
    if ((c != NULL) && !c->peer_closed && (c->peer != NULL) && ((buf != NULL) || (len == 0U))) {
        ts_io_conn_t *pc = &c->peer->conn[c->peer_conn];
        if (pc->used && (pc->peer == f) && (pc->peer_conn == (uint8_t)h)) {
            size_t space = (size_t)TS_IO_FAKE_TCP_CAP - (size_t)pc->rx_count;
            size_t n = (len < space) ? len : space;
            if ((f->tcp_send_limit > 0U) && (n > (size_t)f->tcp_send_limit)) {
                n = (size_t)f->tcp_send_limit;
            }
            for (size_t i = 0U; i < n; i++) {
                uint32_t idx = ((uint32_t)pc->rx_head + (uint32_t)pc->rx_count) % TS_IO_FAKE_TCP_CAP;
                pc->rx[idx] = buf[i];
                pc->rx_count++;
            }
            rc = (int32_t)n;
        }
    }
    return rc;
}

static void fake_tcp_close(ts_io_t *io, ts_io_handle_t h)
{
    ts_io_fake_t *f = fake_of(io);
    ts_io_conn_t *c = conn_get(f, h);
    if (c != NULL) {
        if (!c->peer_closed && (c->peer != NULL)) {
            ts_io_conn_t *pc = &c->peer->conn[c->peer_conn];
            if (pc->used && (pc->peer == f) && (pc->peer_conn == (uint8_t)h)) {
                pc->peer_closed = true;
            }
        }
        /* never handed out by accept: drop it from the pending list */
        {
            uint8_t w = 0U;
            for (uint8_t i = 0U; i < f->pending_count; i++) {
                if (f->pending[i] != (uint8_t)h) {
                    f->pending[w] = f->pending[i];
                    w++;
                }
            }
            f->pending_count = w;
        }
        (void)memset(c, 0, sizeof(*c));
    }
}

static uint32_t fake_now_ms(ts_io_t *io)
{
    const ts_io_fake_t *f = fake_of(io);
    return (f != NULL) ? f->now_ms : 0U;
}

static const ts_io_vt_t fake_vt = {
    fake_udp_send, fake_udp_recv, fake_tcp_accept, fake_tcp_connect,
    fake_tcp_recv, fake_tcp_send, fake_tcp_close, fake_now_ms
};

/* ---- public helpers -------------------------------------------------------- */

void ts_io_fake_init(ts_io_fake_t *f, ts_addr_t self)
{
    if (f != NULL) {
        (void)memset(f, 0, sizeof(*f));
        f->io.vt = &fake_vt;
        f->self = self;
    }
}

void ts_io_fake_set_time(ts_io_fake_t *f, uint32_t now_ms)
{
    if (f != NULL) {
        f->now_ms = now_ms;
    }
}

void ts_io_fake_link_tcp(ts_io_fake_t *client, ts_io_fake_t *server)
{
    if (client != NULL) {
        client->tcp_server = server;
    }
}

ts_err_t ts_io_fake_push_udp(ts_io_fake_t *f, const ts_io_dgram_t *d)
{
    ts_err_t rc = TS_OK;
    if ((f == NULL) || (d == NULL) || (d->len > TS_IO_DGRAM_MAX)) {
        rc = TS_E_INVAL;
    } else if (!dq_push(&f->udp_in, d)) {
        rc = TS_E_FULL;
    } else {
        /* queued */
    }
    return rc;
}

bool ts_io_fake_pop_udp(ts_io_fake_t *f, ts_io_dgram_t *out)
{
    bool ok = false;
    if ((f != NULL) && (out != NULL)) {
        ok = dq_pop(&f->udp_out, out);
    }
    return ok;
}

uint16_t ts_io_fake_out_count(const ts_io_fake_t *f)
{
    return (f != NULL) ? f->udp_out.count : 0U;
}

uint16_t ts_io_fake_in_count(const ts_io_fake_t *f)
{
    return (f != NULL) ? f->udp_in.count : 0U;
}

void ts_io_fake_pump(ts_io_fake_t *a, ts_io_fake_t *b)
{
    if ((a != NULL) && (b != NULL)) {
        ts_io_dgram_t d;
        while (dq_pop(&a->udp_out, &d)) {
            (void)dq_push(&b->udp_in, &d);
        }
    }
}
