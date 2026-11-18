/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- src/capture/cap_v4l2.c
 * V4L2 source for UVC webcams (Linux only): GREY or YUYV, mmap streaming with
 * TS_CAP_V4L2_BUFS kernel buffers mapped once at open (no heap), non-blocking
 * VIDIOC_DQBUF. Any capture size >= 320x240 is accepted and nearest-neighbour
 * sampled down to TS_WIDTH x TS_HEIGHT; only the luma is kept.
 * Off Linux every entry point reports TS_E_NOTIMPL.
 */
#include <stddef.h>
#include <stdint.h>
#include "ts_capture.h"
#include "cap_internal.h"

#ifdef __linux__

#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/time.h>
#include <linux/videodev2.h>

#define TS_CAP_V4L2_BUFS 4U

/* One camera per daemon: the mapping table is static storage (no heap). */
typedef struct cap_v4l2 {
    uint8_t *start[TS_CAP_V4L2_BUFS];
    size_t   length[TS_CAP_V4L2_BUFS];
    uint32_t nbufs;
    uint32_t width;
    uint32_t height;
    uint32_t bytesperline;
    uint32_t fourcc;
    bool     streaming;
} cap_v4l2_t;

static cap_v4l2_t g_v4l2;

static int v4l2_ioctl(int fd, unsigned long req, void *arg)
{
    int r;
    do {
        r = ioctl(fd, req, arg);
    } while ((r < 0) && (errno == EINTR));
    return r;
}

static bool v4l2_try_format(int fd, uint32_t fourcc)
{
    bool ok = false;
    struct v4l2_format fmt;
    (void)memset(&fmt, 0, sizeof(fmt));
    fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    fmt.fmt.pix.width = TS_WIDTH;
    fmt.fmt.pix.height = TS_HEIGHT;
    fmt.fmt.pix.pixelformat = fourcc;
    fmt.fmt.pix.field = V4L2_FIELD_NONE;
    if ((v4l2_ioctl(fd, VIDIOC_S_FMT, &fmt) == 0) && (fmt.fmt.pix.pixelformat == fourcc) &&
        (fmt.fmt.pix.width >= TS_WIDTH) && (fmt.fmt.pix.height >= TS_HEIGHT)) {
        uint32_t bpp = (fourcc == V4L2_PIX_FMT_YUYV) ? 2U : 1U;
        g_v4l2.width = fmt.fmt.pix.width;
        g_v4l2.height = fmt.fmt.pix.height;
        g_v4l2.bytesperline = fmt.fmt.pix.bytesperline;
        if (g_v4l2.bytesperline < (g_v4l2.width * bpp)) {
            g_v4l2.bytesperline = g_v4l2.width * bpp;
        }
        g_v4l2.fourcc = fourcc;
        ok = true;
    }
    return ok;
}

static void v4l2_unmap(void)
{
    for (uint32_t i = 0U; i < g_v4l2.nbufs; i++) {
        if (g_v4l2.start[i] != NULL) {
            (void)munmap(g_v4l2.start[i], g_v4l2.length[i]);
            g_v4l2.start[i] = NULL;
        }
    }
    g_v4l2.nbufs = 0U;
}

static bool v4l2_map_and_queue(int fd)
{
    bool ok = true;
    struct v4l2_requestbuffers req;
    (void)memset(&req, 0, sizeof(req));
    req.count = TS_CAP_V4L2_BUFS;
    req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    req.memory = V4L2_MEMORY_MMAP;
    if ((v4l2_ioctl(fd, VIDIOC_REQBUFS, &req) != 0) || (req.count < 2U)) {
        ok = false;
    } else {
        g_v4l2.nbufs = (req.count < TS_CAP_V4L2_BUFS) ? req.count : TS_CAP_V4L2_BUFS;
    }
    for (uint32_t i = 0U; ok && (i < g_v4l2.nbufs); i++) {
        struct v4l2_buffer b;
        (void)memset(&b, 0, sizeof(b));
        b.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        b.memory = V4L2_MEMORY_MMAP;
        b.index = i;
        if (v4l2_ioctl(fd, VIDIOC_QUERYBUF, &b) != 0) {
            ok = false;
        } else {
            void *p = mmap(NULL, (size_t)b.length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, (off_t)b.m.offset);
            if (p == MAP_FAILED) {
                ok = false;
            } else {
                /* cppcheck-suppress misra-c2012-11.5 ; mmap() returns void *, the buffer is bytes */
                g_v4l2.start[i] = (uint8_t *)p;
                g_v4l2.length[i] = (size_t)b.length;
                ok = (v4l2_ioctl(fd, VIDIOC_QBUF, &b) == 0);
            }
        }
    }
    if (ok) {
        enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        ok = (v4l2_ioctl(fd, VIDIOC_STREAMON, &type) == 0);
        g_v4l2.streaming = ok;
    }
    return ok;
}

ts_err_t ts_cap_v4l2_open(ts_cap_t *c)
{
    ts_err_t rc = TS_E_IO;
    int fd = open(c->cfg.path, O_RDWR | O_NONBLOCK);
    (void)memset(&g_v4l2, 0, sizeof(g_v4l2));
    if (fd >= 0) {
        struct v4l2_capability cap;
        bool ok;
        (void)memset(&cap, 0, sizeof(cap));
        ok = (v4l2_ioctl(fd, VIDIOC_QUERYCAP, &cap) == 0) &&
             ((cap.capabilities & V4L2_CAP_VIDEO_CAPTURE) != 0U) &&
             ((cap.capabilities & V4L2_CAP_STREAMING) != 0U);
        ok = ok && (v4l2_try_format(fd, V4L2_PIX_FMT_GREY) || v4l2_try_format(fd, V4L2_PIX_FMT_YUYV));
        ok = ok && v4l2_map_and_queue(fd);
        if (ok) {
            c->fd = (int32_t)fd;
            rc = TS_OK;
        } else {
            v4l2_unmap();
            (void)close(fd);
        }
    }
    return rc;
}

/* Nearest-neighbour luma extraction from a complete driver buffer. */
static void v4l2_convert(const uint8_t *src, size_t len, uint8_t *dst)
{
    uint32_t step = (g_v4l2.fourcc == V4L2_PIX_FMT_YUYV) ? 2U : 1U;
    for (uint32_t y = 0U; y < TS_HEIGHT; y++) {
        uint32_t sy = (y * g_v4l2.height) / TS_HEIGHT;
        size_t row = (size_t)sy * (size_t)g_v4l2.bytesperline;
        for (uint32_t x = 0U; x < TS_WIDTH; x++) {
            uint32_t sx = (x * g_v4l2.width) / TS_WIDTH;
            size_t at = row + ((size_t)sx * (size_t)step);
            dst[((size_t)y * (size_t)TS_WIDTH) + (size_t)x] = (at < len) ? src[at] : 0U;
        }
    }
}

ts_err_t ts_cap_v4l2_read(ts_cap_t *c, uint8_t *frame)
{
    ts_err_t rc = TS_E_IO;
    struct v4l2_buffer b;
    (void)memset(&b, 0, sizeof(b));
    b.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    b.memory = V4L2_MEMORY_MMAP;
    if (v4l2_ioctl((int)c->fd, VIDIOC_DQBUF, &b) == 0) {
        if ((b.index < g_v4l2.nbufs) && ((b.flags & V4L2_BUF_FLAG_ERROR) == 0U)) {
            size_t used = ((size_t)b.bytesused <= g_v4l2.length[b.index]) ? (size_t)b.bytesused
                                                                           : g_v4l2.length[b.index];
            /* the driver buffer is complete: convert into staging, then publish */
            v4l2_convert(g_v4l2.start[b.index], used, c->stage);
            (void)memcpy(frame, c->stage, TS_FRAME_BYTES);
            c->frame_idx++;
            rc = TS_OK;
        } else {
            c->short_reads++;
            rc = TS_E_AGAIN;
        }
        if (v4l2_ioctl((int)c->fd, VIDIOC_QBUF, &b) != 0) {
            rc = TS_E_IO;
        }
    } else if (errno == EAGAIN) {
        c->stalls++;
        rc = TS_E_AGAIN;
    } else {
        rc = TS_E_IO;
    }
    return rc;
}

void ts_cap_v4l2_close(ts_cap_t *c)
{
    if (c->fd >= 0) {
        if (g_v4l2.streaming) {
            enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            (void)v4l2_ioctl((int)c->fd, VIDIOC_STREAMOFF, &type);
            g_v4l2.streaming = false;
        }
        v4l2_unmap();
        (void)close((int)c->fd);
        c->fd = -1;
    }
}

#else /* !__linux__ */

ts_err_t ts_cap_v4l2_open(ts_cap_t *c)
{
    (void)c;
    return TS_E_NOTIMPL;
}

ts_err_t ts_cap_v4l2_read(ts_cap_t *c, uint8_t *frame)
{
    (void)c;
    (void)frame;
    return TS_E_NOTIMPL;
}

void ts_cap_v4l2_close(ts_cap_t *c)
{
    (void)c;
}

#endif /* __linux__ */
