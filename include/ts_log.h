/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- ts_log.h
 * Fixed-size ring log of numeric events. No formatting, no stdio in the hot
 * path: an entry is (ms, level, module, code, a, b). Compiled OUT of the daemon
 * (TS_LOG_ENABLED defaults to 0) and IN for tools and tests (CMake sets
 * TS_LOG_ENABLED=1 there); ts_log_dump() renders entries with stdio and is
 * only available when enabled.
 *
 * FROZEN API: additive, non-breaking changes only.
 */
#ifndef TS_LOG_H
#define TS_LOG_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#ifndef TS_LOG_ENABLED
#define TS_LOG_ENABLED 0
#endif

#define TS_LOG_RING 256U

typedef enum ts_log_level {
    TS_LOG_ERR  = 0,
    TS_LOG_WARN = 1,
    TS_LOG_INFO = 2,
    TS_LOG_DBG  = 3
} ts_log_level_t;

typedef enum ts_module {
    TS_MOD_PROTO    = 0,
    TS_MOD_CODEC    = 1,
    TS_MOD_TXQ      = 2,
    TS_MOD_RATE     = 3,
    TS_MOD_CLIENTS  = 4,
    TS_MOD_NET      = 5,
    TS_MOD_POOL     = 6,
    TS_MOD_CAPTURE  = 7,
    TS_MOD_DAEMON   = 8,
    TS_MOD_RECEIVER = 9,
    TS_MOD_NETEM    = 10,
    TS_MOD_IO       = 11,
    TS_MOD_MAX_
} ts_module_t;

typedef struct ts_log_entry {
    uint32_t ms;
    uint8_t  level;
    uint8_t  module;
    uint16_t code;     /**< module-defined event code */
    uint32_t a;        /**< two free arguments */
    uint32_t b;
} ts_log_entry_t;

typedef struct ts_log {
    ts_log_entry_t e[TS_LOG_RING];
    uint16_t       head;      /**< next write */
    uint16_t       count;     /**< <= TS_LOG_RING */
    uint32_t       dropped;   /**< entries overwritten before being popped */
    uint8_t        min_level; /**< entries above this level are ignored */
} ts_log_t;

void ts_log_init(ts_log_t *l, ts_log_level_t min_level);
/** Append (overwrites the oldest when full). Always available; no-op when level > min_level. */
void ts_log_push(ts_log_t *l, uint32_t ms, ts_log_level_t level, ts_module_t module,
                 uint16_t code, uint32_t a, uint32_t b);
/** Pop the oldest entry; false when empty. */
bool ts_log_pop(ts_log_t *l, ts_log_entry_t *out);
uint16_t ts_log_count(const ts_log_t *l);
/** Static names for rendering ("proto", "codec", ...). */
const char *ts_log_module_name(ts_module_t m);
const char *ts_log_level_name(ts_log_level_t lv);

#if TS_LOG_ENABLED
/* stdio is compiled into tools and tests only; the daemon builds with TS_LOG_ENABLED=0 */
/* cppcheck-suppress misra-c2012-21.6 */
#include <stdio.h>
/** Render and drain every entry to `out` (tools / tests only). */
void ts_log_dump(ts_log_t *l, FILE *out);
#define TS_LOG(l, ms, lvl, mod, code, a, b) ts_log_push((l), (ms), (lvl), (mod), (code), (a), (b))
#else
#define TS_LOG(l, ms, lvl, mod, code, a, b) ((void)0)
#endif

#ifdef __cplusplus
}
#endif

#endif /* TS_LOG_H */
