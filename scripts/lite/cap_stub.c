/* SPDX-License-Identifier: MIT */
/*
 * tinystream -- scripts/lite/cap_stub.c
 * The capture HAL (include/ts_capture.h) with the synth source only, for the
 * Docker-free ladder on Windows (scripts/lite_gates.py): src/capture/capture.c
 * reads files and pipes through POSIX file descriptors, which mingw lacks.
 * Never part of the daemon build or the container ladder.
 *
 * The synth path (pacing and the frame index) is the one in capture.c and
 * must stay identical to it. File, pipe and v4l2 sources report TS_E_NOTIMPL,
 * so opening one fails the way a missing file does. Linux and macOS build the
 * real capture.c instead.
 */
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include "ts_capture.h"

#define CAP_MAX_FPS 1000U

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

ts_err_t ts_cap_open(ts_cap_t *c, const ts_cap_cfg_t *cfg)
{
    ts_err_t rc = TS_E_INVAL;
    if ((c != NULL) && (cfg != NULL) && (cfg->fps <= CAP_MAX_FPS) &&
        (memchr(cfg->path, 0, sizeof(cfg->path)) != NULL)) {
        (void)memset(c, 0, sizeof(*c));
        c->cfg = *cfg;
        c->fd = -1;
        rc = (cfg->kind == TS_CAP_SYNTH) ? TS_OK : TS_E_NOTIMPL;
        c->open = (rc == TS_OK);
    }
    return rc;
}

/* The synth frame index: idx_start + frame_idx, held back one frame during the quiet period. */
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
        } else if (c->cfg.kind == TS_CAP_SYNTH) {
            rc = TS_E_AGAIN;
            if (cap_due(c, now_ms)) {
                uint32_t scf = ts_cap_scene_cut_frames(c->cfg.scene_cut_ms, cap_fps(c));
                ts_cap_synth_frame(frame, cap_synth_index(c, scf), c->cfg.seed, scf);
                c->frame_idx++;
                rc = TS_OK;
            }
        } else {
            rc = TS_E_NOTIMPL;
        }
    }
    return rc;
}

void ts_cap_close(ts_cap_t *c)
{
    if (c != NULL) {
        c->open = false;
        c->fd = -1;
        c->partial = 0U;
        c->skip = 0U;
    }
}
