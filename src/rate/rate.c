/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- src/rate/rate.c
 * Token buckets (per client + global cap), loss adaptation and the keyframe
 * scheduler (include/ts_rate.h, PROTOCOL.md section 8).
 *
 * Units: kbps = 1000 bit/s, so `kbps` is exactly the number of bits earned per
 * millisecond and a bucket earns kbps * elapsed_ms / 8 BYTES. Time differences
 * are u32 serial differences (now - last), so the 2^32 ms clock wrap is
 * harmless. Pure functions of their state and `now_ms`; no heap, no stdio.
 */
#include <stddef.h>
#include <string.h>
#include "ts_rate.h"

#define RATE_BITS_PER_BYTE  8U

/* ---- Token bucket ---------------------------------------------------------- */
void ts_bucket_init(ts_bucket_t *b, uint32_t kbps, uint32_t burst_bytes, uint32_t now_ms)
{
    if (b != NULL) {
        (void)memset(b, 0, sizeof(*b));
        b->kbps = kbps;
        b->burst_bytes = burst_bytes;
        b->tokens = burst_bytes;
        b->last_ms = now_ms;
    }
}

void ts_bucket_set_rate(ts_bucket_t *b, uint32_t kbps)
{
    if (b != NULL) {
        b->kbps = kbps;
    }
}

void ts_bucket_refill(ts_bucket_t *b, uint32_t now_ms)
{
    if (b != NULL) {
        if ((b->kbps == 0U) || (b->tokens >= b->burst_bytes)) {
            /* unlimited, or already full: nothing to bank, restart the clock */
            b->tokens = b->burst_bytes;
            b->last_ms = now_ms;
        } else {
            uint32_t elapsed = now_ms - b->last_ms;
            uint64_t add = ((uint64_t)elapsed * (uint64_t)b->kbps) / (uint64_t)RATE_BITS_PER_BYTE;
            /* While less than one byte was earned the clock is not advanced, so
             * slow rates and fine ticks still accumulate (error < 1 byte per refill). */
            if (add > 0U) {
                uint32_t room = b->burst_bytes - b->tokens;
                if (add >= (uint64_t)room) {
                    b->tokens = b->burst_bytes;
                } else {
                    b->tokens += (uint32_t)add;
                }
                b->last_ms = now_ms;
            }
        }
    }
}

bool ts_bucket_take(ts_bucket_t *b, uint32_t bytes)
{
    bool ok = false;

    if (b != NULL) {
        if (b->kbps == 0U) {
            ok = true;
        } else if (b->tokens >= bytes) {
            b->tokens -= bytes;
            ok = true;
        } else {
            b->refused++;
        }
        if (ok) {
            b->taken += bytes;
        }
    }
    return ok;
}

uint32_t ts_bucket_tokens(const ts_bucket_t *b)
{
    return (b != NULL) ? b->tokens : 0U;
}

uint32_t ts_bucket_wait_ms(const ts_bucket_t *b, uint32_t bytes)
{
    uint32_t ms = 0U;

    if ((b != NULL) && (b->kbps != 0U) && (b->tokens < bytes)) {
        if (bytes > b->burst_bytes) {
            ms = UINT32_MAX;            /* can never be granted at this capacity */
        } else {
            uint64_t need_bits = (uint64_t)(bytes - b->tokens) * (uint64_t)RATE_BITS_PER_BYTE;
            uint64_t w = (need_bits + (uint64_t)b->kbps - 1U) / (uint64_t)b->kbps;
            ms = (w > (uint64_t)UINT32_MAX) ? UINT32_MAX : (uint32_t)w;
        }
    }
    return ms;
}

static bool bucket_has(const ts_bucket_t *b, uint32_t bytes)
{
    return (b == NULL) || (b->kbps == 0U) || (b->tokens >= bytes);
}

bool ts_bucket_take_pair(ts_bucket_t *b, ts_bucket_t *global, uint32_t bytes)
{
    bool ok = false;

    if (b != NULL) {
        bool ok_b = bucket_has(b, bytes);
        bool ok_g = bucket_has(global, bytes);
        if (ok_b && ok_g) {
            (void)ts_bucket_take(b, bytes);
            if (global != NULL) {
                (void)ts_bucket_take(global, bytes);
            }
            ok = true;
        } else {
            if (!ok_b) {
                b->refused++;
            }
            if ((!ok_g) && (global != NULL)) {
                global->refused++;
            }
        }
    }
    return ok;
}

/* ---- Loss adaptation ------------------------------------------------------- */
void ts_loss_init(ts_loss_t *l, uint32_t now_ms)
{
    if (l != NULL) {
        (void)memset(l, 0, sizeof(*l));
        l->window_start_ms = now_ms;
        l->quality = 1U;
    }
}

void ts_loss_on_sent(ts_loss_t *l, uint32_t pkts)
{
    if (l != NULL) {
        l->pkts += pkts;
    }
}

void ts_loss_on_nack(ts_loss_t *l, uint32_t entries)
{
    if (l != NULL) {
        l->nacks += entries;
    }
}

uint32_t ts_loss_ratio_pct(const ts_loss_t *l)
{
    uint32_t pct = 0U;

    if ((l != NULL) && (l->nacks > 0U)) {
        if (l->pkts == 0U) {
            pct = 100U;
        } else {
            uint64_t r = ((uint64_t)l->nacks * 100U) / (uint64_t)l->pkts;
            pct = (r > 100U) ? 100U : (uint32_t)r;
        }
    }
    return pct;
}

uint8_t ts_loss_update(ts_loss_t *l, uint32_t now_ms)
{
    uint8_t q = 1U;

    if (l != NULL) {
        if ((now_ms - l->window_start_ms) >= TS_LOSS_WINDOW_MS) {
            /* Strictly above the threshold steps down at once (1 -> 2 -> 4); a
             * whole window without any NACK steps up one level (slow up). */
            if (ts_loss_ratio_pct(l) > TS_LOSS_STEP_DOWN_PCT) {
                if (l->quality < (uint8_t)TS_QUALITY_MAX) {
                    l->quality = (uint8_t)(l->quality * 2U);
                    l->steps_down++;
                }
            } else if (l->nacks == 0U) {
                if (l->quality > 1U) {
                    l->quality = (uint8_t)(l->quality / 2U);
                    l->steps_up++;
                }
            } else {
                /* 0 < ratio <= threshold: hold */
            }
            l->window_start_ms = now_ms;
            l->pkts = 0U;
            l->nacks = 0U;
        }
        q = l->quality;
    }
    return q;
}

/* ---- Keyframe scheduler ---------------------------------------------------- */
void ts_keysched_init(ts_keysched_t *k, uint32_t now_ms)
{
    if (k != NULL) {
        (void)memset(k, 0, sizeof(*k));
        k->last_key_ms = now_ms;
        k->period_ms = TS_KEYFRAME_PERIOD_MS;
        k->cooldown_ms = TS_KEYFRAME_COOLDOWN_MS;
        k->forced = true;                   /* the first frame is a keyframe */
        k->last_reason = TS_KEY_NONE;
        k->pending_reason = TS_KEY_NONE;
    }
}

void ts_keysched_request(ts_keysched_t *k, ts_key_reason_t why)
{
    if (k != NULL) {
        if (why == TS_KEY_FORCED) {
            k->forced = true;
        } else if ((why == TS_KEY_REQUEST) || (why == TS_KEY_TOO_OLD) ||
                   (why == TS_KEY_SCENE) || (why == TS_KEY_PERIODIC)) {
            k->requests_total++;
            if (k->requested) {
                k->requests_dropped++;      /* coalesced into the pending one */
            } else {
                k->requested = true;
                k->pending_reason = why;
            }
        } else {
            /* TS_KEY_NONE / unknown: ignored */
        }
    }
}

void ts_keysched_force(ts_keysched_t *k)
{
    if (k != NULL) {
        k->forced = true;
    }
}

ts_key_reason_t ts_keysched_decide(ts_keysched_t *k, uint32_t now_ms, uint16_t changed_tiles)
{
    ts_key_reason_t r = TS_KEY_NONE;

    if (k != NULL) {
        uint32_t since = now_ms - k->last_key_ms;
        bool cooled = since >= k->cooldown_ms;
        bool scene = ((uint32_t)changed_tiles * 100U) > (TS_SCENE_CHANGE_PCT * TS_TILES);

        if (k->forced) {
            r = TS_KEY_FORCED;
        } else if (k->requested && cooled) {
            r = k->pending_reason;          /* deferred until the cooldown passed */
        } else if (scene && cooled) {
            r = TS_KEY_SCENE;
        } else if ((k->period_ms != 0U) && (since >= k->period_ms)) {
            r = TS_KEY_PERIODIC;
        } else {
            r = TS_KEY_NONE;
        }
        k->last_reason = r;
    }
    return r;
}

void ts_keysched_sent(ts_keysched_t *k, uint32_t now_ms)
{
    if (k != NULL) {
        k->last_key_ms = now_ms;
        k->forced = false;
        k->requested = false;
        k->pending_reason = TS_KEY_NONE;
    }
}

/* ---- Congestion control ---------------------------------------------------- */
void ts_cc_init(ts_cc_t *cc, uint32_t now_ms)
{
    if (cc != NULL) {
        (void)memset(cc, 0, sizeof(*cc));
        cc->window_start_ms = now_ms;
        cc->quality = 1U;
    }
}

void ts_cc_on_frame(ts_cc_t *cc, uint32_t wire_bytes, bool keyframe)
{
    if (cc != NULL) {
        cc->frames++;
        if (!keyframe) {
            cc->p_frames++;
            cc->p_bytes = ((UINT32_MAX - cc->p_bytes) < wire_bytes) ? UINT32_MAX : (cc->p_bytes + wire_bytes);
        }
    }
}

void ts_cc_on_backlog(ts_cc_t *cc, uint32_t backlog_ms)
{
    if (cc != NULL) {
        if ((!cc->have_backlog) || (backlog_ms < cc->min_backlog_ms)) {
            cc->min_backlog_ms = backlog_ms;
            cc->have_backlog = true;
        }
    }
}

uint32_t ts_cc_backlog_ms(uint32_t pending_pkts, uint32_t kbps)
{
    uint32_t ms = 0U;
    if ((kbps != 0U) && (pending_pkts != 0U)) {
        /* kbps = bits per ms */
        const uint32_t pkt_bytes = TS_PKT_MAX;
        uint64_t bits = (uint64_t)pending_pkts * (uint64_t)pkt_bytes * (uint64_t)RATE_BITS_PER_BYTE;
        uint64_t w = (bits + (uint64_t)kbps - 1U) / (uint64_t)kbps;
        ms = (w > (uint64_t)UINT32_MAX) ? UINT32_MAX : (uint32_t)w;
    }
    return ms;
}

/* Stream rate of the window from its P-frames: mean P-frame size x frame rate. */
static uint32_t cc_estimate(const ts_cc_t *cc, uint32_t elapsed_ms)
{
    uint32_t kbps = 0U;
    if ((cc->p_frames > 0U) && (elapsed_ms > 0U)) {
        uint64_t bits = ((uint64_t)cc->p_bytes * (uint64_t)cc->frames * (uint64_t)RATE_BITS_PER_BYTE) /
                        (uint64_t)cc->p_frames;
        uint64_t r = bits / (uint64_t)elapsed_ms;
        kbps = (r > (uint64_t)UINT32_MAX) ? UINT32_MAX : (uint32_t)r;
    }
    return kbps;
}

uint8_t ts_cc_update(ts_cc_t *cc, uint32_t now_ms, uint32_t limit_kbps)
{
    uint8_t q = 1U;

    if (cc != NULL) {
        uint32_t elapsed = now_ms - cc->window_start_ms;
        if (elapsed >= TS_LOSS_WINDOW_MS) {
            uint32_t est = cc_estimate(cc, elapsed);
            bool queue = cc->have_backlog && (cc->min_backlog_ms > TS_CC_QUEUE_MS);
            bool calm = (!cc->have_backlog) || (cc->min_backlog_ms <= (TS_CC_QUEUE_MS / 2U));
            uint64_t est100 = (uint64_t)est * 100U;
            bool over = (limit_kbps != 0U) && (est100 > ((uint64_t)limit_kbps * TS_CC_HIGH_PCT));
            bool fits_up = (limit_kbps == 0U) || ((est100 * 2U) <= ((uint64_t)limit_kbps * TS_CC_LOW_PCT));

            if ((over || queue) && (cc->p_frames > 0U)) {
                if (cc->quality < (uint8_t)TS_QUALITY_MAX) {
                    cc->quality = (uint8_t)(cc->quality * 2U);
                    cc->steps_down++;
                }
            } else if ((cc->quality > 1U) && fits_up && calm && ((cc->p_frames > 0U) || (limit_kbps == 0U))) {
                cc->quality = (uint8_t)(cc->quality / 2U);
                cc->steps_up++;
            } else {
                /* hold */
            }
            if (cc->p_frames > 0U) {
                cc->est_kbps = est;
            }
            cc->window_start_ms = now_ms;
            cc->p_bytes = 0U;
            cc->p_frames = 0U;
            cc->frames = 0U;
            cc->have_backlog = false;
            cc->min_backlog_ms = 0U;
        }
        q = cc->quality;
    }
    return q;
}
