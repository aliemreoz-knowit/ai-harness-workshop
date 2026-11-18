/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tools/tsnetem/lib/netem.c
 * Chaos filter, in-process shim and scenario timeline (include/ts_netem.h).
 *
 * The same ts_netem_filter_len() runs inside the tsnetem UDP relay
 * (tools/tsnetem/main.c) and between two ts_io_fake_t endpoints in loopback
 * tests (ts_netem_pump / ts_netem_pump_star). Deterministic: the only
 * randomness is the xorshift32 state seeded from the config, the only clock
 * is the `now_ms` argument. No dynamic memory, no stdio.
 */
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include "ts_netem.h"

/* ---- small helpers ---------------------------------------------------------- */

/** true when `now` is at or after `at` (serial u32 time). */
static bool netem_due(uint32_t now_ms, uint32_t at_ms)
{
    return (uint32_t)(now_ms - at_ms) < 0x80000000U;
}

/** One draw when `pct` > 0; true when the feature hits. */
static bool netem_hit(ts_netem_t *n, uint8_t pct)
{
    bool hit = false;
    if (pct > 0U) {
        uint32_t r = ts_netem_rand(&n->rng);
        hit = (r % 100U) < (uint32_t)pct;
    }
    return hit;
}

static uint32_t netem_throttle_cap(uint32_t kbps)
{
    /* kbps * 1000 / 8 bytes per second = kbps * 125 bytes/s */
    uint64_t cap = ((uint64_t)kbps * 125U * (uint64_t)TS_NETEM_THROTTLE_BURST_MS) / 1000U;
    uint64_t floor_bytes = 2U * (uint64_t)TS_IO_DGRAM_MAX;
    if (cap < floor_bytes) {
        cap = floor_bytes;
    }
    if (cap > 0xFFFFFFFFU) {
        cap = 0xFFFFFFFFU;
    }
    return (uint32_t)cap;
}

static void netem_throttle_refill(ts_netem_t *n, uint32_t now_ms)
{
    uint32_t kbps = n->cfg.throttle_kbps;
    if (kbps > 0U) {
        uint32_t elapsed = now_ms - n->throttle_last_ms;
        if (elapsed >= 0x80000000U) {
            /* clock went backwards: restart the measurement */
            n->throttle_last_ms = now_ms;
        } else {
            /* kbps * 125 bytes/s = kbps / 8 bytes per ms */
            uint64_t add = ((uint64_t)elapsed * (uint64_t)kbps) / 8U;
            if (add > 0U) {
                uint64_t cap = (uint64_t)netem_throttle_cap(kbps);
                uint64_t t = (uint64_t)n->throttle_tokens + add;
                n->throttle_tokens = (uint32_t)((t > cap) ? cap : t);
                n->throttle_last_ms = now_ms;
            }
        }
    }
}

static bool netem_throttle_take(ts_netem_t *n, uint32_t now_ms, uint16_t len)
{
    bool ok = true;
    if (n->cfg.throttle_kbps > 0U) {
        netem_throttle_refill(n, now_ms);
        if (n->throttle_tokens >= (uint32_t)len) {
            n->throttle_tokens -= (uint32_t)len;
        } else {
            ok = false;
        }
    }
    return ok;
}

static void netem_burst_arm(ts_netem_t *n, uint32_t now_ms)
{
    n->burst_left = (uint32_t)n->cfg.burst_len;
    n->next_burst_ms = now_ms + n->cfg.burst_period_ms;
}

/** Consumes one packet of the current burst; re-arms periodic bursts. */
static bool netem_burst_drop(ts_netem_t *n, uint32_t now_ms)
{
    bool drop = false;
    if ((n->cfg.burst_len > 0U) && (n->cfg.burst_period_ms > 0U) && (n->burst_left == 0U) &&
        netem_due(now_ms, n->next_burst_ms)) {
        netem_burst_arm(n, now_ms);
    }
    if (n->burst_left > 0U) {
        n->burst_left--;
        drop = true;
    }
    return drop;
}

static void netem_bitflip(ts_netem_t *n, uint8_t *data, uint16_t len)
{
    uint32_t lo = 0U;
    uint32_t hi = (uint32_t)len;
    if (n->cfg.bitflip_scope == (uint8_t)TS_NETEM_FLIP_HEADER) {
        if (hi > TS_HDR_LEN) {
            hi = TS_HDR_LEN;
        }
    } else if (n->cfg.bitflip_scope == (uint8_t)TS_NETEM_FLIP_PAYLOAD) {
        if (hi > TS_HDR_LEN) {
            lo = TS_HDR_LEN;
        }
    } else {
        /* TS_NETEM_FLIP_ANY: whole datagram */
    }
    if ((data != NULL) && (hi > lo)) {
        uint32_t r = ts_netem_rand(&n->rng);
        uint32_t bit = r % ((hi - lo) * 8U);
        uint32_t idx = lo + (bit / 8U);
        data[idx] ^= (uint8_t)(1U << (bit % 8U));
        n->st.flipped++;
    }
}

static ts_netem_verdict_t netem_decide(ts_netem_t *n, uint8_t *data, uint16_t *len, bool allow_trunc,
                                       uint32_t now_ms, uint32_t *delay_ms)
{
    ts_netem_verdict_t v = TS_NETEM_PASS;
    uint32_t extra = 0U;

    n->st.seen++;
    if (n->cfg.cable_pulled) {
        v = TS_NETEM_DROP;
        n->st.dropped_cable++;
    } else if (netem_burst_drop(n, now_ms)) {
        v = TS_NETEM_DROP;
        n->st.dropped_burst++;
    } else if (netem_hit(n, n->cfg.loss_pct)) {
        v = TS_NETEM_DROP;
    } else if (!netem_throttle_take(n, now_ms, *len)) {
        v = TS_NETEM_DROP;
        n->st.dropped_throttle++;
    } else {
        if (netem_hit(n, n->cfg.bitflip_pct)) {
            netem_bitflip(n, data, *len);
        }
        if (allow_trunc && netem_hit(n, n->cfg.truncate_pct) && (*len >= 2U)) {
            uint32_t r = ts_netem_rand(&n->rng);
            uint32_t nl = 1U + (r % ((uint32_t)*len - 1U));
            *len = (uint16_t)nl;
            n->st.truncated++;
        }
        extra = n->cfg.delay_ms;
        if (netem_hit(n, n->cfg.reorder_pct)) {
            uint32_t hold = (n->cfg.reorder_delay_ms > 0U) ? n->cfg.reorder_delay_ms : TS_NETEM_REORDER_DEFAULT_MS;
            extra += hold;
            n->st.reordered++;
        }
        if (netem_hit(n, n->cfg.dup_pct)) {
            v = TS_NETEM_DUP;
            n->st.duplicated++;
        }
    }

    if (v == TS_NETEM_DROP) {
        n->st.dropped++;
        extra = 0U;
    } else {
        n->st.passed++;
    }
    if (delay_ms != NULL) {
        *delay_ms = extra;
    }
    return v;
}

/* ---- public: PRNG, lifecycle, filter ----------------------------------------- */

uint32_t ts_netem_rand(uint32_t *state)
{
    uint32_t x = (*state == 0U) ? 1U : *state;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    *state = x;
    return x;
}

void ts_netem_init(ts_netem_t *n, const ts_netem_cfg_t *cfg, uint32_t now_ms)
{
    if (n != NULL) {
        (void)memset(n, 0, sizeof(*n));
        if (cfg != NULL) {
            n->cfg = *cfg;
        }
        n->rng = (n->cfg.seed == 0U) ? 1U : n->cfg.seed;
        n->throttle_last_ms = now_ms;
        n->throttle_tokens = netem_throttle_cap(n->cfg.throttle_kbps);
        n->next_burst_ms = now_ms;
        if (n->cfg.burst_len > 0U) {
            netem_burst_arm(n, now_ms);
        }
    }
}

void ts_netem_set(ts_netem_t *n, const ts_netem_cfg_t *cfg, uint32_t now_ms)
{
    if ((n != NULL) && (cfg != NULL)) {
        ts_netem_cfg_t old = n->cfg;
        netem_throttle_refill(n, now_ms);
        n->cfg = *cfg;
        if (cfg->throttle_kbps != old.throttle_kbps) {
            uint32_t cap = netem_throttle_cap(cfg->throttle_kbps);
            if ((old.throttle_kbps == 0U) || (n->throttle_tokens > cap)) {
                n->throttle_tokens = cap;
            }
            n->throttle_last_ms = now_ms;
        }
        if ((cfg->burst_len > 0U) &&
            ((cfg->burst_len != old.burst_len) || (cfg->burst_period_ms != old.burst_period_ms))) {
            netem_burst_arm(n, now_ms);
        }
    }
}

void ts_netem_burst_now(ts_netem_t *n, uint16_t len)
{
    if ((n != NULL) && (len > 0U)) {
        n->burst_left = (uint32_t)len;
    }
}

void ts_netem_scenario_start(ts_netem_t *n, const ts_netem_scenario_t *s, uint32_t now_ms)
{
    if (n != NULL) {
        n->scenario = s;
        n->scenario_start_ms = now_ms;
        n->scenario_step = 0U;
        ts_netem_tick(n, now_ms);
    }
}

void ts_netem_tick(ts_netem_t *n, uint32_t now_ms)
{
    if ((n != NULL) && (n->scenario != NULL) && (n->scenario->steps != NULL) &&
        netem_due(now_ms, n->scenario_start_ms)) {
        uint32_t elapsed = now_ms - n->scenario_start_ms;
        while ((n->scenario_step < n->scenario->count) &&
               (n->scenario->steps[n->scenario_step].at_ms <= elapsed)) {
            ts_netem_set(n, &n->scenario->steps[n->scenario_step].cfg, now_ms);
            n->scenario_step++;
        }
    }
}

ts_netem_verdict_t ts_netem_filter(ts_netem_t *n, uint8_t *data, uint16_t len, uint32_t now_ms, uint32_t *delay_ms)
{
    ts_netem_verdict_t v = TS_NETEM_PASS;
    uint16_t l = len;
    if (n != NULL) {
        v = netem_decide(n, data, &l, false, now_ms, delay_ms);
    } else if (delay_ms != NULL) {
        *delay_ms = 0U;
    } else {
        /* no filter, no delay requested: clean link */
    }
    return v;
}

ts_netem_verdict_t ts_netem_filter_len(ts_netem_t *n, uint8_t *data, uint16_t *len, uint32_t now_ms, uint32_t *delay_ms)
{
    ts_netem_verdict_t v = TS_NETEM_PASS;
    if ((n != NULL) && (len != NULL)) {
        v = netem_decide(n, data, len, true, now_ms, delay_ms);
    } else if (delay_ms != NULL) {
        *delay_ms = 0U;
    } else {
        /* nothing to decide */
    }
    return v;
}

const ts_netem_stats_t *ts_netem_stats(const ts_netem_t *n)
{
    return (n != NULL) ? &n->st : NULL;
}

/* ---- shim: two fake endpoints ---------------------------------------------- */

static void netem_deliver(ts_io_fake_t *dest, const ts_io_dgram_t *d)
{
    /* a full destination queue counts the drop in dest->udp_in.dropped */
    (void)ts_io_fake_push_udp(dest, d);
}

static uint16_t netem_held_count(const ts_netem_t *n)
{
    uint16_t c = 0U;
    while ((c < TS_NETEM_HOLD) && n->held[c].used) {
        c++;
    }
    return c;
}

/** Held entries form a prefix in arrival order (stable compaction on release). */
static void netem_hold(ts_netem_t *n, ts_io_fake_t *dest, const ts_io_dgram_t *d, uint32_t release_ms)
{
    uint16_t idx = netem_held_count(n);
    if (idx >= TS_NETEM_HOLD) {
        n->st.held_overflow++;
    } else {
        n->held[idx].used = true;
        n->held[idx].release_ms = release_ms;
        n->held[idx].dest = dest;
        n->held[idx].d = *d;
    }
}

static void netem_release(ts_netem_t *n, uint32_t now_ms)
{
    uint16_t count = netem_held_count(n);
    uint16_t w = 0U;
    for (uint16_t i = 0U; i < count; i++) {
        if (netem_due(now_ms, n->held[i].release_ms)) {
            netem_deliver(n->held[i].dest, &n->held[i].d);
        } else {
            if (w != i) {
                n->held[w] = n->held[i];
            }
            w++;
        }
    }
    for (uint16_t i = w; i < count; i++) {
        n->held[i].used = false;
        n->held[i].dest = NULL;
    }
}

static void netem_forward(ts_netem_t *n, ts_io_dgram_t *d, ts_io_fake_t *dest, uint32_t now_ms)
{
    uint32_t delay = 0U;
    uint16_t len = d->len;
    ts_netem_verdict_t v = netem_decide(n, d->data, &len, true, now_ms, &delay);
    d->len = len;
    if (v != TS_NETEM_DROP) {
        uint8_t copies = (v == TS_NETEM_DUP) ? 2U : 1U;
        for (uint8_t c = 0U; c < copies; c++) {
            if (delay == 0U) {
                netem_deliver(dest, d);
            } else {
                netem_hold(n, dest, d, now_ms + delay);
            }
        }
    }
}

void ts_netem_pump(ts_netem_t *n, ts_io_fake_t *a, ts_io_fake_t *b, uint32_t now_ms)
{
    if ((n != NULL) && (a != NULL) && (b != NULL)) {
        ts_io_dgram_t d;
        ts_netem_tick(n, now_ms);
        netem_release(n, now_ms);
        while (ts_io_fake_pop_udp(a, &d)) {
            if (ts_addr_eq(&d.to, &b->self)) {
                netem_forward(n, &d, b, now_ms);
            } else {
                n->st.misrouted++;
            }
        }
        while (ts_io_fake_pop_udp(b, &d)) {
            if (ts_addr_eq(&d.to, &a->self)) {
                netem_forward(n, &d, a, now_ms);
            } else {
                n->st.misrouted++;
            }
        }
    }
}

void ts_netem_pump_star(ts_netem_t *n, ts_io_fake_t *hub, ts_io_fake_t *const *leaves, uint8_t nleaves, uint32_t now_ms)
{
    if ((n != NULL) && (hub != NULL) && ((leaves != NULL) || (nleaves == 0U))) {
        ts_io_dgram_t d;
        ts_netem_tick(n, now_ms);
        netem_release(n, now_ms);
        while (ts_io_fake_pop_udp(hub, &d)) {
            ts_io_fake_t *dest = NULL;
            for (uint8_t i = 0U; (i < nleaves) && (dest == NULL); i++) {
                if ((leaves[i] != NULL) && ts_addr_eq(&d.to, &leaves[i]->self)) {
                    dest = leaves[i];
                }
            }
            if (dest != NULL) {
                netem_forward(n, &d, dest, now_ms);
            } else {
                n->st.misrouted++;
            }
        }
        for (uint8_t i = 0U; i < nleaves; i++) {
            if (leaves[i] != NULL) {
                while (ts_io_fake_pop_udp(leaves[i], &d)) {
                    netem_forward(n, &d, hub, now_ms);
                }
            }
        }
    }
}

/* ---- text: cfg specs ------------------------------------------------------------ */

typedef struct netem_span {
    const char *p;
    size_t      n;
} netem_span_t;

static bool netem_is_sep(char c)
{
    return (c == ',') || (c == ';') || (c == ' ') || (c == '\t') || (c == '\r') || (c == '\n');
}

static bool netem_is_digit(char c)
{
    return (c >= '0') && (c <= '9');
}

static uint32_t netem_digit(char c)
{
    return (uint32_t)(uint8_t)c - 48U;   /* '0' */
}

static bool netem_ch_is(char a, char b)
{
    return a == b;
}

static char netem_lower(char c)
{
    static const char upper[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";
    static const char lower[] = "abcdefghijklmnopqrstuvwxyz";
    char r = c;
    for (size_t i = 0U; i < (sizeof(upper) - 1U); i++) {
        if (netem_ch_is(upper[i], c)) {
            r = lower[i];
        }
    }
    return r;
}

/** Case-insensitive comparison of a span with a NUL-terminated (lowercase) literal. */
static bool netem_span_is(netem_span_t s, const char *lit)
{
    size_t i = 0U;
    bool eq = true;
    while (eq && (i < s.n)) {
        if ((lit[i] == '\0') || !netem_ch_is(netem_lower(s.p[i]), lit[i])) {
            eq = false;
        }
        i++;
    }
    return eq && (lit[s.n] == '\0');
}

static bool netem_span_in(netem_span_t s, const char *const *list, size_t count)
{
    bool found = false;
    for (size_t i = 0U; (i < count) && !found; i++) {
        found = netem_span_is(s, list[i]);
    }
    return found;
}

/**
 * Decimal number with an optional fraction of up to 3 digits, returned in
 * thousandths; `*unit` receives the rest of the span.
 */
static ts_err_t netem_parse_milli(netem_span_t s, uint64_t *milli, netem_span_t *unit)
{
    ts_err_t rc = TS_OK;
    size_t i = 0U;
    uint64_t ip = 0U;
    uint64_t frac = 0U;
    uint32_t fdigits = 0U;

    if ((s.n == 0U) || !netem_is_digit(s.p[0])) {
        rc = TS_E_INVAL;
    }
    while ((rc == TS_OK) && (i < s.n) && netem_is_digit(s.p[i])) {
        ip = (ip * 10U) + (uint64_t)netem_digit(s.p[i]);
        if (ip > 0xFFFFFFFFU) {
            rc = TS_E_INVAL;
        }
        i++;
    }
    if ((rc == TS_OK) && (i < s.n) && netem_ch_is(s.p[i], '.')) {
        i++;
        if ((i >= s.n) || !netem_is_digit(s.p[i])) {
            rc = TS_E_INVAL;
        }
        while ((rc == TS_OK) && (i < s.n) && netem_is_digit(s.p[i])) {
            if (fdigits >= 3U) {
                rc = TS_E_INVAL;
            } else {
                frac = (frac * 10U) + (uint64_t)netem_digit(s.p[i]);
                fdigits++;
            }
            i++;
        }
    }
    if (rc == TS_OK) {
        while (fdigits < 3U) {
            frac *= 10U;
            fdigits++;
        }
        *milli = (ip * 1000U) + frac;
        unit->p = &s.p[i];
        unit->n = s.n - i;
    }
    return rc;
}

static bool netem_unit_none(netem_span_t u)
{
    return u.n == 0U;
}

static ts_err_t netem_parse_int(netem_span_t s, uint32_t max, uint32_t *out)
{
    ts_err_t rc;
    uint64_t m = 0U;
    netem_span_t u = { NULL, 0U };
    rc = netem_parse_milli(s, &m, &u);
    if ((rc == TS_OK) && (!netem_unit_none(u) || ((m % 1000U) != 0U) || ((m / 1000U) > (uint64_t)max))) {
        rc = TS_E_INVAL;
    }
    if (rc == TS_OK) {
        *out = (uint32_t)(m / 1000U);
    }
    return rc;
}

static ts_err_t netem_parse_pct(netem_span_t s, uint8_t *out)
{
    ts_err_t rc;
    netem_span_t v = s;
    uint32_t x = 0U;
    if ((v.n > 0U) && netem_ch_is(v.p[v.n - 1U], '%')) {
        v.n--;
    }
    rc = netem_parse_int(v, 100U, &x);
    if (rc == TS_OK) {
        *out = (uint8_t)x;
    }
    return rc;
}

static ts_err_t netem_parse_ms(netem_span_t s, uint32_t *out)
{
    ts_err_t rc;
    uint64_t m = 0U;
    uint64_t ms = 0U;
    netem_span_t u = { NULL, 0U };
    rc = netem_parse_milli(s, &m, &u);
    if (rc == TS_OK) {
        if (netem_unit_none(u) || netem_span_is(u, "ms")) {
            if ((m % 1000U) != 0U) {
                rc = TS_E_INVAL;
            } else {
                ms = m / 1000U;
            }
        } else if (netem_span_is(u, "s")) {
            ms = m;
        } else {
            rc = TS_E_INVAL;
        }
    }
    if ((rc == TS_OK) && (ms > 0xFFFFFFFFU)) {
        rc = TS_E_INVAL;
    }
    if (rc == TS_OK) {
        *out = (uint32_t)ms;
    }
    return rc;
}

static ts_err_t netem_parse_kbps(netem_span_t s, uint32_t *out)
{
    static const char *const kunits[] = { "k", "kbps", "kbit", "kbit/s" };
    static const char *const munits[] = { "m", "mbps", "mbit", "mbit/s" };
    ts_err_t rc;
    uint64_t m = 0U;
    uint64_t kbps = 0U;
    netem_span_t u = { NULL, 0U };
    rc = netem_parse_milli(s, &m, &u);
    if (rc == TS_OK) {
        if (netem_unit_none(u) || netem_span_in(u, kunits, sizeof(kunits) / sizeof(kunits[0]))) {
            if ((m % 1000U) != 0U) {
                rc = TS_E_INVAL;
            } else {
                kbps = m / 1000U;
            }
        } else if (netem_span_in(u, munits, sizeof(munits) / sizeof(munits[0]))) {
            kbps = m;
        } else {
            rc = TS_E_INVAL;
        }
    }
    if ((rc == TS_OK) && (kbps > 0xFFFFFFFFU)) {
        rc = TS_E_INVAL;
    }
    if (rc == TS_OK) {
        *out = (uint32_t)kbps;
    }
    return rc;
}

static ts_err_t netem_parse_bool(netem_span_t s, bool *out)
{
    static const char *const yes[] = { "1", "on", "yes", "true", "pulled" };
    static const char *const no[] = { "0", "off", "no", "false" };
    ts_err_t rc = TS_OK;
    if (netem_span_in(s, yes, sizeof(yes) / sizeof(yes[0]))) {
        *out = true;
    } else if (netem_span_in(s, no, sizeof(no) / sizeof(no[0]))) {
        *out = false;
    } else {
        rc = TS_E_INVAL;
    }
    return rc;
}

static ts_err_t netem_parse_scope(netem_span_t s, uint8_t *out)
{
    ts_err_t rc = TS_OK;
    if (netem_span_is(s, "any") || netem_span_is(s, "all")) {
        *out = (uint8_t)TS_NETEM_FLIP_ANY;
    } else if (netem_span_is(s, "header") || netem_span_is(s, "hdr")) {
        *out = (uint8_t)TS_NETEM_FLIP_HEADER;
    } else if (netem_span_is(s, "payload") || netem_span_is(s, "data")) {
        *out = (uint8_t)TS_NETEM_FLIP_PAYLOAD;
    } else {
        rc = TS_E_INVAL;
    }
    return rc;
}

static void netem_cfg_clean(ts_netem_cfg_t *cfg)
{
    uint32_t seed = cfg->seed;
    (void)memset(cfg, 0, sizeof(*cfg));
    cfg->seed = seed;
}

/** Bare word (no '='). */
static ts_err_t netem_apply_word(netem_span_t w, ts_netem_cfg_t *cfg)
{
    static const char *const clean[] = { "clean", "clear", "reset", "none" };
    static const char *const pull[] = { "cable", "pull", "pulled", "unplug" };
    static const char *const plug[] = { "plug", "restore", "reconnect" };
    ts_err_t rc = TS_OK;
    if (netem_span_in(w, clean, sizeof(clean) / sizeof(clean[0]))) {
        netem_cfg_clean(cfg);
    } else if (netem_span_in(w, pull, sizeof(pull) / sizeof(pull[0]))) {
        cfg->cable_pulled = true;
    } else if (netem_span_in(w, plug, sizeof(plug) / sizeof(plug[0]))) {
        cfg->cable_pulled = false;
    } else {
        rc = TS_E_INVAL;
    }
    return rc;
}

static ts_err_t netem_apply_kv(netem_span_t k, netem_span_t v, ts_netem_cfg_t *cfg)
{
    static const char *const k_loss[] = { "loss", "drop" };
    static const char *const k_period[] = { "burst_period", "period", "burst_every" };
    static const char *const k_hold[] = { "reorder_delay", "hold", "reorder_hold" };
    static const char *const k_dup[] = { "dup", "duplicate" };
    static const char *const k_flip[] = { "bitflip", "flip", "corrupt" };
    static const char *const k_scope[] = { "flip_scope", "scope" };
    static const char *const k_trunc[] = { "truncate", "trunc" };
    static const char *const k_rate[] = { "throttle", "kbps", "rate", "bw" };
    ts_err_t rc;
    uint32_t x = 0U;

    if (netem_span_in(k, k_loss, sizeof(k_loss) / sizeof(k_loss[0]))) {
        rc = netem_parse_pct(v, &cfg->loss_pct);
    } else if (netem_span_is(k, "burst")) {
        rc = netem_parse_int(v, 0xFFFFU, &x);
        if (rc == TS_OK) {
            cfg->burst_len = (uint16_t)x;
        }
    } else if (netem_span_in(k, k_period, sizeof(k_period) / sizeof(k_period[0]))) {
        rc = netem_parse_ms(v, &cfg->burst_period_ms);
    } else if (netem_span_is(k, "reorder")) {
        rc = netem_parse_pct(v, &cfg->reorder_pct);
    } else if (netem_span_in(k, k_hold, sizeof(k_hold) / sizeof(k_hold[0]))) {
        rc = netem_parse_ms(v, &cfg->reorder_delay_ms);
    } else if (netem_span_in(k, k_dup, sizeof(k_dup) / sizeof(k_dup[0]))) {
        rc = netem_parse_pct(v, &cfg->dup_pct);
    } else if (netem_span_in(k, k_flip, sizeof(k_flip) / sizeof(k_flip[0]))) {
        rc = netem_parse_pct(v, &cfg->bitflip_pct);
    } else if (netem_span_in(k, k_scope, sizeof(k_scope) / sizeof(k_scope[0]))) {
        rc = netem_parse_scope(v, &cfg->bitflip_scope);
    } else if (netem_span_in(k, k_trunc, sizeof(k_trunc) / sizeof(k_trunc[0]))) {
        rc = netem_parse_pct(v, &cfg->truncate_pct);
    } else if (netem_span_in(k, k_rate, sizeof(k_rate) / sizeof(k_rate[0]))) {
        rc = netem_parse_kbps(v, &cfg->throttle_kbps);
    } else if (netem_span_is(k, "delay")) {
        rc = netem_parse_ms(v, &cfg->delay_ms);
    } else if (netem_span_is(k, "cable")) {
        rc = netem_parse_bool(v, &cfg->cable_pulled);
    } else if (netem_span_is(k, "seed")) {
        rc = netem_parse_int(v, 0xFFFFFFFFU, &cfg->seed);
    } else {
        rc = TS_E_INVAL;
    }
    return rc;
}

/** Next token of `str` from `*pos`; false when none is left. */
static bool netem_next_token(const char *str, size_t *pos, netem_span_t *tok)
{
    size_t i = *pos;
    bool found = false;
    while ((str[i] != '\0') && netem_is_sep(str[i])) {
        i++;
    }
    if ((str[i] != '\0') && (str[i] != '#')) {
        size_t start = i;
        while ((str[i] != '\0') && !netem_is_sep(str[i]) && (str[i] != '#')) {
            i++;
        }
        tok->p = &str[start];
        tok->n = i - start;
        found = true;
    }
    *pos = i;
    return found;
}

/** Split "key=value"; `*has_val` false for a bare word. */
static void netem_split_kv(netem_span_t tok, netem_span_t *k, netem_span_t *v, bool *has_val)
{
    size_t i = 0U;
    while ((i < tok.n) && !netem_ch_is(tok.p[i], '=')) {
        i++;
    }
    k->p = tok.p;
    k->n = i;
    if (i < tok.n) {
        v->p = &tok.p[i + 1U];
        v->n = tok.n - i - 1U;
        *has_val = true;
    } else {
        v->p = &tok.p[tok.n];
        v->n = 0U;
        *has_val = false;
    }
}

static ts_err_t netem_apply_token(netem_span_t tok, ts_netem_cfg_t *cfg)
{
    ts_err_t rc;
    netem_span_t k;
    netem_span_t v;
    bool has_val = false;
    netem_split_kv(tok, &k, &v, &has_val);
    if (has_val) {
        rc = netem_apply_kv(k, v, cfg);
    } else {
        rc = netem_apply_word(k, cfg);
    }
    return rc;
}

ts_err_t ts_netem_cfg_apply(const char *spec, ts_netem_cfg_t *cfg)
{
    ts_err_t rc = TS_OK;
    if ((spec == NULL) || (cfg == NULL)) {
        rc = TS_E_INVAL;
    } else {
        ts_netem_cfg_t tmp = *cfg;
        size_t pos = 0U;
        netem_span_t tok;
        while ((rc == TS_OK) && netem_next_token(spec, &pos, &tok)) {
            rc = netem_apply_token(tok, &tmp);
        }
        if (rc == TS_OK) {
            *cfg = tmp;
        }
    }
    return rc;
}

ts_err_t ts_netem_cfg_parse(const char *spec, ts_netem_cfg_t *cfg)
{
    ts_err_t rc = TS_E_INVAL;
    if ((spec != NULL) && (cfg != NULL)) {
        ts_netem_cfg_t tmp;
        (void)memset(&tmp, 0, sizeof(tmp));
        rc = ts_netem_cfg_apply(spec, &tmp);
        if (rc == TS_OK) {
            *cfg = tmp;
        }
    }
    return rc;
}

/* ---- text: scenario timeline ------------------------------------------------------ */

void ts_netem_script_init(ts_netem_script_t *s, const ts_netem_cfg_t *base)
{
    if (s != NULL) {
        (void)memset(s, 0, sizeof(*s));
        if (base != NULL) {
            s->cur = *base;
        }
        s->scenario.steps = s->steps;
        s->scenario.count = 0U;
    }
}

/** Append (or merge with a step at the same time). */
static void netem_script_emit(ts_netem_script_t *s, uint32_t at_ms, const ts_netem_cfg_t *cfg)
{
    if ((s->count > 0U) && (s->steps[s->count - 1U].at_ms == at_ms)) {
        s->steps[s->count - 1U].cfg = *cfg;
    } else if (s->count < TS_NETEM_SCRIPT_STEPS) {
        s->steps[s->count].at_ms = at_ms;
        s->steps[s->count].cfg = *cfg;
        s->count++;
    } else {
        /* capacity is checked by the caller before any emit */
    }
    s->scenario.steps = s->steps;
    s->scenario.count = s->count;
}

/** Steps a line will append (worst case), for the capacity check. */
static uint8_t netem_script_need(const ts_netem_script_t *s, uint32_t at_ms)
{
    uint8_t need = 1U;
    if (s->blackout && (s->blackout_end_ms <= at_ms)) {
        need++;
    }
    return need;
}

/** Pass 1 of a script line: `t=` and `blackout` tokens. */
static ts_err_t netem_script_scan(const char *line, bool *any, bool *has_t, uint32_t *at_ms, uint32_t *blackout_ms)
{
    ts_err_t rc = TS_OK;
    size_t pos = 0U;
    netem_span_t tok;
    while ((rc == TS_OK) && netem_next_token(line, &pos, &tok)) {
        netem_span_t k;
        netem_span_t v;
        bool has_val = false;
        *any = true;
        netem_split_kv(tok, &k, &v, &has_val);
        if (has_val && (netem_span_is(k, "t") || netem_span_is(k, "at"))) {
            rc = netem_parse_ms(v, at_ms);
            *has_t = true;
        } else if (netem_span_is(k, "blackout")) {
            if (!has_val) {
                /* "blackout 2s": the duration is the next token */
                if (!netem_next_token(line, &pos, &v)) {
                    rc = TS_E_INVAL;
                }
            }
            if (rc == TS_OK) {
                rc = netem_parse_ms(v, blackout_ms);
            }
            if ((rc == TS_OK) && (*blackout_ms == 0U)) {
                rc = TS_E_INVAL;
            }
        } else {
            /* link keys: pass 2 */
        }
    }
    return rc;
}

/** Pass 2 of a script line: every other token modifies `cfg`. */
static ts_err_t netem_script_apply(const char *line, ts_netem_cfg_t *cfg)
{
    ts_err_t rc = TS_OK;
    size_t pos = 0U;
    netem_span_t tok;
    while ((rc == TS_OK) && netem_next_token(line, &pos, &tok)) {
        netem_span_t k;
        netem_span_t v;
        bool has_val = false;
        netem_split_kv(tok, &k, &v, &has_val);
        if (has_val && (netem_span_is(k, "t") || netem_span_is(k, "at"))) {
            /* time: pass 1 */
        } else if (netem_span_is(k, "blackout")) {
            if (!has_val) {
                (void)netem_next_token(line, &pos, &v);   /* skip the duration */
            }
        } else {
            rc = netem_apply_token(tok, cfg);
        }
    }
    return rc;
}

ts_err_t ts_netem_script_line(ts_netem_script_t *s, const char *line)
{
    ts_err_t rc = TS_OK;
    bool any = false;
    bool has_t = false;
    uint32_t at_ms = 0U;
    uint32_t blackout_ms = 0U;
    ts_netem_cfg_t next;

    (void)memset(&next, 0, sizeof(next));
    if ((s == NULL) || (line == NULL)) {
        rc = TS_E_INVAL;
    } else {
        s->lines++;
        rc = netem_script_scan(line, &any, &has_t, &at_ms, &blackout_ms);
    }
    if ((rc == TS_OK) && any) {
        if (!has_t || (at_ms < s->last_at_ms)) {
            rc = TS_E_INVAL;
        } else if (((uint32_t)s->count + (uint32_t)netem_script_need(s, at_ms)) > TS_NETEM_SCRIPT_STEPS) {
            rc = TS_E_FULL;
        } else {
            next = s->cur;
            if ((s->count > 0U) && (at_ms > s->last_at_ms) && (next.burst_period_ms == 0U)) {
                /* a one-shot burst fires at its own step only; a later burst line re-arms it */
                next.burst_len = 0U;
            }
            rc = netem_script_apply(line, &next);
        }
        if (rc == TS_OK) {
            ts_netem_cfg_t emitted;
            if (s->blackout && (s->blackout_end_ms <= at_ms)) {
                netem_script_emit(s, s->blackout_end_ms, &s->cur);
                s->blackout = false;
            }
            s->cur = next;
            if (blackout_ms > 0U) {
                uint32_t end = at_ms + blackout_ms;
                if (!s->blackout || (end > s->blackout_end_ms)) {
                    s->blackout_end_ms = end;
                }
                s->blackout = true;
            }
            emitted = s->cur;
            if (s->blackout) {
                emitted.cable_pulled = true;
            }
            netem_script_emit(s, at_ms, &emitted);
            s->last_at_ms = at_ms;
        }
    }
    return rc;
}

const ts_netem_scenario_t *ts_netem_script_finish(ts_netem_script_t *s)
{
    const ts_netem_scenario_t *sc = NULL;
    if (s != NULL) {
        if (s->blackout) {
            if (s->count < TS_NETEM_SCRIPT_STEPS) {
                netem_script_emit(s, s->blackout_end_ms, &s->cur);
            } else {
                /* no room for the restore step: end the last step with the cable plugged */
                s->steps[s->count - 1U].cfg.cable_pulled = s->cur.cable_pulled;
            }
            s->blackout = false;
        }
        s->scenario.steps = s->steps;
        s->scenario.count = s->count;
        sc = &s->scenario;
    }
    return sc;
}
