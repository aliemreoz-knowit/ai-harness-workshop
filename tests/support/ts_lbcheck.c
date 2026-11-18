/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/support/ts_lbcheck.c
 * Loopback scenario checker (see ts_lbcheck.h). Test glue: reads public
 * daemon / receiver state after each virtual step, never changes it.
 */
#include <stddef.h>
#include <string.h>
#include "ts_lbcheck.h"

static void lb_record(ts_lb_t *lb)
{
    const tsd_t *d = lb->L->d;
    if (d != NULL) {
        if (d->state == TSD_STOPPED) {
            lb->daemon_stopped++;
        }
        if (!lb->have_seq) {
            lb->have_seq = true;
            lb->seq_next_seen = d->frame_seq_next;
        } else if (d->frame_seq_next != lb->seq_next_seen) {
            uint16_t seq = (uint16_t)(d->frame_seq_next - 1U);
            uint16_t encoded = (uint16_t)(d->frame_seq_next - lb->seq_next_seen);
            ts_lb_truth_t *t = &lb->truth[(uint32_t)seq % TS_LB_TRUTH_SLOTS];
            t->used = true;
            t->frame_seq = seq;
            (void)memcpy(t->raw, d->frame, TS_FRAME_BYTES);
            (void)memcpy(t->recon, d->enc.ref, TS_FRAME_BYTES);
            lb->recorded++;
            lb->skipped += (uint32_t)encoded - 1U;
            lb->seq_next_seen = d->frame_seq_next;
        } else {
            /* nothing encoded in this step */
        }
    }
}

static void lb_check_rx(ts_lb_t *lb, uint8_t i, uint32_t now_ms)
{
    const tsr_t *r = lb->L->rx[i];
    ts_lb_rx_t *x = &lb->rx[i];
    if (r != NULL) {
        const tsr_metrics_t *m = tsr_metrics(r);
        if (tsr_state(r) == TSR_CLOSED) {
            lb->rx_closed++;
        }
        if ((m != NULL) && (m->frames_decoded != x->decoded_seen)) {
            const uint8_t *frame = tsr_frame(r);
            const ts_lb_truth_t *t = ts_lb_truth(lb, r->last_decoded_seq);
            if ((m->frames_decoded - x->decoded_seen) > 1U) {
                x->multi++;
            }
            x->decoded_seen = m->frames_decoded;
            if ((t != NULL) && (frame != NULL)) {
                x->compared++;
                if (memcmp(frame, t->raw, TS_FRAME_BYTES) == 0) {
                    x->exact_raw++;
                }
                if (memcmp(frame, t->recon, TS_FRAME_BYTES) == 0) {
                    x->exact_recon++;
                } else if (x->mismatch_seq == 0U) {
                    x->mismatch_seq = (uint32_t)r->last_decoded_seq + 1U;
                } else {
                    /* first mismatch already noted */
                }
            } else {
                x->no_truth++;
            }
            if (x->have_last) {
                uint32_t gap = now_ms - x->last_decode_ms;
                if (gap > x->max_gap_ms) {
                    x->max_gap_ms = gap;
                }
            } else {
                x->first_decode_ms = now_ms;
            }
            x->have_last = true;
            x->last_seq = r->last_decoded_seq;
            x->last_decode_ms = now_ms;
        }
    }
}

void ts_lb_attach(ts_lb_t *lb, ts_loop_t *L)
{
    if (lb != NULL) {
        (void)memset(lb, 0, sizeof(*lb));
        lb->L = L;
        if (L != NULL) {
            lb_record(lb);
            for (uint8_t i = 0U; i < L->cfg.receivers; i++) {
                if (L->rx[i] != NULL) {
                    lb->rx[i].decoded_seen = tsr_metrics(L->rx[i])->frames_decoded;
                }
            }
        }
    }
}

void ts_lb_step(ts_lb_t *lb)
{
    if ((lb != NULL) && (lb->L != NULL)) {
        uint32_t t = lb->L->now_ms;   /* virtual time of this step */
        ts_loop_step(lb->L);
        lb_record(lb);
        for (uint8_t i = 0U; i < lb->L->cfg.receivers; i++) {
            lb_check_rx(lb, i, t);
        }
    }
}

void ts_lb_run(ts_lb_t *lb, uint32_t ms)
{
    if ((lb != NULL) && (lb->L != NULL)) {
        uint32_t end = lb->L->now_ms + ms;
        while ((uint32_t)(end - lb->L->now_ms) - 1U < 0x7FFFFFFFU) {
            ts_lb_step(lb);
        }
    }
}

uint32_t ts_lb_decoded(const ts_lb_t *lb, uint8_t i)
{
    uint32_t n = 0U;
    if ((lb != NULL) && (lb->L != NULL) && (i < TS_LOOP_MAX_RX) && (lb->L->rx[i] != NULL)) {
        n = tsr_metrics(lb->L->rx[i])->frames_decoded;
    }
    return n;
}

static void lb_daemon_counters(const ts_lb_t *lb, uint32_t *bytes, uint32_t *pkts, uint32_t *retx)
{
    const ts_stats_global_t *g = (lb->L->d != NULL) ? tsd_stats(lb->L->d) : NULL;
    *bytes = (g != NULL) ? g->bytes_sent : 0U;
    *pkts = (g != NULL) ? g->pkts_sent : 0U;
    *retx = (g != NULL) ? g->retx_sent : 0U;
}

void ts_lb_window(ts_lb_t *lb, uint32_t ms, ts_lb_window_t *w)
{
    if ((lb != NULL) && (lb->L != NULL) && (w != NULL)) {
        uint32_t b0;
        uint32_t p0;
        uint32_t r0;
        uint32_t b1;
        uint32_t p1;
        uint32_t r1;
        uint32_t start[TS_LOOP_MAX_RX];
        uint32_t sec[TS_LOOP_MAX_RX];
        uint32_t pk0[TS_LOOP_MAX_RX];
        uint32_t na0[TS_LOOP_MAX_RX];
        uint32_t done = 0U;

        (void)memset(w, 0, sizeof(*w));
        w->ms = ms;
        lb_daemon_counters(lb, &b0, &p0, &r0);
        for (uint8_t i = 0U; i < TS_LOOP_MAX_RX; i++) {
            const tsr_t *r = lb->L->rx[i];
            start[i] = ts_lb_decoded(lb, i);
            sec[i] = start[i];
            pk0[i] = (r != NULL) ? tsr_metrics(r)->pkts_rx : 0U;
            na0[i] = (r != NULL) ? tsr_metrics(r)->nacks_sent : 0U;
            w->min_fps_1s[i] = 0xFFFFFFFFU;
        }
        while (done < ms) {
            uint32_t chunk = ((ms - done) < 1000U) ? (ms - done) : 1000U;
            ts_lb_run(lb, chunk);
            done += chunk;
            if (chunk == 1000U) {
                for (uint8_t i = 0U; i < TS_LOOP_MAX_RX; i++) {
                    uint32_t now = ts_lb_decoded(lb, i);
                    uint32_t n = now - sec[i];
                    if (n < w->min_fps_1s[i]) {
                        w->min_fps_1s[i] = n;
                    }
                    if (n > w->max_fps_1s[i]) {
                        w->max_fps_1s[i] = n;
                    }
                    sec[i] = now;
                }
            }
        }
        lb_daemon_counters(lb, &b1, &p1, &r1);
        w->daemon_bytes = b1 - b0;
        w->daemon_pkts = p1 - p0;
        w->daemon_retx = r1 - r0;
        w->daemon_kbps = (ms > 0U) ? (uint32_t)(((uint64_t)w->daemon_bytes * 8U) / (uint64_t)ms) : 0U;
        for (uint8_t i = 0U; i < TS_LOOP_MAX_RX; i++) {
            const tsr_t *r = lb->L->rx[i];
            w->decoded[i] = ts_lb_decoded(lb, i) - start[i];
            w->fps_x100[i] = (ms > 0U) ? (uint32_t)(((uint64_t)w->decoded[i] * 100000U) / (uint64_t)ms) : 0U;
            if (w->min_fps_1s[i] == 0xFFFFFFFFU) {
                w->min_fps_1s[i] = 0U;
            }
            w->rx_pkts[i] = (r != NULL) ? (tsr_metrics(r)->pkts_rx - pk0[i]) : 0U;
            w->rx_nacks[i] = (r != NULL) ? (tsr_metrics(r)->nacks_sent - na0[i]) : 0U;
        }
    }
}

const ts_lb_truth_t *ts_lb_truth(const ts_lb_t *lb, uint16_t frame_seq)
{
    const ts_lb_truth_t *t = NULL;
    if (lb != NULL) {
        const ts_lb_truth_t *c = &lb->truth[(uint32_t)frame_seq % TS_LB_TRUTH_SLOTS];
        if (c->used && (c->frame_seq == frame_seq)) {
            t = c;
        }
    }
    return t;
}

bool ts_lb_exact_at_least(uint32_t exact, uint32_t compared, uint32_t pct)
{
    return (compared > 0U) && (((uint64_t)exact * 100U) >= ((uint64_t)compared * (uint64_t)pct));
}
