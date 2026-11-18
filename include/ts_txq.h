/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- ts_txq.h
 * Transmit queue: fragments an encoded frame into DATA packets (<= TS_MTU_PAYLOAD
 * payload, <= TS_MAX_FRAGS fragments), keeps them in a static history ring of
 * TS_RING_SLOTS, gives every client a cursor over fresh packets and answers
 * NACK lookups with the slot or TS_E_TOO_OLD. PROTOCOL.md sections 3, 4, 8.
 *
 * FROZEN API: additive, non-breaking changes only.
 * Rules: no dynamic memory (the ring lives inside ts_txq_t, placed in the pool),
 * no stdio.
 */
#ifndef TS_TXQ_H
#define TS_TXQ_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "ts_config.h"
#include "ts_proto.h"

#ifdef __cplusplus
extern "C" {
#endif

/** One ring slot = one fully packed DATA packet plus its keys. */
typedef struct ts_txq_slot {
    bool     used;
    uint16_t frame_seq;
    uint16_t pkt_seq;        /**< assigned at push, kept on retransmit */
    uint8_t  frag_idx;
    uint8_t  frag_count;
    uint8_t  flags;          /**< TS_F_KEYFRAME */
    uint16_t ts_ms;
    uint16_t len;            /**< bytes in pkt[] (TS_HDR_LEN + payload_len) */
    uint8_t  pkt[TS_PKT_MAX];
} ts_txq_slot_t;

typedef struct ts_txq {
    ts_txq_slot_t slots[TS_RING_SLOTS];
    uint16_t      head;          /**< next slot to write (oldest is overwritten) */
    uint16_t      pkt_seq_next;  /**< pkt_seq for the next pushed fragment */
    uint32_t      pushed;        /**< total fragments pushed (monotonic, for cursors) */
    uint32_t      frames;        /**< frames pushed */
    uint32_t      evicted;       /**< slots overwritten while still used */
} ts_txq_t;

/** Per-client read position: `next` is the global push index of the next fresh packet. */
typedef struct ts_txq_cursor {
    uint32_t next;
    uint32_t skipped;        /**< packets this cursor lost because the ring wrapped past it */
} ts_txq_cursor_t;

/** Empty ring; the first pushed packet gets pkt_seq = `pkt_seq_start`. */
void ts_txq_init(ts_txq_t *q, uint16_t pkt_seq_start);

/**
 * Fragment `enc[0..enc_len)` into packed DATA packets for frame `frame_seq`
 * (flags = TS_F_KEYFRAME or 0, `ts_ms` capture time) and append them to the
 * ring. Returns the fragment count in `*frag_count`. Errors: TS_E_INVAL
 * (enc_len 0 or > TS_ENC_MAX).
 */
ts_err_t ts_txq_push_frame(ts_txq_t *q, uint16_t frame_seq, uint8_t flags, uint16_t ts_ms,
                           const uint8_t *enc, size_t enc_len, uint8_t *frag_count);

/**
 * Find the slot holding (frame_seq, frag_idx). TS_OK with `*slot` set,
 * TS_E_TOO_OLD when the frame was in the ring but is overwritten (or older
 * than anything in the ring), TS_E_NOTFOUND when never pushed / in the future.
 */
ts_err_t ts_txq_lookup(const ts_txq_t *q, uint16_t frame_seq, uint8_t frag_idx, const ts_txq_slot_t **slot);

/** Re-pack a slot as a retransmission (TS_F_RETRANSMIT set, CRC recomputed) into `out`. */
ts_err_t ts_txq_make_retransmit(const ts_txq_slot_t *slot, uint8_t *out, size_t out_cap, size_t *out_len);

/** Start a cursor at the current end of the ring (nothing pending). */
void ts_txq_cursor_init(const ts_txq_t *q, ts_txq_cursor_t *c);
/**
 * Next fresh packet for this cursor, oldest first; NULL when the cursor is
 * caught up. If the ring wrapped past the cursor it jumps to the oldest
 * available slot and counts the loss in `c->skipped`.
 */
const ts_txq_slot_t *ts_txq_cursor_peek(const ts_txq_t *q, ts_txq_cursor_t *c);
/** Advance after a successful send of the peeked packet. */
void ts_txq_cursor_advance(ts_txq_cursor_t *c);
/** Packets pending for the cursor. */
uint32_t ts_txq_cursor_pending(const ts_txq_t *q, const ts_txq_cursor_t *c);

/** Oldest and newest frame_seq currently held (false when empty). */
bool ts_txq_range(const ts_txq_t *q, uint16_t *oldest_frame, uint16_t *newest_frame);

#ifdef __cplusplus
}
#endif

#endif /* TS_TXQ_H */
