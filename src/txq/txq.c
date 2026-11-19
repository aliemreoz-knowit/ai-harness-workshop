/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- src/txq/txq.c
 * Transmit queue: fragmenter, static history ring, per-client cursors and NACK
 * lookup (include/ts_txq.h, PROTOCOL.md sections 3, 4, 8).
 *
 * Ring model. Every pushed fragment gets the next u32 push index p (q->pushed
 * counts them) and the next u16 pkt_seq. Pushes are written at q->head, which
 * advances by one modulo TS_RING_SLOTS, so the ring always holds the newest
 * `held` pushes contiguously. Positions are addressed by their distance `back`
 * from the newest slot (0 = newest), which stays correct across the u32 wrap
 * of `pushed` and the u16 wrap of pkt_seq (65536 is not a multiple of 448, so
 * "pkt_seq % slots" is never used as an index). Within the ring pkt_seq is
 * contiguous, hence back = serial_diff(newest.pkt_seq, pkt_seq).
 *
 * No dynamic memory, no stdio. All u16 counters use serial arithmetic.
 */
#include <stddef.h>
#include <string.h>
#include "ts_txq.h"

/* DATA header field offsets (PROTOCOL.md section 3). */
#define TXQ_OFF_MAGIC       0U
#define TXQ_OFF_VER         2U
#define TXQ_OFF_FLAGS       3U
#define TXQ_OFF_FRAME_SEQ   4U
#define TXQ_OFF_PKT_SEQ     6U
#define TXQ_OFF_FRAG_IDX    8U
#define TXQ_OFF_FRAG_COUNT  9U
#define TXQ_OFF_FRAME_LEN   10U
#define TXQ_OFF_PAYLOAD_LEN 14U
#define TXQ_OFF_TS_MS       16U
#define TXQ_OFF_RESERVED    18U
#define TXQ_OFF_CRC         20U

/* A cursor more than this far "behind" is really ahead of the ring (stale). */
#define TXQ_CURSOR_AHEAD    0x80000000U

/* Serial distance a - b of two u16 counters (PROTOCOL.md 7), well defined. */
static int32_t txq_seq_dist(uint16_t a, uint16_t b)
{
    uint16_t d = (uint16_t)(a - b);
    int32_t r = (int32_t)d;
    if (d >= 0x8000U) {
        r -= (int32_t)0x10000;
    }
    return r;
}

/* Number of slots currently holding a packet. */
static uint32_t txq_held(const ts_txq_t *q)
{
    uint32_t n = (uint32_t)q->head;
    if (q->slots[q->head].used) {
        n = (uint32_t)TS_RING_SLOTS;   /* head already holds an old packet: ring is full */
    }
    return n;
}

/* Slot `back` positions behind the newest one (0 = newest). Requires back < held. */
static const ts_txq_slot_t *txq_back(const ts_txq_t *q, uint32_t back)
{
    uint32_t idx = (((uint32_t)q->head + (uint32_t)TS_RING_SLOTS) - 1U - back) % (uint32_t)TS_RING_SLOTS;
    return &q->slots[idx];
}

/* CRC over header[0..20) + payload, PROTOCOL.md 2 and 3. */
static uint32_t txq_crc(const uint8_t *pkt, uint16_t payload_len)
{
    uint32_t st = ts_crc32_init();
    st = ts_crc32_update(st, pkt, (size_t)TXQ_OFF_CRC);
    st = ts_crc32_update(st, &pkt[TS_HDR_LEN], (size_t)payload_len);
    return ts_crc32_final(st);
}

void ts_txq_init(ts_txq_t *q, uint16_t pkt_seq_start)
{
    if (q != NULL) {
        (void)memset(q, 0, sizeof(*q));
        q->pkt_seq_next = pkt_seq_start;
    }
}

/* Write one fragment into the slot at q->head and advance the ring. */
static void txq_put(ts_txq_t *q, uint16_t frame_seq, uint8_t flags, uint16_t ts_ms,
                    const uint8_t *payload, uint16_t payload_len, uint8_t frag_idx,
                    uint8_t frag_count, uint32_t frame_len)
{
    ts_txq_slot_t *s = &q->slots[q->head];
    uint8_t *p = s->pkt;

    if (s->used) {
        q->evicted++;
    }
    s->used = true;
    s->frame_seq = frame_seq;
    s->pkt_seq = q->pkt_seq_next;
    s->frag_idx = frag_idx;
    s->frag_count = frag_count;
    s->flags = flags;
    s->ts_ms = ts_ms;
    s->len = (uint16_t)(TS_HDR_LEN + (uint32_t)payload_len);

    ts_wr16(&p[TXQ_OFF_MAGIC], (uint16_t)TS_MAGIC_DATA);
    p[TXQ_OFF_VER] = (uint8_t)TS_PROTOCOL_VERSION;
    p[TXQ_OFF_FLAGS] = flags;
    ts_wr16(&p[TXQ_OFF_FRAME_SEQ], frame_seq);
    ts_wr16(&p[TXQ_OFF_PKT_SEQ], s->pkt_seq);
    p[TXQ_OFF_FRAG_IDX] = frag_idx;
    p[TXQ_OFF_FRAG_COUNT] = frag_count;
    ts_wr32(&p[TXQ_OFF_FRAME_LEN], frame_len);
    ts_wr16(&p[TXQ_OFF_PAYLOAD_LEN], payload_len);
    ts_wr16(&p[TXQ_OFF_TS_MS], ts_ms);
    ts_wr16(&p[TXQ_OFF_RESERVED], 0U);
    (void)memcpy(&p[TS_HDR_LEN], payload, (size_t)payload_len);
    ts_wr32(&p[TXQ_OFF_CRC], txq_crc(p, payload_len));

    q->pkt_seq_next = (uint16_t)(q->pkt_seq_next + 1U);
    q->pushed++;
    q->head = (uint16_t)(((uint32_t)q->head + 1U) % (uint32_t)TS_RING_SLOTS);
}

ts_err_t ts_txq_push_frame(ts_txq_t *q, uint16_t frame_seq, uint8_t flags, uint16_t ts_ms,
                           const uint8_t *enc, size_t enc_len, uint8_t *frag_count)
{
    ts_err_t rc = TS_OK;

    if ((q == NULL) || (enc == NULL) || (enc_len == 0U) || (enc_len > TS_ENC_MAX) ||
        (((uint32_t)flags & ~(uint32_t)TS_F_KEYFRAME) != 0U)) {
        rc = TS_E_INVAL;
    } else {
        uint32_t len = (uint32_t)enc_len;
        uint32_t count = (len + (TS_MTU_PAYLOAD - 1U)) / TS_MTU_PAYLOAD;   /* 1..TS_MAX_FRAGS */
        uint32_t off = 0U;
        for (uint32_t i = 0U; i < count; i++) {
            uint32_t chunk = len - off;
            if (chunk > TS_MTU_PAYLOAD) {
                chunk = TS_MTU_PAYLOAD;
            }
            txq_put(q, frame_seq, flags, ts_ms, &enc[off], (uint16_t)chunk, (uint8_t)i,
                    (uint8_t)count, len);
            off += chunk;
        }
        q->frames++;
        if (frag_count != NULL) {
            *frag_count = (uint8_t)count;
        }
    }
    return rc;
}

ts_err_t ts_txq_lookup(const ts_txq_t *q, uint16_t frame_seq, uint8_t frag_idx, const ts_txq_slot_t **slot)
{
    ts_err_t rc = TS_E_NOTFOUND;
    const ts_txq_slot_t *hit = NULL;

    if ((q == NULL) || (slot == NULL)) {
        rc = TS_E_INVAL;
    } else {
        uint32_t held = txq_held(q);
        if (held > 0U) {
            const ts_txq_slot_t *newest = txq_back(q, 0U);
            if (txq_seq_dist(frame_seq, newest->frame_seq) <= 0) {
                /* Walk back frame by frame: the slot `back` is fragment frag_idx of
                 * its frame, so that frame's first fragment is frag_idx further back
                 * and the previous frame's last fragment one more. */
                uint32_t back = 0U;
                bool done = false;
                rc = TS_E_TOO_OLD;            /* fell off the oldest end */
                while ((!done) && (back < held)) {
                    const ts_txq_slot_t *s = txq_back(q, back);
                    if (s->frame_seq == frame_seq) {
                        done = true;
                        if (frag_idx >= s->frag_count) {
                            rc = TS_E_NOTFOUND;
                        } else {
                            uint16_t want = (uint16_t)((uint32_t)s->pkt_seq - (uint32_t)s->frag_idx + (uint32_t)frag_idx);
                            int32_t dist = txq_seq_dist(newest->pkt_seq, want);
                            if ((dist < 0) || ((uint32_t)dist >= held)) {
                                rc = TS_E_TOO_OLD;   /* fragment of a partially overwritten frame */
                            } else {
                                const ts_txq_slot_t *t = txq_back(q, (uint32_t)dist);
                                if (t->used && (t->frame_seq == frame_seq) && (t->frag_idx == frag_idx) &&
                                    (t->pkt_seq == want)) {
                                    hit = t;
                                    rc = TS_OK;
                                } else {
                                    rc = TS_E_TOO_OLD;
                                }
                            }
                        }
                    } else if (txq_seq_dist(frame_seq, s->frame_seq) > 0) {
                        done = true;          /* passed it: a gap, never pushed */
                        rc = TS_E_NOTFOUND;
                    } else {
                        back += (uint32_t)s->frag_idx + 1U;
                    }
                }
            }
        }
        *slot = hit;
    }
    return rc;
}

ts_err_t ts_txq_make_retransmit(const ts_txq_slot_t *slot, uint8_t *out, size_t out_cap, size_t *out_len)
{
    ts_err_t rc = TS_OK;

    if ((slot == NULL) || (out == NULL) || (out_len == NULL) || (!slot->used) ||
        (slot->len <= TS_HDR_LEN) || (slot->len > TS_PKT_MAX)) {
        rc = TS_E_INVAL;
    } else if (out_cap < (size_t)slot->len) {
        rc = TS_E_NOMEM;
    } else {
        uint16_t payload_len = (uint16_t)((uint32_t)slot->len - TS_HDR_LEN);
        (void)memcpy(out, slot->pkt, (size_t)slot->len);
        out[TXQ_OFF_FLAGS] = (uint8_t)(slot->flags | (uint8_t)TS_F_RETRANSMIT);
        ts_wr32(&out[TXQ_OFF_CRC], txq_crc(out, payload_len));
        *out_len = (size_t)slot->len;
    }
    return rc;
}

void ts_txq_cursor_init(const ts_txq_t *q, ts_txq_cursor_t *c)
{
    if ((q != NULL) && (c != NULL)) {
        c->next = q->pushed;
        c->skipped = 0U;
    }
}

const ts_txq_slot_t *ts_txq_cursor_peek(const ts_txq_t *q, ts_txq_cursor_t *c)
{
    const ts_txq_slot_t *s = NULL;

    if ((q != NULL) && (c != NULL)) {
        uint32_t behind = q->pushed - c->next;    /* u32 serial distance */
        uint32_t held = txq_held(q);
        if (behind >= TXQ_CURSOR_AHEAD) {
            c->next = q->pushed;                  /* cursor from a previous ring life */
            behind = 0U;
        }
        if (behind > held) {
            c->skipped += behind - held;          /* the ring wrapped past this cursor */
            c->next = q->pushed - held;
            behind = held;
        }
        if (behind > 0U) {
            s = txq_back(q, behind - 1U);
        }
    }
    return s;
}

void ts_txq_cursor_advance(ts_txq_cursor_t *c)
{
    if (c != NULL) {
        c->next++;
    }
}

uint32_t ts_txq_cursor_pending(const ts_txq_t *q, const ts_txq_cursor_t *c)
{
    uint32_t n = 0U;

    if ((q != NULL) && (c != NULL)) {
        uint32_t behind = q->pushed - c->next;
        uint32_t held = txq_held(q);
        if (behind < TXQ_CURSOR_AHEAD) {
            n = (behind > held) ? held : behind;
        }
    }
    return n;
}

bool ts_txq_range(const ts_txq_t *q, uint16_t *oldest_frame, uint16_t *newest_frame)
{
    bool ok = false;

    if (q != NULL) {
        uint32_t held = txq_held(q);
        if (held > 0U) {
            if (oldest_frame != NULL) {
                *oldest_frame = txq_back(q, held - 1U)->frame_seq;
            }
            if (newest_frame != NULL) {
                *newest_frame = txq_back(q, 0U)->frame_seq;
            }
            ok = true;
        }
    }
    return ok;
}
