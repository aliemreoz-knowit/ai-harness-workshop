/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/unit/test_capture.c
 * Capture HAL: synth determinism and regeneration, pacing with an injected
 * clock (incl. u32 wrap), file looping, open errors. No sockets, no sleeps.
 */
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "unity.h"
#include "ts_config.h"
#include "ts_capture.h"

#define SCF 300U   /* scene_cut_frames for 10 s at 30 fps */

static uint8_t fa[TS_FRAME_BYTES];
static uint8_t fb[TS_FRAME_BYTES];
static ts_cap_t cap;

void setUp(void)
{
    (void)memset(&cap, 0, sizeof(cap));
    cap.fd = -1;
}

void tearDown(void)
{
    ts_cap_close(&cap);
}

/* Tiles where any pixel differs by more than TS_TILE_THRESH (PROTOCOL.md 6.6), computed locally. */
static uint32_t changed_tiles(const uint8_t *a, const uint8_t *b)
{
    uint32_t n = 0U;
    for (uint32_t ty = 0U; ty < TS_TILES_Y; ty++) {
        for (uint32_t tx = 0U; tx < TS_TILES_X; tx++) {
            bool ch = false;
            for (uint32_t y = ty * TS_TILE; (y < ((ty + 1U) * TS_TILE)) && !ch; y++) {
                for (uint32_t x = tx * TS_TILE; (x < ((tx + 1U) * TS_TILE)) && !ch; x++) {
                    uint32_t i = (y * TS_WIDTH) + x;
                    uint32_t d = (a[i] > b[i]) ? (uint32_t)(a[i] - b[i]) : (uint32_t)(b[i] - a[i]);
                    ch = (d > TS_TILE_THRESH);
                }
            }
            n += ch ? 1U : 0U;
        }
    }
    return n;
}

static void cfg_init(ts_cap_cfg_t *c, ts_cap_kind_t kind)
{
    (void)memset(c, 0, sizeof(*c));
    c->kind = kind;
    c->fd = -1;
    c->fps = 30U;
}

/* ---- synth generator -------------------------------------------------------- */

static void test_kind_parse(void)
{
    ts_cap_kind_t k = TS_CAP_V4L2;
    TEST_ASSERT_EQUAL(TS_OK, ts_cap_kind_parse("synth", &k));
    TEST_ASSERT_EQUAL(TS_CAP_SYNTH, k);
    TEST_ASSERT_EQUAL(TS_OK, ts_cap_kind_parse("pipe", &k));
    TEST_ASSERT_EQUAL(TS_CAP_PIPE, k);
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_cap_kind_parse("camera", &k));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_cap_kind_parse(NULL, &k));
}

static void test_synth_deterministic(void)
{
    ts_cap_synth_frame(fa, 1234U, 7U, SCF);
    ts_cap_synth_frame(fb, 1234U, 7U, SCF);
    TEST_ASSERT_EQUAL_MEMORY(fa, fb, TS_FRAME_BYTES);
    /* period 65536: frame_seq alone regenerates the frame */
    ts_cap_synth_frame(fb, 1234U + 65536U, 7U, SCF);
    TEST_ASSERT_EQUAL_MEMORY(fa, fb, TS_FRAME_BYTES);
    /* another seed or index gives another picture */
    ts_cap_synth_frame(fb, 1234U, 8U, SCF);
    TEST_ASSERT_TRUE(memcmp(fa, fb, TS_FRAME_BYTES) != 0);
    ts_cap_synth_frame(fb, 1235U, 7U, SCF);
    TEST_ASSERT_TRUE(memcmp(fa, fb, TS_FRAME_BYTES) != 0);
}

static void test_synth_motion_is_local(void)
{
    /* consecutive frames inside a scene: something moves, but far below the scene-cut threshold */
    for (uint32_t i = 20U; i < 40U; i++) {
        ts_cap_synth_frame(fa, i, 0U, SCF);
        ts_cap_synth_frame(fb, i + 1U, 0U, SCF);
        TEST_ASSERT_TRUE(memcmp(fa, fb, TS_FRAME_BYTES) != 0);
        TEST_ASSERT_TRUE(changed_tiles(fa, fb) < ((TS_TILES * TS_SCENE_CHANGE_PCT) / 100U));
    }
}

static void test_synth_scene_cut_every_10s(void)
{
    TEST_ASSERT_EQUAL_UINT32(SCF, ts_cap_scene_cut_frames(0U, 30U));
    TEST_ASSERT_EQUAL_UINT32(SCF, ts_cap_scene_cut_frames(10000U, 0U));
    TEST_ASSERT_EQUAL_UINT32(1U, ts_cap_scene_cut_frames(1U, 1U));
    for (uint32_t seed = 0U; seed < 3U; seed++) {
        for (uint32_t s = 1U; s < 9U; s++) {
            ts_cap_synth_frame(fa, (s * SCF) - 1U, seed, SCF);
            ts_cap_synth_frame(fb, s * SCF, seed, SCF);
            TEST_ASSERT_TRUE_MESSAGE(changed_tiles(fa, fb) > ((TS_TILES * TS_SCENE_CHANGE_PCT) / 100U),
                                     "a scene cut changes more than 60 % of the tiles");
        }
    }
}

static void test_synth_noise_burst(void)
{
    uint32_t t = (2U * SCF) + TS_CAP_SYNTH_NOISE_AFTER;
    uint8_t lo = 255U;
    uint8_t hi = 0U;
    ts_cap_synth_frame(fa, t - 1U, 0U, SCF);
    ts_cap_synth_frame(fb, t, 0U, SCF);
    TEST_ASSERT_TRUE(changed_tiles(fa, fb) > ((TS_TILES * TS_SCENE_CHANGE_PCT) / 100U));
    for (size_t i = 0U; i < TS_FRAME_BYTES; i++) {
        lo = (fb[i] < lo) ? fb[i] : lo;
        hi = (fb[i] > hi) ? fb[i] : hi;
    }
    TEST_ASSERT_TRUE((lo < 8U) && (hi > 247U));
    /* one frame only */
    ts_cap_synth_frame(fa, t + 1U, 0U, SCF);
    TEST_ASSERT_TRUE(changed_tiles(fa, fb) > ((TS_TILES * TS_SCENE_CHANGE_PCT) / 100U));
}

/* ---- synth source: pacing with the injected clock ------------------------------ */

static void test_synth_source_paced_30fps(void)
{
    ts_cap_cfg_t c;
    uint32_t got = 0U;
    cfg_init(&c, TS_CAP_SYNTH);
    c.seed = 3U;
    TEST_ASSERT_EQUAL(TS_OK, ts_cap_open(&cap, &c));
    for (uint32_t now = 5000U; now < 6000U; now++) {
        ts_err_t r = ts_cap_read(&cap, fa, now);
        if (r == TS_OK) {
            ts_cap_synth_frame(fb, got, 3U, SCF);
            TEST_ASSERT_EQUAL_MEMORY(fb, fa, TS_FRAME_BYTES);
            got++;
        } else {
            TEST_ASSERT_EQUAL(TS_E_AGAIN, r);
        }
    }
    TEST_ASSERT_EQUAL_UINT32(30U, got);
    TEST_ASSERT_EQUAL_UINT32(30U, cap.frame_idx);
}

static void test_synth_source_idx_start_and_clock_wrap(void)
{
    ts_cap_cfg_t c;
    uint32_t got = 0U;
    uint32_t now = 0xFFFFFE00U;   /* the u32 ms clock wraps half way */
    cfg_init(&c, TS_CAP_SYNTH);
    c.idx_start = 65530U;         /* tsd sets this to seq_start */
    TEST_ASSERT_EQUAL(TS_OK, ts_cap_open(&cap, &c));
    for (uint32_t i = 0U; i < 1000U; i++) {
        if (ts_cap_read(&cap, fa, now) == TS_OK) {
            ts_cap_synth_frame(fb, (uint32_t)(uint16_t)(65530U + got), 0U, SCF);   /* = frame_seq */
            TEST_ASSERT_EQUAL_MEMORY(fb, fa, TS_FRAME_BYTES);
            got++;
        }
        now++;
    }
    TEST_ASSERT_EQUAL_UINT32(30U, got);
}

static void test_synth_source_quiet_period(void)
{
    /* noise_after_ms 20 s: the bursts at 0.5 s and 10.5 s repeat the frame before them,
       the one at 20.5 s is the real noise frame; everything else is the plain pattern */
    ts_cap_cfg_t c;
    uint32_t got = 0U;
    uint32_t held = 0U;
    cfg_init(&c, TS_CAP_SYNTH);
    c.noise_after_ms = 20000U;
    TEST_ASSERT_EQUAL(TS_OK, ts_cap_open(&cap, &c));
    for (uint32_t now = 1000U; now < 22000U; now++) {
        if (ts_cap_read(&cap, fa, now) == TS_OK) {
            uint32_t want = got;
            if (((got % SCF) == TS_CAP_SYNTH_NOISE_AFTER) && (((got * 1000U) / 30U) < 20000U)) {
                want = got - 1U;
                held++;
            }
            ts_cap_synth_frame(fb, want, 0U, SCF);
            TEST_ASSERT_EQUAL_MEMORY(fb, fa, TS_FRAME_BYTES);
            got++;
        }
    }
    TEST_ASSERT_EQUAL_UINT32(630U, got);
    TEST_ASSERT_EQUAL_UINT32(2U, held);
    /* frame 615 (20.5 s) was delivered as the noise frame itself */
    TEST_ASSERT_TRUE(got > ((2U * SCF) + TS_CAP_SYNTH_NOISE_AFTER));
}

static void test_synth_source_drops_backlog(void)
{
    ts_cap_cfg_t c;
    cfg_init(&c, TS_CAP_SYNTH);
    TEST_ASSERT_EQUAL(TS_OK, ts_cap_open(&cap, &c));
    TEST_ASSERT_EQUAL(TS_OK, ts_cap_read(&cap, fa, 100U));
    /* the caller stalled for 1 s: one frame now, not a burst of 30 */
    TEST_ASSERT_EQUAL(TS_OK, ts_cap_read(&cap, fa, 1100U));
    TEST_ASSERT_EQUAL(TS_E_AGAIN, ts_cap_read(&cap, fa, 1100U));
    TEST_ASSERT_EQUAL(TS_E_AGAIN, ts_cap_read(&cap, fa, 1110U));
}

static void test_open_invalid(void)
{
    ts_cap_cfg_t c;
    cfg_init(&c, TS_CAP_SYNTH);
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_cap_open(NULL, &c));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_cap_open(&cap, NULL));
    c.fps = 100000U;
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_cap_open(&cap, &c));
    cfg_init(&c, TS_CAP_FILE);
    (void)strcpy(c.path, "/nonexistent/clip.y8");
    TEST_ASSERT_EQUAL(TS_E_IO, ts_cap_open(&cap, &c));
    TEST_ASSERT_FALSE(cap.open);
    TEST_ASSERT_EQUAL(TS_E_STATE, ts_cap_read(&cap, fa, 0U));
    cfg_init(&c, TS_CAP_V4L2);
    (void)strcpy(c.path, "/nonexistent/video9");
#ifdef __linux__
    TEST_ASSERT_EQUAL(TS_E_IO, ts_cap_open(&cap, &c));
#else
    TEST_ASSERT_EQUAL(TS_E_NOTIMPL, ts_cap_open(&cap, &c));
#endif
}

/* ---- file source -------------------------------------------------------------- */

static void test_file_loops_clip(void)
{
    char path[] = "/tmp/ts_capture_clipXXXXXX";
    int fd = mkstemp(path);
    ts_cap_cfg_t c;
    TEST_ASSERT_TRUE(fd >= 0);
    for (uint32_t f = 0U; f < 2U; f++) {
        (void)memset(fa, (int)(0x40U + f), TS_FRAME_BYTES);
        TEST_ASSERT_EQUAL_INT((int)TS_FRAME_BYTES, (int)write(fd, fa, TS_FRAME_BYTES));
    }
    (void)close(fd);
    cfg_init(&c, TS_CAP_FILE);
    (void)strcpy(c.path, path);
    TEST_ASSERT_EQUAL(TS_OK, ts_cap_open(&cap, &c));
    TEST_ASSERT_EQUAL_UINT32(2U, cap.file_frames);
    for (uint32_t k = 0U; k < 5U; k++) {
        TEST_ASSERT_EQUAL(TS_OK, ts_cap_read(&cap, fb, 1000U + (k * 40U)));
        TEST_ASSERT_EQUAL_HEX8((uint8_t)(0x40U + (k & 1U)), fb[0]);
        TEST_ASSERT_EQUAL_HEX8((uint8_t)(0x40U + (k & 1U)), fb[TS_FRAME_BYTES - 1U]);
        TEST_ASSERT_EQUAL(TS_E_AGAIN, ts_cap_read(&cap, fb, 1000U + (k * 40U)));
    }
    ts_cap_close(&cap);
    (void)unlink(path);
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_kind_parse);
    RUN_TEST(test_synth_deterministic);
    RUN_TEST(test_synth_motion_is_local);
    RUN_TEST(test_synth_scene_cut_every_10s);
    RUN_TEST(test_synth_noise_burst);
    RUN_TEST(test_synth_source_paced_30fps);
    RUN_TEST(test_synth_source_idx_start_and_clock_wrap);
    RUN_TEST(test_synth_source_quiet_period);
    RUN_TEST(test_synth_source_drops_backlog);
    RUN_TEST(test_open_invalid);
    RUN_TEST(test_file_loops_clip);
    return UNITY_END();
}
