/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- ts_codec.h
 * Frame codec (libtsproto): keyframe = RLE, P-frame = XOR delta against the
 * reference + RLE, quality step = row subsampling, changed-tile count on
 * 16x16 tiles. Implements PROTOCOL.md section 6.
 *
 * FROZEN API: additive, non-breaking changes only.
 * Rules: no dynamic memory (contexts are caller-owned structs), no stdio.
 */
#ifndef TS_CODEC_H
#define TS_CODEC_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "ts_config.h"
#include "ts_proto.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- Codec header (8 bytes, PROTOCOL.md 6.1) ------------------------------ */
typedef struct ts_codec_hdr {
    uint8_t  kind;        /**< TS_CODEC_KIND_KEY or TS_CODEC_KIND_PFRAME */
    uint8_t  quality;     /**< 1, 2 or 4 */
    uint16_t frame_seq;
    uint16_t ref_seq;     /**< P: reference frame_seq; K: == frame_seq */
    uint16_t rows;        /**< TS_HEIGHT / quality */
} ts_codec_hdr_t;

/** Serialise 8 bytes. Errors: TS_E_INVAL (bad kind/quality/rows), TS_E_NOMEM. */
ts_err_t ts_codec_hdr_pack(const ts_codec_hdr_t *h, uint8_t *out, size_t out_cap);
/** Parse and validate 8 bytes. Errors: TS_E_TRUNC, TS_E_CORRUPT. */
ts_err_t ts_codec_hdr_parse(const uint8_t *in, size_t len, ts_codec_hdr_t *h);

/* ---- RLE primitive (PROTOCOL.md 6.4) --------------------------------------- */
/**
 * Canonical RLE encoder (PROTOCOL.md 6.4): `n` symbols -> tokens in `out`.
 * Errors: TS_E_NOMEM when `out_cap` is too small.
 */
ts_err_t ts_rle_encode(const uint8_t *sym, size_t n, uint8_t *out, size_t out_cap, size_t *out_len);
/**
 * Decode exactly `n` symbols from `in[0..len)`. Rejects runs/literals that
 * would exceed `n`, literal length 0, truncated tokens and trailing bytes.
 * Errors: TS_E_CORRUPT, TS_E_TRUNC. `*consumed` = bytes read on success.
 */
ts_err_t ts_rle_decode(const uint8_t *in, size_t len, uint8_t *sym, size_t n, size_t *consumed);

/* ---- Frame info returned by encode/decode ---------------------------------- */
typedef struct ts_frame_info {
    bool     keyframe;
    uint8_t  quality;
    uint16_t frame_seq;
    uint16_t ref_seq;
    uint16_t changed_tiles;   /**< encoder: vs reference before encoding; decoder: vs its previous output */
    uint32_t enc_len;         /**< bytes of the encoded frame */
} ts_frame_info_t;

/* ---- Encoder --------------------------------------------------------------- */
typedef struct ts_enc {
    uint8_t  ref[TS_FRAME_BYTES];     /**< reference (PROTOCOL.md 6.5) */
    bool     have_ref;
    bool     force_key;               /**< next frame is a keyframe */
    uint8_t  quality;                 /**< current step 1/2/4 */
    uint16_t last_seq;                /**< frame_seq of the last encoded frame */
    uint16_t changed_tiles;           /**< result of the last encode */
    uint8_t  tile_bitmap[TS_TILE_BITMAP_BYTES]; /**< changed tiles of the last encode */
    uint32_t frames;                  /**< frames encoded */
    uint32_t keyframes;               /**< keyframes encoded */
} ts_enc_t;

/** Reset to "no reference, quality 1". */
void ts_enc_init(ts_enc_t *e);
/**
 * Encode `frame` (TS_FRAME_BYTES) as frame `frame_seq` into `out` (capacity
 * `out_cap` bytes). A keyframe is produced when there is no reference or
 * force_keyframe was requested; otherwise a P-frame (PROTOCOL.md 6.3, 6.5).
 * Errors: TS_E_INVAL, TS_E_NOMEM (out_cap too small; the reference is unchanged).
 */
ts_err_t ts_enc_encode(ts_enc_t *e, const uint8_t *frame, uint16_t frame_seq,
                       uint8_t *out, size_t out_cap, ts_frame_info_t *info);
/** Make the next frame a keyframe. */
void ts_enc_force_keyframe(ts_enc_t *e);
/** Set the quality step (1, 2 or 4). Errors: TS_E_INVAL. */
ts_err_t ts_enc_set_quality(ts_enc_t *e, uint8_t quality);
uint8_t  ts_enc_quality(const ts_enc_t *e);
/** Changed-tile count of the last encode (PROTOCOL.md 6.6). */
uint16_t ts_enc_changed_tiles(const ts_enc_t *e);

/* ---- Decoder --------------------------------------------------------------- */
typedef struct ts_dec {
    uint8_t  ref[TS_FRAME_BYTES];     /**< last reconstructed frame */
    bool     have_ref;
    uint16_t last_seq;                /**< frame_seq of the last decoded frame */
    uint8_t  tile_bitmap[TS_TILE_BITMAP_BYTES];
    uint32_t frames;
    uint32_t keyframes;
    uint32_t errors;
} ts_dec_t;

void ts_dec_init(ts_dec_t *d);
/**
 * Decode `in[0..len)` into `out` (TS_FRAME_BYTES). On success the reference is
 * updated and `info` filled. Errors: TS_E_TRUNC, TS_E_CORRUPT (bitstream),
 * TS_E_NOREF (P-frame without matching reference; the reference is untouched).
 */
ts_err_t ts_dec_decode(ts_dec_t *d, const uint8_t *in, size_t len, uint8_t *out, ts_frame_info_t *info);
/** Changed tiles between the last two decoded frames, as a 300-bit bitmap. */
const uint8_t *ts_dec_tile_bitmap(const ts_dec_t *d);

/* ---- Helpers shared by both sides ------------------------------------------ */
/**
 * Count tiles where any pixel of `a` differs from `b` by more than
 * TS_TILE_THRESH; optionally fills `bitmap` (TS_TILE_BITMAP_BYTES, may be NULL).
 */
uint16_t ts_codec_changed_tiles(const uint8_t *a, const uint8_t *b, uint8_t *bitmap);
/**
 * (additive) Changed tiles of frame `a` at quality step `quality` against the
 * reference `b` (PROTOCOL.md 6.6). Quality 1 is ts_codec_changed_tiles(); an
 * invalid quality counts every tile as changed.
 */
uint16_t ts_codec_changed_tiles_q(const uint8_t *a, const uint8_t *b, uint8_t quality, uint8_t *bitmap);

#ifdef __cplusplus
}
#endif

#endif /* TS_CODEC_H */
