/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tools/tsrecv/lib/reasm.c
 * UDP ingest and the reassembly table (TS_REASM_FRAMES frames in flight):
 *   - fragment i lands at offset i * TS_MTU_PAYLOAD, the frame size comes from
 *     the u32 frame_len, a per-frame bitmap (have[]) drops duplicates;
 *   - frames are decoded strictly in frame_seq order (serial arithmetic); a
 *     complete keyframe after the play position is decoded at once and makes
 *     everything older obsolete;
 *   - late-frame policy: packets for frames at or before the play position
 *     (decoded or skipped) are dropped. TSR_LATE_DROP gives the awaited frame
 *     up on a NACK give-up / timeout and skips P-frames until a keyframe;
 *     TSR_LATE_DECODE keeps waiting for it (a late completion still decodes and
 *     restores the reference chain) and only drops it on eviction;
 *   - when the table is full the oldest frame is evicted and counted dropped.
 * The decoder output goes to r->x.scratch first and becomes r->frame only on
 * success, so a corrupt frame never tears the displayed picture.
 */
#include <stddef.h>
#include <string.h>
#include "tsr_internal.h"

static void    slot_free(tsr_t *r, uint32_t i);
static int32_t slot_oldest_free(const tsr_t *r);
static int32_t slot_oldest(const tsr_t *r, bool complete_only, bool key_only, bool after_play_only);
static int32_t slot_alloc(tsr_t *r, const ts_hdr_t *h, uint32_t now_ms);
static void    reasm_insert(tsr_t *r, const ts_hdr_t *h, const uint8_t *payload, uint32_t now_ms);
static void    skip_to(tsr_t *r, uint16_t frame_seq, uint32_t now_ms);
static void    drop_older(tsr_t *r, uint16_t frame_seq, int32_t keep);
static bool    decode_slot(tsr_t *r, uint32_t i, uint32_t now_ms);
static bool    slot_gap_evident(const tsr_t *r, const tsr_reasm_t *s);
static bool    awaited_abandoned(tsr_t *r, int32_t n, uint16_t next, uint32_t now_ms);
static bool    slot_is_key(const tsr_reasm_t *s);
static bool    after_play(const tsr_t *r, uint16_t frame_seq);

/* ---- helpers -------------------------------------------------------------------- */
static bool slot_is_key(const tsr_reasm_t *s)
{
    return (s->flags & (uint8_t)TS_F_KEYFRAME) != 0U;
}

static bool after_play(const tsr_t *r, uint16_t frame_seq)
{
    return (!r->x.have_play) || ts_seq_after(frame_seq, r->x.play_seq);
}

int32_t tsr_i_find_slot(const tsr_t *r, uint16_t frame_seq)
{
    int32_t idx = -1;
    for (uint32_t i = 0U; (i < TS_REASM_FRAMES) && (idx < 0); i++) {
        if (r->reasm[i].used && (r->reasm[i].frame_seq == frame_seq)) {
            idx = (int32_t)i;
        }
    }
    return idx;
}

static void slot_free(tsr_t *r, uint32_t i)
{
    r->reasm[i].used = false;
    r->reasm[i].complete = false;
    r->reasm[i].frags_have = 0U;
    r->x.slot_giveup[i] = 0U;
}

/* Oldest used slot (serial order) matching the filters, or -1. */
static int32_t slot_oldest(const tsr_t *r, bool complete_only, bool key_only, bool after_play_only)
{
    int32_t best = -1;
    for (uint32_t i = 0U; i < TS_REASM_FRAMES; i++) {
        const tsr_reasm_t *s = &r->reasm[i];
        bool ok = s->used;
        if (ok && complete_only) {
            ok = s->complete;
        }
        if (ok && key_only) {
            ok = slot_is_key(s);
        }
        if (ok && after_play_only) {
            ok = after_play(r, s->frame_seq);
        }
        if (ok) {
            if ((best < 0) || (ts_seq_diff(s->frame_seq, r->reasm[(uint32_t)best].frame_seq) < 0)) {
                best = (int32_t)i;
            }
        }
    }
    return best;
}

uint32_t tsr_i_frame_timeout(const tsr_t *r)
{
    uint32_t t;
    if (r->cfg.no_nack) {
        t = 3U * r->cfg.nack_delay_ms;
    } else {
        t = r->cfg.nack_delay_ms + r->cfg.nack_retry_ms;
    }
    return t;
}

void tsr_i_stream_reset(tsr_t *r, uint32_t now_ms)
{
    for (uint32_t i = 0U; i < TS_REASM_FRAMES; i++) {
        slot_free(r, i);
    }
    r->x.have_play = false;
    r->x.abandon_next = false;
    r->x.late_streak = 0U;
    r->x.have_jitter_ref = false;
    r->have_pkt_seq = false;
    r->need_keyframe = false;
    ts_dec_init(&r->dec);
    tsr_i_nack_reset(r);
    TS_LOG(&r->log, now_ms, TS_LOG_WARN, TS_MOD_RECEIVER, (uint16_t)TSR_EV_RESYNC, 0U, 0U);
}

/* ---- ingest ------------------------------------------------------------------------- */
void tsr_i_udp_drain(tsr_t *r, uint32_t now_ms)
{
    uint32_t n = 0U;
    bool more = true;
    while (more && (n < TSR_DRAIN_MAX)) {
        ts_addr_t from = { 0U, 0U };
        int32_t got = ts_io_udp_recv(r->io, &from, r->pkt_buf, sizeof(r->pkt_buf));
        if (got > 0) {
            ts_hdr_t h;
            const uint8_t *payload = NULL;
            uint32_t len = (uint32_t)got;
            ts_err_t rc;
            n++;
            r->window_bytes += len;
            rc = ts_hdr_parse(r->pkt_buf, (size_t)len, &h, &payload);
            if ((rc == TS_OK) && (payload != NULL)) {
                r->x.data_from = from;
                r->x.have_data_from = true;
                r->last_rx_ms = now_ms;
                r->x.last_alive_ms = now_ms;
                tsr_i_rx_packet(r, &h, payload, now_ms);
            } else if (rc == TS_E_BADCRC) {
                r->m.crc_fail++;
            } else {
                r->m.parse_fail++;
            }
        } else {
            more = false;
        }
    }
}

void tsr_i_rx_packet(tsr_t *r, const ts_hdr_t *h, const uint8_t *payload, uint32_t now_ms)
{
    uint32_t off = (uint32_t)h->frag_idx * TS_MTU_PAYLOAD;
    /* defensive re-check of the fragment geometry (PROTOCOL.md 3.1, rule 5) */
    bool ok = (h->frag_count >= 1U) && ((uint32_t)h->frag_count <= TS_MAX_FRAGS) &&
              (h->frag_idx < h->frag_count) && (h->frame_len >= 1U) && (h->frame_len <= TS_ENC_MAX) &&
              (h->payload_len >= 1U) && ((uint32_t)h->payload_len <= TS_MTU_PAYLOAD) &&
              ((off + (uint32_t)h->payload_len) <= h->frame_len);
    if (!ok) {
        r->m.parse_fail++;
    } else {
        bool retx = (h->flags & (uint8_t)TS_F_RETRANSMIT) != 0U;
        r->m.pkts_rx++;
        if (retx) {
            r->m.retx_rx++;
        }
        tsr_i_gap_track(r, h->pkt_seq, retx, now_ms);
        tsr_i_frange_learn(r, h->frame_seq, (uint16_t)(h->pkt_seq - (uint16_t)h->frag_idx), h->frag_count);
        if (!after_play(r, h->frame_seq)) {
            r->m.late_pkts++;
            if (!retx) {
                r->x.late_streak++;
                if (r->x.late_streak >= TSR_LATE_RESYNC) {
                    /* the sender's frame_seq jumped backwards for good (restart without
                     * a new WELCOME): start over instead of dropping everything */
                    tsr_i_stream_reset(r, now_ms);
                    tsr_i_gap_track(r, h->pkt_seq, retx, now_ms);
                    reasm_insert(r, h, payload, now_ms);
                }
            }
        } else {
            r->x.late_streak = 0U;
            reasm_insert(r, h, payload, now_ms);
        }
    }
}

/* First unused slot or -1. */
static int32_t slot_oldest_free(const tsr_t *r)
{
    int32_t idx = -1;
    for (uint32_t i = 0U; (i < TS_REASM_FRAMES) && (idx < 0); i++) {
        if (!r->reasm[i].used) {
            idx = (int32_t)i;
        }
    }
    return idx;
}

/* Allocate a slot for a new frame; evicts the oldest when full. -1 = drop the packet. */
static int32_t slot_alloc(tsr_t *r, const ts_hdr_t *h, uint32_t now_ms)
{
    int32_t idx = slot_oldest_free(r);
    if (idx < 0) {
        /* full: frames completed in this burst may be decodable already */
        tsr_i_decode_pass(r, now_ms);
        idx = slot_oldest_free(r);
    }
    if ((idx < 0) && (!after_play(r, h->frame_seq))) {
        /* the decode pass moved the play position past this frame */
        r->m.late_pkts++;
    } else if (idx < 0) {
        int32_t old = slot_oldest(r, false, false, false);
        if ((old >= 0) && (ts_seq_diff(h->frame_seq, r->reasm[(uint32_t)old].frame_seq) > 0)) {
            uint16_t ev = r->reasm[(uint32_t)old].frame_seq;
            TS_LOG(&r->log, now_ms, TS_LOG_INFO, TS_MOD_RECEIVER, (uint16_t)TSR_EV_EVICT,
                   (uint32_t)ev, (uint32_t)r->reasm[(uint32_t)old].frags_have);
            skip_to(r, ev, now_ms);   /* frees the evicted (oldest) slot */
            idx = slot_oldest_free(r);
        } else {
            /* older than everything in a full table: nothing to gain from it */
            r->m.late_pkts++;
        }
    } else {
        /* a free slot was found */
    }
    if (idx >= 0) {
        tsr_reasm_t *s = &r->reasm[(uint32_t)idx];
        s->used = true;
        s->complete = false;
        s->frame_seq = h->frame_seq;
        s->frag_count = h->frag_count;
        s->frags_have = 0U;
        s->flags = (uint8_t)(h->flags & (uint8_t)TS_F_KEYFRAME);
        s->ts_ms = h->ts_ms;
        s->frame_len = h->frame_len;
        s->first_rx_ms = now_ms;
        s->last_rx_ms = now_ms;
        s->nack_sent_ms = 0U;
        (void)memset(s->have, 0, sizeof(s->have));
        r->x.slot_giveup[(uint32_t)idx] = 0U;
    }
    return idx;
}

static void reasm_insert(tsr_t *r, const ts_hdr_t *h, const uint8_t *payload, uint32_t now_ms)
{
    int32_t idx = tsr_i_find_slot(r, h->frame_seq);
    if (idx < 0) {
        idx = slot_alloc(r, h, now_ms);
    }
    if (idx >= 0) {
        tsr_reasm_t *s = &r->reasm[(uint32_t)idx];
        uint32_t fi = (uint32_t)h->frag_idx;
        if ((s->frag_count != h->frag_count) || (s->frame_len != h->frame_len)) {
            /* same frame_seq, different geometry: corrupt or a stale stream */
            r->m.parse_fail++;
        } else if (s->have[fi] != 0U) {
            r->m.dups++;
        } else {
            uint32_t off = fi * TS_MTU_PAYLOAD;
            (void)memcpy(&s->data[off], payload, (size_t)h->payload_len);
            s->have[fi] = 1U;
            s->pkt_seq[fi] = h->pkt_seq;
            s->frags_have++;
            s->last_rx_ms = now_ms;
            s->flags = (uint8_t)(s->flags | (uint8_t)(h->flags & (uint8_t)TS_F_KEYFRAME));
            if (s->frags_have == s->frag_count) {
                s->complete = true;
                r->m.frames_complete++;
            }
        }
    }
}

/* ---- play position -------------------------------------------------------------------- */
/* Free every slot not after `frame_seq` except `keep`; counts them only before the first decode. */
static void drop_older(tsr_t *r, uint16_t frame_seq, int32_t keep)
{
    for (uint32_t i = 0U; i < TS_REASM_FRAMES; i++) {
        if (r->reasm[i].used && ((int32_t)i != keep) &&
            (!ts_seq_after(r->reasm[i].frame_seq, frame_seq))) {
            if (!r->x.have_play) {
                r->m.frames_dropped++;
            }
            slot_free(r, i);
        }
    }
}

/* Give up every frame up to and including `frame_seq`; the reference chain is broken. */
static void skip_to(tsr_t *r, uint16_t frame_seq, uint32_t now_ms)
{
    if (r->x.have_play) {
        int16_t d = ts_seq_diff(frame_seq, r->x.play_seq);
        if (d > 0) {
            r->m.frames_dropped += (uint32_t)(uint16_t)d;
        }
    }
    /* before the first decode drop_older counts the freed slots itself */
    drop_older(r, frame_seq, -1);
    r->x.have_play = true;
    r->x.play_seq = frame_seq;
    r->need_keyframe = true;
    TS_LOG(&r->log, now_ms, TS_LOG_INFO, TS_MOD_RECEIVER, (uint16_t)TSR_EV_DROP, (uint32_t)frame_seq, 0U);
}

/* Decode slot i (frames between the play position and it are skipped). true on success. */
static bool decode_slot(tsr_t *r, uint32_t i, uint32_t now_ms)
{
    tsr_reasm_t *s = &r->reasm[i];
    uint16_t fs = s->frame_seq;
    ts_frame_info_t info;
    ts_err_t rc;
    bool ok = false;

    if (r->x.have_play) {
        int16_t d = ts_seq_diff(fs, r->x.play_seq);
        if (d > 1) {
            int16_t skipped = (int16_t)(d - 1);
            r->m.frames_dropped += (uint32_t)(uint16_t)skipped;
        }
    }
    drop_older(r, fs, (int32_t)i);
    r->x.have_play = true;
    r->x.play_seq = fs;

    (void)memset(&info, 0, sizeof(info));
    rc = ts_dec_decode(&r->dec, s->data, (size_t)s->frame_len, r->x.scratch, &info);
    if (rc == TS_OK) {
        tsr_i_on_decoded(r, s, &info, now_ms);
        (void)memcpy(r->frame, r->x.scratch, TS_FRAME_BYTES);
        r->have_decoded = true;
        r->last_decoded_seq = fs;
        r->need_keyframe = false;
        ok = true;
    } else if (rc == TS_E_NOREF) {
        /* P-frame whose reference we do not hold: wait for a keyframe */
        r->m.frames_dropped++;
        r->need_keyframe = true;
        TS_LOG(&r->log, now_ms, TS_LOG_INFO, TS_MOD_RECEIVER, (uint16_t)TSR_EV_DROP, (uint32_t)fs, (uint32_t)rc);
    } else {
        r->m.decode_errors++;
        r->need_keyframe = true;
        TS_LOG(&r->log, now_ms, TS_LOG_WARN, TS_MOD_RECEIVER, (uint16_t)TSR_EV_DECODE_ERR, (uint32_t)fs, (uint32_t)rc);
    }
    slot_free(r, i);
    return ok;
}

/* Some packet after the first missing fragment of `s` has arrived (loss, not pacing). */
static bool slot_gap_evident(const tsr_t *r, const tsr_reasm_t *s)
{
    bool ev = false;
    if (r->have_pkt_seq && (s->frags_have > 0U)) {
        uint32_t known = TS_MAX_FRAGS;
        uint32_t missing = TS_MAX_FRAGS;
        for (uint32_t j = 0U; j < (uint32_t)s->frag_count; j++) {
            if ((s->have[j] != 0U) && (known == TS_MAX_FRAGS)) {
                known = j;
            }
            if ((s->have[j] == 0U) && (missing == TS_MAX_FRAGS)) {
                missing = j;
            }
        }
        if ((known < TS_MAX_FRAGS) && (missing < TS_MAX_FRAGS)) {
            uint16_t base = (uint16_t)(s->pkt_seq[known] - (uint16_t)known);
            uint16_t want = (uint16_t)(base + (uint16_t)missing);
            uint16_t highest = (uint16_t)(r->pkt_seq_expect - 1U);
            ev = ts_seq_after(highest, want);
        }
    }
    return ev;
}

/* Should the awaited frame `next` (slot n, or -1 when never seen) be given up? */
static bool awaited_abandoned(tsr_t *r, int32_t n, uint16_t next, uint32_t now_ms)
{
    bool ab = false;
    uint32_t tmo = tsr_i_frame_timeout(r);
    if (r->x.abandon_next && (!ts_seq_after(r->x.abandon_seq, r->x.play_seq))) {
        r->x.abandon_next = false;   /* stale give-up for a frame already passed */
    }
    if (r->x.abandon_next && (r->x.abandon_seq == next)) {
        ab = true;
    } else if (n >= 0) {
        const tsr_reasm_t *s = &r->reasm[(uint32_t)n];
        uint32_t idle = now_ms - s->last_rx_ms;
        if (r->x.slot_giveup[(uint32_t)n] != 0U) {
            ab = true;
        } else if ((idle >= tmo) && slot_gap_evident(r, s)) {
            ab = true;
        } else if (idle >= TSR_STALE_MS) {
            ab = true;
        } else {
            ab = false;
        }
    } else {
        /* never seen: give up once a later frame has been waiting long enough */
        int32_t o = slot_oldest(r, false, false, true);
        if ((o >= 0) && ((now_ms - r->reasm[(uint32_t)o].first_rx_ms) >= tmo)) {
            ab = true;
        }
    }
    return ab;
}

void tsr_i_decode_pass(tsr_t *r, uint32_t now_ms)
{
    bool progress = true;
    uint32_t guard = 0U;
    while (progress && (guard < (4U * TS_REASM_FRAMES))) {
        int32_t key = slot_oldest(r, true, true, true);
        progress = false;
        guard++;
        if (!r->x.have_play) {
            /* start-up: the first decodable frame must be a keyframe */
            int32_t o = slot_oldest(r, true, false, false);
            if (key >= 0) {
                (void)decode_slot(r, (uint32_t)key, now_ms);
                progress = true;
            } else if (o >= 0) {
                int32_t kin = slot_oldest(r, false, true, false);
                if ((kin < 0) || (ts_seq_diff(r->reasm[(uint32_t)kin].frame_seq,
                                              r->reasm[(uint32_t)o].frame_seq) > 0)) {
                    /* complete P-frame and no older keyframe on its way: useless */
                    r->m.frames_dropped++;
                    r->need_keyframe = true;
                    slot_free(r, (uint32_t)o);
                    progress = true;
                }
            } else {
                /* nothing complete yet */
            }
        } else {
            uint16_t next = (uint16_t)(r->x.play_seq + 1U);
            int32_t n = tsr_i_find_slot(r, next);
            bool drop_policy = (r->cfg.late_policy == TSR_LATE_DROP);
            if ((n >= 0) && r->reasm[(uint32_t)n].complete) {
                (void)decode_slot(r, (uint32_t)n, now_ms);
                progress = true;
            } else if (key >= 0) {
                (void)decode_slot(r, (uint32_t)key, now_ms);
                progress = true;
            } else if (drop_policy && r->need_keyframe && (n >= 0) && (!slot_is_key(&r->reasm[(uint32_t)n]))) {
                /* chain broken: an incomplete P-frame cannot be decoded anyway */
                skip_to(r, next, now_ms);
                progress = true;
            } else if (awaited_abandoned(r, n, next, now_ms)) {
                r->x.abandon_next = false;
                if (drop_policy) {
                    skip_to(r, next, now_ms);
                    progress = true;
                } else {
                    /* TSR_LATE_DECODE: ask for a keyframe but keep the awaited frame; a late
                     * completion still decodes (it is the reference the decoder waits for) */
                    r->need_keyframe = true;
                    if (n >= 0) {
                        r->x.slot_giveup[(uint32_t)n] = 0U;
                        r->reasm[(uint32_t)n].last_rx_ms = now_ms;
                    }
                }
            } else {
                /* wait */
            }
        }
    }
}
