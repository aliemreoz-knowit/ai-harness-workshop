/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- ts_clients.h
 * Static table of TS_MAX_CLIENTS viewers. Lifecycle:
 * TCP accept -> HELLO -> active -> BYE / TS_CLIENT_TIMEOUT_MS silence -> free.
 * Each client owns a token bucket, a ring cursor, a TLV accumulate buffer and
 * its counters (PROTOCOL.md 5, 5.1).
 *
 * FROZEN API: additive, non-breaking changes only.
 * Rules: no dynamic memory, no stdio.
 */
#ifndef TS_CLIENTS_H
#define TS_CLIENTS_H

#include <stdint.h>
#include <stdbool.h>
#include "ts_config.h"
#include "ts_proto.h"
#include "ts_io.h"
#include "ts_rate.h"
#include "ts_txq.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum ts_client_state {
    TS_CL_FREE     = 0,
    TS_CL_PENDING  = 1,   /**< TCP accepted, HELLO not yet received (not counted as active) */
    TS_CL_ACTIVE   = 2,   /**< HELLO received, WELCOME sent, data flowing */
    TS_CL_CLOSING  = 3    /**< BYE / timeout / protocol error; daemon closes the socket and frees */
} ts_client_state_t;

typedef struct ts_client_counters {
    uint32_t pkts_sent;
    uint32_t bytes_sent;
    uint32_t nacks_rx;
    uint32_t retx_sent;
    uint32_t retx_dropped;
    uint32_t keyframe_reqs;
} ts_client_counters_t;

typedef struct ts_client {
    ts_client_state_t     state;
    uint8_t               id;            /**< slot index */
    ts_addr_t             udp;           /**< client UDP address: (peer ip, HELLO.udp_port) */
    ts_addr_t             tcp_peer;      /**< accepted TCP peer address */
    ts_io_handle_t        tcp;           /**< control connection handle */
    uint32_t              last_seen_ms;  /**< last TCP or UDP packet from the client */
    uint32_t              last_stats_ms;
    uint32_t              kbps;          /**< current per-client rate */
    ts_bucket_t           bucket;
    ts_txq_cursor_t       cursor;
    ts_tlv_acc_t          acc;
    ts_client_counters_t  ctr;
    uint8_t               retx_this_tick;
    bool                  keyreq_seen;     /**< (additive) a KEYFRAME_REQ was accepted before */
    uint32_t              last_keyreq_ms;  /**< (additive) time of the last accepted KEYFRAME_REQ */
} ts_client_t;

/** (additive) Minimum interval between two accepted KEYFRAME_REQ of one client (PROTOCOL.md 5). */
#define TS_KEYREQ_MIN_INTERVAL_MS   500U

/** (additive) Outcome of ts_client_nack(): slots to retransmit plus per-entry accounting. */
typedef struct ts_retx_batch {
    const ts_txq_slot_t *slot[TS_NACK_MAX]; /**< admitted retransmissions, NACK order */
    uint8_t              count;             /**< valid entries in slot[] */
    uint8_t              too_old;           /**< entries that left the ring (TS_E_TOO_OLD) */
    uint8_t              not_found;         /**< entries never pushed / in the future */
    uint8_t              dropped;           /**< refused by the per-tick cap or the buckets */
} ts_retx_batch_t;

typedef struct ts_clients {
    ts_client_t c[TS_MAX_CLIENTS];
    uint8_t     active;       /**< clients in TS_CL_ACTIVE */
    uint32_t    accepted;     /**< lifetime counters */
    uint32_t    refused;      /**< table full */
    uint32_t    expired;
} ts_clients_t;

void ts_clients_init(ts_clients_t *t);

/** Reserve a slot for an accepted TCP connection (state PENDING). NULL when full. */
ts_client_t *ts_client_accept(ts_clients_t *t, ts_io_handle_t tcp, const ts_addr_t *tcp_peer, uint32_t now_ms);
/**
 * HELLO received: the client's UDP address becomes (tcp_peer.ip, udp_port). If
 * another slot already has that address it is closed (restart); `c` becomes
 * ACTIVE with a fresh bucket of
 * `kbps` (0 -> default) and a cursor at the ring end.
 * (additive clarification) A repeated HELLO on a client that is already ACTIVE
 * is a rate change only: the bucket keeps the tokens earned so far (as
 * ts_client_set_rate(), no refill to the burst), and the cursor is kept unless
 * udp_port changed (then it restarts at the ring end).
 */
ts_err_t ts_client_hello(ts_clients_t *t, ts_client_t *c, uint16_t udp_port, uint32_t kbps,
                         const ts_txq_t *q, uint32_t now_ms);
/** Find the ACTIVE client identified by the UDP address `udp`. NULL when none. */
ts_client_t *ts_client_find(ts_clients_t *t, const ts_addr_t *udp);
/** Find any non-free client by its TCP handle. */
ts_client_t *ts_client_find_tcp(ts_clients_t *t, ts_io_handle_t tcp);
/** Mark for closing (BYE, protocol error, table policy). Idempotent. */
void ts_client_close(ts_clients_t *t, ts_client_t *c);
/** Free the slot after the daemon closed the socket. */
void ts_client_free(ts_clients_t *t, ts_client_t *c);
/** Record activity (TCP or UDP) at `now_ms`. */
void ts_client_touch(ts_client_t *c, uint32_t now_ms);
/** Move every client silent for more than `timeout_ms` to CLOSING; returns how many. */
uint8_t ts_clients_expire(ts_clients_t *t, uint32_t now_ms, uint32_t timeout_ms);
/** Number of ACTIVE clients. */
uint8_t ts_clients_active(const ts_clients_t *t);
/** Fill the per-client STATS blocks; returns the count written (<= TS_MAX_CLIENTS). */
uint8_t ts_clients_stats(const ts_clients_t *t, uint32_t now_ms, ts_stats_client_t *out);

/* ---- (additive) send path helpers ------------------------------------------ */
/** Start of a daemon tick: refill every ACTIVE client's bucket at `now_ms` and reset its retransmit budget. */
void ts_clients_tick(ts_clients_t *t, uint32_t now_ms);
/** SET_RATE: kbps (0 -> TS_DEFAULT_KBPS); tokens earned at the old rate up to `now_ms` are kept. */
void ts_client_set_rate(ts_client_t *c, uint32_t kbps, uint32_t now_ms);
/**
 * Next fresh packet for an ACTIVE client: peeks its cursor and, when the client
 * budget and the global cap (`global`, may be NULL) allow it, charges both with
 * `slot->len` bytes, advances the cursor and counts pkts_sent/bytes_sent. NULL
 * when caught up, not active, or not allowed now (the cursor then waits).
 */
const ts_txq_slot_t *ts_client_next_fresh(ts_client_t *c, const ts_txq_t *q, ts_bucket_t *global);
/**
 * Admission of one retransmission of `bytes` for `c` (`cap`: per-tick limit,
 * 0 -> TS_RETX_PER_TICK; `global` may be NULL). TS_OK: admitted, counted in
 * retx_sent/bytes_sent. Refusals count retx_dropped: TS_E_FULL (per-tick limit
 * reached), TS_E_AGAIN (not now). Errors: TS_E_INVAL.
 */
ts_err_t ts_client_retx_charge(ts_client_t *c, ts_bucket_t *global, uint32_t bytes, uint8_t cap);
/**
 * Serve a parsed NACK from an ACTIVE client: touch, nacks_rx += count, look
 * every entry up in `q`; hits are admitted as by ts_client_retx_charge().
 * Entries that left the ring request a TS_KEY_TOO_OLD keyframe on `k` (may be
 * NULL). `out` lists the slots to re-pack with ts_txq_make_retransmit().
 * Errors: TS_E_INVAL (NULL args), TS_E_STATE (client not ACTIVE).
 */
ts_err_t ts_client_nack(ts_client_t *c, const ts_nack_t *n, const ts_txq_t *q, ts_bucket_t *global,
                        uint8_t cap, ts_keysched_t *k, uint32_t now_ms, ts_retx_batch_t *out);
/**
 * KEYFRAME_REQ from an ACTIVE client: touch, keyframe_reqs++, and forward it to
 * `k` (may be NULL) as TS_KEY_REQUEST, at most once per
 * TS_KEYREQ_MIN_INTERVAL_MS per client. Returns true when forwarded.
 */
bool ts_client_keyframe_req(ts_client_t *c, ts_keysched_t *k, uint32_t now_ms);

#ifdef __cplusplus
}
#endif

#endif /* TS_CLIENTS_H */
