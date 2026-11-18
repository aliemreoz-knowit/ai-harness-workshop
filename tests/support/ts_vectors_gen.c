/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/support/ts_vectors_gen.c
 * Frame generators, bit-identical to scripts/protocol_vectors.py:
 *   gradientS : px(x,y) = (x + 2*y + S) & 0xFF
 *   noiseS    : xorshift32 seeded S (0 -> 1), state stepped before each pixel, px = state & 0xFF
 *   barsN     : px = 255 when (x / N) is odd else 0
 */
#include <stddef.h>
#include <string.h>
#include <stdlib.h>
#include "ts_config.h"
#include "ts_vectors_gen.h"

static bool parse_num(const char *s, uint32_t *out)
{
    bool ok = false;
    if ((s != NULL) && (*s != '\0')) {
        char *end = NULL;
        unsigned long v = strtoul(s, &end, 10);
        if ((end != NULL) && (*end == '\0')) {
            *out = (uint32_t)v;
            ok = true;
        }
    }
    return ok;
}

bool tsv_gen_frame(const char *generator, uint8_t *frame)
{
    bool ok = false;
    uint32_t arg = 0U;
    if ((generator == NULL) || (frame == NULL)) {
        ok = false;
    } else if ((strncmp(generator, "gradient", 8U) == 0) && parse_num(&generator[8], &arg)) {
        for (uint32_t y = 0U; y < TS_HEIGHT; y++) {
            for (uint32_t x = 0U; x < TS_WIDTH; x++) {
                frame[(y * TS_WIDTH) + x] = (uint8_t)((x + (2U * y) + arg) & 0xFFU);
            }
        }
        ok = true;
    } else if ((strncmp(generator, "noise", 5U) == 0) && parse_num(&generator[5], &arg)) {
        uint32_t s = (arg == 0U) ? 1U : arg;
        for (uint32_t i = 0U; i < TS_FRAME_BYTES; i++) {
            s ^= s << 13;
            s ^= s >> 17;
            s ^= s << 5;
            frame[i] = (uint8_t)(s & 0xFFU);
        }
        ok = true;
    } else if ((strncmp(generator, "bars", 4U) == 0) && parse_num(&generator[4], &arg) && (arg > 0U)) {
        for (uint32_t y = 0U; y < TS_HEIGHT; y++) {
            for (uint32_t x = 0U; x < TS_WIDTH; x++) {
                frame[(y * TS_WIDTH) + x] = (((x / arg) % 2U) == 1U) ? 255U : 0U;
            }
        }
        ok = true;
    } else {
        ok = false;
    }
    return ok;
}
