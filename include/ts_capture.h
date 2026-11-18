/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- ts_capture.h
 * Capture HAL: produces TS_WIDTH x TS_HEIGHT gray8 frames from one of
 *   synth : deterministic patterns (scene cut every 10 s, noise burst), no I/O;
 *   file  : loops a raw .y8 clip (tests/data/poster_30f.y8: recorded on the Pi,
 *           not in git; scripts/make_clip.py writes a stand-in, see README);
 *   pipe  : rpicam-vid YUV420 from a pipe fd, Y plane only, UV skipped;
 *   v4l2  : UVC webcams (GREY/YUYV -> gray8), Linux only.
 *
 * FROZEN API: additive, non-breaking changes only.
 * Rules: no dynamic memory, never blocks the caller.
 */
#ifndef TS_CAPTURE_H
#define TS_CAPTURE_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "ts_config.h"
#include "ts_proto.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum ts_cap_kind {
    TS_CAP_SYNTH = 0,
    TS_CAP_FILE  = 1,
    TS_CAP_PIPE  = 2,
    TS_CAP_V4L2  = 3
} ts_cap_kind_t;

#define TS_CAP_PATH_MAX     256U
#define TS_CAP_YUV420_BYTES (TS_FRAME_BYTES + TS_FRAME_BYTES / 2U)  /**< one pipe frame */

typedef struct ts_cap_cfg {
    ts_cap_kind_t kind;
    char          path[TS_CAP_PATH_MAX];  /**< file / device path (file, pipe, v4l2) */
    int32_t       fd;                     /**< pipe: already-open fd (-1 = open `path`); tests inject a fake fd */
    uint32_t      fps;                    /**< pacing for synth/file (0 = TS_DEFAULT_FPS) */
    uint32_t      seed;                   /**< synth pattern seed */
    uint32_t      scene_cut_ms;           /**< synth: scene cut period (0 = 10000) */
    uint32_t      idx_start;              /**< synth: pattern index of the first frame (added v2.0.1;
                                               tsd_create sets it to seq_start so (uint16_t)idx == frame_seq) */
    uint32_t      noise_after_ms;         /**< synth (added v2.3.0): no noise burst during the first
                                               noise_after_ms of capture time -- a noise frame inside that
                                               quiet period repeats the frame before it (0 = from the start) */
} ts_cap_cfg_t;

typedef struct ts_cap {
    ts_cap_cfg_t cfg;
    bool         open;
    int32_t      fd;                      /**< -1 for synth */
    uint32_t     frame_idx;               /**< frames delivered */
    uint32_t     next_ms;                 /**< synth/file pacing: next frame due */
    uint32_t     file_frames;             /**< frames in the clip (file) */
    uint32_t     file_pos;                /**< next frame index in the clip */
    size_t       partial;                 /**< pipe: bytes of the current frame received so far */
    size_t       skip;                    /**< pipe: UV bytes still to discard */
    uint32_t     short_reads;             /**< stats: partial reads */
    uint32_t     stalls;                  /**< stats: ticks with no data at all */
    uint8_t      stage[TS_FRAME_BYTES];   /**< frame buffer of the fd sources (file, pipe, v4l2) */
} ts_cap_t;

/** Parse "synth" | "file" | "pipe" | "v4l2". Errors: TS_E_INVAL. */
ts_err_t ts_cap_kind_parse(const char *name, ts_cap_kind_t *kind);

/** Open the source. Errors: TS_E_INVAL (bad cfg), TS_E_IO (open failed), TS_E_NOTIMPL (v4l2 off-Linux). */
ts_err_t ts_cap_open(ts_cap_t *c, const ts_cap_cfg_t *cfg);
/**
 * Try to produce one frame into `frame` (TS_FRAME_BYTES). TS_OK: a complete
 * frame was copied; TS_E_AGAIN: no frame yet;
 * TS_E_CLOSED: pipe EOF; TS_E_IO: read error.
 */
ts_err_t ts_cap_read(ts_cap_t *c, uint8_t *frame, uint32_t now_ms);
void     ts_cap_close(ts_cap_t *c);

/**
 * Synthetic generator, exposed for tests and the receiver's exact-match check:
 * deterministic in (frame_idx, seed), moving gradient + bars, a scene cut every
 * `scene_cut_frames` frames and a one-frame noise burst 15 frames after each cut.
 */
void ts_cap_synth_frame(uint8_t *frame, uint32_t frame_idx, uint32_t seed, uint32_t scene_cut_frames);

/* ---- Additive (v2.0.1): synth regeneration helpers ------------------------- */
#define TS_CAP_SCENE_CUT_MS      10000U  /**< default synth scene cut period */
#define TS_CAP_SYNTH_NOISE_AFTER 15U     /**< noise burst this many frames after each cut */
/**
 * Frames per synth scene for (scene_cut_ms, fps); 0 arguments take the defaults
 * (10000 ms, TS_DEFAULT_FPS), never returns 0. ts_cap_read() uses exactly this
 * value, so a receiver regenerates frame `frame_seq` of a synth daemon started
 * with `seq_start` S, seed X and fps F as
 *   ts_cap_synth_frame(buf, frame_seq, X, ts_cap_scene_cut_frames(0, F));
 * (the generator only looks at frame_idx modulo 65536, and tsd_create sets
 * idx_start = S, so frame_idx and frame_seq agree modulo 65536).
 */
uint32_t ts_cap_scene_cut_frames(uint32_t scene_cut_ms, uint32_t fps);

#ifdef __cplusplus
}
#endif

#endif /* TS_CAPTURE_H */
