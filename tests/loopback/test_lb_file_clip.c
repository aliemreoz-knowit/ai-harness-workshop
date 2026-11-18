/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/loopback/test_lb_file_clip.c
 * loopback file source: the daemon streams a recorded gray8 clip (capture kind
 * TS_CAP_FILE, the HIL `file:` source) and the receiver reconstructs every
 * frame exactly at quality 1. The clip is the stand-in of
 * tests/data/poster_30f.y8 (scripts/make_clip.py, same generator), written to
 * a temporary file so the test does not depend on the recording.
 */
#include <stdlib.h>
#include <unistd.h>
#include "lb_common.h"

#define CLIP_FRAMES 30U

static uint8_t g_clip[CLIP_FRAMES][TS_FRAME_BYTES];
static char    g_path[] = "/tmp/ts_lb_clipXXXXXX";

void setUp(void)
{
}

void tearDown(void)
{
}

/* scripts/make_clip.py poster_pixel() */
static uint8_t poster_pixel(uint32_t x, uint32_t y)
{
    uint32_t v = (x + (2U * y)) & 0xFFU;
    if ((y >= 24U) && (y < 72U) && (x >= 16U) && (x < 304U)) {
        uint32_t cx = (x - 16U) / 6U;
        uint32_t cy = (y - 24U) / 8U;
        uint32_t stroke = ((cx * 7U) + (cy * 13U) + (((x - 16U) % 6U) * 3U) + (((y - 24U) % 8U) * 5U)) % 7U;
        v = (stroke < 3U) ? 230U : 30U;
    } else if ((y >= 200U) && (y < 232U)) {
        v = (((y / 2U) % 2U) == 0U) ? 200U : 60U;
    } else {
        /* gradient */
    }
    return (uint8_t)v;
}

/* scripts/make_clip.py frame(k) */
static void clip_frame(uint32_t k, uint8_t *out)
{
    uint32_t ox = (40U + (3U * k)) % (TS_WIDTH - 48U);
    uint32_t oy = 100U;
    for (uint32_t y = 0U; y < TS_HEIGHT; y++) {
        for (uint32_t x = 0U; x < TS_WIDTH; x++) {
            out[(y * TS_WIDTH) + x] = poster_pixel(x, y);
        }
    }
    for (uint32_t y = oy; y < (oy + 48U); y++) {
        for (uint32_t x = ox; x < (ox + 48U); x++) {
            out[(y * TS_WIDTH) + x] = ((((x - ox) / 8U) + ((y - oy) / 8U)) % 2U == 0U) ? 255U : 0U;
        }
    }
}

static void write_clip(void)
{
    int fd = mkstemp(g_path);
    TEST_ASSERT_TRUE(fd >= 0);
    for (uint32_t k = 0U; k < CLIP_FRAMES; k++) {
        clip_frame(k, g_clip[k]);
        TEST_ASSERT_EQUAL_INT((int)TS_FRAME_BYTES, (int)write(fd, g_clip[k], TS_FRAME_BYTES));
    }
    (void)close(fd);
}

static void test_file_clip_reconstructs_exactly(void)
{
    ts_loop_cfg_t c;
    ts_lb_window_t w;
    const uint8_t *last;
    bool matches = false;

    write_clip();
    lb_cfg(&c);
    c.daemon.capture.kind = TS_CAP_FILE;
    (void)strcpy(c.daemon.capture.path, g_path);
    c.daemon.loss_adapt = false;          /* quality 1: lossless, exact reconstruction */
    lb_start(&c);
    TEST_ASSERT_EQUAL_UINT32(CLIP_FRAMES, g_L.d->cap.file_frames);

    ts_lb_run(&g_lb, 1000U);
    ts_lb_window(&g_lb, 3000U, &w);       /* the clip loops three times */
    lb_print_window("file", &w);
    lb_print_rx("file", 0U);

    lb_assert_invariants(1U);
    TEST_ASSERT_TRUE_MESSAGE(w.min_fps_1s[0] >= 29U, "file source below 29 fps on a clean link");
    TEST_ASSERT_TRUE(g_lb.rx[0].compared >= 100U);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(g_lb.rx[0].compared, g_lb.rx[0].exact_raw,
                                     "a decoded frame differs from the clip frame the daemon read");
    /* and what the daemon read is the clip: the last decoded frame is one of its frames */
    last = tsr_frame(g_L.rx[0]);
    TEST_ASSERT_NOT_NULL(last);
    for (uint32_t k = 0U; (k < CLIP_FRAMES) && !matches; k++) {
        matches = (memcmp(last, g_clip[k], TS_FRAME_BYTES) == 0);
    }
    TEST_ASSERT_TRUE_MESSAGE(matches, "decoded frame is not a frame of the clip");
    lb_stop();
    (void)unlink(g_path);
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_file_clip_reconstructs_exactly);
    return UNITY_END();
}
