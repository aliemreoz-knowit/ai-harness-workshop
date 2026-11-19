/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- src/codec/codec.c
 * Frame codec (PROTOCOL.md section 6) behind include/ts_codec.h:
 *   - 8-byte codec header (6.1),
 *   - row subsampling quality step q in {1, 2, 4} (6.2),
 *   - keyframe symbols = pixels, P-frame symbols = pixel XOR reconstructed
 *     reference (6.3),
 *   - canonical RLE with run and escape/literal tokens (6.4),
 *   - reconstructed reference rule: the encoder replaces its reference with
 *     exactly what the decoder will output (6.5),
 *   - changed-tile count on 16x16 tiles (6.6).
 *
 * Symbols are never materialised in a scratch buffer: the RLE core reads them
 * through a "source" (pixel or pixel^ref of encoded row r, column j) and the
 * decoder writes them through a "sink" straight into the output frame, so no
 * static state is needed and every function is reentrant.
 *
 * Size bound: the canonical encoder never emits more than (5/3)*n + 2 bytes
 * (a literal of L <= 255 symbols costs L + 2 and is only chosen when no run of
 * 3 starts inside it; runs cost 2 bytes for >= 2 symbols). With n <= 76800 an
 * encoded frame is at most 8 + 128002 = 128010 bytes < TS_ENC_MAX.
 * Every write is nevertheless checked against out_cap.
 */
#include <stddef.h>
#include <string.h>
#include "ts_codec.h"

#define CH_OFF_KIND      0U
#define CH_OFF_QUALITY   1U
#define CH_OFF_FRAME_SEQ 2U
#define CH_OFF_REF_SEQ   4U
#define CH_OFF_ROWS      6U

#define RLE_MAX_RUN      255U
#define RLE_MAX_LIT      255U
#define RLE_ESCAPE       0x00U
#define RLE_LIT_MIN_RUN  3U    /* a literal stops before a run of >= 3 */

/* size_t views of the composite geometry constants (MISRA 10.8: no casts of
 * composite expressions); checked against ts_config.h below. */
#define CODEC_TILES_X       ((size_t)TS_WIDTH / (size_t)TS_TILE)
#define CODEC_TILES_Y       ((size_t)TS_HEIGHT / (size_t)TS_TILE)
#define CODEC_TILES         (CODEC_TILES_X * CODEC_TILES_Y)
#define CODEC_BITMAP_BYTES  ((CODEC_TILES + 7U) / 8U)
#define CODEC_FRAME_BYTES   ((size_t)TS_WIDTH * (size_t)TS_HEIGHT)

_Static_assert(CODEC_TILES_X == TS_TILES_X, "tile columns");
_Static_assert(CODEC_TILES_Y == TS_TILES_Y, "tile rows");
_Static_assert(CODEC_TILES == TS_TILES, "tiles");
_Static_assert(CODEC_BITMAP_BYTES == TS_TILE_BITMAP_BYTES, "tile bitmap bytes");
_Static_assert(CODEC_FRAME_BYTES == TS_FRAME_BYTES, "frame bytes");
_Static_assert(((5U * TS_FRAME_BYTES) / 3U) + 2U + TS_CODEC_HDR_LEN <= TS_ENC_MAX,
               "canonical RLE worst case fits TS_ENC_MAX");

/* ---- symbol source / sink ---------------------------------------------------- */

/* Symbol i lives at encoded row i / TS_WIDTH, column i % TS_WIDTH, i.e. frame
 * row (i / TS_WIDTH) * q. For a plain symbol array use q = 1 and no ref: the
 * mapping is then the identity for any n. */
typedef struct sym_map {
    const uint8_t *src;   /* encoder: pixels; decoder: unused */
    uint8_t       *dst;   /* decoder: output frame / symbol array; encoder: unused */
    const uint8_t *ref;   /* XOR reference or NULL */
    size_t         q;     /* row step */
} sym_map_t;

static size_t sym_index(const sym_map_t *m, size_t i)
{
    const size_t row = i / (size_t)TS_WIDTH;
    const size_t col = i % (size_t)TS_WIDTH;
    return (row * m->q * (size_t)TS_WIDTH) + col;
}

static uint8_t sym_get(const sym_map_t *m, size_t i)
{
    const size_t idx = sym_index(m, i);
    uint8_t v = m->src[idx];
    if (m->ref != NULL) {
        v = (uint8_t)(v ^ m->ref[idx]);
    }
    return v;
}

static void sym_put(const sym_map_t *m, size_t i, uint8_t v)
{
    const size_t idx = sym_index(m, i);
    uint8_t w = v;
    if (m->ref != NULL) {
        w = (uint8_t)(w ^ m->ref[idx]);
    }
    m->dst[idx] = w;
}

/* Number of equal symbols starting at p (p < n), capped at `cap`. */
static size_t sym_runlen(const sym_map_t *m, size_t p, size_t n, size_t cap)
{
    const uint8_t v = sym_get(m, p);
    size_t r = 1U;
    while (((p + r) < n) && (r < cap) && (sym_get(m, p + r) == v)) {
        r++;
    }
    return r;
}

/* ---- RLE core (PROTOCOL.md 6.4) ---------------------------------------------- */
static ts_err_t rle_encode_core(const sym_map_t *m, size_t n, uint8_t *out, size_t out_cap, size_t *out_len)
{
    ts_err_t err = TS_OK;
    size_t pos = 0U;
    size_t o = 0U;

    while ((pos < n) && (err == TS_OK)) {
        const size_t run = sym_runlen(m, pos, n, RLE_MAX_RUN);
        if (run >= 2U) {
            if ((out_cap - o) < 2U) {
                err = TS_E_NOMEM;
            } else {
                out[o] = (uint8_t)run;
                out[o + 1U] = sym_get(m, pos);
                o += 2U;
                pos += run;
            }
        } else {
            size_t lit = 1U;
            while (((pos + lit) < n) && (lit < RLE_MAX_LIT) &&
                   (sym_runlen(m, pos + lit, n, RLE_LIT_MIN_RUN) < RLE_LIT_MIN_RUN)) {
                lit++;
            }
            if ((out_cap - o) < (2U + lit)) {
                err = TS_E_NOMEM;
            } else {
                out[o] = RLE_ESCAPE;
                out[o + 1U] = (uint8_t)lit;
                o += 2U;
                for (size_t k = 0U; k < lit; k++) {
                    out[o + k] = sym_get(m, pos + k);
                }
                o += lit;
                pos += lit;
            }
        }
    }
    if (err == TS_OK) {
        *out_len = o;
    }
    return err;
}

/* Decode exactly n symbols from in[0..len) into the sink. Error order mirrors
 * the reference decoder: truncation of a token is TS_E_TRUNC, a zero literal or
 * any token overrunning n is TS_E_CORRUPT, trailing bytes are TS_E_CORRUPT.
 * The run/literal length is checked against n BEFORE anything is written. */
static ts_err_t rle_decode_core(const uint8_t *in, size_t len, const sym_map_t *m, size_t n, size_t *consumed)
{
    ts_err_t err = TS_OK;
    size_t i = 0U;
    size_t produced = 0U;

    while ((produced < n) && (err == TS_OK)) {
        if (i >= len) {
            err = TS_E_TRUNC;
        } else {
            const uint8_t t = in[i];
            i++;
            if (t == RLE_ESCAPE) {
                if (i >= len) {
                    err = TS_E_TRUNC;
                } else {
                    const size_t lit = (size_t)in[i];
                    i++;
                    if ((lit == 0U) || (lit > (n - produced))) {
                        err = TS_E_CORRUPT;
                    } else if (lit > (len - i)) {
                        err = TS_E_TRUNC;
                    } else {
                        for (size_t k = 0U; k < lit; k++) {
                            sym_put(m, produced + k, in[i + k]);
                        }
                        i += lit;
                        produced += lit;
                    }
                }
            } else {
                if (i >= len) {
                    err = TS_E_TRUNC;
                } else if ((size_t)t > (n - produced)) {
                    err = TS_E_CORRUPT;
                } else {
                    const uint8_t v = in[i];
                    i++;
                    for (size_t k = 0U; k < (size_t)t; k++) {
                        sym_put(m, produced + k, v);
                    }
                    produced += (size_t)t;
                }
            }
        }
    }
    if ((err == TS_OK) && (i != len)) {
        err = TS_E_CORRUPT;   /* trailing bytes after the n-th symbol */
    }
    if ((err == TS_OK) && (consumed != NULL)) {
        *consumed = i;
    }
    return err;
}

/* ---- public RLE primitive ------------------------------------------------------ */
ts_err_t ts_rle_encode(const uint8_t *sym, size_t n, uint8_t *out, size_t out_cap, size_t *out_len)
{
    ts_err_t err = TS_OK;
    if ((out_len == NULL) || ((n != 0U) && ((sym == NULL) || (out == NULL)))) {
        err = TS_E_INVAL;
    } else {
        const sym_map_t m = { sym, NULL, NULL, 1U };
        err = rle_encode_core(&m, n, out, out_cap, out_len);
    }
    return err;
}

ts_err_t ts_rle_decode(const uint8_t *in, size_t len, uint8_t *sym, size_t n, size_t *consumed)
{
    ts_err_t err = TS_OK;
    if (((in == NULL) && (len != 0U)) || ((sym == NULL) && (n != 0U))) {
        err = TS_E_INVAL;
    } else {
        const sym_map_t m = { NULL, sym, NULL, 1U };
        err = rle_decode_core(in, len, &m, n, consumed);
    }
    return err;
}

/* ---- codec header (PROTOCOL.md 6.1) ------------------------------------------- */
static bool quality_valid(uint8_t q)
{
    return (q == 1U) || (q == 2U) || (q == 4U);
}

static bool codec_hdr_valid(uint8_t kind, uint8_t quality, uint16_t rows)
{
    bool ok = false;
    if (((kind == (uint8_t)TS_CODEC_KIND_KEY) || (kind == (uint8_t)TS_CODEC_KIND_PFRAME)) &&
        quality_valid(quality)) {
        ok = ((uint32_t)rows == (TS_HEIGHT / (uint32_t)quality));
    }
    return ok;
}

ts_err_t ts_codec_hdr_pack(const ts_codec_hdr_t *h, uint8_t *out, size_t out_cap)
{
    ts_err_t err = TS_OK;
    if ((h == NULL) || (out == NULL)) {
        err = TS_E_INVAL;
    } else if (!codec_hdr_valid(h->kind, h->quality, h->rows)) {
        err = TS_E_INVAL;
    } else if (out_cap < (size_t)TS_CODEC_HDR_LEN) {
        err = TS_E_NOMEM;
    } else {
        out[CH_OFF_KIND] = h->kind;
        out[CH_OFF_QUALITY] = h->quality;
        ts_wr16(&out[CH_OFF_FRAME_SEQ], h->frame_seq);
        ts_wr16(&out[CH_OFF_REF_SEQ], h->ref_seq);
        ts_wr16(&out[CH_OFF_ROWS], h->rows);
    }
    return err;
}

ts_err_t ts_codec_hdr_parse(const uint8_t *in, size_t len, ts_codec_hdr_t *h)
{
    ts_err_t err = TS_OK;
    if (h == NULL) {
        err = TS_E_INVAL;
    } else if ((in == NULL) || (len < (size_t)TS_CODEC_HDR_LEN)) {
        err = TS_E_TRUNC;
    } else {
        const uint8_t kind = in[CH_OFF_KIND];
        const uint8_t quality = in[CH_OFF_QUALITY];
        const uint16_t rows = ts_rd16(&in[CH_OFF_ROWS]);
        if (!codec_hdr_valid(kind, quality, rows)) {
            err = TS_E_CORRUPT;
        } else {
            h->kind = kind;
            h->quality = quality;
            h->frame_seq = ts_rd16(&in[CH_OFF_FRAME_SEQ]);
            h->ref_seq = ts_rd16(&in[CH_OFF_REF_SEQ]);
            h->rows = rows;
        }
    }
    return err;
}

/* ---- changed tiles (PROTOCOL.md 6.6) ------------------------------------------ */
/* Frame `a` is read as reconstructed at quality q: row y comes from row y - y % q. */
static bool tile_changed(const uint8_t *a, const uint8_t *b, size_t tx, size_t ty, size_t q)
{
    bool changed = false;
    for (size_t y = ty * (size_t)TS_TILE; (y < ((ty + 1U) * (size_t)TS_TILE)) && (!changed); y++) {
        const size_t col = tx * (size_t)TS_TILE;
        const size_t base_a = ((y - (y % q)) * (size_t)TS_WIDTH) + col;
        const size_t base_b = (y * (size_t)TS_WIDTH) + col;
        for (size_t x = 0U; (x < (size_t)TS_TILE) && (!changed); x++) {
            const uint8_t pa = a[base_a + x];
            const uint8_t pb = b[base_b + x];
            const uint8_t d = (pa > pb) ? (uint8_t)(pa - pb) : (uint8_t)(pb - pa);
            changed = ((uint32_t)d > TS_TILE_THRESH);
        }
    }
    return changed;
}

static uint16_t changed_tiles_q(const uint8_t *a, const uint8_t *b, size_t q, uint8_t *bitmap)
{
    uint16_t count = 0U;
    if (bitmap != NULL) {
        (void)memset(bitmap, 0, CODEC_BITMAP_BYTES);
    }
    if ((a != NULL) && (b != NULL)) {
        for (size_t ty = 0U; ty < CODEC_TILES_Y; ty++) {
            for (size_t tx = 0U; tx < CODEC_TILES_X; tx++) {
                if (tile_changed(a, b, tx, ty, q)) {
                    const size_t t = (ty * CODEC_TILES_X) + tx;
                    count++;
                    if (bitmap != NULL) {
                        bitmap[t >> 3U] = (uint8_t)(bitmap[t >> 3U] | (uint8_t)(1U << (t & 7U)));
                    }
                }
            }
        }
    }
    return count;
}

uint16_t ts_codec_changed_tiles(const uint8_t *a, const uint8_t *b, uint8_t *bitmap)
{
    return changed_tiles_q(a, b, 1U, bitmap);
}

static uint16_t tiles_all(uint8_t *bitmap);

uint16_t ts_codec_changed_tiles_q(const uint8_t *a, const uint8_t *b, uint8_t quality, uint8_t *bitmap)
{
    uint16_t count = 0U;
    if (quality_valid(quality)) {
        count = changed_tiles_q(a, b, (size_t)quality, bitmap);
    } else if (bitmap != NULL) {
        count = tiles_all(bitmap);
    } else {
        count = (uint16_t)CODEC_TILES;
    }
    return count;
}

/* Bitmap with every tile set (a keyframe without a previous frame). */
static uint16_t tiles_all(uint8_t *bitmap)
{
    (void)memset(bitmap, 0, CODEC_BITMAP_BYTES);
    for (size_t t = 0U; t < CODEC_TILES; t++) {
        bitmap[t >> 3U] = (uint8_t)(bitmap[t >> 3U] | (uint8_t)(1U << (t & 7U)));
    }
    return (uint16_t)CODEC_TILES;
}

/* Replicate encoded row r into rows r+1 .. r+q-1 (PROTOCOL.md 6.2), using
 * `src` row r as the source for all of them (dst may equal src). */
static void rows_expand(uint8_t *dst, const uint8_t *src, size_t q)
{
    for (size_t r = 0U; r < (size_t)TS_HEIGHT; r += q) {
        const uint8_t *row = &src[r * (size_t)TS_WIDTH];
        for (size_t k = 0U; k < q; k++) {
            uint8_t *d = &dst[(r + k) * (size_t)TS_WIDTH];
            if (d != row) {
                (void)memmove(d, row, (size_t)TS_WIDTH);
            }
        }
    }
}

/* ---- encoder ------------------------------------------------------------------ */
void ts_enc_init(ts_enc_t *e)
{
    if (e != NULL) {
        (void)memset(e, 0, sizeof(*e));
        e->quality = 1U;
    }
}

ts_err_t ts_enc_encode(ts_enc_t *e, const uint8_t *frame, uint16_t frame_seq,
                       uint8_t *out, size_t out_cap, ts_frame_info_t *info)
{
    ts_err_t err = TS_OK;

    if ((e == NULL) || (frame == NULL) || (out == NULL) || (!quality_valid(e->quality))) {
        err = TS_E_INVAL;
    } else {
        const bool key = (!e->have_ref) || e->force_key;
        const uint8_t q = e->quality;
        const size_t n = ((size_t)TS_HEIGHT / (size_t)q) * (size_t)TS_WIDTH;
        uint8_t bitmap[TS_TILE_BITMAP_BYTES];
        uint16_t tiles = 0U;
        ts_codec_hdr_t ch;
        size_t rle_len = 0U;

        /* changed tiles of the input as it will be reconstructed at q vs the
         * reconstructed reference (6.6): rows the decoder never sees cannot change a tile */
        if (e->have_ref) {
            tiles = ts_codec_changed_tiles_q(frame, e->ref, q, bitmap);
        } else {
            tiles = tiles_all(bitmap);
        }

        ch.kind = key ? (uint8_t)TS_CODEC_KIND_KEY : (uint8_t)TS_CODEC_KIND_PFRAME;
        ch.quality = q;
        ch.frame_seq = frame_seq;
        ch.ref_seq = key ? frame_seq : e->last_seq;
        ch.rows = (uint16_t)((uint32_t)TS_HEIGHT / (uint32_t)q);
        err = ts_codec_hdr_pack(&ch, out, out_cap);
        if (err == TS_OK) {
            const sym_map_t m = { frame, NULL, key ? NULL : e->ref, (size_t)q };
            err = rle_encode_core(&m, n, &out[TS_CODEC_HDR_LEN], out_cap - (size_t)TS_CODEC_HDR_LEN, &rle_len);
        }

        if (err == TS_OK) {
            (void)memcpy(e->ref, frame, sizeof(e->ref));
            e->have_ref = true;
            if (key) {
                e->force_key = false;
                e->keyframes++;
            }
            e->last_seq = frame_seq;
            e->changed_tiles = tiles;
            (void)memcpy(e->tile_bitmap, bitmap, sizeof(bitmap));
            e->frames++;
            if (info != NULL) {
                info->keyframe = key;
                info->quality = q;
                info->frame_seq = frame_seq;
                info->ref_seq = ch.ref_seq;
                info->changed_tiles = tiles;
                info->enc_len = (uint32_t)((size_t)TS_CODEC_HDR_LEN + rle_len);
            }
        }
    }
    return err;
}

void ts_enc_force_keyframe(ts_enc_t *e)
{
    if (e != NULL) {
        e->force_key = true;
    }
}

ts_err_t ts_enc_set_quality(ts_enc_t *e, uint8_t quality)
{
    ts_err_t err = TS_OK;
    if ((e == NULL) || (!quality_valid(quality))) {
        err = TS_E_INVAL;
    } else {
        e->quality = quality;
    }
    return err;
}

uint8_t ts_enc_quality(const ts_enc_t *e)
{
    return (e != NULL) ? e->quality : 0U;
}

uint16_t ts_enc_changed_tiles(const ts_enc_t *e)
{
    return (e != NULL) ? e->changed_tiles : 0U;
}

/* ---- decoder ------------------------------------------------------------------ */
void ts_dec_init(ts_dec_t *d)
{
    if (d != NULL) {
        (void)memset(d, 0, sizeof(*d));
    }
}

ts_err_t ts_dec_decode(ts_dec_t *d, const uint8_t *in, size_t len, uint8_t *out, ts_frame_info_t *info)
{
    ts_err_t err = TS_OK;
    ts_codec_hdr_t ch;

    if ((d == NULL) || (out == NULL) || (out == d->ref)) {
        err = TS_E_INVAL;   /* out must not alias the reference (it is kept on error) */
    } else {
        err = ts_codec_hdr_parse(in, len, &ch);
        if (err == TS_OK) {
            const bool key = (ch.kind == (uint8_t)TS_CODEC_KIND_KEY);
            if ((!key) && ((!d->have_ref) || (ch.ref_seq != d->last_seq))) {
                err = TS_E_NOREF;   /* 6.5: reference untouched */
            } else {
                const size_t n = (size_t)ch.rows * (size_t)TS_WIDTH;   /* <= TS_FRAME_BYTES */
                const sym_map_t m = { NULL, out, key ? NULL : d->ref, (size_t)ch.quality };
                err = rle_decode_core(&in[TS_CODEC_HDR_LEN], len - (size_t)TS_CODEC_HDR_LEN, &m, n, NULL);
            }
        }
        if (err == TS_OK) {
            const bool key = (ch.kind == (uint8_t)TS_CODEC_KIND_KEY);
            uint16_t tiles = 0U;
            rows_expand(out, out, (size_t)ch.quality);
            if (d->have_ref) {
                tiles = ts_codec_changed_tiles(out, d->ref, d->tile_bitmap);
            } else {
                tiles = tiles_all(d->tile_bitmap);
            }
            (void)memcpy(d->ref, out, CODEC_FRAME_BYTES);
            d->have_ref = true;
            d->last_seq = ch.frame_seq;
            d->frames++;
            if (key) {
                d->keyframes++;
            }
            if (info != NULL) {
                info->keyframe = key;
                info->quality = ch.quality;
                info->frame_seq = ch.frame_seq;
                info->ref_seq = ch.ref_seq;
                info->changed_tiles = tiles;
                info->enc_len = (uint32_t)len;
            }
        } else {
            d->errors++;
        }
    }
    return err;
}

const uint8_t *ts_dec_tile_bitmap(const ts_dec_t *d)
{
    return (d != NULL) ? d->tile_bitmap : NULL;
}
