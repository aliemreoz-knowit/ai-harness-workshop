/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/support/ts_lbcheck.h
 * Loopback scenario checker on top of ts_loop: steps the loop and, after
 * every step, records the frame the daemon just encoded (raw capture and the
 * encoder's reconstructed reference, keyed by frame_seq) and compares every
 * frame a receiver decodes against it. Also measures windows: decoded frames
 * per receiver (average and worst whole second), daemon bytes/packets.
 *
 *   static ts_loop_t L; static ts_lb_t lb; ts_lb_window_t w;
 *   ts_loop_init(&L, &c); ts_lb_attach(&lb, &L);
 *   ts_lb_run(&lb, 2000);                 // warm-up
 *   ts_lb_window(&lb, 5000, &w);          // measured
 *   TEST_ASSERT(w.min_fps_1s[0] >= 29);
 *   TEST_ASSERT(lb.rx[0].exact_recon == lb.rx[0].compared);
 *
 * Exact match: `exact_raw` counts decodes identical to the captured frame
 * (end-to-end exact match, quality 1 only); `exact_recon` counts decodes
 * identical to the encoder's reconstructed reference (must ALWAYS hold:
 * a mismatch is a displayed wrong picture, whatever the quality step).
 * Test glue only: no protocol logic, no dynamic memory, no stdio.
 */
#ifndef TS_LBCHECK_H
#define TS_LBCHECK_H

#include <stdint.h>
#include <stdbool.h>
#include "ts_config.h"
#include "ts_loop.h"

#ifdef __cplusplus
extern "C" {
#endif

#define TS_LB_TRUTH_SLOTS 32U   /**< ~1 s of encoded frames kept for comparison */

typedef struct ts_lb_truth {
    bool     used;
    uint16_t frame_seq;
    uint8_t  raw[TS_FRAME_BYTES];     /**< tsd_t.frame when it was encoded */
    uint8_t  recon[TS_FRAME_BYTES];   /**< tsd_t.enc.ref right after encoding it */
} ts_lb_truth_t;

typedef struct ts_lb_rx {
    uint32_t decoded_seen;     /**< frames_decoded at the last check */
    uint32_t compared;         /**< observed decodes that had a truth to compare with */
    uint32_t exact_raw;
    uint32_t exact_recon;
    uint32_t no_truth;         /**< decode observed but its frame_seq not recorded */
    uint32_t multi;            /**< steps in which more than one frame was decoded */
    bool     have_last;
    uint16_t last_seq;         /**< frame_seq of the last observed decode */
    uint32_t last_decode_ms;
    uint32_t first_decode_ms;
    uint32_t max_gap_ms;       /**< longest gap between two observed decodes */
    uint32_t mismatch_seq;     /**< frame_seq of the first recon mismatch (+1, 0 = none) */
} ts_lb_rx_t;

typedef struct ts_lb {
    ts_loop_t     *L;
    bool           have_seq;
    uint16_t       seq_next_seen;     /**< tsd_t.frame_seq_next at the last check */
    uint32_t       recorded;          /**< frames recorded */
    uint32_t       skipped;           /**< frames encoded but not recorded (several per step) */
    uint32_t       daemon_stopped;    /**< steps after which the daemon was not running */
    uint32_t       rx_closed;         /**< steps after which a receiver was CLOSED */
    ts_lb_truth_t  truth[TS_LB_TRUTH_SLOTS];
    ts_lb_rx_t     rx[TS_LOOP_MAX_RX];
} ts_lb_t;

typedef struct ts_lb_window {
    uint32_t ms;
    uint32_t decoded[TS_LOOP_MAX_RX];      /**< frames decoded in the window */
    uint32_t fps_x100[TS_LOOP_MAX_RX];     /**< average over the window */
    uint32_t min_fps_1s[TS_LOOP_MAX_RX];   /**< fewest frames decoded in any whole second */
    uint32_t max_fps_1s[TS_LOOP_MAX_RX];
    uint32_t daemon_bytes;                 /**< tsd_stats bytes_sent delta */
    uint32_t daemon_kbps;                  /**< daemon_bytes * 8 / ms */
    uint32_t daemon_pkts;                  /**< fresh packets */
    uint32_t daemon_retx;
    uint32_t rx_pkts[TS_LOOP_MAX_RX];
    uint32_t rx_nacks[TS_LOOP_MAX_RX];
} ts_lb_window_t;

/** Reset the checker for a freshly initialised loop. */
void     ts_lb_attach(ts_lb_t *lb, ts_loop_t *L);
/** One ts_loop_step() followed by recording + comparison. */
void     ts_lb_step(ts_lb_t *lb);
/** Step for `ms` virtual milliseconds. */
void     ts_lb_run(ts_lb_t *lb, uint32_t ms);
/** Step for `ms` and measure (ms >= 1000 for the per-second numbers). */
void     ts_lb_window(ts_lb_t *lb, uint32_t ms, ts_lb_window_t *w);
/** frames_decoded of receiver i (0 when absent). */
uint32_t ts_lb_decoded(const ts_lb_t *lb, uint8_t i);
/** Truth recorded for frame_seq (NULL when not / no longer held). */
const ts_lb_truth_t *ts_lb_truth(const ts_lb_t *lb, uint16_t frame_seq);
/** exact * 100 >= compared * pct (and compared > 0). */
bool     ts_lb_exact_at_least(uint32_t exact, uint32_t compared, uint32_t pct);

#ifdef __cplusplus
}
#endif

#endif /* TS_LBCHECK_H */
