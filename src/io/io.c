/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- src/io/io.c
 * vtable call-through wrappers and address helper (final).
 */
#include <stddef.h>
#include "ts_io.h"

bool ts_addr_eq(const ts_addr_t *a, const ts_addr_t *b)
{
    bool eq = false;
    if ((a != NULL) && (b != NULL)) {
        eq = (a->ip == b->ip) && (a->port == b->port);
    }
    return eq;
}

int32_t ts_io_udp_send(ts_io_t *io, const ts_addr_t *to, const uint8_t *buf, size_t len)
{
    return io->vt->udp_send(io, to, buf, len);
}

int32_t ts_io_udp_recv(ts_io_t *io, ts_addr_t *from, uint8_t *buf, size_t cap)
{
    return io->vt->udp_recv(io, from, buf, cap);
}

ts_io_handle_t ts_io_tcp_accept(ts_io_t *io, ts_addr_t *peer)
{
    return io->vt->tcp_accept(io, peer);
}

ts_io_handle_t ts_io_tcp_connect(ts_io_t *io, const ts_addr_t *to)
{
    return io->vt->tcp_connect(io, to);
}

int32_t ts_io_tcp_recv(ts_io_t *io, ts_io_handle_t h, uint8_t *buf, size_t cap)
{
    return io->vt->tcp_recv(io, h, buf, cap);
}

int32_t ts_io_tcp_send(ts_io_t *io, ts_io_handle_t h, const uint8_t *buf, size_t len)
{
    return io->vt->tcp_send(io, h, buf, len);
}

void ts_io_tcp_close(ts_io_t *io, ts_io_handle_t h)
{
    io->vt->tcp_close(io, h);
}

uint32_t ts_io_now_ms(ts_io_t *io)
{
    return io->vt->now_ms(io);
}
