/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- ts_io.h
 * In-process I/O abstraction. The daemon, the receiver and the tools talk to
 * the network only through a `ts_io_t` vtable so that the whole pipeline runs
 * deterministically in unit and loopback tests with a fake endpoint and an
 * injected clock. Two implementations ship in src/io/:
 *   - ts_io_posix_*: real UDP/TCP sockets + poll(), monotonic clock;
 *   - ts_io_fake_*:  queues in memory, time set by the test, TCP wired to a
 *                    peer fake, UDP moved by the netem shim (ts_netem.h).
 *
 * FROZEN API: additive, non-breaking changes only.
 * Conventions: handles are int32_t (>= 0 valid, TS_IO_INVALID otherwise);
 * every call is non-blocking; no dynamic memory in either implementation.
 */
#ifndef TS_IO_H
#define TS_IO_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "ts_config.h"
#include "ts_proto.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef int32_t ts_io_handle_t;
#define TS_IO_INVALID   ((ts_io_handle_t)-1)

/** Return codes for recv/send: > 0 bytes, 0 = would block / nothing now. */
/** (additive) Zero-length datagrams one udp_recv() call may skip before it returns 0. */
#define TS_IO_UDP_EMPTY_SKIP  16U
#define TS_IO_ERR       (-1)
#define TS_IO_CLOSED    (-2)   /**< peer closed (tcp_recv only) */

/** IPv4 address + port in host byte order. */
typedef struct ts_addr {
    uint32_t ip;
    uint16_t port;
} ts_addr_t;

bool ts_addr_eq(const ts_addr_t *a, const ts_addr_t *b);

typedef struct ts_io ts_io_t;

/** Operations. All non-blocking; `now_ms` is monotonic and wraps at 2^32. */
typedef struct ts_io_vt {
    /** Send one datagram. Returns bytes sent or TS_IO_ERR. */
    int32_t        (*udp_send)(ts_io_t *io, const ts_addr_t *to, const uint8_t *buf, size_t len);
    /**
     * Receive one datagram into buf (cap bytes). Returns bytes, 0 = none, TS_IO_ERR. Truncates silently.
     * (additive) Zero-length datagrams are consumed and skipped, at most TS_IO_UDP_EMPTY_SKIP per
     * call, so 0 means "nothing queued" and a drain loop never stops on an empty datagram.
     */
    int32_t        (*udp_recv)(ts_io_t *io, ts_addr_t *from, uint8_t *buf, size_t cap);
    /** Accept a pending TCP connection. Returns a handle or TS_IO_INVALID when none. */
    ts_io_handle_t (*tcp_accept)(ts_io_t *io, ts_addr_t *peer);
    /** Open a TCP connection (receiver side). Returns a handle or TS_IO_INVALID. */
    ts_io_handle_t (*tcp_connect)(ts_io_t *io, const ts_addr_t *to);
    /** Returns bytes, 0 = would block, TS_IO_CLOSED, TS_IO_ERR. */
    int32_t        (*tcp_recv)(ts_io_t *io, ts_io_handle_t h, uint8_t *buf, size_t cap);
    /** Returns bytes written (may be < len), 0 = would block, TS_IO_ERR. */
    int32_t        (*tcp_send)(ts_io_t *io, ts_io_handle_t h, const uint8_t *buf, size_t len);
    void           (*tcp_close)(ts_io_t *io, ts_io_handle_t h);
    /** Monotonic milliseconds (injected clock in the fake). */
    uint32_t       (*now_ms)(ts_io_t *io);
} ts_io_vt_t;

/** Base object; implementations embed it as their first member. */
struct ts_io {
    const ts_io_vt_t *vt;
};

/* Convenience wrappers (inline-able, just call through the vtable). */
int32_t        ts_io_udp_send(ts_io_t *io, const ts_addr_t *to, const uint8_t *buf, size_t len);
int32_t        ts_io_udp_recv(ts_io_t *io, ts_addr_t *from, uint8_t *buf, size_t cap);
ts_io_handle_t ts_io_tcp_accept(ts_io_t *io, ts_addr_t *peer);
ts_io_handle_t ts_io_tcp_connect(ts_io_t *io, const ts_addr_t *to);
int32_t        ts_io_tcp_recv(ts_io_t *io, ts_io_handle_t h, uint8_t *buf, size_t cap);
int32_t        ts_io_tcp_send(ts_io_t *io, ts_io_handle_t h, const uint8_t *buf, size_t len);
void           ts_io_tcp_close(ts_io_t *io, ts_io_handle_t h);
uint32_t       ts_io_now_ms(ts_io_t *io);

/* ---- POSIX implementation -------------------------------------------------- */
#define TS_IO_MAX_TCP   8U   /**< accepted + connected sockets per endpoint */

typedef struct ts_io_posix {
    ts_io_t  io;                         /**< must be first */
    int32_t  udp_fd;                     /**< -1 when closed */
    int32_t  listen_fd;                  /**< -1 when not listening */
    int32_t  tcp_fd[TS_IO_MAX_TCP];      /**< -1 = free slot; handle == slot index */
    uint32_t pending_accept;             /**< set by wait() when the listener is readable */
} ts_io_posix_t;

/**
 * Bind a UDP socket to `udp_port` (0 = ephemeral) and, when `tcp_listen_port`
 * != 0, a TCP listener. All sockets are non-blocking. Errors: TS_E_IO.
 */
ts_err_t ts_io_posix_open(ts_io_posix_t *p, uint16_t udp_port, uint16_t tcp_listen_port);
/** poll() on every fd for up to `timeout_ms`; returns number of ready fds or TS_IO_ERR. */
int32_t  ts_io_posix_wait(ts_io_posix_t *p, uint32_t timeout_ms);
/** Local UDP port actually bound (after ephemeral bind). */
uint16_t ts_io_posix_udp_port(const ts_io_posix_t *p);
void     ts_io_posix_close(ts_io_posix_t *p);

/* ---- Fake implementation (tests / loopback) ------------------------------- */
#define TS_IO_DGRAM_MAX       1500U
#define TS_IO_FAKE_QDEPTH     256U   /**< datagrams per queue (>= TS_MAX_FRAGS + retransmits) */
#define TS_IO_FAKE_MAX_CONN   8U
#define TS_IO_FAKE_TCP_CAP    4096U  /**< rx bytes buffered per connection */

typedef struct ts_io_dgram {
    ts_addr_t from;
    ts_addr_t to;
    uint16_t  len;
    uint8_t   data[TS_IO_DGRAM_MAX];
} ts_io_dgram_t;

typedef struct ts_io_dq {
    ts_io_dgram_t q[TS_IO_FAKE_QDEPTH];
    uint16_t      head;
    uint16_t      count;
    uint32_t      dropped;   /**< pushes refused because the queue was full */
} ts_io_dq_t;

struct ts_io_fake;

typedef struct ts_io_conn {
    bool               used;
    bool               peer_closed;
    struct ts_io_fake *peer;       /**< other endpoint */
    uint8_t            peer_conn;  /**< index of this connection in the peer */
    ts_addr_t          peer_addr;
    uint8_t            rx[TS_IO_FAKE_TCP_CAP];
    uint16_t           rx_head;
    uint16_t           rx_count;
} ts_io_conn_t;

typedef struct ts_io_fake {
    ts_io_t            io;                        /**< must be first */
    ts_addr_t          self;
    uint32_t           now_ms;                    /**< injected clock */
    ts_io_dq_t         udp_in;                    /**< delivered to this endpoint (udp_recv pops) */
    ts_io_dq_t         udp_out;                   /**< sent by this endpoint (shim/test pops) */
    ts_io_conn_t       conn[TS_IO_FAKE_MAX_CONN];
    uint8_t            pending[TS_IO_FAKE_MAX_CONN]; /**< connection indices waiting for accept */
    uint8_t            pending_count;
    struct ts_io_fake *tcp_server;                /**< where tcp_connect lands (ts_io_fake_link) */
    uint32_t           tcp_send_limit;            /**< max bytes per tcp_send (0 = unlimited) */
    uint32_t           udp_sent;
    uint32_t           udp_received;
} ts_io_fake_t;

/** Reset all queues, clock 0, no peer. */
void      ts_io_fake_init(ts_io_fake_t *f, ts_addr_t self);
/** Advance / set the injected clock. */
void      ts_io_fake_set_time(ts_io_fake_t *f, uint32_t now_ms);
/** Make tcp_connect() on `client` land in `server`'s accept queue. */
void      ts_io_fake_link_tcp(ts_io_fake_t *client, ts_io_fake_t *server);
/** Deliver a datagram into `udp_in` (what the shim does). Errors: TS_E_FULL, TS_E_INVAL. */
ts_err_t  ts_io_fake_push_udp(ts_io_fake_t *f, const ts_io_dgram_t *d);
/** Take the oldest datagram from `udp_out`; false when empty. */
bool      ts_io_fake_pop_udp(ts_io_fake_t *f, ts_io_dgram_t *out);
/** Datagrams waiting in udp_out / udp_in. */
uint16_t  ts_io_fake_out_count(const ts_io_fake_t *f);
uint16_t  ts_io_fake_in_count(const ts_io_fake_t *f);
/** Move every datagram from a.udp_out to b.udp_in unfiltered (direct wire). */
void      ts_io_fake_pump(ts_io_fake_t *a, ts_io_fake_t *b);

#ifdef __cplusplus
}
#endif

#endif /* TS_IO_H */
