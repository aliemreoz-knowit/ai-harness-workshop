/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- src/log/log.c
 * Ring log of numeric events (final). ts_log_dump() exists only when TS_LOG_ENABLED.
 */
#include <stddef.h>
#include <string.h>
#include "ts_log.h"

void ts_log_init(ts_log_t *l, ts_log_level_t min_level)
{
    if (l != NULL) {
        (void)memset(l, 0, sizeof(*l));
        l->min_level = (uint8_t)min_level;
    }
}

void ts_log_push(ts_log_t *l, uint32_t ms, ts_log_level_t level, ts_module_t module,
                 uint16_t code, uint32_t a, uint32_t b)
{
    if ((l != NULL) && ((uint8_t)level <= l->min_level)) {
        ts_log_entry_t *e = &l->e[l->head];
        e->ms = ms;
        e->level = (uint8_t)level;
        e->module = (uint8_t)module;
        e->code = code;
        e->a = a;
        e->b = b;
        l->head = (uint16_t)((l->head + 1U) % TS_LOG_RING);
        if (l->count < (uint16_t)TS_LOG_RING) {
            l->count++;
        } else {
            l->dropped++;
        }
    }
}

bool ts_log_pop(ts_log_t *l, ts_log_entry_t *out)
{
    bool ok = false;
    if ((l != NULL) && (out != NULL) && (l->count > 0U)) {
        uint16_t tail = (uint16_t)((l->head + TS_LOG_RING - l->count) % TS_LOG_RING);
        *out = l->e[tail];
        l->count--;
        ok = true;
    }
    return ok;
}

uint16_t ts_log_count(const ts_log_t *l)
{
    return (l != NULL) ? l->count : 0U;
}

const char *ts_log_module_name(ts_module_t m)
{
    static const char *const names[TS_MOD_MAX_] = {
        "proto", "codec", "txq", "rate", "clients", "net",
        "pool", "capture", "daemon", "receiver", "netem", "io"
    };
    return ((m >= TS_MOD_PROTO) && (m < TS_MOD_MAX_)) ? names[m] : "?";
}

const char *ts_log_level_name(ts_log_level_t lv)
{
    static const char *const names[4] = { "ERR", "WARN", "INFO", "DBG" };
    return ((lv >= TS_LOG_ERR) && (lv <= TS_LOG_DBG)) ? names[lv] : "?";
}

#if TS_LOG_ENABLED
void ts_log_dump(ts_log_t *l, FILE *out)
{
    ts_log_entry_t e;
    if ((l != NULL) && (out != NULL)) {
        while (ts_log_pop(l, &e)) {
            (void)fprintf(out, "%10u %-4s %-8s code=%u a=%u b=%u\n",
                          (unsigned)e.ms, ts_log_level_name((ts_log_level_t)e.level),
                          ts_log_module_name((ts_module_t)e.module),
                          (unsigned)e.code, (unsigned)e.a, (unsigned)e.b);
        }
        if (l->dropped != 0U) {
            (void)fprintf(out, "(%u entries dropped)\n", (unsigned)l->dropped);
        }
    }
}
#endif
