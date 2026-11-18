/*
 * tinystream v2 -- hidden evaluation test: tests/unit/test_capture_pipe.c
 * Capture HAL, pipe source (rpicam-vid YUV420) fed through a real pipe(2) fd:
 * short reads across calls, EAGAIN stalls, U/V skipping, "frame written only
 * when complete" and EOF. No sockets, no sleeps.
 */
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include "unity.h"
#include "ts_config.h"
#include "ts_capture.h"

static uint8_t fa[TS_FRAME_BYTES];
static uint8_t yuv[TS_CAP_YUV420_BYTES];
static ts_cap_t cap;
static int pfd[2] = { -1, -1 };

void setUp(void)
{
    (void)memset(&cap, 0, sizeof(cap));
    cap.fd = -1;
    pfd[0] = -1;
    pfd[1] = -1;
}

void tearDown(void)
{
    ts_cap_close(&cap);
    if (pfd[0] >= 0) {
        (void)close(pfd[0]);
    }
    if (pfd[1] >= 0) {
        (void)close(pfd[1]);
    }
}

static void cfg_init(ts_cap_cfg_t *c, ts_cap_kind_t kind)
{
    (void)memset(c, 0, sizeof(*c));
    c->kind = kind;
    c->fd = -1;
    c->fps = 30U;
}

/* ---- pipe source (rpicam-vid YUV420 on a non-blocking fd) ------------------------ */

static void make_yuv(uint8_t seed)
{
    for (size_t i = 0U; i < TS_FRAME_BYTES; i++) {
        yuv[i] = (uint8_t)((i * 7U) + seed);
    }
    (void)memset(&yuv[TS_FRAME_BYTES], 0x80, TS_CAP_YUV420_BYTES - TS_FRAME_BYTES);   /* chroma */
}

static void feed(const uint8_t *p, size_t n)
{
    /* the test stays below the pipe capacity, so the blocking write end never blocks */
    TEST_ASSERT_TRUE(n <= 32768U);
    TEST_ASSERT_EQUAL_INT((int)n, (int)write(pfd[1], p, n));
}

static void open_pipe_source(void)
{
    ts_cap_cfg_t c;
    TEST_ASSERT_EQUAL_INT(0, pipe(pfd));
    cfg_init(&c, TS_CAP_PIPE);
    c.fd = pfd[0];   /* injected fake fd */
    TEST_ASSERT_EQUAL(TS_OK, ts_cap_open(&cap, &c));
    TEST_ASSERT_TRUE((fcntl(pfd[0], F_GETFL) & O_NONBLOCK) != 0);
}

static void test_pipe_eagain_is_a_stall(void)
{
    open_pipe_source();
    (void)memset(fa, 0xAA, TS_FRAME_BYTES);
    TEST_ASSERT_EQUAL(TS_E_AGAIN, ts_cap_read(&cap, fa, 0U));
    TEST_ASSERT_EQUAL(TS_E_AGAIN, ts_cap_read(&cap, fa, 1U));
    TEST_ASSERT_EQUAL_UINT32(2U, cap.stalls);
    TEST_ASSERT_EQUAL_HEX8(0xAAU, fa[0]);
}

static void test_pipe_short_reads_across_ticks(void)
{
    size_t off = 0U;
    uint32_t calls = 0U;
    open_pipe_source();
    make_yuv(3U);
    (void)memset(fa, 0xAA, TS_FRAME_BYTES);
    /* Y plane in odd-sized chunks, one read call per chunk: never a frame before the last byte */
    while (off < TS_FRAME_BYTES) {
        size_t n = ((TS_FRAME_BYTES - off) < 9973U) ? (TS_FRAME_BYTES - off) : 9973U;
        feed(&yuv[off], n);
        off += n;
        calls++;
        if (off < TS_FRAME_BYTES) {
            TEST_ASSERT_EQUAL(TS_E_AGAIN, ts_cap_read(&cap, fa, calls));
            TEST_ASSERT_EQUAL_HEX8_MESSAGE(0xAAU, fa[0], "frame written before it was complete");
            TEST_ASSERT_EQUAL_HEX8(0xAAU, fa[TS_FRAME_BYTES - 1U]);
            TEST_ASSERT_EQUAL_size_t(off, cap.partial);
        } else {
            TEST_ASSERT_EQUAL(TS_OK, ts_cap_read(&cap, fa, calls));
        }
    }
    TEST_ASSERT_EQUAL_MEMORY(yuv, fa, TS_FRAME_BYTES);
    TEST_ASSERT_EQUAL_UINT32(1U, cap.frame_idx);
    TEST_ASSERT_TRUE(cap.short_reads >= 7U);
    TEST_ASSERT_EQUAL_size_t(TS_CAP_YUV420_BYTES - TS_FRAME_BYTES, cap.skip);
}

static void test_pipe_skips_uv_and_resyncs(void)
{
    open_pipe_source();
    for (uint8_t f = 0U; f < 3U; f++) {
        make_yuv((uint8_t)(10U + f));
        (void)memset(fa, 0xAA, TS_FRAME_BYTES);
        /* the frame arrives in four writes; the chroma of the previous frame is still in front */
        for (size_t off = 0U; off < TS_CAP_YUV420_BYTES; off += 28800U) {
            ts_err_t r;
            feed(&yuv[off], 28800U);
            r = ts_cap_read(&cap, fa, 0U);
            if ((off + 28800U) < 76800U) {
                TEST_ASSERT_EQUAL(TS_E_AGAIN, r);
            } else if ((off + 28800U) < (76800U + 28800U)) {
                TEST_ASSERT_EQUAL(TS_OK, r);   /* Y complete: frame published, U/V not needed */
                TEST_ASSERT_EQUAL_MEMORY(yuv, fa, TS_FRAME_BYTES);
            } else {
                TEST_ASSERT_EQUAL(TS_E_AGAIN, r);   /* only chroma left: discarded */
            }
        }
        TEST_ASSERT_EQUAL_size_t(0U, cap.skip);
        TEST_ASSERT_EQUAL_size_t(0U, cap.partial);
    }
    TEST_ASSERT_EQUAL_UINT32(3U, cap.frame_idx);
    TEST_ASSERT_TRUE(cap.stalls == 0U);
}

static void test_pipe_eof_is_closed(void)
{
    open_pipe_source();
    make_yuv(1U);
    feed(yuv, 1000U);
    (void)close(pfd[1]);
    pfd[1] = -1;
    (void)memset(fa, 0xAA, TS_FRAME_BYTES);
    TEST_ASSERT_EQUAL(TS_E_CLOSED, ts_cap_read(&cap, fa, 0U));
    TEST_ASSERT_EQUAL_HEX8(0xAAU, fa[0]);   /* the torn frame is never handed out */
    ts_cap_close(&cap);
    /* injected descriptors belong to the caller: still open */
    TEST_ASSERT_TRUE(fcntl(pfd[0], F_GETFL) >= 0);
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_pipe_eagain_is_a_stall);
    RUN_TEST(test_pipe_short_reads_across_ticks);
    RUN_TEST(test_pipe_skips_uv_and_resyncs);
    RUN_TEST(test_pipe_eof_is_closed);
    return UNITY_END();
}
