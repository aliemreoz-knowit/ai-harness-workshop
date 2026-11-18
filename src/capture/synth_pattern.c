/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- src/capture/synth_pattern.c
 * Deterministic synthetic test pattern (ts_cap_synth_frame). Pure function of
 * (frame_idx mod 65536, seed, scene_cut_frames): no state, no I/O, no heap, so
 * the receiver (libtsr links libtsd) regenerates the exact frame the daemon
 * encoded from the frame_seq it received, as long as the daemon was started
 * with ts_cap_cfg_t.idx_start == seq_start (tsd_create does that).
 *
 * Layers, back to front:
 *   background : static per scene, one of 4 variants (diagonal gradient,
 *                coarse checkerboard, bars over a vertical ramp, row ramp),
 *                offset by a per-scene brightness so a cut changes > 60 % tiles;
 *   band       : stepped gradient moving right by 1 px every 2 frames (rows 48..95);
 *   object     : 64x64 checkerboard (8 px cells) moving 2 px / frame (rows 120..183);
 *   ticker     : pseudo-text of hashed 5x7 glyphs scrolling 1 px / frame (rows 216..223);
 *   noise      : a full-frame noise burst TS_CAP_SYNTH_NOISE_AFTER frames after each cut.
 * Moving content is kept small so P-frames stay far below the default bitrate.
 */
#include <stddef.h>
#include <stdint.h>
#include "ts_capture.h"

#define SYN_BAND_Y0     48U
#define SYN_BAND_Y1     96U
#define SYN_BAND_STEP   20U     /* px per gradient step */
#define SYN_OBJ_Y0      120U
#define SYN_OBJ_SIZE    64U
#define SYN_OBJ_CELL    8U
#define SYN_OBJ_SPEED   2U
#define SYN_TICK_Y0     216U
#define SYN_TICK_H      8U
#define SYN_GLYPH_W     5U
#define SYN_GLYPH_H     7U
#define SYN_GLYPH_ADV   6U
#define SYN_SCENE_STEP  97U     /* per-scene brightness step: |delta| is 97 or 159, always > TS_TILE_THRESH */

_Static_assert(SYN_BAND_Y1 <= SYN_OBJ_Y0, "band and object do not overlap");
_Static_assert((SYN_OBJ_Y0 + SYN_OBJ_SIZE) <= SYN_TICK_Y0, "object and ticker do not overlap");
_Static_assert((SYN_TICK_Y0 + SYN_TICK_H) <= TS_HEIGHT, "ticker inside the frame");
_Static_assert(SYN_OBJ_SIZE < TS_WIDTH, "object narrower than the frame");

static uint32_t syn_mix(uint32_t v)
{
    uint32_t h = v;
    h ^= h >> 16;
    h *= 0x7FEB352DU;
    h ^= h >> 15;
    h *= 0x846CA68BU;
    h ^= h >> 16;
    return h;
}

static uint32_t syn_xorshift(uint32_t s)
{
    uint32_t x = s;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    return x;
}

static uint32_t syn_background(uint32_t variant, uint32_t x, uint32_t y)
{
    uint32_t v;
    switch (variant) {
    case 0U:
        v = x + (2U * y);
        break;
    case 1U:
        v = (((x / 32U) + (y / 32U)) & 1U) * 96U;
        break;
    case 2U:
        v = (((x / 16U) & 1U) * 128U) + (y / 2U);
        break;
    default:
        v = y;
        break;
    }
    return v;
}

/* 35 glyph bits for character `ci`; every 5th character is a space. */
static uint32_t syn_glyph_bit(uint32_t ci, uint32_t seed, uint32_t gx, uint32_t gy)
{
    uint32_t bit = 0U;
    if ((ci % 5U) != 4U) {
        uint32_t h1 = syn_mix((ci * 2U) + 1U + (seed * 7919U));
        uint32_t idx = (gy * SYN_GLYPH_W) + gx;      /* 0..34 */
        if (idx < 32U) {
            bit = (h1 >> idx) & 1U;
        } else {
            bit = (syn_mix(h1) >> (idx - 32U)) & 1U;
        }
    }
    return bit;
}

static void syn_noise(uint8_t *frame, uint32_t scene, uint32_t seed)
{
    uint32_t st = syn_mix((scene * 2654435761U) ^ seed ^ 0x9E3779B9U) | 1U;
    for (size_t i = 0U; i < TS_FRAME_BYTES; i++) {
        st = syn_xorshift(st);
        frame[i] = (uint8_t)(st & 0xFFU);
    }
}

static void syn_row(uint8_t *row, uint32_t y, uint32_t t, uint32_t variant, uint32_t base, uint32_t seed)
{
    uint32_t obj_x0 = (t * SYN_OBJ_SPEED) % (TS_WIDTH - SYN_OBJ_SIZE);
    for (uint32_t x = 0U; x < TS_WIDTH; x++) {
        uint32_t v = syn_background(variant, x, y) + base;
        if ((y >= SYN_BAND_Y0) && (y < SYN_BAND_Y1)) {
            v = (((x + (t / 2U)) / SYN_BAND_STEP) * 16U) + base + 40U;
        } else if ((y >= SYN_OBJ_Y0) && (y < (SYN_OBJ_Y0 + SYN_OBJ_SIZE)) &&
                   (x >= obj_x0) && (x < (obj_x0 + SYN_OBJ_SIZE))) {
            uint32_t cx = (x - obj_x0) / SYN_OBJ_CELL;
            uint32_t cy = (y - SYN_OBJ_Y0) / SYN_OBJ_CELL;
            v = 0U;
            if (((cx + cy) & 1U) != 0U) {
                v = 255U;
            }
        } else if ((y >= SYN_TICK_Y0) && (y < (SYN_TICK_Y0 + SYN_TICK_H))) {
            uint32_t u = x + t;
            uint32_t gx = u % SYN_GLYPH_ADV;
            uint32_t gy = y - SYN_TICK_Y0;
            v = 16U;
            if ((gx < SYN_GLYPH_W) && (gy < SYN_GLYPH_H) &&
                (syn_glyph_bit(u / SYN_GLYPH_ADV, seed, gx, gy) != 0U)) {
                v = 235U;
            }
        } else {
            /* background only */
        }
        row[x] = (uint8_t)(v & 0xFFU);
    }
}

void ts_cap_synth_frame(uint8_t *frame, uint32_t frame_idx, uint32_t seed, uint32_t scene_cut_frames)
{
    if (frame != NULL) {
        uint32_t idx = frame_idx & 0xFFFFU;   /* period 65536: frame_seq is enough to regenerate */
        uint32_t scene = 0U;
        uint32_t t = idx;
        if (scene_cut_frames != 0U) {
            scene = idx / scene_cut_frames;
            t = idx % scene_cut_frames;
        }
        if ((scene_cut_frames > TS_CAP_SYNTH_NOISE_AFTER) && (t == TS_CAP_SYNTH_NOISE_AFTER)) {
            syn_noise(frame, scene, seed);
        } else {
            uint32_t variant = (scene + seed) & 3U;
            uint32_t base = ((scene * SYN_SCENE_STEP) + (seed * 31U)) & 0xFFU;
            for (uint32_t y = 0U; y < TS_HEIGHT; y++) {
                syn_row(&frame[(size_t)y * (size_t)TS_WIDTH], y, t, variant, base, seed);
            }
        }
    }
}

uint32_t ts_cap_scene_cut_frames(uint32_t scene_cut_ms, uint32_t fps)
{
    uint32_t ms = (scene_cut_ms != 0U) ? scene_cut_ms : TS_CAP_SCENE_CUT_MS;
    uint32_t f = (fps != 0U) ? fps : TS_DEFAULT_FPS;
    uint64_t frames = ((uint64_t)ms * (uint64_t)f) / 1000U;
    if (frames == 0U) {
        frames = 1U;
    }
    if (frames > 0xFFFFFFFFU) {
        frames = 0xFFFFFFFFU;
    }
    return (uint32_t)frames;
}
