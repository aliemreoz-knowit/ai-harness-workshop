/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/unit/test_prop_codec.c
 * Property tests for src/codec (labels unit + property, gate property-tests):
 *   - RLE round-trip and size bound (<= 5n/3 + 2 <= 2n + 2) on random
 *     sequences over small alphabets (the run/literal boundary cases),
 *   - random frame sequences (random quality steps, forced keyframes, start
 *     seq near the wrap): decoder output == raw frame with rows replicated and
 *     encoder reference == decoder reference after every frame,
 *   - the decoder never accepts a P-frame against a stale reference.
 */
#include <string.h>
#include "unity.h"
#include "ts_config.h"
#include "ts_proto.h"
#include "ts_codec.h"

static ts_enc_t g_enc;
static ts_dec_t g_dec;
static uint8_t g_frame[TS_FRAME_BYTES];
static uint8_t g_expect[TS_FRAME_BYTES];
static uint8_t g_out[TS_FRAME_BYTES];
static uint8_t g_buf[TS_ENC_MAX];

void setUp(void)
{
}

void tearDown(void)
{
}

static uint32_t rnd(uint32_t *s)
{
    uint32_t x = *s;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    *s = x;
    return x;
}

static void test_prop_rle_roundtrip_and_bound(void)
{
    static uint8_t sym[2048];
    static uint8_t enc[(2U * 2048U) + 2U];
    static uint8_t dec[2048];
    uint32_t s = 0x1234567U;
    for (uint32_t it = 0U; it < 3000U; it++) {
        const size_t n = (size_t)(rnd(&s) % 2049U);
        const uint32_t alphabet = 1U + (rnd(&s) % 4U);
        const uint32_t stickiness = rnd(&s) % 4U;      /* longer runs when larger */
        size_t len = 0U;
        size_t used = 0U;
        uint8_t v = 0U;
        for (size_t i = 0U; i < n; i++) {
            if ((rnd(&s) % 4U) >= stickiness) {
                v = (uint8_t)(rnd(&s) % alphabet);
            }
            sym[i] = v;
        }
        TEST_ASSERT_EQUAL(TS_OK, ts_rle_encode(sym, n, enc, sizeof(enc), &len));
        TEST_ASSERT_TRUE(len <= (((5U * n) / 3U) + 2U));
        TEST_ASSERT_TRUE(len <= ((2U * n) + 2U));
        TEST_ASSERT_EQUAL(TS_OK, ts_rle_decode(enc, len, dec, n, &used));
        TEST_ASSERT_EQUAL_size_t(len, used);
        if (n > 0U) {
            TEST_ASSERT_EQUAL_MEMORY(sym, dec, n);
        }
    }
}

static void gen_scene(uint8_t *f, uint32_t i, uint32_t *s)
{
    const uint32_t kind = rnd(s) % 4U;
    if (kind == 0U) {
        for (size_t p = 0U; p < TS_FRAME_BYTES; p++) {      /* noise */
            f[p] = (uint8_t)rnd(s);
        }
    } else if (kind == 1U) {
        /* static: keep the previous frame */
    } else {
        const uint32_t bx = rnd(s) % (TS_WIDTH - 32U);
        const uint32_t by = rnd(s) % (TS_HEIGHT - 32U);
        for (uint32_t y = 0U; y < 32U; y++) {               /* moving block */
            (void)memset(&f[((by + y) * TS_WIDTH) + bx], (int)(i & 0xFFU), 32U);
        }
        for (uint32_t k = 0U; k < 64U; k++) {               /* sparse noise */
            const uint32_t r = rnd(s);
            f[r % TS_FRAME_BYTES] = (uint8_t)(r >> 24);
        }
    }
}

static void test_prop_random_sequences_stay_in_sync(void)
{
    const uint8_t qs[3] = { 1U, 2U, 4U };
    uint32_t s = 0xFACADEU;
    for (uint32_t run = 0U; run < 6U; run++) {
        uint16_t seq = (uint16_t)(65500U + (rnd(&s) % 64U));
        ts_enc_init(&g_enc);
        ts_dec_init(&g_dec);
        for (size_t p = 0U; p < TS_FRAME_BYTES; p++) {
            g_frame[p] = (uint8_t)((p % TS_WIDTH) + (p / TS_WIDTH));
        }
        for (uint32_t i = 0U; i < 30U; i++) {
            ts_frame_info_t ei;
            ts_frame_info_t di;
            const uint8_t q = qs[rnd(&s) % 3U];
            gen_scene(g_frame, i, &s);
            TEST_ASSERT_EQUAL(TS_OK, ts_enc_set_quality(&g_enc, q));
            if ((rnd(&s) % 10U) == 0U) {
                ts_enc_force_keyframe(&g_enc);
            }
            TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc, g_frame, seq, g_buf, sizeof(g_buf), &ei));
            TEST_ASSERT_TRUE(ei.enc_len <= TS_ENC_MAX);
            TEST_ASSERT_EQUAL(TS_OK, ts_dec_decode(&g_dec, g_buf, ei.enc_len, g_out, &di));
            for (uint32_t r = 0U; r < TS_HEIGHT; r += q) {
                for (uint32_t k = 0U; k < q; k++) {
                    (void)memcpy(&g_expect[(r + k) * TS_WIDTH], &g_frame[r * TS_WIDTH], TS_WIDTH);
                }
            }
            TEST_ASSERT_EQUAL_MEMORY(g_expect, g_out, TS_FRAME_BYTES);
            TEST_ASSERT_EQUAL_MEMORY(g_enc.ref, g_dec.ref, TS_FRAME_BYTES);
            TEST_ASSERT_EQUAL_UINT16(seq, di.frame_seq);
            seq++;
        }
        TEST_ASSERT_EQUAL_UINT32(0U, g_dec.errors);
    }
}

static void test_prop_lost_frame_needs_keyframe(void)
{
    /* Drop one P-frame: every following P-frame is TS_E_NOREF (never decoded
     * against a stale reference) until the next keyframe resyncs. */
    uint32_t s = 0x777U;
    ts_enc_init(&g_enc);
    ts_dec_init(&g_dec);
    (void)memset(g_frame, 50, sizeof(g_frame));
    for (uint32_t i = 0U; i < 12U; i++) {
        ts_frame_info_t ei;
        ts_err_t e;
        gen_scene(g_frame, i, &s);
        if (i == 8U) {
            ts_enc_force_keyframe(&g_enc);
        }
        TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc, g_frame, (uint16_t)(65533U + i), g_buf, sizeof(g_buf), &ei));
        if (i == 4U) {
            continue;                                   /* lost */
        }
        e = ts_dec_decode(&g_dec, g_buf, ei.enc_len, g_out, NULL);
        if ((i > 4U) && (i < 8U)) {
            TEST_ASSERT_EQUAL(TS_E_NOREF, e);
        } else {
            TEST_ASSERT_EQUAL(TS_OK, e);
            TEST_ASSERT_EQUAL_MEMORY(g_enc.ref, g_out, TS_FRAME_BYTES);
        }
    }
    TEST_ASSERT_EQUAL_UINT32(3U, g_dec.errors);
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_prop_rle_roundtrip_and_bound);
    RUN_TEST(test_prop_random_sequences_stay_in_sync);
    RUN_TEST(test_prop_lost_frame_needs_keyframe);
    return UNITY_END();
}
