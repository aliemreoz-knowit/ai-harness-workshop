/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/budget/test_budget_codec.c
 * Gate budgets codec budgets (label "budget", run by scripts/check.sh budgets):
 *   - compression ratio of the synth stream at quality 1 with the daemon
 *     keyframe policy (first frame, scene change > 60 % tiles, periodic 10 s);
 *   - PSNR of the decoded stream at quality 2 and 4 (quality 1 is lossless);
 *   - TS_ENC_MAX: the analytic RLE bound and the largest frame measured on
 *     worst-case inputs stay within TS_ENC_MAX and within TS_MAX_FRAGS
 *     fragments; the encoder never writes past the buffer it was given.
 * The measured values are printed (lines starting with BUDGET) so the gate
 * log shows the margin to every budget.
 */
#include <stdio.h>
#include <string.h>
#include "unity.h"
#include "ts_config.h"
#include "ts_codec.h"
#include "ts_capture.h"
#include "../../tools/tsrecv/lib/tsr_internal.h"

/* ---- budgets ---------------------------------------------------------------- */
#define BUDGET_RATIO_X100     1000U    /**< raw / encoded >= 10.0 at quality 1 (synth, 20 s; golden 11.6) */
#define BUDGET_PSNR_Q2_CDB    2200U    /**< mean frame PSNR >= 22 dB at quality 2 (golden: 23.9 dB) */
#define BUDGET_PSNR_Q4_CDB    2000U    /**< mean frame PSNR >= 20 dB at quality 4 (golden: 22.1 dB) */
#define BUDGET_FRAMES          600U    /**< 20 s at 30 fps: two scene cuts, two noise bursts */

#define RLE_BOUND   (((5U * TS_FRAME_BYTES) / 3U) + 2U + TS_CODEC_HDR_LEN)
#define GUARD_BYTES 64U
#define GUARD_VALUE 0xA5U

/* static: several hundred KB */
static ts_enc_t g_enc;
static ts_dec_t g_dec;
static uint8_t  g_raw[TS_FRAME_BYTES];
static uint8_t  g_out[TS_FRAME_BYTES];
static uint8_t  g_buf[TS_ENC_MAX + GUARD_BYTES];

void setUp(void)
{
}

void tearDown(void)
{
}

typedef struct run_result {
    uint64_t raw_bytes;
    uint64_t enc_bytes;
    uint32_t keyframes;
    uint32_t max_enc;
    uint32_t min_psnr_cdb;   /**< worst lossy frame (0 = never lossy; the noise burst) */
    uint64_t sum_psnr_cdb;   /**< over lossy frames */
    uint32_t lossy;          /**< frames with PSNR != inf */
    uint32_t mismatches;     /**< decoded != encoder reference */
} run_result_t;

static uint32_t xorshift(uint32_t *s)
{
    uint32_t x = *s;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    *s = x;
    return x;
}

static void guard_fill(void)
{
    (void)memset(&g_buf[TS_ENC_MAX], (int)GUARD_VALUE, GUARD_BYTES);
}

static void guard_check(void)
{
    for (uint32_t i = 0U; i < GUARD_BYTES; i++) {
        TEST_ASSERT_EQUAL_HEX8_MESSAGE(GUARD_VALUE, g_buf[TS_ENC_MAX + i], "encoder wrote past TS_ENC_MAX");
    }
}

/* daemon keyframe policy without request / too-old (no receiver here) */
static void run_synth(uint8_t quality, run_result_t *r)
{
    uint32_t cut = ts_cap_scene_cut_frames(0U, TS_DEFAULT_FPS);
    uint32_t period = (TS_KEYFRAME_PERIOD_MS * TS_DEFAULT_FPS) / 1000U;
    uint32_t since_key = 0U;
    (void)memset(r, 0, sizeof(*r));
    ts_enc_init(&g_enc);
    ts_dec_init(&g_dec);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_enc_set_quality(&g_enc, quality));
    for (uint32_t i = 0U; i < BUDGET_FRAMES; i++) {
        ts_frame_info_t ei;
        ts_frame_info_t di;
        uint32_t p;
        ts_cap_synth_frame(g_raw, i, 1U, cut);
        if (g_enc.have_ref) {
            uint16_t changed = ts_codec_changed_tiles_q(g_raw, g_enc.ref, quality, NULL);   /* as the daemon */
            if (((uint32_t)changed * 100U) > (TS_TILES * TS_SCENE_CHANGE_PCT)) {
                ts_enc_force_keyframe(&g_enc);
            }
        }
        if (since_key >= period) {
            ts_enc_force_keyframe(&g_enc);
        }
        guard_fill();
        TEST_ASSERT_EQUAL_INT(TS_OK, ts_enc_encode(&g_enc, g_raw, (uint16_t)i, g_buf, TS_ENC_MAX, &ei));
        guard_check();
        TEST_ASSERT_EQUAL_INT(TS_OK, ts_dec_decode(&g_dec, g_buf, ei.enc_len, g_out, &di));
        if (memcmp(g_out, g_enc.ref, TS_FRAME_BYTES) != 0) {
            r->mismatches++;
        }
        p = tsr_i_psnr_cdb(g_raw, g_out);
        if (p != 0U) {
            r->sum_psnr_cdb += p;
            r->lossy++;
            if ((r->min_psnr_cdb == 0U) || (p < r->min_psnr_cdb)) {
                r->min_psnr_cdb = p;
            }
        }
        since_key = ei.keyframe ? 1U : (since_key + 1U);
        if (ei.keyframe) {
            r->keyframes++;
        }
        if (ei.enc_len > r->max_enc) {
            r->max_enc = ei.enc_len;
        }
        r->raw_bytes += TS_FRAME_BYTES;
        r->enc_bytes += ei.enc_len;
    }
}

static void test_budget_ratio_q1(void)
{
    run_result_t r;
    uint64_t ratio_x100;
    run_synth(1U, &r);
    ratio_x100 = (r.raw_bytes * 100U) / r.enc_bytes;
    (void)printf("BUDGET ratio_q1_x100=%lu (min %u) frames=%u keyframes=%lu enc_bytes=%lu max_frame=%lu\n",
                 (unsigned long)ratio_x100, BUDGET_RATIO_X100, BUDGET_FRAMES, (unsigned long)r.keyframes,
                 (unsigned long)r.enc_bytes, (unsigned long)r.max_enc);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, r.mismatches, "decoder output differs from the encoder reference");
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, r.min_psnr_cdb, "quality 1 must be lossless");
    TEST_ASSERT_TRUE_MESSAGE(ratio_x100 >= (uint64_t)BUDGET_RATIO_X100, "compression ratio below budget");
}

static void test_budget_psnr_q2_q4(void)
{
    run_result_t r2;
    run_result_t r4;
    run_synth(2U, &r2);
    uint32_t mean2;
    uint32_t mean4;
    run_synth(4U, &r4);
    TEST_ASSERT_TRUE(r2.lossy > 0U);
    TEST_ASSERT_TRUE(r4.lossy > 0U);
    mean2 = (uint32_t)(r2.sum_psnr_cdb / r2.lossy);
    mean4 = (uint32_t)(r4.sum_psnr_cdb / r4.lossy);
    (void)printf("BUDGET psnr_q2_mean_cdb=%lu (min %u, worst frame %lu) psnr_q4_mean_cdb=%lu (min %u, worst frame %lu)"
                 " ratio_q2_x100=%lu ratio_q4_x100=%lu\n",
                 (unsigned long)mean2, BUDGET_PSNR_Q2_CDB, (unsigned long)r2.min_psnr_cdb,
                 (unsigned long)mean4, BUDGET_PSNR_Q4_CDB, (unsigned long)r4.min_psnr_cdb,
                 (unsigned long)((r2.raw_bytes * 100U) / r2.enc_bytes),
                 (unsigned long)((r4.raw_bytes * 100U) / r4.enc_bytes));
    TEST_ASSERT_EQUAL_UINT32(0U, r2.mismatches);
    TEST_ASSERT_EQUAL_UINT32(0U, r4.mismatches);
    TEST_ASSERT_TRUE_MESSAGE(mean2 >= BUDGET_PSNR_Q2_CDB, "mean PSNR at quality 2 below budget");
    TEST_ASSERT_TRUE_MESSAGE(mean4 >= BUDGET_PSNR_Q4_CDB, "mean PSNR at quality 4 below budget");
}

/* RLE worst case per PROTOCOL.md 6.4 (5n/3 + 2 token bytes) plus the codec header */
_Static_assert(RLE_BOUND <= TS_ENC_MAX, "RLE worst case exceeds TS_ENC_MAX");
_Static_assert(TS_ENC_MAX <= (TS_MAX_FRAGS * TS_MTU_PAYLOAD), "TS_ENC_MAX does not fit in TS_MAX_FRAGS fragments");

static uint32_t encode_worst(const uint8_t *frame, bool key)
{
    ts_frame_info_t ei;
    if (key) {
        ts_enc_force_keyframe(&g_enc);
    }
    guard_fill();
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_enc_encode(&g_enc, frame, (uint16_t)(g_enc.frames & 0xFFFFU),
                                               g_buf, TS_ENC_MAX, &ei));
    guard_check();
    return ei.enc_len;
}

static void test_budget_enc_max(void)
{
    uint32_t worst = 0U;
    uint32_t s = 0x1234567U;
    static const uint8_t pat[4][3] = { {0U, 0U, 1U}, {0U, 1U, 1U}, {7U, 7U, 9U}, {0U, 255U, 0U} };
    for (uint8_t q = 1U; q <= TS_QUALITY_MAX; q = (uint8_t)(q * 2U)) {
        ts_enc_init(&g_enc);
        TEST_ASSERT_EQUAL_INT(TS_OK, ts_enc_set_quality(&g_enc, q));
        for (uint32_t f = 0U; f < 8U; f++) {
            uint32_t n;
            for (uint32_t i = 0U; i < TS_FRAME_BYTES; i++) {
                g_raw[i] = (uint8_t)(xorshift(&s) & 0xFFU);
            }
            n = encode_worst(g_raw, (f & 1U) == 0U);
            worst = (n > worst) ? n : worst;
        }
        for (uint32_t p = 0U; p < 4U; p++) {
            uint32_t n;
            for (uint32_t i = 0U; i < TS_FRAME_BYTES; i++) {
                g_raw[i] = pat[p][i % 3U];
            }
            n = encode_worst(g_raw, true);
            worst = (n > worst) ? n : worst;
        }
    }
    (void)printf("BUDGET enc_max_measured=%lu rle_bound=%lu TS_ENC_MAX=%u fragments=%lu (max %u)\n",
                 (unsigned long)worst, (unsigned long)RLE_BOUND, TS_ENC_MAX,
                 (unsigned long)((worst + TS_MTU_PAYLOAD - 1U) / TS_MTU_PAYLOAD), TS_MAX_FRAGS);
    TEST_ASSERT_TRUE(worst <= RLE_BOUND);
    TEST_ASSERT_TRUE(worst <= TS_ENC_MAX);
    /* a buffer sized to the raw frame is refused, never overflowed */
    {
        ts_frame_info_t ei;
        for (uint32_t i = 0U; i < TS_FRAME_BYTES; i++) {
            g_raw[i] = (uint8_t)(xorshift(&s) & 0xFFU);
        }
        ts_enc_init(&g_enc);
        TEST_ASSERT_EQUAL_INT(TS_E_NOMEM, ts_enc_encode(&g_enc, g_raw, 0U, g_buf, TS_FRAME_BYTES, &ei));
        TEST_ASSERT_FALSE(g_enc.have_ref);
    }
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_budget_ratio_q1);
    RUN_TEST(test_budget_psnr_q2_q4);
    RUN_TEST(test_budget_enc_max);
    return UNITY_END();
}
