/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tools/tsrecv/lib/metrics.c
 * Receiver metrics (tsr_metrics_t) and their JSON rendering.
 *   fps / kbps       1 s windows
 *   loss             pkt_seq gaps vs pkt_seq span (raw) and unrepaired (residual)
 *   freezes          a gap > TS_FREEZE_MS between decoded frames counts once, at onset
 *   latency          first fragment -> decode; owd = arrival - ts_ms (serial)
 *   jitter           RFC 3550: J += (|D| - J) / 16, D = arrival spacing - ts_ms spacing (serial)
 *   truth            exact-match % of frames and PSNR vs ground truth; without truth,
 *                    PSNR of the last P-frame output vs the keyframe that follows it
 *   tiles            changed-tile count + 20x15 bitmap from the decoder
 * No stdio and no libm: the JSON writer and log10 are implemented here.
 */
#include <stddef.h>
#include <string.h>
#include "tsr_internal.h"

/* ---- pure helpers ------------------------------------------------------------------ */
int32_t tsr_i_owd_ms(uint32_t rx_ms, uint16_t ts_ms)
{
    uint16_t rx16 = (uint16_t)(rx_ms & 0xFFFFU);
    return (int32_t)ts_seq_diff(rx16, ts_ms);
}

uint32_t tsr_i_jitter_step(uint32_t jitter_x16, int32_t d_ms)
{
    int64_t mag = (d_ms < 0) ? -(int64_t)d_ms : (int64_t)d_ms;
    uint32_t ad = (uint32_t)mag;
    return (jitter_x16 + ad) - (jitter_x16 / 16U);
}

/* log2(v) for v >= 1 with ~30 fractional bits, using only + - * / (no libm). */
static double log2_ge1(double v)
{
    double x = v;
    double res = 0.0;
    double frac = 0.5;
    while (x >= 2.0) {
        x = x / 2.0;
        res += 1.0;
    }
    for (uint32_t i = 0U; i < 30U; i++) {
        x = x * x;
        if (x >= 2.0) {
            x = x / 2.0;
            res += frac;
        }
        frac = frac / 2.0;
    }
    return res;
}

uint32_t tsr_i_psnr_cdb(const uint8_t *a, const uint8_t *b)
{
    uint64_t sse = 0U;
    uint32_t cdb = 0U;
    for (uint32_t i = 0U; i < TS_FRAME_BYTES; i++) {
        int32_t d = (int32_t)a[i] - (int32_t)b[i];
        int32_t sq = d * d;
        uint32_t d2 = (uint32_t)sq;
        sse += (uint64_t)d2;
    }
    if (sse != 0U) {
        /* PSNR = 10 log10(255^2 * N / SSE) dB; centi-dB = 1000 * log10(...) */
        static const uint32_t npx = TS_FRAME_BYTES;
        double num = 65025.0 * (double)npx;
        double ratio = num / (double)sse;
        double l2 = (ratio > 1.0) ? log2_ge1(ratio) : 0.0;
        double v = (l2 * 301.02999566398120) + 0.5;   /* 1000 * log10(2) */
        cdb = (uint32_t)v;
    }
    return cdb;
}

/* ---- events ------------------------------------------------------------------------- */
void tsr_i_on_decoded(tsr_t *r, const tsr_reasm_t *s, const ts_frame_info_t *info, uint32_t now_ms)
{
    tsr_metrics_t *m = &r->m;
    uint32_t lat = now_ms - s->first_rx_ms;
    bool have_t = false;

    /* freeze: the gap that just ended (counted at onset if the tick saw it first) */
    if (r->have_decoded) {
        uint32_t gap = now_ms - r->last_decoded_ms;
        if (gap > TS_FREEZE_MS) {
            if (!r->x.in_freeze) {
                m->freezes++;
            }
            m->freeze_ms_total += gap;
        }
    }
    r->x.in_freeze = false;
    m->freeze_ms_current = 0U;
    r->last_decoded_ms = now_ms;

    m->frames_decoded++;
    r->window_frames++;
    if (info->keyframe) {
        m->keyframes++;
    }

    m->latency_ms = lat;
    r->x.latency_n++;
    r->x.latency_sum_ms += (uint64_t)lat;
    m->latency_avg_ms = (uint32_t)(r->x.latency_sum_ms / (uint64_t)r->x.latency_n);
    m->owd_ms = tsr_i_owd_ms(s->first_rx_ms, s->ts_ms);

    if (r->x.have_jitter_ref) {
        uint32_t arr = s->first_rx_ms - r->x.prev_arrival_ms;
        int32_t darr = (int32_t)arr;
        int32_t dts = (int32_t)ts_seq_diff(s->ts_ms, r->x.prev_ts_ms);
        r->x.jitter_x16 = tsr_i_jitter_step(r->x.jitter_x16, darr - dts);
        m->jitter_ms = (r->x.jitter_x16 + 8U) / 16U;
    }
    r->x.have_jitter_ref = true;
    r->x.prev_arrival_ms = s->first_rx_ms;
    r->x.prev_ts_ms = s->ts_ms;

    m->changed_tiles = info->changed_tiles;
    (void)memcpy(m->tile_bitmap, ts_dec_tile_bitmap(&r->dec), TS_TILE_BITMAP_BYTES);

    /* ground truth: per-frame provider first, else the one-shot tsr_set_truth() */
    if (r->x.truth_fn != NULL) {
        have_t = r->x.truth_fn(r->x.truth_ctx, s->frame_seq, r->truth);
    } else if (r->have_truth) {
        have_t = true;
        r->have_truth = false;
    } else {
        have_t = false;
    }
    if (have_t) {
        m->truth_frames++;
        if (memcmp(r->x.scratch, r->truth, TS_FRAME_BYTES) == 0) {
            m->truth_exact++;
        }
        m->exact_match_pct = (uint32_t)(((uint64_t)m->truth_exact * 100U) / (uint64_t)m->truth_frames);
        m->psnr_cdb = tsr_i_psnr_cdb(r->x.scratch, r->truth);
        m->psnr_src = (uint8_t)TSR_PSNR_TRUTH;
    } else if (info->keyframe && r->have_decoded && (r->x.truth_fn == NULL) &&
               (s->frame_seq == (uint16_t)(r->last_decoded_seq + 1U))) {
        /* drift check: what the P-chain showed vs the keyframe that replaces it */
        m->psnr_cdb = tsr_i_psnr_cdb(r->frame, r->x.scratch);
        m->psnr_src = (uint8_t)TSR_PSNR_KEYFRAME;
    } else {
        /* keep the last value */
    }
}

void tsr_i_metrics_tick(tsr_t *r, uint32_t now_ms)
{
    tsr_metrics_t *m = &r->m;
    uint32_t el = now_ms - r->window_start_ms;

    m->uptime_ms = now_ms - r->start_ms;
    m->state = (uint8_t)r->state;
    m->rtt_ms = r->x.rtt_ms;

    if (r->have_decoded) {
        uint32_t gap = now_ms - r->last_decoded_ms;
        if (gap > TS_FREEZE_MS) {
            if (!r->x.in_freeze) {
                r->x.in_freeze = true;
                m->freezes++;
            }
            m->freeze_ms_current = gap;
        } else {
            m->freeze_ms_current = 0U;
        }
    }

    if (el >= 1000U) {
        m->fps_x100 = (uint32_t)(((uint64_t)r->window_frames * 100000U) / (uint64_t)el);
        m->kbps = (uint32_t)(((uint64_t)r->window_bytes * 8U) / (uint64_t)el);
        r->window_frames = 0U;
        r->window_bytes = 0U;
        r->window_start_ms = now_ms;
    }

    if (m->pkts_expected > 0U) {
        m->loss_pct_x100 = (uint32_t)(((uint64_t)m->pkts_missing * 10000U) / (uint64_t)m->pkts_expected);
        m->residual_loss_pct_x100 = (uint32_t)(((uint64_t)m->pkts_lost * 10000U) / (uint64_t)m->pkts_expected);
    }
}

/* ---- JSON writer (bounded, no stdio) --------------------------------------------- */
typedef struct jw {
    char  *buf;
    size_t cap;
    size_t len;
    bool   ovf;
    bool   fresh;   /* just opened an object: no comma before the next key */
} jw_t;

static void jw_ch(jw_t *w, char c);
static void jw_open(jw_t *w);
static void jw_str(jw_t *w, const char *s);
static void jw_u64(jw_t *w, uint64_t v);
static void jw_key(jw_t *w, const char *k);
static void jw_ku(jw_t *w, const char *k, uint32_t v);
static void jw_ki(jw_t *w, const char *k, int32_t v);
static void jw_kfix2(jw_t *w, const char *k, uint32_t v_x100);
static void jw_kstr(jw_t *w, const char *k, const char *v);
static const char *state_name(uint8_t st);
static const char *psnr_src_name(uint8_t src);
static void jw_daemon(jw_t *w, const ts_tlv_stats_t *st);

static void jw_ch(jw_t *w, char c)
{
    if ((w->len + 1U) < w->cap) {
        w->buf[w->len] = c;
        w->len++;
    } else {
        w->ovf = true;
    }
}

static void jw_str(jw_t *w, const char *s)
{
    for (size_t i = 0U; s[i] != '\0'; i++) {
        jw_ch(w, s[i]);
    }
}

static void jw_u64(jw_t *w, uint64_t v)
{
    static const char digits[] = "0123456789";
    char tmp[24];
    uint32_t n = 0U;
    uint64_t x = v;
    do {
        tmp[n] = digits[(uint32_t)(x % 10U)];
        n++;
        x /= 10U;
    } while ((x != 0U) && (n < (uint32_t)sizeof(tmp)));
    while (n > 0U) {
        n--;
        jw_ch(w, tmp[n]);
    }
}

static void jw_open(jw_t *w)
{
    jw_ch(w, '{');
    w->fresh = true;
}

static void jw_key(jw_t *w, const char *k)
{
    if (!w->fresh) {
        jw_ch(w, ',');
    }
    w->fresh = false;
    jw_ch(w, '"');
    jw_str(w, k);
    jw_str(w, "\":");
}

static void jw_ku(jw_t *w, const char *k, uint32_t v)
{
    jw_key(w, k);
    jw_u64(w, (uint64_t)v);
}

static void jw_ki(jw_t *w, const char *k, int32_t v)
{
    jw_key(w, k);
    if (v < 0) {
        jw_ch(w, '-');
        int64_t mag = -(int64_t)v;
        jw_u64(w, (uint64_t)mag);
    } else {
        jw_u64(w, (uint64_t)v);
    }
}

static void jw_kfix2(jw_t *w, const char *k, uint32_t v_x100)
{
    uint32_t frac = v_x100 % 100U;
    uint32_t whole = v_x100 / 100U;
    jw_key(w, k);
    jw_u64(w, (uint64_t)whole);
    jw_ch(w, '.');
    if (frac < 10U) {
        jw_ch(w, '0');
    }
    jw_u64(w, (uint64_t)frac);
}

static void jw_kstr(jw_t *w, const char *k, const char *v)
{
    jw_key(w, k);
    jw_ch(w, '"');
    jw_str(w, v);
    jw_ch(w, '"');
}

static const char *state_name(uint8_t st)
{
    static const char *const names[5] = { "disconnected", "connecting", "hello_sent", "streaming", "closed" };
    return (st < 5U) ? names[st] : "unknown";
}

static const char *psnr_src_name(uint8_t src)
{
    static const char *const names[3] = { "none", "truth", "keyframe" };
    return (src < 3U) ? names[src] : "unknown";
}

static void jw_daemon(jw_t *w, const ts_tlv_stats_t *st)
{
    uint32_t nc = (uint32_t)st->g.client_count;
    if (nc > TS_MAX_CLIENTS) {
        nc = TS_MAX_CLIENTS;
    }
    jw_key(w, "daemon");
    jw_open(w);
    jw_ku(w, "uptime_ms", st->g.uptime_ms);
    jw_ku(w, "frames_captured", st->g.frames_captured);
    jw_ku(w, "frames_sent", st->g.frames_sent);
    jw_ku(w, "keyframes_sent", st->g.keyframes_sent);
    jw_ku(w, "pkts_sent", st->g.pkts_sent);
    jw_ku(w, "bytes_sent", st->g.bytes_sent);
    jw_ku(w, "nacks_rx", st->g.nacks_rx);
    jw_ku(w, "retx_sent", st->g.retx_sent);
    jw_ku(w, "nack_too_old", st->g.nack_too_old);
    jw_ku(w, "ctrl_errors", st->g.ctrl_errors);
    jw_ku(w, "client_count", (uint32_t)st->g.client_count);
    jw_key(w, "clients");
    jw_ch(w, '[');
    for (uint32_t i = 0U; i < nc; i++) {
        const ts_stats_client_t *c = &st->c[i];
        if (i > 0U) {
            jw_ch(w, ',');
        }
        jw_open(w);
        jw_ku(w, "id", (uint32_t)c->id);
        jw_ku(w, "state", (uint32_t)c->state);
        jw_ku(w, "udp_port", (uint32_t)c->udp_port);
        jw_ku(w, "kbps", c->kbps);
        jw_ku(w, "pkts_sent", c->pkts_sent);
        jw_ku(w, "bytes_sent", c->bytes_sent);
        jw_ku(w, "nacks_rx", c->nacks_rx);
        jw_ku(w, "retx_sent", c->retx_sent);
        jw_ku(w, "retx_dropped", c->retx_dropped);
        jw_ku(w, "keyframe_reqs", c->keyframe_reqs);
        jw_ku(w, "last_seen_ms", c->last_seen_ms);
        jw_ch(w, '}');
    }
    jw_ch(w, ']');
    jw_ch(w, '}');
}

size_t tsr_metrics_json(const tsr_metrics_t *m, char *buf, size_t cap)
{
    static const char hex[] = "0123456789abcdef";
    size_t out = 0U;
    if ((buf != NULL) && (cap > 0U)) {
        jw_t w;
        w.buf = buf;
        w.cap = cap;
        w.len = 0U;
        w.ovf = (m == NULL);
        w.fresh = false;
        if (m != NULL) {
            jw_open(&w);
            jw_ku(&w, "uptime_ms", m->uptime_ms);
            jw_kstr(&w, "state", state_name(m->state));
            jw_ku(&w, "stream_id", m->stream_id);
            jw_kfix2(&w, "fps", m->fps_x100);
            jw_ku(&w, "kbps", m->kbps);
            jw_ku(&w, "pkts_rx", m->pkts_rx);
            jw_ku(&w, "pkts_expected", m->pkts_expected);
            jw_ku(&w, "pkts_missing", m->pkts_missing);
            jw_ku(&w, "pkts_lost", m->pkts_lost);
            jw_kfix2(&w, "loss_pct", m->loss_pct_x100);
            jw_kfix2(&w, "residual_loss_pct", m->residual_loss_pct_x100);
            jw_ku(&w, "dups", m->dups);
            jw_ku(&w, "late_pkts", m->late_pkts);
            jw_ku(&w, "crc_fail", m->crc_fail);
            jw_ku(&w, "parse_fail", m->parse_fail);
            jw_ku(&w, "nacks_sent", m->nacks_sent);
            jw_ku(&w, "retx_rx", m->retx_rx);
            jw_ku(&w, "nack_giveups", m->nack_giveups);
            jw_ku(&w, "rtt_ms", m->rtt_ms);
            jw_ku(&w, "frames_complete", m->frames_complete);
            jw_ku(&w, "frames_decoded", m->frames_decoded);
            jw_ku(&w, "frames_dropped", m->frames_dropped);
            jw_ku(&w, "decode_errors", m->decode_errors);
            jw_ku(&w, "keyframes", m->keyframes);
            jw_ku(&w, "keyframe_reqs", m->keyframe_reqs);
            jw_ku(&w, "freezes", m->freezes);
            jw_ku(&w, "freeze_ms_total", m->freeze_ms_total);
            jw_ku(&w, "freeze_ms_current", m->freeze_ms_current);
            jw_ku(&w, "latency_ms", m->latency_ms);
            jw_ku(&w, "latency_avg_ms", m->latency_avg_ms);
            jw_ki(&w, "owd_ms", m->owd_ms);
            jw_ku(&w, "jitter_ms", m->jitter_ms);
            jw_ku(&w, "exact_match_pct", m->exact_match_pct);
            jw_ku(&w, "truth_frames", m->truth_frames);
            jw_ku(&w, "truth_exact", m->truth_exact);
            jw_kfix2(&w, "psnr_db", m->psnr_cdb);
            jw_kstr(&w, "psnr_src", psnr_src_name(m->psnr_src));
            jw_ku(&w, "changed_tiles", (uint32_t)m->changed_tiles);
            jw_key(&w, "tile_bitmap");
            jw_ch(&w, '"');
            for (uint32_t i = 0U; i < TS_TILE_BITMAP_BYTES; i++) {
                uint32_t b = (uint32_t)m->tile_bitmap[i];
                jw_ch(&w, hex[(b >> 4) & 0xFU]);
                jw_ch(&w, hex[b & 0xFU]);
            }
            jw_ch(&w, '"');
            jw_key(&w, "tiles");
            jw_ch(&w, '[');
            for (uint32_t ty = 0U; ty < TS_TILES_Y; ty++) {
                if (ty > 0U) {
                    jw_ch(&w, ',');
                }
                jw_ch(&w, '"');
                for (uint32_t tx = 0U; tx < TS_TILES_X; tx++) {
                    uint32_t t = (ty * TS_TILES_X) + tx;
                    uint32_t bit = ((uint32_t)m->tile_bitmap[t >> 3] >> (t & 7U)) & 1U;
                    jw_ch(&w, (bit != 0U) ? '1' : '0');
                }
                jw_ch(&w, '"');
            }
            jw_ch(&w, ']');
            jw_ku(&w, "stats_rx", m->stats_rx);
            jw_ku(&w, "reconnects", m->reconnects);
            jw_ku(&w, "ctrl_errors", m->ctrl_errors);
            jw_daemon(&w, &m->last_stats);
            jw_ch(&w, '}');
        }
        if (w.ovf) {
            buf[0] = '\0';
        } else {
            buf[w.len] = '\0';
            out = w.len;
        }
    }
    return out;
}
