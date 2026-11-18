/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tools/tsrecv/rx_args.h
 * Strict numeric argument parsing for the tsrecv command line, kept apart
 * from main.c so tests/unit/test_tsrecv.c can check it.
 *
 * Same rules as the daemon's parser (src/main.c): decimal digits only, no
 * sign, no whitespace, no overflow, value within [lo, hi]. strtoul() accepted
 * leading blanks, '+' and "-0", and any value up to 2^32 - 1.
 */
#ifndef TSRECV_RX_ARGS_H
#define TSRECV_RX_ARGS_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

/** Largest --seconds value whose millisecond count still fits a u32 (4294967 s, ~49.7 days). */
#define RX_SECONDS_MAX  (UINT32_MAX / 1000U)

static inline bool rx_parse_u32(const char *s, uint32_t lo, uint32_t hi, uint32_t *out)
{
    bool ok = (s != NULL) && (s[0] != '\0') && (out != NULL);
    uint64_t v = 0U;
    for (size_t i = 0U; ok && (s[i] != '\0'); i++) {
        char ch = s[i];
        if ((ch >= '0') && (ch <= '9') && (i < 10U)) {
            uint32_t digit = (uint32_t)(uint8_t)ch - (uint32_t)(uint8_t)'0';
            v = (v * 10U) + (uint64_t)digit;
        } else {
            ok = false;
        }
    }
    if (ok && (v >= (uint64_t)lo) && (v <= (uint64_t)hi)) {
        *out = (uint32_t)v;
    } else {
        ok = false;
    }
    return ok;
}

/** --seconds limit reached: elapsed >= seconds * 1000 without u32 wrap (0 = unlimited). */
static inline bool rx_seconds_done(uint32_t elapsed_ms, uint32_t seconds)
{
    return (seconds != 0U) && ((uint64_t)elapsed_ms >= ((uint64_t)seconds * 1000U));
}

#endif /* TSRECV_RX_ARGS_H */
