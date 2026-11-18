/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tools/tsrecv/rx_tear.h
 * Tear detector for decoded frames (tsrecv metrics, HIL station).
 *
 * A torn frame is encoded half from one picture and half from another (or
 * from bytes that are not a picture at all), so a horizontal line appears
 * where two rows no longer belong together. The detector measures that
 * row-pair discontinuity:
 *
 *   d[k] = sum over x of |p(x, k) - p(x, k + 1)|          k = 0 .. H - 2
 *
 * A static scene has natural edges too (text, stripes), so a row pair only
 * counts when it is discontinuous NOW and was continuous in the previous
 * checked frame:
 *
 *   d[k] >= RX_TEAR_MIN_STEP * W            (a visible line: mean step >= 24 levels)
 *   d[k] >= prev[k] + RX_TEAR_MIN_STEP * W  (new: not there in the previous frame)
 *   d[k] >= RX_TEAR_RATIO * prev[k]
 *
 * A frame is torn when 1 .. RX_TEAR_MAX_ROWS row pairs are new
 * discontinuities. More than that is a new picture (scene cut, quality step,
 * the first frame after a freeze): counted as a picture change, not a tear.
 * The file source (a looped clip with a static poster and a small object
 * moving sideways) never produces a new discontinuity, so any tear there is
 * a capture or transport defect; on the camera the count is informative.
 *
 * Pure functions, no heap, no stdio: tests/unit/test_tsrecv.c checks them.
 */
#ifndef TSRECV_RX_TEAR_H
#define TSRECV_RX_TEAR_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include "ts_config.h"

#define RX_TEAR_PAIRS     (TS_HEIGHT - 1U)  /**< row pairs per frame */
#define RX_TEAR_MIN_STEP  24U               /**< mean step per pixel of a visible line */
#define RX_TEAR_RATIO     3U                /**< new line: at least 3x the previous frame */
#define RX_TEAR_MAX_ROWS  32U               /**< more new lines than this = a new picture */

typedef struct rx_tear {
    bool     have_prev;
    uint32_t prev[RX_TEAR_PAIRS];   /**< d[k] of the previous checked frame */
    uint32_t frames_checked;
    uint32_t tear_frames;           /**< frames with 1 .. RX_TEAR_MAX_ROWS new lines */
    uint32_t tear_rows_total;       /**< new lines summed over the torn frames */
    uint32_t picture_changes;       /**< frames with more than RX_TEAR_MAX_ROWS new lines */
    uint32_t last_row;              /**< first new line of the last torn frame (row index) */
    uint32_t last_rows;             /**< new lines in the last checked frame */
} rx_tear_t;

static inline void rx_tear_init(rx_tear_t *t)
{
    if (t != NULL) {
        t->have_prev = false;
        for (uint32_t k = 0U; k < RX_TEAR_PAIRS; k++) {
            t->prev[k] = 0U;
        }
        t->frames_checked = 0U;
        t->tear_frames = 0U;
        t->tear_rows_total = 0U;
        t->picture_changes = 0U;
        t->last_row = 0U;
        t->last_rows = 0U;
    }
}

/** Sum of |row k - row k + 1| over the frame width. */
static inline uint32_t rx_tear_row_step(const uint8_t *frame, uint32_t k)
{
    const uint8_t *a = &frame[k * TS_WIDTH];
    const uint8_t *b = &frame[(k + 1U) * TS_WIDTH];
    uint32_t sum = 0U;
    for (uint32_t x = 0U; x < TS_WIDTH; x++) {
        uint32_t pa = (uint32_t)a[x];
        uint32_t pb = (uint32_t)b[x];
        sum += (pa > pb) ? (pa - pb) : (pb - pa);
    }
    return sum;
}

/** True when row pair k with step d is a new discontinuity against the previous step p. */
static inline bool rx_tear_is_new_line(uint32_t d, uint32_t p)
{
    const uint32_t min_sum = RX_TEAR_MIN_STEP * TS_WIDTH;
    return (d >= min_sum) && (d >= (p + min_sum)) && (d >= (RX_TEAR_RATIO * p));
}

/**
 * Check one decoded frame (TS_FRAME_BYTES). Returns the number of new
 * discontinuous row pairs (0 for the first frame). Updates the counters.
 */
static inline uint32_t rx_tear_check(rx_tear_t *t, const uint8_t *frame)
{
    uint32_t rows = 0U;
    if ((t != NULL) && (frame != NULL)) {
        uint32_t first = 0U;
        bool compare = t->have_prev;
        for (uint32_t k = 0U; k < RX_TEAR_PAIRS; k++) {
            uint32_t d = rx_tear_row_step(frame, k);
            if (compare && rx_tear_is_new_line(d, t->prev[k])) {
                if (rows == 0U) {
                    first = k;
                }
                rows++;
            }
            t->prev[k] = d;
        }
        t->have_prev = true;
        t->frames_checked++;
        t->last_rows = rows;
        if ((rows > 0U) && (rows <= RX_TEAR_MAX_ROWS)) {
            t->tear_frames++;
            t->tear_rows_total += rows;
            t->last_row = first + 1U;   /* the first row below the line */
        } else if (rows > RX_TEAR_MAX_ROWS) {
            t->picture_changes++;
        } else {
            /* clean frame */
        }
    }
    return rows;
}

#endif /* TSRECV_RX_TEAR_H */
