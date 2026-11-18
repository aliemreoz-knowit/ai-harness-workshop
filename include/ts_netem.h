/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- ts_netem.h
 * Network emulation: a per-packet filter (loss, burst loss, reorder, duplicate,
 * bit flip, throttle, fixed delay, cable pull) with a scenario timeline. The
 * same filter is compiled twice:
 *   - tools/tsnetem: userspace UDP relay between a real daemon and receivers;
 *   - tsnetem_shim:  in-process shim that sits between two ts_io_fake_t
 *                    endpoints in loopback tests (ts_netem_pump).
 * Deterministic: xorshift32 PRNG seeded from the config, time injected.
 *
 * FROZEN API: additive, non-breaking changes only. No dynamic memory.
 */
#ifndef TS_NETEM_H
#define TS_NETEM_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "ts_config.h"
#include "ts_proto.h"
#include "ts_io.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct ts_netem_cfg {
    uint8_t  loss_pct;          /**< independent loss 0..100 */
    uint16_t burst_len;         /**< >0: drop this many consecutive packets ... */
    uint32_t burst_period_ms;   /**< ... every burst_period_ms (0 = once at activation) */
    uint8_t  reorder_pct;       /**< chance a packet is held back reorder_delay_ms */
    uint32_t reorder_delay_ms;
    uint8_t  dup_pct;           /**< chance a packet is delivered twice */
    uint8_t  bitflip_pct;       /**< chance one random bit of the packet is flipped */
    uint32_t throttle_kbps;     /**< 0 = unlimited; excess is dropped (token bucket) */
    uint32_t delay_ms;          /**< fixed one-way delay */
    bool     cable_pulled;      /**< drop everything */
    uint32_t seed;              /**< PRNG seed (0 = 1) */
    /* --- additive (tsnetem v2) -------------------------------------------- */
    uint8_t  truncate_pct;      /**< chance a datagram is cut short (ts_netem_filter_len, pump, relay) */
    uint8_t  bitflip_scope;     /**< TS_NETEM_FLIP_ANY / _HEADER / _PAYLOAD: where a flipped bit lands */
} ts_netem_cfg_t;

/* bitflip_scope values (additive) */
#define TS_NETEM_FLIP_ANY      0U   /**< any byte of the datagram */
#define TS_NETEM_FLIP_HEADER   1U   /**< first TS_HDR_LEN bytes only */
#define TS_NETEM_FLIP_PAYLOAD  2U   /**< bytes after TS_HDR_LEN (ANY for datagrams <= TS_HDR_LEN) */

/** Hold for a reordered datagram when reorder_delay_ms is 0 (< TS_NACK_DELAY_MS: pure reorder never NACKs). */
#define TS_NETEM_REORDER_DEFAULT_MS 10U
/** Throttle bucket depth: this many ms of traffic at throttle_kbps (at least 2 x TS_IO_DGRAM_MAX bytes). */
#define TS_NETEM_THROTTLE_BURST_MS  100U

typedef enum ts_netem_verdict {
    TS_NETEM_PASS = 0,
    TS_NETEM_DROP,
    TS_NETEM_DUP          /**< deliver twice */
} ts_netem_verdict_t;

typedef struct ts_netem_step {
    uint32_t       at_ms;    /**< relative to scenario start */
    ts_netem_cfg_t cfg;
} ts_netem_step_t;

typedef struct ts_netem_scenario {
    const ts_netem_step_t *steps;   /**< sorted by at_ms */
    uint8_t                count;
} ts_netem_scenario_t;

#define TS_NETEM_HOLD 64U   /**< delayed / reordered datagrams held per direction */

typedef struct ts_netem_held {
    bool          used;
    uint32_t      release_ms;
    ts_io_fake_t *dest;      /**< endpoint that receives it on release */
    ts_io_dgram_t d;
} ts_netem_held_t;

typedef struct ts_netem_stats {
    uint32_t seen;
    uint32_t passed;
    uint32_t dropped;
    uint32_t dropped_burst;
    uint32_t dropped_throttle;
    uint32_t dropped_cable;
    uint32_t duplicated;
    uint32_t reordered;
    uint32_t flipped;
    uint32_t held_overflow;  /**< delayed packets dropped because the hold table was full */
    uint32_t misrouted;      /**< datagrams whose `to` matched no endpoint */
    uint32_t truncated;      /**< additive: datagrams cut short (truncate_pct) */
} ts_netem_stats_t;

typedef struct ts_netem {
    ts_netem_cfg_t   cfg;
    uint32_t         rng;
    uint32_t         burst_left;       /**< packets still to drop in the current burst */
    uint32_t         next_burst_ms;
    uint32_t         throttle_tokens;  /**< bytes */
    uint32_t         throttle_last_ms;
    ts_netem_held_t  held[TS_NETEM_HOLD];
    ts_netem_stats_t st;
    uint32_t         scenario_start_ms;
    uint8_t          scenario_step;    /**< next step to apply */
    const ts_netem_scenario_t *scenario;
} ts_netem_t;

/** Clean link with the given config at `now_ms`. */
void ts_netem_init(ts_netem_t *n, const ts_netem_cfg_t *cfg, uint32_t now_ms);
/** Replace the config at run time (keeps PRNG and stats). */
void ts_netem_set(ts_netem_t *n, const ts_netem_cfg_t *cfg, uint32_t now_ms);
/** Attach a scenario starting at `now_ms`; steps are applied by ts_netem_tick/pump. */
void ts_netem_scenario_start(ts_netem_t *n, const ts_netem_scenario_t *s, uint32_t now_ms);
/** Apply due scenario steps (called by pump; exposed for the relay tool). */
void ts_netem_tick(ts_netem_t *n, uint32_t now_ms);
/**
 * Decide for one datagram (may mutate `data` for bit flips). `*delay_ms`
 * receives the extra delay (fixed delay and/or reorder hold) to apply on PASS/DUP.
 */
ts_netem_verdict_t ts_netem_filter(ts_netem_t *n, uint8_t *data, uint16_t len, uint32_t now_ms, uint32_t *delay_ms);
/**
 * Loopback shim: move a.udp_out -> b.udp_in and b.udp_out -> a.udp_in through
 * the filter (a datagram is delivered only when its `to` equals the
 * destination's `self`; otherwise it is counted in `misrouted`), releasing
 * held datagrams whose time has come. Call once per virtual tick after both
 * endpoints ticked. Both directions pass through the same filter.
 */
void ts_netem_pump(ts_netem_t *n, ts_io_fake_t *a, ts_io_fake_t *b, uint32_t now_ms);
/**
 * Star variant for multi-client scenarios: hub.udp_out is routed by `to` to
 * the leaf whose `self` matches; every leaf.udp_out goes to the hub.
 */
void ts_netem_pump_star(ts_netem_t *n, ts_io_fake_t *hub, ts_io_fake_t *const *leaves, uint8_t nleaves, uint32_t now_ms);
const ts_netem_stats_t *ts_netem_stats(const ts_netem_t *n);
/** Parse "loss=5,burst=50,reorder=2,delay=10,cable=1,..." into cfg (tsnetem CLI and tests). Errors: TS_E_INVAL. */
ts_err_t ts_netem_cfg_parse(const char *spec, ts_netem_cfg_t *cfg);
/** xorshift32 step, exposed so tests can predict decisions. */
uint32_t ts_netem_rand(uint32_t *state);

/* ---- Additive API (tsnetem v2) ----------------------------------------------
 * Filter order: cable pull, burst, loss, throttle, bitflip, truncate, delay +
 * reorder hold, dup; evaluation stops at the first drop. One xorshift32 draw
 * per ENABLED random feature reached, in this order: loss, bitflip (decision,
 * then bit position), truncate (decision, then new length), reorder, dup; a
 * feature hits when `rand % 100 < pct`. Cable pull, burst and throttle never
 * draw. A burst is armed by init/set when burst_len (> 0) or burst_period_ms
 * changed, re-armed every period, and always runs to completion. The shim
 * releases held datagrams (delay / reorder) in arrival order once due.
 */

/**
 * Same decision as ts_netem_filter() but may also shorten the datagram
 * (truncate_pct): `*len` is updated in place (1 <= new len < old len).
 * ts_netem_filter() never truncates because its caller cannot learn the new length.
 */
ts_netem_verdict_t ts_netem_filter_len(ts_netem_t *n, uint8_t *data, uint16_t *len, uint32_t now_ms, uint32_t *delay_ms);
/** Start a burst of `len` consecutive drops now (live control, tests). */
void ts_netem_burst_now(ts_netem_t *n, uint16_t len);
/**
 * Like ts_netem_cfg_parse() but modifies only the keys present in `spec`
 * (live slider, scenario lines); `cfg` is untouched on error. Errors: TS_E_INVAL.
 * Keys: loss|drop, burst, burst_period|period, reorder, reorder_delay|hold,
 * dup, bitflip|flip|corrupt, flip_scope|scope (any|header|payload),
 * truncate|trunc, throttle|kbps|rate (suffix kbps|mbps), delay, cable
 * (0|1|on|off), seed; bare words: clean (all impairments off, seed kept),
 * cable|pull (cable=1). Times take ms|s suffixes ("2s", "1.5s", "250ms"),
 * percentages an optional '%'. Separators: ',', ';', blanks.
 */
ts_err_t ts_netem_cfg_apply(const char *spec, ts_netem_cfg_t *cfg);

/** Scenario timeline built from text lines ("t=5s loss=5%", "t=20s blackout 2s"). */
#define TS_NETEM_SCRIPT_STEPS 64U

typedef struct ts_netem_script {
    ts_netem_step_t     steps[TS_NETEM_SCRIPT_STEPS];
    ts_netem_scenario_t scenario;          /**< points at `steps` after ts_netem_script_finish */
    ts_netem_cfg_t      cur;               /**< link state after the last line (blackouts excluded) */
    uint8_t             count;
    bool                blackout;          /**< a blackout window is open */
    uint32_t            blackout_end_ms;
    uint32_t            last_at_ms;
    uint32_t            lines;             /**< lines fed (for error messages) */
} ts_netem_script_t;

/** Empty timeline; steps are cumulative on top of `base` (NULL = clean link). */
void ts_netem_script_init(ts_netem_script_t *s, const ts_netem_cfg_t *base);
/**
 * Feed one line: "t=<time> [key=value ...] [blackout <dur> | blackout=<dur>]".
 * A blackout pulls the cable for <dur>, then restores the state of the
 * timeline. '#' starts a comment; blank lines are ignored; times must not
 * decrease. A one-shot burst (burst_period 0) fires once, at its own line.
 * Errors: TS_E_INVAL (syntax, missing t=, time going backwards), TS_E_FULL
 * (more than TS_NETEM_SCRIPT_STEPS steps). `s` is unchanged on error
 * (except `lines`, which counts every line fed).
 */
ts_err_t ts_netem_script_line(ts_netem_script_t *s, const char *line);
/** Close an open blackout and return the scenario for ts_netem_scenario_start(). */
const ts_netem_scenario_t *ts_netem_script_finish(ts_netem_script_t *s);

#ifdef __cplusplus
}
#endif

#endif /* TS_NETEM_H */
