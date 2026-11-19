/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- src/capture/capture.c
 * Capture HAL (include/ts_capture.h): synth | file | pipe | v4l2 (cap_v4l2.c).
 *
 * No heap. The file source stages each frame in `c->stage` before copying it
 * out; the pipe source keeps only the Y plane of each YUV420 frame.
 */
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/types.h>
#include <sys/stat.h>
#include "ts_capture.h"
#include "cap_internal.h"

#define CAP_PIPE_UV_BYTES   (TS_CAP_YUV420_BYTES - TS_FRAME_BYTES)
#define CAP_MAX_FPS         1000U

_Static_assert(CAP_PIPE_UV_BYTES <= TS_FRAME_BYTES, "UV planes are discarded through the staging buffer");

/* one gray8 frame as a plain object, so file offsets are computed without casting a composite macro */
static const size_t cap_frame_bytes = TS_FRAME_BYTES;

ts_err_t ts_cap_kind_parse(const char *name, ts_cap_kind_t *kind)
{
    ts_err_t rc = TS_E_INVAL;
    if ((name != NULL) && (kind != NULL)) {
        if (strcmp(name, "synth") == 0) {
            *kind = TS_CAP_SYNTH;
            rc = TS_OK;
        } else if (strcmp(name, "file") == 0) {
            *kind = TS_CAP_FILE;
            rc = TS_OK;
        } else if (strcmp(name, "pipe") == 0) {
            *kind = TS_CAP_PIPE;
            rc = TS_OK;
        } else if (strcmp(name, "v4l2") == 0) {
            *kind = TS_CAP_V4L2;
            rc = TS_OK;
        } else {
            rc = TS_E_INVAL;
        }
    }
    return rc;
}

/* ---- pacing (synth / file) ------------------------------------------------ */

/* Serial comparison of two u32 millisecond stamps: true when `a` is at or after `b`. */
static bool cap_time_reached(uint32_t a, uint32_t b)
{
    return (uint32_t)(a - b) < 0x80000000U;
}

static uint32_t cap_fps(const ts_cap_t *c)
{
    return (c->cfg.fps != 0U) ? c->cfg.fps : TS_DEFAULT_FPS;
}

/* Length of frame interval k (ms) such that exactly fps frames fit in 1000 ms. */
static uint32_t cap_period_ms(uint32_t fps, uint32_t k)
{
    uint32_t i = k % fps;
    return (((i + 1U) * 1000U) / fps) - ((i * 1000U) / fps);
}

/* true when a paced frame is due at now_ms; advances the schedule. */
static bool cap_due(ts_cap_t *c, uint32_t now_ms)
{
    bool due = false;
    uint32_t fps = cap_fps(c);
    if (c->frame_idx == 0U) {
        due = true;
        c->next_ms = now_ms + cap_period_ms(fps, 0U);
    } else if (cap_time_reached(now_ms, c->next_ms)) {
        due = true;
        c->next_ms += cap_period_ms(fps, c->frame_idx);
        /* fell behind by more than one interval: drop the backlog instead of bursting */
        if (cap_time_reached(now_ms, c->next_ms)) {
            c->next_ms = now_ms + cap_period_ms(fps, c->frame_idx);
        }
    } else {
        due = false;
    }
    return due;
}

/* ---- file -------------------------------------------------------------- */

static ts_err_t cap_file_open(ts_cap_t *c)
{
    ts_err_t rc = TS_E_IO;
    int fd = open(c->cfg.path, O_RDONLY);
    if (fd >= 0) {
        struct stat st;
        if ((fstat(fd, &st) == 0) && (st.st_size >= (off_t)cap_frame_bytes)) {
            uint64_t frames = (uint64_t)st.st_size / (uint64_t)cap_frame_bytes;
            c->file_frames = (frames > 0xFFFFFFFFU) ? 0xFFFFFFFFU : (uint32_t)frames;
            c->file_pos = 0U;
            c->fd = (int32_t)fd;
            rc = TS_OK;
        } else {
            (void)close(fd);
        }
    }
    return rc;
}

/* Read the whole frame `file_pos` into the staging buffer (regular file: no EAGAIN). */
static ts_err_t cap_file_stage(ts_cap_t *c)
{
    ts_err_t rc = TS_OK;
    off_t off = (off_t)c->file_pos * (off_t)cap_frame_bytes;
    size_t got = 0U;
    if (lseek((int)c->fd, off, SEEK_SET) != off) {
        rc = TS_E_IO;
    }
    while ((rc == TS_OK) && (got < TS_FRAME_BYTES)) {
        ssize_t n = read((int)c->fd, &c->stage[got], TS_FRAME_BYTES - got);
        if (n > 0) {
            if ((size_t)n < (TS_FRAME_BYTES - got)) {
                c->short_reads++;
            }
            got += (size_t)n;
        } else if ((n < 0) && (errno == EINTR)) {
            /* retry */
        } else {
            rc = TS_E_IO;   /* EOF inside a frame: the clip shrank */
        }
    }
    return rc;
}

static ts_err_t cap_file_read(ts_cap_t *c, uint8_t *frame, uint32_t now_ms)
{
    ts_err_t rc = TS_E_AGAIN;
    if (cap_due(c, now_ms)) {
        rc = cap_file_stage(c);
        if (rc == TS_OK) {
            (void)memcpy(frame, c->stage, TS_FRAME_BYTES);
            c->file_pos = (c->file_pos + 1U) % c->file_frames;
            c->frame_idx++;
        }
    }
    return rc;
}

/* ---- pipe (rpicam-vid --codec yuv420) ---------------------------------- */

static ts_err_t cap_pipe_open(ts_cap_t *c)
{
    ts_err_t rc = TS_E_IO;
    int fd = -1;
    if (c->cfg.fd >= 0) {
        fd = (int)c->cfg.fd;                       /* injected (tests, systemd socket) */
    } else if ((strlen(c->cfg.path) == 0U) || (strcmp(c->cfg.path, "-") == 0)) {
        fd = STDIN_FILENO;                         /* rpicam-vid ... -o - | tinystreamd */
    } else {
        fd = open(c->cfg.path, O_RDONLY);          /* FIFO */
    }
    if (fd >= 0) {
        c->fd = (int32_t)fd;
        rc = TS_OK;
    }
    return rc;
}

/*
 * One YUV420 frame per call: the Y plane goes to `frame`, the U and V planes
 * are read into the staging buffer and dropped.
 */
static ts_err_t cap_pipe_read(ts_cap_t *c, uint8_t *frame)
{
    ts_err_t rc = TS_E_AGAIN;
    ssize_t n = read((int)c->fd, frame, TS_FRAME_BYTES);
    if (n > 0) {
        if ((size_t)n < TS_FRAME_BYTES) {
            c->short_reads++;
        }
        (void)read((int)c->fd, c->stage, CAP_PIPE_UV_BYTES);
        c->frame_idx++;
        rc = TS_OK;
    } else if (n == 0) {
        rc = TS_E_CLOSED;
    } else if ((errno == EAGAIN) || (errno == EWOULDBLOCK) || (errno == EINTR)) {
        c->stalls++;
        rc = TS_E_AGAIN;
    } else {
        rc = TS_E_IO;
    }
    return rc;
}

/* ---- public ------------------------------------------------------------ */

ts_err_t ts_cap_open(ts_cap_t *c, const ts_cap_cfg_t *cfg)
{
    ts_err_t rc = TS_E_INVAL;
    if ((c != NULL) && (cfg != NULL) && (cfg->fps <= CAP_MAX_FPS) &&
        (memchr(cfg->path, 0, sizeof(cfg->path)) != NULL)) {
        (void)memset(c, 0, sizeof(*c));
        c->cfg = *cfg;
        c->fd = -1;
        switch (cfg->kind) {
        case TS_CAP_SYNTH:
            rc = TS_OK;
            break;
        case TS_CAP_FILE:
            rc = cap_file_open(c);
            break;
        case TS_CAP_PIPE:
            rc = cap_pipe_open(c);
            break;
        case TS_CAP_V4L2:
            rc = ts_cap_v4l2_open(c);
            break;
        default:
            rc = TS_E_INVAL;
            break;
        }
        c->open = (rc == TS_OK);
        if (!c->open) {
            c->fd = -1;
        }
    }
    return rc;
}

/*
 * Pattern index of the next synth frame. During the quiet period (cfg.noise_after_ms
 * of capture time, counted in frames at the capture rate) the one-frame noise burst
 * after a scene cut is replaced by the frame before it, t - 1 of the same scene
 * (t == TS_CAP_SYNTH_NOISE_AFTER > 0, so idx - 1 stays inside the scene and the
 * 65536 period). Outside the quiet period the index is idx_start + frame_idx.
 */
static uint32_t cap_synth_index(const ts_cap_t *c, uint32_t scene_cut_frames)
{
    uint32_t idx = c->cfg.idx_start + c->frame_idx;
    if ((c->cfg.noise_after_ms != 0U) && (scene_cut_frames > TS_CAP_SYNTH_NOISE_AFTER)) {
        uint64_t elapsed_ms = ((uint64_t)c->frame_idx * 1000U) / (uint64_t)cap_fps(c);
        uint32_t t = (idx & 0xFFFFU) % scene_cut_frames;
        if ((elapsed_ms < (uint64_t)c->cfg.noise_after_ms) && (t == TS_CAP_SYNTH_NOISE_AFTER)) {
            idx--;
        }
    }
    return idx;
}

ts_err_t ts_cap_read(ts_cap_t *c, uint8_t *frame, uint32_t now_ms)
{
    ts_err_t rc = TS_E_INVAL;
    if ((c != NULL) && (frame != NULL)) {
        if (!c->open) {
            rc = TS_E_STATE;
        } else {
            switch (c->cfg.kind) {
            case TS_CAP_SYNTH:
                rc = TS_E_AGAIN;
                if (cap_due(c, now_ms)) {
                    uint32_t scf = ts_cap_scene_cut_frames(c->cfg.scene_cut_ms, cap_fps(c));
                    ts_cap_synth_frame(frame, cap_synth_index(c, scf), c->cfg.seed, scf);
                    c->frame_idx++;
                    rc = TS_OK;
                }
                break;
            case TS_CAP_FILE:
                rc = cap_file_read(c, frame, now_ms);
                break;
            case TS_CAP_PIPE:
                rc = cap_pipe_read(c, frame);
                break;
            case TS_CAP_V4L2:
                rc = ts_cap_v4l2_read(c, frame);
                break;
            default:
                rc = TS_E_INVAL;
                break;
            }
        }
    }
    return rc;
}

void ts_cap_close(ts_cap_t *c)
{
    if (c != NULL) {
        if (c->open) {
            if (c->cfg.kind == TS_CAP_V4L2) {
                ts_cap_v4l2_close(c);
            } else if ((c->fd >= 0) && (c->cfg.fd < 0) && (c->fd != (int32_t)STDIN_FILENO)) {
                (void)close((int)c->fd);   /* only descriptors we opened ourselves */
            } else {
                /* synth, or an injected / inherited fd owned by the caller */
            }
        }
        c->open = false;
        c->fd = -1;
        c->partial = 0U;
        c->skip = 0U;
    }
}
