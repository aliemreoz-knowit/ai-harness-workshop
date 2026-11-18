/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- ts_proto.h
 * Wire format shared by daemon and receiver (libtsproto): DATA header, NACK,
 * control TLVs, CRC32 and serial arithmetic. Implements PROTOCOL.md sections 2-5, 7.
 *
 * FROZEN API: additive, non-breaking changes only.
 * Rules for implementers:
 *   - no dynamic memory, no stdio, single-threaded, every function reentrant
 *     with respect to its arguments;
 *   - every parser is bounded by the length it is given and never reads past it;
 *   - all multi-byte fields are little-endian regardless of host endianness.
 */
#ifndef TS_PROTO_H
#define TS_PROTO_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "ts_config.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- Error codes (shared by all modules) ----------------------------------- */
typedef enum ts_err {
    TS_OK = 0,
    TS_E_INVAL      = 1,   /**< invalid argument or inconsistent field */
    TS_E_NOMEM      = 2,   /**< destination buffer too small */
    TS_E_BADMAGIC   = 3,
    TS_E_BADVER     = 4,
    TS_E_BADCRC     = 5,
    TS_E_TRUNC      = 6,   /**< input shorter than the declared length */
    TS_E_TOOLONG    = 7,   /**< declared length above the protocol maximum */
    TS_E_FULL       = 8,   /**< table / queue / ring full */
    TS_E_NOTFOUND   = 9,
    TS_E_TOO_OLD    = 10,  /**< NACK for a slot that left the ring */
    TS_E_AGAIN      = 11,  /**< nothing available yet, retry later */
    TS_E_IO         = 12,  /**< I/O layer failure */
    TS_E_STATE      = 13,  /**< call not valid in the current state */
    TS_E_NOREF      = 14,  /**< P-frame without a matching reference */
    TS_E_CORRUPT    = 15,  /**< malformed bitstream */
    TS_E_CLOSED     = 16,  /**< peer closed the connection */
    TS_E_NOTIMPL    = 17,  /**< stub: not implemented yet */
    TS_E_MAX_
} ts_err_t;

/** Human readable name of an error code ("TS_OK", "TS_E_INVAL", ...). Never NULL. */
const char *ts_err_str(ts_err_t e);

/* ---- CRC32 (PROTOCOL.md section 2) ----------------------------------------- */
/** One-shot CRC32 (reflected 0xEDB88320, init 0xFFFFFFFF, final xor). */
uint32_t ts_crc32(const uint8_t *buf, size_t len);
/** Streaming interface: state = ts_crc32_init(); state = update(state, ...); crc = final(state). */
uint32_t ts_crc32_init(void);
uint32_t ts_crc32_update(uint32_t state, const uint8_t *buf, size_t len);
uint32_t ts_crc32_final(uint32_t state);

/* ---- Serial arithmetic (PROTOCOL.md section 7) ----------------------------- */
/** (int16_t)(uint16_t)(a - b): >0 when a is ahead of b, <0 behind, -32768 at the midpoint. */
int16_t ts_seq_diff(uint16_t a, uint16_t b);
/** true when a is strictly ahead of b in serial order (diff > 0). */
bool ts_seq_after(uint16_t a, uint16_t b);

/* ---- Little-endian helpers (bounded by the caller) ------------------------- */
uint16_t ts_rd16(const uint8_t *p);
uint32_t ts_rd32(const uint8_t *p);
void     ts_wr16(uint8_t *p, uint16_t v);
void     ts_wr32(uint8_t *p, uint32_t v);

/* ---- DATA header (PROTOCOL.md section 3) ----------------------------------- */
/** Parsed DATA header. `crc32` is the value found in the packet. */
typedef struct ts_hdr {
    uint8_t  ver;          /**< always TS_PROTOCOL_VERSION on the wire */
    uint8_t  flags;        /**< TS_F_KEYFRAME | TS_F_RETRANSMIT */
    uint16_t frame_seq;
    uint16_t pkt_seq;
    uint8_t  frag_idx;
    uint8_t  frag_count;
    uint32_t frame_len;    /**< total encoded frame length, 1..TS_ENC_MAX */
    uint16_t payload_len;  /**< 1..TS_MTU_PAYLOAD */
    uint16_t ts_ms;        /**< capture time modulo 65536 */
    uint32_t crc32;        /**< as read from / written to the packet */
} ts_hdr_t;

/**
 * Serialise header + payload into `out` (capacity `out_cap`). Sets magic, ver,
 * reserved = 0 and computes the CRC over header[0..20) + payload. On success
 * `*out_len = TS_HDR_LEN + h->payload_len` and `h->crc32` is updated.
 * Errors: TS_E_INVAL (field out of range, see PROTOCOL.md 3.1), TS_E_NOMEM.
 */
ts_err_t ts_hdr_pack(ts_hdr_t *h, const uint8_t *payload, uint8_t *out, size_t out_cap, size_t *out_len);

/**
 * Validate a datagram of `len` bytes and fill `*h`; `*payload` points into `buf`.
 * Errors in the order of PROTOCOL.md 3.1: TS_E_TRUNC, TS_E_BADMAGIC, TS_E_BADVER,
 * TS_E_INVAL, TS_E_BADCRC. On error `*h` is unspecified and `*payload` is NULL.
 */
ts_err_t ts_hdr_parse(const uint8_t *buf, size_t len, ts_hdr_t *h, const uint8_t **payload);

/* ---- NACK packet (PROTOCOL.md section 4) ----------------------------------- */
typedef struct ts_nack_entry {
    uint16_t frame_seq;
    uint8_t  frag_idx;
} ts_nack_entry_t;

typedef struct ts_nack {
    uint8_t         count;               /**< 0..TS_NACK_MAX */
    ts_nack_entry_t e[TS_NACK_MAX];
} ts_nack_t;

/** Serialise; `*out_len = 4 + 4*count`. Errors: TS_E_INVAL (count > 32), TS_E_NOMEM. */
ts_err_t ts_nack_pack(const ts_nack_t *n, uint8_t *out, size_t out_cap, size_t *out_len);
/** Parse; never writes more than TS_NACK_MAX entries. Errors: TS_E_TRUNC, TS_E_BADMAGIC, TS_E_INVAL. */
ts_err_t ts_nack_parse(const uint8_t *buf, size_t len, ts_nack_t *n);

/* ---- Control TLVs (PROTOCOL.md section 5) ---------------------------------- */
typedef enum ts_tlv_type {
    TS_TLV_HELLO        = 1,
    TS_TLV_WELCOME      = 2,
    TS_TLV_STATS        = 3,
    TS_TLV_KEYFRAME_REQ = 4,
    TS_TLV_SET_RATE     = 5,
    TS_TLV_BYE          = 6
} ts_tlv_type_t;

/** A complete TLV as returned by the accumulate parser. `value` points into the
 *  accumulate buffer and is valid until the next ts_tlv_acc_* call. */
typedef struct ts_tlv {
    uint8_t        type;
    uint8_t        flags;
    uint16_t       len;
    const uint8_t *value;
} ts_tlv_t;

typedef struct ts_tlv_hello {
    uint16_t udp_port;
    uint32_t kbps;         /**< requested rate, 0 = daemon default */
} ts_tlv_hello_t;

typedef struct ts_tlv_welcome {
    uint16_t w;
    uint16_t h;
    uint16_t mtu;          /**< TS_MTU_PAYLOAD */
    uint32_t stream_id;    /**< random per daemon start */
    uint16_t ring_slots;   /**< TS_RING_SLOTS */
} ts_tlv_welcome_t;

typedef struct ts_tlv_set_rate {
    uint32_t kbps;
} ts_tlv_set_rate_t;

/** STATS global block (44 bytes on the wire, PROTOCOL.md 5.1). */
typedef struct ts_stats_global {
    uint32_t uptime_ms;
    uint32_t frames_captured;
    uint32_t frames_sent;
    uint32_t keyframes_sent;
    uint32_t pkts_sent;
    uint32_t bytes_sent;
    uint32_t nacks_rx;
    uint32_t retx_sent;
    uint32_t nack_too_old;
    uint32_t ctrl_errors;
    uint8_t  client_count;
} ts_stats_global_t;

/** STATS per-client block (36 bytes on the wire). */
typedef struct ts_stats_client {
    uint8_t  id;
    uint8_t  state;        /**< 1 active, 2 closing */
    uint16_t udp_port;
    uint32_t kbps;
    uint32_t pkts_sent;
    uint32_t bytes_sent;
    uint32_t nacks_rx;
    uint32_t retx_sent;
    uint32_t retx_dropped;
    uint32_t keyframe_reqs;
    uint32_t last_seen_ms; /**< ms since last packet from the client */
} ts_stats_client_t;

typedef struct ts_tlv_stats {
    ts_stats_global_t g;
    ts_stats_client_t c[TS_MAX_CLIENTS];  /**< first g.client_count entries valid */
} ts_tlv_stats_t;

#define TS_STATS_GLOBAL_LEN   44U
#define TS_STATS_CLIENT_LEN   36U
#define TS_STATS_MAX_LEN      (TS_STATS_GLOBAL_LEN + TS_STATS_CLIENT_LEN * TS_MAX_CLIENTS)
#define TS_TLV_HELLO_LEN      6U
#define TS_TLV_WELCOME_LEN    12U
#define TS_TLV_SET_RATE_LEN   4U
_Static_assert(TS_STATS_MAX_LEN <= TS_TLV_MAX_LEN, "STATS fits a TLV");

/** Generic pack: writes type/flags/len + value. Errors: TS_E_TOOLONG (len > TS_TLV_MAX_LEN), TS_E_NOMEM. */
ts_err_t ts_tlv_pack(uint8_t type, uint8_t flags, const uint8_t *value, uint16_t len,
                     uint8_t *out, size_t out_cap, size_t *out_len);

/* Typed packers (flags = 0). */
ts_err_t ts_tlv_pack_hello(const ts_tlv_hello_t *v, uint8_t *out, size_t out_cap, size_t *out_len);
ts_err_t ts_tlv_pack_welcome(const ts_tlv_welcome_t *v, uint8_t *out, size_t out_cap, size_t *out_len);
ts_err_t ts_tlv_pack_stats(const ts_tlv_stats_t *v, uint8_t *out, size_t out_cap, size_t *out_len);
ts_err_t ts_tlv_pack_set_rate(const ts_tlv_set_rate_t *v, uint8_t *out, size_t out_cap, size_t *out_len);
ts_err_t ts_tlv_pack_keyframe_req(uint8_t *out, size_t out_cap, size_t *out_len);
ts_err_t ts_tlv_pack_bye(uint8_t *out, size_t out_cap, size_t *out_len);

/* Typed parsers: check `tlv->type` and exact `tlv->len`. Errors: TS_E_INVAL. */
ts_err_t ts_tlv_parse_hello(const ts_tlv_t *tlv, ts_tlv_hello_t *v);
ts_err_t ts_tlv_parse_welcome(const ts_tlv_t *tlv, ts_tlv_welcome_t *v);
ts_err_t ts_tlv_parse_stats(const ts_tlv_t *tlv, ts_tlv_stats_t *v);
ts_err_t ts_tlv_parse_set_rate(const ts_tlv_t *tlv, ts_tlv_set_rate_t *v);

/**
 * Accumulate parser for the TLV byte stream of one TCP connection (PROTOCOL.md
 * section 5): ts_tlv_acc_push() takes received bytes, ts_tlv_acc_next() yields
 * complete TLVs. At most TS_TLV_ACC_CAP bytes buffered, no allocation.
 */
typedef struct ts_tlv_acc {
    uint8_t  buf[TS_TLV_ACC_CAP];
    uint16_t len;          /**< bytes currently buffered */
    uint16_t pos;          /**< consumed prefix */
} ts_tlv_acc_t;

void     ts_tlv_acc_init(ts_tlv_acc_t *a);
/** Append `len` bytes. Errors: TS_E_FULL when the buffer cannot hold them (protocol error -> close). */
ts_err_t ts_tlv_acc_push(ts_tlv_acc_t *a, const uint8_t *data, size_t len);
/**
 * Pop the next complete TLV. TS_OK: `*out` filled (value points into `a`);
 * TS_E_AGAIN: incomplete, wait for more bytes; TS_E_TOOLONG: declared len >
 * TS_TLV_MAX_LEN (protocol error -> close connection).
 */
ts_err_t ts_tlv_acc_next(ts_tlv_acc_t *a, ts_tlv_t *out);
/** Bytes currently buffered (for tests and STATS). */
uint16_t ts_tlv_acc_pending(const ts_tlv_acc_t *a);

#ifdef __cplusplus
}
#endif

#endif /* TS_PROTO_H */
