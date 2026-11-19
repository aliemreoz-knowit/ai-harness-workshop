/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tools/tsrecv/lib/nack.c
 * Loss detection and repair requests.
 *   - pkt_seq gap detection with serial arithmetic (PROTOCOL.md 7): a packet
 *     ahead of the expected one opens one "missing" entry per skipped pkt_seq,
 *     a packet behind it (reordered or retransmitted, retransmits keep their
 *     pkt_seq) closes the matching entry. Jumps above TSR_GAP_TRACK_MAX are an
 *     outage: counted lost at once and left to the keyframe path.
 *   - a missing pkt_seq is mapped to (frame_seq, frag_idx) through the
 *     remembered pkt_seq range of each frame (fresh fragments of one frame are
 *     consecutive on the wire); a frame lost completely between two known
 *     frames F and F+2 is inferred as the range between them.
 *   - NACK rounds: the first after nack_delay_ms (reordering tolerance), the
 *     second ~1 RTT estimate later, then the packet is given up (counted lost)
 *     and a KEYFRAME_REQ goes out over TCP. Entries are chunked into NACK
 *     packets of at most TS_NACK_MAX and limited to max_nack_per_tick per tick.
 *   - RTT estimate: EWMA of NACK -> retransmit samples.
 */
#include <stddef.h>
#include <string.h>
#include "tsr_internal.h"

static void miss_add(tsr_t *r, uint16_t pkt_seq, uint32_t now_ms);
static void miss_fill(tsr_t *r, uint16_t pkt_seq, bool retransmit, uint32_t now_ms);
static void miss_compact(tsr_t *r);
static void miss_lost(tsr_t *r, tsr_miss_t *e);
static void nack_send(tsr_t *r, ts_nack_t *n, uint32_t now_ms);
static void mark_giveup(tsr_t *r, uint16_t frame_seq, uint32_t now_ms);
static void rtt_sample(tsr_t *r, uint32_t sample_ms);

/* ---- missing list (ring ordered by detection) ---------------------------------------- */
void tsr_i_nack_reset(tsr_t *r)
{
    r->x.miss_head = 0U;
    r->x.miss_count = 0U;
    r->x.frange_next = 0U;
    (void)memset(r->x.frange, 0, sizeof(r->x.frange));
    r->x.abandon_next = false;
}

static void miss_lost(tsr_t *r, tsr_miss_t *e)
{
    e->open = 0U;
    r->m.pkts_lost++;
}

static void miss_add(tsr_t *r, uint16_t pkt_seq, uint32_t now_ms)
{
    tsr_miss_t *e;
    if (r->x.miss_count >= (uint16_t)TSR_MISS_CAP) {
        /* full: the oldest entry is forced out */
        tsr_miss_t *old = &r->x.miss[r->x.miss_head];
        if (old->open != 0U) {
            miss_lost(r, old);
        }
        r->x.miss_head = (uint16_t)(((uint32_t)r->x.miss_head + 1U) % TSR_MISS_CAP);
        r->x.miss_count--;
    }
    e = &r->x.miss[((uint32_t)r->x.miss_head + (uint32_t)r->x.miss_count) % TSR_MISS_CAP];
    e->pkt_seq = pkt_seq;
    e->rounds = 0U;
    e->open = 1U;
    e->detect_ms = now_ms;
    e->nack_ms = 0U;
    r->x.miss_count++;
}

static void rtt_sample(tsr_t *r, uint32_t sample_ms)
{
    if (r->x.rtt_ms == 0U) {
        r->x.rtt_ms = sample_ms;
    } else {
        r->x.rtt_ms = ((7U * r->x.rtt_ms) + sample_ms) / 8U;
    }
}

static void miss_fill(tsr_t *r, uint16_t pkt_seq, bool retransmit, uint32_t now_ms)
{
    bool found = false;
    for (uint32_t i = 0U; (i < (uint32_t)r->x.miss_count) && (!found); i++) {
        tsr_miss_t *e = &r->x.miss[((uint32_t)r->x.miss_head + i) % TSR_MISS_CAP];
        if ((e->open != 0U) && (e->pkt_seq == pkt_seq)) {
            found = true;
            e->open = 0U;
            if (retransmit && (e->rounds == 1U)) {
                rtt_sample(r, now_ms - e->nack_ms);
            } else if (retransmit && (e->rounds > 1U)) {
                /* ambiguous (Karn): measure from the first round */
                rtt_sample(r, now_ms - (e->detect_ms + r->cfg.nack_delay_ms));
            } else {
                /* reordered original: no sample */
            }
        }
    }
    miss_compact(r);
}

static void miss_compact(tsr_t *r)
{
    while ((r->x.miss_count > 0U) && (r->x.miss[r->x.miss_head].open == 0U)) {
        r->x.miss_head = (uint16_t)(((uint32_t)r->x.miss_head + 1U) % TSR_MISS_CAP);
        r->x.miss_count--;
    }
}

void tsr_i_gap_track(tsr_t *r, uint16_t pkt_seq, bool retransmit, uint32_t now_ms)
{
    if (!r->have_pkt_seq) {
        r->have_pkt_seq = true;
        r->pkt_seq_expect = (uint16_t)(pkt_seq + 1U);
        r->m.pkts_expected++;
    } else {
        int16_t d = ts_seq_diff(pkt_seq, r->pkt_seq_expect);
        if (d == 0) {
            r->pkt_seq_expect = (uint16_t)(pkt_seq + 1U);
            r->m.pkts_expected++;
        } else if (d > 0) {
            uint32_t gap = (uint32_t)(uint16_t)d;
            r->m.pkts_missing += gap;
            r->m.pkts_expected += gap + 1U;
            if (gap > TSR_GAP_TRACK_MAX) {
                r->m.pkts_lost += gap;   /* outage: nothing worth NACKing */
            } else {
                for (uint32_t k = 0U; k < gap; k++) {
                    miss_add(r, (uint16_t)(r->pkt_seq_expect + (uint16_t)k), now_ms);
                }
            }
            r->pkt_seq_expect = (uint16_t)(pkt_seq + 1U);
        } else {
            miss_fill(r, pkt_seq, retransmit, now_ms);
        }
    }
}

/* ---- frame -> pkt_seq ranges ---------------------------------------------------------- */
void tsr_i_frange_learn(tsr_t *r, uint16_t frame_seq, uint16_t base, uint8_t count)
{
    bool known = false;
    for (uint32_t i = 0U; (i < TSR_FRANGE_CAP) && (!known); i++) {
        const tsr_frange_t *f = &r->x.frange[i];
        if ((f->used != 0U) && (f->frame_seq == frame_seq)) {
            known = true;
        }
    }
    if (!known) {
        tsr_frange_t *f = &r->x.frange[r->x.frange_next];
        f->frame_seq = frame_seq;
        f->base = base;
        f->count = count;
        f->used = 1U;
        r->x.frange_next = (uint8_t)(((uint32_t)r->x.frange_next + 1U) % TSR_FRANGE_CAP);
    }
}

bool tsr_i_map_pkt(const tsr_t *r, uint16_t pkt_seq, uint16_t *frame_seq, uint8_t *frag_idx)
{
    bool found = false;
    int32_t below = -1;
    int32_t above = -1;
    int32_t below_d = 0;
    int32_t above_d = 0;
    for (uint32_t i = 0U; (i < TSR_FRANGE_CAP) && (!found); i++) {
        const tsr_frange_t *f = &r->x.frange[i];
        if (f->used != 0U) {
            int32_t off = (int32_t)ts_seq_diff(pkt_seq, f->base);
            int32_t cnt = (int32_t)f->count;
            if ((off >= 0) && (off < cnt)) {
                *frame_seq = f->frame_seq;
                *frag_idx = (uint8_t)off;
                found = true;
            } else if (off >= cnt) {
                int32_t dist = off - cnt;
                if ((below < 0) || (dist < below_d)) {
                    below = (int32_t)i;
                    below_d = dist;
                }
            } else {
                int32_t dist = -off;
                if ((above < 0) || (dist < above_d)) {
                    above = (int32_t)i;
                    above_d = dist;
                }
            }
        }
    }
    if ((!found) && (below >= 0) && (above >= 0)) {
        const tsr_frange_t *a = &r->x.frange[(uint32_t)below];
        const tsr_frange_t *b = &r->x.frange[(uint32_t)above];
        int32_t dframes = (int32_t)ts_seq_diff(b->frame_seq, a->frame_seq);
        uint16_t gbase = (uint16_t)(a->base + (uint16_t)a->count);
        int32_t gcount = (int32_t)ts_seq_diff(b->base, gbase);
        int32_t off = (int32_t)ts_seq_diff(pkt_seq, gbase);
        if ((gcount > 0) && (off >= 0) && (off < gcount)) {
            if ((dframes == 2) && (gcount <= (int32_t)TS_MAX_FRAGS)) {
                /* exactly one frame missing between a and b: it owns the whole gap */
                *frame_seq = (uint16_t)(a->frame_seq + 1U);
                *frag_idx = (uint8_t)off;
                found = true;
            } else if ((dframes > 2) && (gcount == (dframes - 1))) {
                /* as many packets as missing frames: one single-fragment frame each */
                int32_t step = off + 1;
                *frame_seq = (uint16_t)(a->frame_seq + (uint16_t)step);
                *frag_idx = 0U;
                found = true;
            } else {
                /* ambiguous: leave it to the keyframe path */
            }
        }
    }
    return found;
}

/* ---- NACK rounds ------------------------------------------------------------------------ */
uint32_t tsr_i_retry_ival(const tsr_t *r)
{
    uint32_t rtt = (r->x.rtt_ms == 0U) ? TSR_RTT_INIT_MS : r->x.rtt_ms;
    uint32_t ival = rtt + (rtt / 2U) + 5U;
    uint32_t hi = r->cfg.nack_retry_ms / TSR_NACK_ROUNDS;
    if (ival > hi) {
        ival = hi;
    }
    if (ival < TSR_RETRY_MIN_MS) {
        ival = TSR_RETRY_MIN_MS;
    }
    return ival;
}

static void nack_send(tsr_t *r, ts_nack_t *n, uint32_t now_ms)
{
    uint8_t buf[TS_NACK_PKT_MAX];
    size_t len = 0U;
    if (ts_nack_pack(n, buf, sizeof(buf), &len) == TS_OK) {
        if (ts_io_udp_send(r->io, &r->x.data_from, buf, len) > 0) {
            r->m.nacks_sent += (uint32_t)n->count;
            r->x.last_udp_tx_ms = now_ms;
        }
    }
    n->count = 0U;
}

static void mark_giveup(tsr_t *r, uint16_t frame_seq, uint32_t now_ms)
{
    int32_t s = tsr_i_find_slot(r, frame_seq);
    if (s >= 0) {
        r->x.slot_giveup[(uint32_t)s] = 1U;
        r->reasm[(uint32_t)s].nack_sent_ms = now_ms;
    } else {
        r->x.abandon_next = true;
        r->x.abandon_seq = frame_seq;
    }
    TS_LOG(&r->log, now_ms, TS_LOG_INFO, TS_MOD_RECEIVER, (uint16_t)TSR_EV_GIVEUP, (uint32_t)frame_seq, 0U);
    /* the reference chain will break: ask for a keyframe right away */
    tsr_i_request_keyframe(r, now_ms);
}

void tsr_i_nack_tick(tsr_t *r, uint32_t now_ms)
{
    ts_nack_t pkt;
    uint32_t budget = (uint32_t)r->cfg.max_nack_per_tick;
    uint32_t ival = tsr_i_retry_ival(r);
    uint32_t t_lost = r->cfg.nack_delay_ms + r->cfg.nack_retry_ms;
    bool can_nack = (!r->cfg.no_nack) && r->x.have_data_from;
    pkt.count = 0U;

    for (uint32_t i = 0U; i < (uint32_t)r->x.miss_count; i++) {
        tsr_miss_t *e = &r->x.miss[((uint32_t)r->x.miss_head + i) % TSR_MISS_CAP];
        if (e->open != 0U) {
            uint16_t fs = 0U;
            uint8_t fi = 0U;
            uint32_t age = now_ms - e->detect_ms;
            bool mapped = tsr_i_map_pkt(r, e->pkt_seq, &fs, &fi);
            int32_t slot = mapped ? tsr_i_find_slot(r, fs) : -1;
            if (mapped && r->x.have_play && (!ts_seq_after(fs, r->x.play_seq))) {
                miss_lost(r, e);          /* its frame was decoded or skipped already */
            } else if ((slot >= 0) && (r->reasm[(uint32_t)slot].have[fi] != 0U)) {
                e->open = 0U;             /* arrived under another pkt_seq: not lost */
            } else if (!can_nack) {
                if (age >= t_lost) {
                    miss_lost(r, e);
                }
            } else if (!mapped) {
                /* a NACK cannot name it (several frames lost, sizes unknown): the reference
                 * chain is broken, so ask for a keyframe now instead of after the timeout */
                if ((e->rounds == 0U) && (age >= r->cfg.nack_delay_ms)) {
                    e->rounds = TSR_ROUNDS_KEYREQ;
                    tsr_i_request_keyframe(r, now_ms);
                }
                if (age >= t_lost) {
                    miss_lost(r, e);
                }
            } else if (e->rounds == 0U) {
                if ((age >= r->cfg.nack_delay_ms) && (budget > 0U)) {
                    pkt.e[pkt.count].frame_seq = fs;
                    pkt.e[pkt.count].frag_idx = fi;
                    pkt.count++;
                    budget--;
                    e->rounds = 1U;
                    e->nack_ms = now_ms;
                }
            } else if ((uint32_t)e->rounds < TSR_NACK_ROUNDS) {
                if (((now_ms - e->nack_ms) >= ival) && (budget > 0U)) {
                    pkt.e[pkt.count].frame_seq = fs;
                    pkt.e[pkt.count].frag_idx = fi;
                    pkt.count++;
                    budget--;
                    e->rounds++;
                    e->nack_ms = now_ms;
                }
            } else {
                if ((now_ms - e->nack_ms) >= ival) {
                    miss_lost(r, e);
                    r->m.nack_giveups++;
                    mark_giveup(r, fs, now_ms);
                }
            }
            if ((uint32_t)pkt.count >= TS_NACK_MAX) {
                nack_send(r, &pkt, now_ms);
            }
        }
    }
    if (pkt.count > 0U) {
        nack_send(r, &pkt, now_ms);
    }
    miss_compact(r);
}

void tsr_i_keepalive(tsr_t *r, uint32_t now_ms)
{
    if ((r->state == TSR_STREAMING) && r->x.have_data_from &&
        ((now_ms - r->x.last_udp_tx_ms) >= TSR_KEEPALIVE_MS)) {
        ts_nack_t empty;
        empty.count = 0U;
        nack_send(r, &empty, now_ms);
        r->x.last_udp_tx_ms = now_ms;
    }
}
