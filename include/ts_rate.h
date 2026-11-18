/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- ts_rate.h
 * Rate control: token bucket (per client and global), loss adaptation
 * (NACK ratio -> quality step) and the keyframe scheduler. PROTOCOL.md section 8.
 *
 * FROZEN API: additive, non-breaking changes only.
 * Rules: pure functions of their state + `now_ms` (injected clock), no
 * dynamic memory, no stdio, u32 arithmetic with explicit wrap handling.
 * Units: kbps = 1000 bits per second; buckets count BYTES.
 */
#ifndef TS_RATE_H
#define TS_RATE_H

#include <stdint.h>
#include <stdbool.h>
#include "ts_config.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- Token bucket ---------------------------------------------------------- */
typedef struct ts_bucket {
    uint32_t kbps;          /**< refill rate */
    uint32_t burst_bytes;   /**< capacity */
    uint32_t tokens;        /**< available bytes, <= burst_bytes */
    uint32_t last_ms;       /**< last refill time */
    uint32_t taken;         /**< bytes granted (stats) */
    uint32_t refused;       /**< take() calls refused (stats) */
} ts_bucket_t;

/** Full bucket at `now_ms`. kbps 0 means "unlimited" (take always succeeds). */
void     ts_bucket_init(ts_bucket_t *b, uint32_t kbps, uint32_t burst_bytes, uint32_t now_ms);
void     ts_bucket_set_rate(ts_bucket_t *b, uint32_t kbps);
/** Add tokens for the elapsed time (serial u32 difference), capped at burst. */
void     ts_bucket_refill(ts_bucket_t *b, uint32_t now_ms);
/** Consume `bytes` if available; false (and `refused`++) otherwise. */
bool     ts_bucket_take(ts_bucket_t *b, uint32_t bytes);
uint32_t ts_bucket_tokens(const ts_bucket_t *b);
/**
 * (additive) Default bucket capacity: one worst-case frame on the wire
 * (TS_MAX_FRAGS full DATA packets), PROTOCOL.md 8 "burst = 1 frame".
 */
#define TS_BUCKET_BURST_BYTES   (TS_MAX_FRAGS * TS_PKT_MAX)
/**
 * (additive) Take `bytes` from the per-client bucket `b` AND the global cap
 * `global` (may be NULL): both or neither. On refusal only the bucket(s) that
 * lacked tokens count `refused`. Neither bucket is refilled here.
 */
bool     ts_bucket_take_pair(ts_bucket_t *b, ts_bucket_t *global, uint32_t bytes);
/** Milliseconds until `bytes` tokens are available (0 if now). */
uint32_t ts_bucket_wait_ms(const ts_bucket_t *b, uint32_t bytes);

/* ---- Loss adaptation ------------------------------------------------------- */
typedef struct ts_loss {
    uint32_t window_start_ms;
    uint32_t pkts;          /**< fresh packets sent in the window */
    uint32_t nacks;         /**< NACK entries received in the window */
    uint8_t  quality;       /**< current step 1/2/4 */
    uint32_t steps_down;
    uint32_t steps_up;
} ts_loss_t;

void    ts_loss_init(ts_loss_t *l, uint32_t now_ms);
void    ts_loss_on_sent(ts_loss_t *l, uint32_t pkts);
void    ts_loss_on_nack(ts_loss_t *l, uint32_t entries);
/**
 * Called every tick. When a TS_LOSS_WINDOW_MS window closes: NACK ratio
 * > TS_LOSS_STEP_DOWN_PCT steps quality down (1->2->4), a window with no NACKs
 * steps it up (4->2->1). Returns the (possibly new) quality.
 */
uint8_t ts_loss_update(ts_loss_t *l, uint32_t now_ms);
/** NACK ratio of the current window in percent. */
uint32_t ts_loss_ratio_pct(const ts_loss_t *l);

/* ---- Keyframe scheduler ---------------------------------------------------- */
typedef enum ts_key_reason {
    TS_KEY_NONE = 0,
    TS_KEY_PERIODIC,     /**< TS_KEYFRAME_PERIOD_MS elapsed */
    TS_KEY_REQUEST,      /**< KEYFRAME_REQ from a client (rate-limited by cooldown) */
    TS_KEY_TOO_OLD,      /**< a NACK could not be served from the ring */
    TS_KEY_SCENE,        /**< changed tiles > TS_SCENE_CHANGE_PCT */
    TS_KEY_FORCED        /**< first frame / explicit force */
} ts_key_reason_t;

typedef struct ts_keysched {
    uint32_t last_key_ms;    /**< when the last keyframe was sent */
    uint32_t period_ms;      /**< TS_KEYFRAME_PERIOD_MS */
    uint32_t cooldown_ms;    /**< TS_KEYFRAME_COOLDOWN_MS */
    bool     requested;      /**< pending request / too-old flag */
    bool     forced;
    uint32_t requests_total;
    uint32_t requests_dropped; /**< requests swallowed by the cooldown */
    ts_key_reason_t last_reason;
    ts_key_reason_t pending_reason; /**< (additive) reason of the pending request */
} ts_keysched_t;

void ts_keysched_init(ts_keysched_t *k, uint32_t now_ms);
/** A client asked (KEYFRAME_REQ) or a NACK was too old. */
void ts_keysched_request(ts_keysched_t *k, ts_key_reason_t why);
/** Unconditional keyframe on the next frame. */
void ts_keysched_force(ts_keysched_t *k);
/**
 * Decide for the frame about to be encoded. `changed_tiles` is the encoder's
 * count for this frame. Returns the reason, TS_KEY_NONE for a P-frame.
 * Requests within `cooldown_ms` of the last keyframe are deferred (not lost).
 */
ts_key_reason_t ts_keysched_decide(ts_keysched_t *k, uint32_t now_ms, uint16_t changed_tiles);
/** Record that a keyframe was sent at `now_ms`. */
void ts_keysched_sent(ts_keysched_t *k, uint32_t now_ms);

/* ---- (additive) Congestion control --------------------------------------------
 * Fits the stream to the slowest viewer. Per TS_LOSS_WINDOW_MS window the daemon
 * reports every pushed frame (wire bytes) and, every tick, the worst client
 * backlog (time its cursor needs to drain at its rate). At the end of a window
 * the stream rate is estimated from the P-frames (keyframes are periodic
 * bursts, not the steady rate): above TS_CC_HIGH_PCT of the limit, or with a
 * standing queue (the smallest backlog seen in the whole window) above
 * TS_CC_QUEUE_MS, quality steps down (1 -> 2 -> 4); quality steps up one level
 * only when twice the current estimate fits below TS_CC_LOW_PCT of the limit
 * and there is no standing queue. A backlog above TS_CC_RESET_MS is not worth
 * sending any more: the daemon drops it (the cursor jumps to the newest
 * keyframe, or a keyframe is requested).
 */
#define TS_CC_HIGH_PCT   90U     /**< estimated stream above this % of the limit steps quality down */
#define TS_CC_LOW_PCT    85U     /**< the next better quality must fit below this % to step up */
#define TS_CC_QUEUE_MS   200U    /**< standing queue (min backlog over a window) that steps quality down */
#define TS_CC_RESET_MS   1000U   /**< a client backlog longer than this is dropped by the daemon */

typedef struct ts_cc {
    uint32_t window_start_ms;
    uint32_t p_bytes;          /**< wire bytes of the P-frames pushed in the window */
    uint32_t p_frames;         /**< P-frames pushed in the window */
    uint32_t frames;           /**< all frames pushed in the window */
    uint32_t min_backlog_ms;   /**< smallest worst-client backlog reported in the window */
    bool     have_backlog;     /**< min_backlog_ms is valid */
    uint8_t  quality;          /**< current quality floor 1/2/4 */
    uint32_t est_kbps;         /**< stream estimate of the last closed window */
    uint32_t steps_down;
    uint32_t steps_up;
    uint32_t resets;           /**< client backlogs dropped (counted by the daemon) */
} ts_cc_t;

void     ts_cc_init(ts_cc_t *cc, uint32_t now_ms);
/** A frame of `wire_bytes` (payload + DATA headers) was pushed into the ring. */
void     ts_cc_on_frame(ts_cc_t *cc, uint32_t wire_bytes, bool keyframe);
/** Worst client backlog in ms at this tick (see ts_cc_backlog_ms). */
void     ts_cc_on_backlog(ts_cc_t *cc, uint32_t backlog_ms);
/**
 * Called every tick. When a TS_LOSS_WINDOW_MS window closes the quality floor
 * is adapted to `limit_kbps` (the slowest viewer's share; 0 = no limit, quality
 * may only step up). Returns the (possibly new) quality floor.
 */
uint8_t  ts_cc_update(ts_cc_t *cc, uint32_t now_ms, uint32_t limit_kbps);
/** Time in ms to send `pending_pkts` full DATA packets at `kbps` (0 when kbps is 0). */
uint32_t ts_cc_backlog_ms(uint32_t pending_pkts, uint32_t kbps);

#ifdef __cplusplus
}
#endif

#endif /* TS_RATE_H */
