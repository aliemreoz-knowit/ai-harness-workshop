/*
 * tinystream v2 -- hidden evaluation test: tests/unit/test_codec_after_loss_eval.c
 * (ticket metadata: meta.yaml next to this file)
 *
 * Encoder/decoder reference sync across quality steps (PROTOCOL.md 6.2, 6.5),
 * through the codec API only. One keyframe at the start, then P-frames only:
 *   - static detailed scene, quality 1 -> 4 -> 2 -> 1: every decoded frame is
 *     the input with rows replicated for its quality step, and after the
 *     return to quality 1 the picture is exactly the input again (no ghost);
 *   - slowly moving object over a detailed background, quality changing every
 *     few frames for 90 frames: same exactness on every frame;
 *   - encoder and decoder references are bit-identical after every frame.
 * The expected picture is computed here independently of the codec.
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
static uint32_t g_ref_bad;

void setUp(void)
{
    g_ref_bad = 0U;
    ts_enc_init(&g_enc);
    ts_dec_init(&g_dec);
}

void tearDown(void)
{
}

/* Rows replicated per quality step: row r*q is shown on rows r*q .. r*q+q-1. */
static void expand(uint8_t *dst, const uint8_t *src, uint32_t q)
{
    for (uint32_t y = 0U; y < (uint32_t)TS_HEIGHT; y++) {
        uint32_t sy = (y / q) * q;
        (void)memcpy(&dst[y * (uint32_t)TS_WIDTH], &src[sy * (uint32_t)TS_WIDTH], (size_t)TS_WIDTH);
    }
}

/* Detailed background (every row differs from its neighbours) plus an optional
 * 40x30 bright object that moves 2 px per frame. */
static void make_frame(uint8_t *f, uint32_t t, bool moving)
{
    for (uint32_t y = 0U; y < (uint32_t)TS_HEIGHT; y++) {
        for (uint32_t x = 0U; x < (uint32_t)TS_WIDTH; x++) {
            uint32_t v = ((x * 5U) ^ (y * 29U)) + ((x / 16U) * 7U) + ((y & 3U) * 40U);
            f[(y * (uint32_t)TS_WIDTH) + x] = (uint8_t)(v & 0xFFU);
        }
    }
    if (moving) {
        uint32_t ox = (20U + (2U * t)) % ((uint32_t)TS_WIDTH - 40U);
        uint32_t oy = 60U + ((t / 4U) % 100U);
        for (uint32_t y = oy; y < (oy + 30U); y++) {
            for (uint32_t x = ox; x < (ox + 40U); x++) {
                f[(y * (uint32_t)TS_WIDTH) + x] = (uint8_t)(230U + ((x + y) & 7U));
            }
        }
    }
}

static uint32_t step(uint16_t seq, uint8_t q, uint32_t *keyframes)
{
    ts_frame_info_t ei;
    ts_frame_info_t di;
    uint32_t bad = 0U;
    TEST_ASSERT_EQUAL(TS_OK, ts_enc_set_quality(&g_enc, q));
    TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc, g_frame, seq, g_buf, sizeof(g_buf), &ei));
    TEST_ASSERT_EQUAL_UINT8(q, ei.quality);
    TEST_ASSERT_EQUAL(TS_OK, ts_dec_decode(&g_dec, g_buf, (size_t)ei.enc_len, g_out, &di));
    if (ei.keyframe) {
        (*keyframes)++;
    }
    expand(g_expect, g_frame, (uint32_t)q);
    if (memcmp(g_out, g_expect, sizeof(g_out)) != 0) {
        bad++;
    }
    if (memcmp(g_enc.ref, g_dec.ref, sizeof(g_out)) != 0) {
        g_ref_bad++;
    }
    return bad;
}

static void test_static_scene_quality_down_and_back_up_stays_exact(void)
{
    static const uint8_t qs[] = { 1U, 1U, 1U, 1U, 4U, 4U, 4U, 4U, 2U, 2U, 2U, 2U,
                                  1U, 1U, 1U, 1U, 1U, 1U, 1U, 1U, 1U, 1U, 1U, 1U };
    uint32_t bad = 0U;
    uint32_t keys = 0U;
    make_frame(g_frame, 0U, false);
    for (uint32_t i = 0U; i < (uint32_t)sizeof(qs); i++) {
        bad += step((uint16_t)(100U + i), qs[i], &keys);
    }
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(1U, keys, "only the first frame may be a keyframe");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, bad, "decoded frames differ from the input at their quality step");
    TEST_ASSERT_EQUAL_MEMORY_MESSAGE(g_frame, g_out, sizeof(g_out),
                                     "back at quality 1 the static picture is not exact (ghost)");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, g_ref_bad, "encoder and decoder references differ (PROTOCOL.md 6.5)");
}

static void test_moving_scene_changing_quality_stays_exact(void)
{
    uint32_t bad = 0U;
    uint32_t keys = 0U;
    uint32_t s = 0xC0FFEEU;
    uint8_t q = 1U;
    for (uint32_t t = 0U; t < 90U; t++) {
        if ((t % 5U) == 4U) {
            s ^= s << 13;
            s ^= s >> 17;
            s ^= s << 5;
            q = (uint8_t)(1U << (s % 3U));      /* 1, 2 or 4 */
        }
        if (t >= 80U) {
            q = 1U;
        }
        make_frame(g_frame, t, true);
        bad += step((uint16_t)(65500U + t), q, &keys);     /* crosses the frame_seq wrap */
    }
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(1U, keys, "only the first frame may be a keyframe");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, bad, "decoded frames differ from the input at their quality step");
    TEST_ASSERT_EQUAL_MEMORY_MESSAGE(g_frame, g_out, sizeof(g_out), "last frame at quality 1 not exact");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, g_ref_bad, "encoder and decoder references differ (PROTOCOL.md 6.5)");
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_static_scene_quality_down_and_back_up_stays_exact);
    RUN_TEST(test_moving_scene_changing_quality_stays_exact);
    return UNITY_END();
}
