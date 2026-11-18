/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/unit/test_codec.c
 * Unit tests for the frame codec (src/codec): codec header and RLE vectors,
 * the five reference frames from PROTOCOL.md (bit-exact encoder output and
 * decoder output), 30-frame reference sync at every quality step across a
 * frame_seq wrap, TS_ENC_MAX worst case on noise, and decoder bounds checks.
 */
#include <string.h>
#include "unity.h"
#include "ts_config.h"
#include "ts_proto.h"
#include "ts_codec.h"
#include "ts_rate.h"
#include "ts_vectors.h"
#include "ts_vectors_gen.h"

#define GUARD_LEN   256U
#define GUARD_BYTE  0xA5U

/* Large buffers are static: tests run single-threaded. */
static ts_enc_t g_enc;
static ts_enc_t g_enc_copy;
static ts_dec_t g_dec;
static ts_dec_t g_dec_copy;
static uint8_t g_frame[TS_FRAME_BYTES];
static uint8_t g_expect[TS_FRAME_BYTES];
static uint8_t g_out[TS_FRAME_BYTES + GUARD_LEN];
static uint8_t g_enc_buf[TS_ENC_MAX + GUARD_LEN];
static uint8_t g_enc_buf2[TS_ENC_MAX + GUARD_LEN];

void setUp(void)
{
}

void tearDown(void)
{
}

static const char *err_name(const char *vec_error)
{
    return (vec_error != NULL) ? vec_error : "TS_OK";
}

static uint32_t xs32(uint32_t *s)
{
    uint32_t x = *s;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    *s = x;
    return x;
}

static void guard_set(uint8_t *p, size_t len)
{
    (void)memset(p, GUARD_BYTE, len);
}

static void guard_check(const uint8_t *p, size_t len)
{
    for (size_t i = 0U; i < len; i++) {
        TEST_ASSERT_EQUAL_HEX8(GUARD_BYTE, p[i]);
    }
}

/* Moving test pattern: gradient + moving block + sparse noise, differs per i. */
static void gen_moving(uint8_t *f, uint32_t i)
{
    uint32_t s = 0x9E3779B9U ^ (i * 2654435761U);
    const uint32_t bx = (i * 7U) % (TS_WIDTH - 40U);
    const uint32_t by = (i * 5U) % (TS_HEIGHT - 40U);
    if (s == 0U) {
        s = 1U;
    }
    for (uint32_t y = 0U; y < TS_HEIGHT; y++) {
        for (uint32_t x = 0U; x < TS_WIDTH; x++) {
            uint8_t v = (uint8_t)((x + (2U * y) + i) & 0xFFU);
            if ((x >= bx) && (x < (bx + 40U)) && (y >= by) && (y < (by + 40U))) {
                v = (uint8_t)((i * 37U) & 0xFFU);
            }
            f[(y * TS_WIDTH) + x] = v;
        }
    }
    for (uint32_t k = 0U; k < 500U; k++) {
        const uint32_t r = xs32(&s);
        f[r % TS_FRAME_BYTES] = (uint8_t)(r >> 24);
    }
}

static void gen_noise(uint8_t *f, uint32_t seed)
{
    uint32_t s = (seed == 0U) ? 1U : seed;
    for (size_t i = 0U; i < TS_FRAME_BYTES; i++) {
        f[i] = (uint8_t)(xs32(&s) & 0xFFU);
    }
}

/* What the decoder must output for raw frame `f` at quality q (PROTOCOL.md 6.2). */
static void reconstruct(uint8_t *dst, const uint8_t *f, uint8_t q)
{
    for (uint32_t r = 0U; r < TS_HEIGHT; r += q) {
        for (uint32_t k = 0U; k < q; k++) {
            (void)memcpy(&dst[(r + k) * TS_WIDTH], &f[r * TS_WIDTH], TS_WIDTH);
        }
    }
}

/* ---- codec header ----------------------------------------------------------- */
static void test_codec_hdr_vectors(void)
{
    uint8_t out[TS_CODEC_HDR_LEN];
    for (size_t i = 0U; i < tsv_codec_hdr_count; i++) {
        const tsv_codec_hdr_t *v = &tsv_codec_hdr[i];
        ts_codec_hdr_t h;
        const ts_err_t e = ts_codec_hdr_parse(v->data, v->len, &h);
        TEST_ASSERT_EQUAL_STRING_MESSAGE(err_name(v->error), ts_err_str(e), v->name);
        if (v->error == NULL) {
            TEST_ASSERT_EQUAL_UINT8(v->kind, h.kind);
            TEST_ASSERT_EQUAL_UINT8(v->quality, h.quality);
            TEST_ASSERT_EQUAL_UINT16(v->frame_seq, h.frame_seq);
            TEST_ASSERT_EQUAL_UINT16(v->ref_seq, h.ref_seq);
            TEST_ASSERT_EQUAL_UINT16(v->rows, h.rows);
            TEST_ASSERT_EQUAL(TS_OK, ts_codec_hdr_pack(&h, out, sizeof(out)));
            TEST_ASSERT_EQUAL_HEX8_ARRAY_MESSAGE(v->data, out, TS_CODEC_HDR_LEN, v->name);
        }
    }
}

static void test_codec_hdr_pack_errors(void)
{
    uint8_t out[TS_CODEC_HDR_LEN];
    ts_codec_hdr_t h = { TS_CODEC_KIND_KEY, 1U, 1U, 1U, 240U };
    TEST_ASSERT_EQUAL(TS_E_NOMEM, ts_codec_hdr_pack(&h, out, 7U));
    h.quality = 3U;
    h.rows = 80U;
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_codec_hdr_pack(&h, out, sizeof(out)));
    h.quality = 2U;
    h.rows = 240U;
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_codec_hdr_pack(&h, out, sizeof(out)));
    h.rows = 120U;
    h.kind = 0x51U;
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_codec_hdr_pack(&h, out, sizeof(out)));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_codec_hdr_pack(NULL, out, sizeof(out)));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_codec_hdr_parse(out, sizeof(out), NULL));
    TEST_ASSERT_EQUAL(TS_E_TRUNC, ts_codec_hdr_parse(NULL, 8U, &h));
}

/* ---- RLE ---------------------------------------------------------------------- */
static void test_rle_vectors(void)
{
    static uint8_t enc[1024];
    static uint8_t sym[512 + GUARD_LEN];
    for (size_t i = 0U; i < tsv_rle_count; i++) {
        const tsv_rle_t *v = &tsv_rle[i];
        size_t consumed = 12345U;
        ts_err_t e;
        TEST_ASSERT_TRUE(v->n <= 512U);
        if (v->error == NULL) {
            size_t len = 0U;
            TEST_ASSERT_EQUAL_MESSAGE(TS_OK, ts_rle_encode(v->symbols, v->n, enc, sizeof(enc), &len), v->name);
            TEST_ASSERT_EQUAL_size_t_MESSAGE(v->enc_len, len, v->name);
            if (len > 0U) {
                TEST_ASSERT_EQUAL_HEX8_ARRAY_MESSAGE(v->encoded, enc, len, v->name);
            }
            TEST_ASSERT_TRUE(len <= ((2U * v->n) + 2U));
        }
        guard_set(sym, sizeof(sym));
        e = ts_rle_decode(v->encoded, v->enc_len, sym, v->n, &consumed);
        TEST_ASSERT_EQUAL_STRING_MESSAGE(err_name(v->error), ts_err_str(e), v->name);
        guard_check(&sym[v->n], GUARD_LEN);   /* nothing written past n */
        if (v->error == NULL) {
            TEST_ASSERT_EQUAL_size_t(v->enc_len, consumed);
            if (v->n > 0U) {
                TEST_ASSERT_EQUAL_HEX8_ARRAY_MESSAGE(v->symbols, sym, v->n, v->name);
            }
        } else {
            TEST_ASSERT_EQUAL_size_t(12345U, consumed);
        }
    }
}

static void test_rle_nomem_at_every_cap(void)
{
    /* lit256 + run: encoding into any too-small buffer fails, never overruns */
    static uint8_t sym[300];
    static uint8_t enc[600 + GUARD_LEN];
    size_t full = 0U;
    for (size_t i = 0U; i < 256U; i++) {
        sym[i] = (uint8_t)i;
    }
    (void)memset(&sym[256], 9, 44U);
    TEST_ASSERT_EQUAL(TS_OK, ts_rle_encode(sym, sizeof(sym), enc, 600U, &full));
    for (size_t cap = 0U; cap < full; cap++) {
        size_t len = 0U;
        guard_set(&enc[cap], GUARD_LEN);
        TEST_ASSERT_EQUAL(TS_E_NOMEM, ts_rle_encode(sym, sizeof(sym), enc, cap, &len));
        guard_check(&enc[cap], GUARD_LEN);
    }
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_rle_encode(NULL, 3U, enc, 600U, &full));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_rle_encode(sym, 3U, enc, 600U, NULL));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_rle_decode(NULL, 3U, sym, 3U, &full));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_rle_decode(enc, 3U, NULL, 3U, &full));
    TEST_ASSERT_EQUAL(TS_OK, ts_rle_encode(NULL, 0U, NULL, 0U, &full));
    TEST_ASSERT_EQUAL_size_t(0U, full);
}

/* ---- reference frames (PROTOCOL.md 9, tsv_frame) ------------------------------- */
static void test_frame_vectors_bit_exact(void)
{
    ts_enc_init(&g_enc);
    ts_dec_init(&g_dec);
    for (size_t i = 0U; i < tsv_frame_count; i++) {
        const tsv_frame_t *v = &tsv_frame[i];
        ts_frame_info_t ei;
        ts_frame_info_t di;
        TEST_ASSERT_TRUE_MESSAGE(tsv_gen_frame(v->generator, g_frame), v->name);
        TEST_ASSERT_EQUAL_HEX32_MESSAGE(v->raw_crc32, ts_crc32(g_frame, TS_FRAME_BYTES), v->name);
        TEST_ASSERT_EQUAL(TS_OK, ts_enc_set_quality(&g_enc, v->quality));
        if ((v->kind == 'K') && g_enc.have_ref) {
            ts_enc_force_keyframe(&g_enc);
        }
        TEST_ASSERT_EQUAL_MESSAGE(TS_OK, ts_enc_encode(&g_enc, g_frame, v->frame_seq, g_enc_buf, TS_ENC_MAX, &ei), v->name);
        TEST_ASSERT_EQUAL_MESSAGE(v->kind == 'K', ei.keyframe, v->name);
        TEST_ASSERT_EQUAL_UINT8(v->quality, ei.quality);
        TEST_ASSERT_EQUAL_UINT16(v->frame_seq, ei.frame_seq);
        TEST_ASSERT_EQUAL_UINT16_MESSAGE(v->ref_seq, ei.ref_seq, v->name);
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(v->enc_len, ei.enc_len, v->name);
        TEST_ASSERT_EQUAL_HEX32_MESSAGE(v->enc_crc32, ts_crc32(g_enc_buf, ei.enc_len), v->name);
        TEST_ASSERT_EQUAL_UINT16_MESSAGE(v->changed_tiles, ei.changed_tiles, v->name);
        TEST_ASSERT_EQUAL_UINT16(v->changed_tiles, ts_enc_changed_tiles(&g_enc));
        TEST_ASSERT_EQUAL_HEX32_MESSAGE(v->dec_crc32, ts_crc32(g_enc.ref, TS_FRAME_BYTES), v->name);

        TEST_ASSERT_EQUAL_MESSAGE(TS_OK, ts_dec_decode(&g_dec, g_enc_buf, ei.enc_len, g_out, &di), v->name);
        TEST_ASSERT_EQUAL_HEX32_MESSAGE(v->dec_crc32, ts_crc32(g_out, TS_FRAME_BYTES), v->name);
        TEST_ASSERT_EQUAL_MEMORY(g_enc.ref, g_dec.ref, TS_FRAME_BYTES);
        TEST_ASSERT_EQUAL(ei.keyframe, di.keyframe);
        TEST_ASSERT_EQUAL_UINT16(v->frame_seq, di.frame_seq);
        TEST_ASSERT_EQUAL_UINT16(v->ref_seq, di.ref_seq);
        TEST_ASSERT_EQUAL_UINT32(ei.enc_len, di.enc_len);
    }
    TEST_ASSERT_EQUAL_UINT32(5U, g_enc.frames);
    TEST_ASSERT_EQUAL_UINT32(2U, g_enc.keyframes);
    TEST_ASSERT_EQUAL_UINT32(5U, g_dec.frames);
    TEST_ASSERT_EQUAL_UINT32(2U, g_dec.keyframes);
    TEST_ASSERT_EQUAL_UINT32(0U, g_dec.errors);
}

/* ---- 30-frame reference sync at every quality step ------------------------------- */
static void run_ref_sync(uint8_t q, uint16_t seq0)
{
    ts_enc_init(&g_enc);
    ts_dec_init(&g_dec);
    TEST_ASSERT_EQUAL(TS_OK, ts_enc_set_quality(&g_enc, q));
    TEST_ASSERT_EQUAL_UINT8(q, ts_enc_quality(&g_enc));
    for (uint32_t i = 0U; i < 30U; i++) {
        const uint16_t seq = (uint16_t)(seq0 + i);
        ts_frame_info_t ei;
        ts_frame_info_t di;
        gen_moving(g_frame, i);
        reconstruct(g_expect, g_frame, q);
        TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc, g_frame, seq, g_enc_buf, TS_ENC_MAX, &ei));
        TEST_ASSERT_EQUAL(i == 0U, ei.keyframe);
        TEST_ASSERT_EQUAL_UINT16((i == 0U) ? seq : (uint16_t)(seq - 1U), ei.ref_seq);
        TEST_ASSERT_TRUE(ei.enc_len <= TS_ENC_MAX);
        TEST_ASSERT_EQUAL(TS_OK, ts_dec_decode(&g_dec, g_enc_buf, ei.enc_len, g_out, &di));
        TEST_ASSERT_EQUAL_UINT16(seq, di.frame_seq);
        /* decoder output == raw frame with rows replicated, no drift at any step */
        TEST_ASSERT_EQUAL_MEMORY(g_expect, g_out, TS_FRAME_BYTES);
        /* encoder reference == decoder reference, bit for bit */
        TEST_ASSERT_EQUAL_MEMORY(g_enc.ref, g_dec.ref, TS_FRAME_BYTES);
        TEST_ASSERT_EQUAL_UINT16(g_enc.last_seq, g_dec.last_seq);
        if ((i > 0U) && (q == 1U)) {
            /* at q1 "raw vs reference" (encoder) and "output vs previous
             * output" (decoder) are the same comparison */
            TEST_ASSERT_EQUAL_UINT16(ei.changed_tiles, di.changed_tiles);
            TEST_ASSERT_EQUAL_MEMORY(g_enc.tile_bitmap, ts_dec_tile_bitmap(&g_dec), TS_TILE_BITMAP_BYTES);
        }
    }
    TEST_ASSERT_EQUAL_UINT32(1U, g_enc.keyframes);
    TEST_ASSERT_EQUAL_UINT32(30U, g_dec.frames);
}

static void test_ref_sync_30_frames_q1(void)
{
    run_ref_sync(1U, 65520U);   /* frame_seq wraps after 16 frames */
}

static void test_ref_sync_30_frames_q2(void)
{
    run_ref_sync(2U, 65520U);
}

static void test_ref_sync_30_frames_q4(void)
{
    run_ref_sync(4U, 65520U);
}

static void test_ref_sync_quality_steps_mid_stream(void)
{
    /* the rate module steps quality 1 -> 2 -> 4 -> 2 -> 1 without keyframes */
    const uint8_t steps[30] = { 1, 1, 2, 2, 2, 4, 4, 4, 4, 2, 1, 1, 4, 1, 2,
                                4, 2, 1, 1, 1, 2, 4, 4, 1, 2, 2, 1, 4, 4, 1 };
    ts_enc_init(&g_enc);
    ts_dec_init(&g_dec);
    for (uint32_t i = 0U; i < 30U; i++) {
        ts_frame_info_t ei;
        ts_frame_info_t di;
        const uint16_t seq = (uint16_t)(65530U + i);
        gen_moving(g_frame, i + 100U);
        reconstruct(g_expect, g_frame, steps[i]);
        TEST_ASSERT_EQUAL(TS_OK, ts_enc_set_quality(&g_enc, steps[i]));
        TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc, g_frame, seq, g_enc_buf, TS_ENC_MAX, &ei));
        TEST_ASSERT_EQUAL(TS_OK, ts_dec_decode(&g_dec, g_enc_buf, ei.enc_len, g_out, &di));
        TEST_ASSERT_EQUAL_UINT8(steps[i], di.quality);
        TEST_ASSERT_EQUAL_MEMORY(g_expect, g_out, TS_FRAME_BYTES);
        TEST_ASSERT_EQUAL_MEMORY(g_enc.ref, g_dec.ref, TS_FRAME_BYTES);
    }
    TEST_ASSERT_EQUAL_UINT32(1U, g_dec.keyframes);
}

static void test_static_scene_after_step_down_is_tiny(void)
{
    /* With the reconstructed reference a static scene costs nothing after a
     * quality step-down: P symbols are all zero (no smear). */
    ts_frame_info_t ei;
    for (uint32_t y = 0U; y < TS_HEIGHT; y++) {               /* smooth: rows differ by <= 1 */
        for (uint32_t x = 0U; x < TS_WIDTH; x++) {
            g_frame[(y * TS_WIDTH) + x] = (uint8_t)((x + y) / 3U);
        }
    }
    ts_enc_init(&g_enc);
    TEST_ASSERT_EQUAL(TS_OK, ts_enc_set_quality(&g_enc, 2U));
    TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc, g_frame, 1U, g_enc_buf, TS_ENC_MAX, &ei));
    TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc, g_frame, 2U, g_enc_buf, TS_ENC_MAX, &ei));
    TEST_ASSERT_FALSE(ei.keyframe);
    /* 120 rows * 320 zero symbols = 38400 -> ceil(38400/255) runs of 2 bytes */
    TEST_ASSERT_EQUAL_UINT32(TS_CODEC_HDR_LEN + (2U * ((38400U + 254U) / 255U)), ei.enc_len);
    TEST_ASSERT_EQUAL_UINT16(0U, ei.changed_tiles);
}

/* ---- worst case / TS_ENC_MAX ---------------------------------------------------- */
static void test_noise_frames_fit_enc_max(void)
{
    const uint8_t qs[3] = { 1U, 2U, 4U };
    uint32_t worst = 0U;
    for (size_t qi = 0U; qi < 3U; qi++) {
        ts_enc_init(&g_enc);
        ts_dec_init(&g_dec);
        TEST_ASSERT_EQUAL(TS_OK, ts_enc_set_quality(&g_enc, qs[qi]));
        for (uint32_t i = 0U; i < 4U; i++) {
            ts_frame_info_t ei;
            gen_noise(g_frame, 1000U + i + (uint32_t)qi);
            reconstruct(g_expect, g_frame, qs[qi]);
            guard_set(g_enc_buf, sizeof(g_enc_buf));
            TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc, g_frame, (uint16_t)i, g_enc_buf, TS_ENC_MAX, &ei));
            TEST_ASSERT_TRUE(ei.enc_len <= TS_ENC_MAX);
            guard_check(&g_enc_buf[TS_ENC_MAX], GUARD_LEN);
            guard_check(&g_enc_buf[ei.enc_len], TS_ENC_MAX - ei.enc_len);   /* exact length reported */
            TEST_ASSERT_EQUAL(TS_OK, ts_dec_decode(&g_dec, g_enc_buf, ei.enc_len, g_out, NULL));
            TEST_ASSERT_EQUAL_MEMORY(g_expect, g_out, TS_FRAME_BYTES);
            if (ei.enc_len > worst) {
                worst = ei.enc_len;
            }
        }
    }
    /* noise is almost all literals: n + 2 per 255 symbols + header */
    TEST_ASSERT_TRUE(worst > TS_FRAME_BYTES);
    TEST_ASSERT_TRUE(worst < (TS_FRAME_BYTES + 700U));
}

static void test_adversarial_patterns_fit_enc_max(void)
{
    /* Patterns that make RLE expand: single literal + run of 3 (5 bytes / 4
     * symbols), 2-runs and alternating literals. Encoded both as keyframe and
     * as P-frame (XOR delta producing the same pattern). */
    for (uint32_t pat = 0U; pat < 4U; pat++) {
        ts_frame_info_t ei;
        for (size_t i = 0U; i < TS_FRAME_BYTES; i++) {
            uint8_t v;
            switch (pat) {
            case 0U: v = ((i % 4U) == 0U) ? (uint8_t)(i / 4U) : 0xEEU; break;        /* a bbb */
            case 1U: v = (uint8_t)(((i / 2U) % 2U) * 255U); break;                      /* aabb */
            case 2U: v = (uint8_t)(((i % 3U) == 2U) ? 0x11U : 0x22U); break;           /* aab */
            default: v = (uint8_t)((i * 131U) >> 3); break;
            }
            g_frame[i] = v;
        }
        ts_enc_init(&g_enc);
        ts_dec_init(&g_dec);
        guard_set(g_enc_buf, sizeof(g_enc_buf));
        TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc, g_frame, 7U, g_enc_buf, TS_ENC_MAX, &ei));
        TEST_ASSERT_TRUE(ei.enc_len <= (((5U * TS_FRAME_BYTES) / 3U) + 2U + TS_CODEC_HDR_LEN));
        guard_check(&g_enc_buf[TS_ENC_MAX], GUARD_LEN);
        TEST_ASSERT_EQUAL(TS_OK, ts_dec_decode(&g_dec, g_enc_buf, ei.enc_len, g_out, NULL));
        TEST_ASSERT_EQUAL_MEMORY(g_frame, g_out, TS_FRAME_BYTES);
        /* P-frame: zero reference trick -> XOR delta equals the pattern again */
        for (size_t i = 0U; i < TS_FRAME_BYTES; i++) {
            g_frame[i] = (uint8_t)(g_frame[i] ^ g_enc.ref[i] ^ g_frame[(i + 1U) % TS_FRAME_BYTES]);
        }
        TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc, g_frame, 8U, g_enc_buf, TS_ENC_MAX, &ei));
        TEST_ASSERT_FALSE(ei.keyframe);
        TEST_ASSERT_TRUE(ei.enc_len <= TS_ENC_MAX);
        TEST_ASSERT_EQUAL(TS_OK, ts_dec_decode(&g_dec, g_enc_buf, ei.enc_len, g_out, NULL));
        TEST_ASSERT_EQUAL_MEMORY(g_frame, g_out, TS_FRAME_BYTES);
    }
}

static void test_encode_nomem_keeps_reference(void)
{
    ts_frame_info_t ei;
    size_t full;
    gen_moving(g_frame, 1U);
    ts_enc_init(&g_enc);
    TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc, g_frame, 1U, g_enc_buf, TS_ENC_MAX, &ei));
    gen_noise(g_frame, 77U);
    /* measure on a copy, then retry the original with one byte too few */
    (void)memcpy(&g_enc_copy, &g_enc, sizeof(g_enc));
    TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc_copy, g_frame, 2U, g_enc_buf2, TS_ENC_MAX, &ei));
    full = ei.enc_len;
    (void)memcpy(&g_enc_copy, &g_enc, sizeof(g_enc));
    guard_set(g_enc_buf, sizeof(g_enc_buf));
    TEST_ASSERT_EQUAL(TS_E_NOMEM, ts_enc_encode(&g_enc, g_frame, 2U, g_enc_buf, full - 1U, &ei));
    guard_check(&g_enc_buf[full - 1U], GUARD_LEN);
    TEST_ASSERT_EQUAL_MEMORY(&g_enc_copy, &g_enc, sizeof(g_enc));   /* nothing changed */
    TEST_ASSERT_EQUAL(TS_E_NOMEM, ts_enc_encode(&g_enc, g_frame, 2U, g_enc_buf, 7U, &ei));
    TEST_ASSERT_EQUAL(TS_E_NOMEM, ts_enc_encode(&g_enc, g_frame, 2U, g_enc_buf, 0U, &ei));
    TEST_ASSERT_EQUAL_MEMORY(&g_enc_copy, &g_enc, sizeof(g_enc));
    TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc, g_frame, 2U, g_enc_buf, full, &ei));
    TEST_ASSERT_EQUAL_UINT32(full, ei.enc_len);
    TEST_ASSERT_EQUAL_MEMORY(g_enc_buf2, g_enc_buf, full);
}

static void test_encoder_api(void)
{
    ts_frame_info_t ei;
    ts_enc_init(&g_enc);
    TEST_ASSERT_EQUAL_UINT8(1U, ts_enc_quality(&g_enc));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_enc_set_quality(&g_enc, 0U));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_enc_set_quality(&g_enc, 3U));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_enc_set_quality(&g_enc, 8U));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_enc_set_quality(NULL, 1U));
    TEST_ASSERT_EQUAL_UINT8(1U, ts_enc_quality(&g_enc));
    TEST_ASSERT_EQUAL_UINT8(0U, ts_enc_quality(NULL));
    TEST_ASSERT_EQUAL_UINT16(0U, ts_enc_changed_tiles(NULL));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_enc_encode(NULL, g_frame, 1U, g_enc_buf, TS_ENC_MAX, &ei));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_enc_encode(&g_enc, NULL, 1U, g_enc_buf, TS_ENC_MAX, &ei));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_enc_encode(&g_enc, g_frame, 1U, NULL, TS_ENC_MAX, &ei));
    ts_enc_force_keyframe(NULL);
    ts_enc_init(NULL);

    /* first frame is a keyframe with all 300 tiles changed */
    gen_moving(g_frame, 0U);
    TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc, g_frame, 9U, g_enc_buf, TS_ENC_MAX, NULL));
    TEST_ASSERT_EQUAL_UINT16(TS_TILES, ts_enc_changed_tiles(&g_enc));
    TEST_ASSERT_EQUAL_HEX8(0xFFU, g_enc.tile_bitmap[0]);
    TEST_ASSERT_EQUAL_HEX8(0x0FU, g_enc.tile_bitmap[TS_TILE_BITMAP_BYTES - 1U]);   /* bits 296..299 */
    /* force_keyframe -> next frame is K, then P again */
    ts_enc_force_keyframe(&g_enc);
    TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc, g_frame, 10U, g_enc_buf, TS_ENC_MAX, &ei));
    TEST_ASSERT_TRUE(ei.keyframe);
    TEST_ASSERT_EQUAL_UINT16(10U, ei.ref_seq);
    TEST_ASSERT_EQUAL_UINT16(0U, ei.changed_tiles);
    TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc, g_frame, 11U, g_enc_buf, TS_ENC_MAX, &ei));
    TEST_ASSERT_FALSE(ei.keyframe);
    TEST_ASSERT_EQUAL_UINT16(10U, ei.ref_seq);
    TEST_ASSERT_EQUAL_UINT32(3U, g_enc.frames);
    TEST_ASSERT_EQUAL_UINT32(2U, g_enc.keyframes);
}

/* ---- decoder robustness ------------------------------------------------------------ */
static size_t encode_one(uint8_t q, uint16_t seq, bool force_key)
{
    ts_frame_info_t ei;
    TEST_ASSERT_EQUAL(TS_OK, ts_enc_set_quality(&g_enc, q));
    if (force_key) {
        ts_enc_force_keyframe(&g_enc);
    }
    TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc, g_frame, seq, g_enc_buf, TS_ENC_MAX, &ei));
    return (size_t)ei.enc_len;
}

static void test_decoder_noref(void)
{
    size_t k_len;
    size_t p_len;
    ts_frame_info_t di;
    ts_enc_init(&g_enc);
    ts_dec_init(&g_dec);
    gen_moving(g_frame, 1U);
    k_len = encode_one(1U, 100U, false);
    (void)memcpy(g_enc_buf2, g_enc_buf, k_len);
    gen_moving(g_frame, 2U);
    p_len = encode_one(1U, 101U, false);

    /* P-frame without any reference */
    TEST_ASSERT_EQUAL(TS_E_NOREF, ts_dec_decode(&g_dec, g_enc_buf, p_len, g_out, &di));
    TEST_ASSERT_EQUAL_UINT32(1U, g_dec.errors);
    TEST_ASSERT_FALSE(g_dec.have_ref);
    /* keyframe recovers */
    TEST_ASSERT_EQUAL(TS_OK, ts_dec_decode(&g_dec, g_enc_buf2, k_len, g_out, &di));
    TEST_ASSERT_TRUE(di.keyframe);
    TEST_ASSERT_EQUAL_UINT16(TS_TILES, di.changed_tiles);
    TEST_ASSERT_EQUAL_HEX8(0xFFU, ts_dec_tile_bitmap(&g_dec)[5]);
    /* P-frame against the wrong reference: rejected, reference untouched */
    (void)memcpy(&g_dec_copy, &g_dec, sizeof(g_dec));
    g_enc_buf[4] = (uint8_t)(g_enc_buf[4] + 1U);   /* ref_seq 100 -> 101 */
    TEST_ASSERT_EQUAL(TS_E_NOREF, ts_dec_decode(&g_dec, g_enc_buf, p_len, g_out, &di));
    TEST_ASSERT_EQUAL_MEMORY(g_dec_copy.ref, g_dec.ref, TS_FRAME_BYTES);
    TEST_ASSERT_EQUAL_UINT16(100U, g_dec.last_seq);
    g_enc_buf[4] = (uint8_t)(g_enc_buf[4] - 1U);
    TEST_ASSERT_EQUAL(TS_OK, ts_dec_decode(&g_dec, g_enc_buf, p_len, g_out, &di));
    TEST_ASSERT_FALSE(di.keyframe);
    TEST_ASSERT_EQUAL_MEMORY(g_enc.ref, g_out, TS_FRAME_BYTES);
    /* replaying the same P-frame: its ref (100) is no longer the last frame (101) */
    TEST_ASSERT_EQUAL(TS_E_NOREF, ts_dec_decode(&g_dec, g_enc_buf, p_len, g_out, &di));
    TEST_ASSERT_EQUAL_UINT32(3U, g_dec.errors);
    TEST_ASSERT_EQUAL_UINT32(2U, g_dec.frames);
}

static void test_decoder_run_overflow_rejected(void)
{
    /* a run that would exceed the frame must not write past it */
    static uint8_t bs[TS_CODEC_HDR_LEN + (2U * 400U)];
    const ts_codec_hdr_t h = { TS_CODEC_KIND_KEY, 1U, 5U, 5U, 240U };
    size_t len = TS_CODEC_HDR_LEN;
    TEST_ASSERT_EQUAL(TS_OK, ts_codec_hdr_pack(&h, bs, sizeof(bs)));
    for (size_t i = 0U; i < 302U; i++) {     /* 302 * 255 = 77010 > 76800 */
        bs[len] = 255U;
        bs[len + 1U] = 0x77U;
        len += 2U;
    }
    ts_dec_init(&g_dec);
    guard_set(g_out, sizeof(g_out));
    TEST_ASSERT_EQUAL(TS_E_CORRUPT, ts_dec_decode(&g_dec, bs, len, g_out, NULL));
    guard_check(&g_out[TS_FRAME_BYTES], GUARD_LEN);
    TEST_ASSERT_FALSE(g_dec.have_ref);
    /* same for a literal crossing the end, at quality 4 (n = 19200) */
    {
        const ts_codec_hdr_t h4 = { TS_CODEC_KIND_KEY, 4U, 5U, 5U, 60U };
        size_t l4 = TS_CODEC_HDR_LEN;
        TEST_ASSERT_EQUAL(TS_OK, ts_codec_hdr_pack(&h4, bs, sizeof(bs)));
        for (size_t i = 0U; i < 75U; i++) {  /* 75 * 255 = 19125 */
            bs[l4] = 255U;
            bs[l4 + 1U] = 1U;
            l4 += 2U;
        }
        bs[l4] = 0U;
        bs[l4 + 1U] = 76U;                   /* 19125 + 76 = 19201 > n */
        l4 += 2U;
        (void)memset(&bs[l4], 3, 76U);
        l4 += 76U;
        guard_set(g_out, sizeof(g_out));
        TEST_ASSERT_EQUAL(TS_E_CORRUPT, ts_dec_decode(&g_dec, bs, l4, g_out, NULL));
        guard_check(&g_out[TS_FRAME_BYTES], GUARD_LEN);
        bs[l4 - 77U] = 75U;                  /* exactly n, one trailing byte left */
        TEST_ASSERT_EQUAL(TS_E_CORRUPT, ts_dec_decode(&g_dec, bs, l4, g_out, NULL));
        TEST_ASSERT_EQUAL(TS_OK, ts_dec_decode(&g_dec, bs, l4 - 1U, g_out, NULL));
        guard_check(&g_out[TS_FRAME_BYTES], GUARD_LEN);
    }
}

static void test_decoder_every_truncation(void)
{
    size_t len;
    ts_enc_init(&g_enc);
    gen_moving(g_frame, 4U);
    for (size_t i = 0U; i < TS_FRAME_BYTES; i++) {
        g_frame[i] = (uint8_t)((i / 1000U) & 0xFFU);   /* small stream */
    }
    len = encode_one(4U, 1U, false);
    TEST_ASSERT_TRUE(len < 400U);
    for (size_t cut = 0U; cut < len; cut++) {
        ts_dec_init(&g_dec);
        TEST_ASSERT_NOT_EQUAL(TS_OK, ts_dec_decode(&g_dec, g_enc_buf, cut, g_out, NULL));
        TEST_ASSERT_EQUAL_UINT32(1U, g_dec.errors);
    }
    ts_dec_init(&g_dec);
    g_enc_buf[len] = 0U;
    TEST_ASSERT_EQUAL(TS_E_CORRUPT, ts_dec_decode(&g_dec, g_enc_buf, len + 1U, g_out, NULL));
    TEST_ASSERT_EQUAL(TS_OK, ts_dec_decode(&g_dec, g_enc_buf, len, g_out, NULL));
}

static void test_decoder_bad_args_and_header(void)
{
    ts_frame_info_t di;
    uint8_t bad[TS_CODEC_HDR_LEN] = { 0x51U, 1U, 0U, 0U, 0U, 0U, 240U, 0U };
    ts_dec_init(&g_dec);
    TEST_ASSERT_EQUAL(TS_E_TRUNC, ts_dec_decode(&g_dec, bad, 7U, g_out, &di));
    TEST_ASSERT_EQUAL(TS_E_TRUNC, ts_dec_decode(&g_dec, NULL, 0U, g_out, &di));
    TEST_ASSERT_EQUAL(TS_E_CORRUPT, ts_dec_decode(&g_dec, bad, sizeof(bad), g_out, &di));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_dec_decode(NULL, bad, sizeof(bad), g_out, &di));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_dec_decode(&g_dec, bad, sizeof(bad), NULL, &di));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_dec_decode(&g_dec, bad, sizeof(bad), g_dec.ref, &di));
    TEST_ASSERT_EQUAL_UINT32(3U, g_dec.errors);
    TEST_ASSERT_NULL(ts_dec_tile_bitmap(NULL));
    ts_dec_init(NULL);
}

static void test_decoder_random_corruption_never_overruns(void)
{
    uint32_t s = 12345U;
    size_t len;
    ts_enc_init(&g_enc);
    gen_moving(g_frame, 9U);
    len = encode_one(4U, 1U, false);
    (void)memcpy(g_enc_buf2, g_enc_buf, len);
    for (uint32_t it = 0U; it < 200U; it++) {
        const uint32_t flips = 1U + (xs32(&s) % 8U);
        (void)memcpy(g_enc_buf, g_enc_buf2, len);
        for (uint32_t f = 0U; f < flips; f++) {
            const uint32_t r = xs32(&s);
            g_enc_buf[TS_CODEC_HDR_LEN + (r % (uint32_t)(len - TS_CODEC_HDR_LEN))] = (uint8_t)(r >> 24);
        }
        ts_dec_init(&g_dec);
        guard_set(g_out, sizeof(g_out));
        (void)ts_dec_decode(&g_dec, g_enc_buf, len, g_out, NULL);
        guard_check(&g_out[TS_FRAME_BYTES], GUARD_LEN);
    }
}

/* ---- changed tiles ------------------------------------------------------------------ */
static void test_changed_tiles(void)
{
    static uint8_t a[TS_FRAME_BYTES];
    static uint8_t b[TS_FRAME_BYTES];
    uint8_t bm[TS_TILE_BITMAP_BYTES];
    (void)memset(a, 100, sizeof(a));
    (void)memcpy(b, a, sizeof(b));
    TEST_ASSERT_EQUAL_UINT16(0U, ts_codec_changed_tiles(a, b, bm));
    b[0] = 116U;                                       /* diff 16: not changed */
    TEST_ASSERT_EQUAL_UINT16(0U, ts_codec_changed_tiles(a, b, bm));
    b[0] = 117U;                                       /* diff 17: tile 0 */
    TEST_ASSERT_EQUAL_UINT16(1U, ts_codec_changed_tiles(a, b, bm));
    TEST_ASSERT_EQUAL_HEX8(0x01U, bm[0]);
    b[0] = 83U;                                        /* diff -17 */
    TEST_ASSERT_EQUAL_UINT16(1U, ts_codec_changed_tiles(b, a, NULL));
    b[TS_FRAME_BYTES - 1U] = 0U;                       /* last pixel: tile 299 */
    b[(16U * TS_WIDTH) + 16U] = 255U;                  /* tile (1,1) = 21 */
    TEST_ASSERT_EQUAL_UINT16(3U, ts_codec_changed_tiles(a, b, bm));
    TEST_ASSERT_EQUAL_HEX8(0x01U, bm[0]);
    TEST_ASSERT_EQUAL_HEX8(0x20U, bm[2]);              /* 21 = byte 2, bit 5 */
    TEST_ASSERT_EQUAL_HEX8(0x08U, bm[37]);             /* 299 = byte 37, bit 3 */
    TEST_ASSERT_EQUAL_UINT16(0U, ts_codec_changed_tiles(NULL, b, bm));
    TEST_ASSERT_EQUAL_HEX8(0x00U, bm[37]);
}

/*
 * A static, detailed scene (2-row horizontal stripes of amplitude 40) must
 * report 0 changed tiles at every quality step: the rows a quality step does
 * not transmit cannot change a tile (PROTOCOL.md 6.6). Comparing the raw frame
 * with the row-replicated reference made every tile "changed" at q2/q4 and the
 * keyframe scheduler sent a scene-change keyframe every second.
 */
static void test_changed_tiles_static_detail_every_quality(void)
{
    static const uint8_t qs[3] = { 1U, 2U, 4U };
    ts_frame_info_t ei;
    for (uint32_t y = 0U; y < TS_HEIGHT; y++) {
        uint8_t v = (((y / 2U) % 2U) == 0U) ? 60U : 100U;
        (void)memset(&g_frame[y * TS_WIDTH], (int)v, TS_WIDTH);
    }
    for (uint32_t i = 0U; i < 3U; i++) {
        ts_keysched_t k;
        ts_enc_init(&g_enc);
        TEST_ASSERT_EQUAL(TS_OK, ts_enc_set_quality(&g_enc, qs[i]));
        TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc, g_frame, 1U, g_enc_buf, TS_ENC_MAX, &ei));
        TEST_ASSERT_TRUE(ei.keyframe);
        /* the identical frame again */
        TEST_ASSERT_EQUAL_UINT16(0U, ts_codec_changed_tiles_q(g_frame, g_enc.ref, qs[i], NULL));
        TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc, g_frame, 2U, g_enc_buf, TS_ENC_MAX, &ei));
        TEST_ASSERT_FALSE(ei.keyframe);
        TEST_ASSERT_EQUAL_UINT16(0U, ei.changed_tiles);
        TEST_ASSERT_EQUAL_UINT16(0U, ts_enc_changed_tiles(&g_enc));
        /* so the scheduler never sees a scene change on a static scene */
        ts_keysched_init(&k, 0U);
        ts_keysched_sent(&k, 0U);
        TEST_ASSERT_EQUAL_INT(TS_KEY_NONE, ts_keysched_decide(&k, 1000U, ei.changed_tiles));
    }
    /* the raw comparison is what went wrong: at q2 every tile differs from the replicated rows */
    TEST_ASSERT_EQUAL_UINT16(TS_TILES, ts_codec_changed_tiles(g_frame, g_enc.ref, NULL));
    /* q1 is the plain comparison; an invalid quality marks every tile */
    TEST_ASSERT_EQUAL_UINT16(ts_codec_changed_tiles(g_frame, g_expect, NULL),
                             ts_codec_changed_tiles_q(g_frame, g_expect, 1U, NULL));
    TEST_ASSERT_EQUAL_UINT16(TS_TILES, ts_codec_changed_tiles_q(g_frame, g_frame, 3U, NULL));
    TEST_ASSERT_EQUAL_UINT16(TS_TILES, ts_codec_changed_tiles_q(g_frame, g_frame, 0U, g_expect));
    /* a real change in a transmitted row still counts at q2 */
    ts_enc_init(&g_enc);
    TEST_ASSERT_EQUAL(TS_OK, ts_enc_set_quality(&g_enc, 2U));
    TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc, g_frame, 1U, g_enc_buf, TS_ENC_MAX, &ei));
    g_frame[(32U * TS_WIDTH) + 40U] = 200U;    /* row 32 is sent at q2 -> tile (2, 2) */
    g_frame[(33U * TS_WIDTH) + 200U] = 200U;   /* row 33 is not -> no tile */
    TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc, g_frame, 2U, g_enc_buf, TS_ENC_MAX, &ei));
    TEST_ASSERT_EQUAL_UINT16(1U, ei.changed_tiles);
    TEST_ASSERT_EQUAL_HEX8(0x04U, g_enc.tile_bitmap[5]);   /* tile 42 = byte 5, bit 2 */
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_codec_hdr_vectors);
    RUN_TEST(test_codec_hdr_pack_errors);
    RUN_TEST(test_rle_vectors);
    RUN_TEST(test_rle_nomem_at_every_cap);
    RUN_TEST(test_frame_vectors_bit_exact);
    RUN_TEST(test_ref_sync_30_frames_q1);
    RUN_TEST(test_ref_sync_30_frames_q2);
    RUN_TEST(test_ref_sync_30_frames_q4);
    RUN_TEST(test_ref_sync_quality_steps_mid_stream);
    RUN_TEST(test_static_scene_after_step_down_is_tiny);
    RUN_TEST(test_noise_frames_fit_enc_max);
    RUN_TEST(test_adversarial_patterns_fit_enc_max);
    RUN_TEST(test_encode_nomem_keeps_reference);
    RUN_TEST(test_encoder_api);
    RUN_TEST(test_decoder_noref);
    RUN_TEST(test_decoder_run_overflow_rejected);
    RUN_TEST(test_decoder_every_truncation);
    RUN_TEST(test_decoder_bad_args_and_header);
    RUN_TEST(test_decoder_random_corruption_never_overruns);
    RUN_TEST(test_changed_tiles);
    RUN_TEST(test_changed_tiles_static_detail_every_quality);
    return UNITY_END();
}
