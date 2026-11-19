/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tools/tsrecv/lib/tsr_internal.h
 * Private interface between the receiver translation units (libtsr):
 *   receiver.c  public API, lifecycle, tick orchestration, TCP control client
 *   reasm.c     UDP ingest, reassembly table, late-frame policy, decode pass
 *   nack.c      pkt_seq gap tracking, frame -> pkt_seq ranges, NACK rounds, RTT
 *   metrics.c   freeze / fps / kbps / loss / latency / jitter / truth / PSNR, JSON
 * Nothing here is part of the frozen API; tests may include it to check the
 * pure helpers (PSNR, one-way delay, jitter step).
 */
#ifndef TSR_INTERNAL_H
#define TSR_INTERNAL_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "ts_receiver.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- tuning (not wire format) --------------------------------------------- */
#define TSR_DRAIN_MAX          1024U   /**< datagrams drained per tick */
#define TSR_GAP_TRACK_MAX      256U    /**< larger pkt_seq jumps are an outage: counted lost, not NACKed */
#define TSR_NACK_ROUNDS        2U      /**< NACK rounds before giving up on a packet */
#define TSR_RTT_INIT_MS        40U     /**< RTT estimate before the first sample */
#define TSR_RETRY_MIN_MS       10U     /**< lower bound of the NACK retry interval */
#define TSR_STALE_MS           1000U   /**< awaited frame without progress for this long is dropped */
#define TSR_LATE_RESYNC        64U     /**< consecutive late fresh packets -> stream resync */
#define TSR_WELCOME_TIMEOUT_MS 3000U   /**< HELLO without WELCOME -> reconnect */
#define TSR_KEEPALIVE_MS       1000U   /**< empty NACK when nothing else went to the daemon */
#define TSR_RECONNECT_DEF_MS   1000U
#define TSR_SILENCE_MS         3000U   /**< STREAMING without any UDP or TLV (no STATS either): re-HELLO */

/* ---- log event codes (module TS_MOD_RECEIVER) ------------------------------ */
#define TSR_EV_CONNECT      1U
#define TSR_EV_WELCOME      2U
#define TSR_EV_DISCONNECT   3U
#define TSR_EV_KEYREQ       4U
#define TSR_EV_DROP         5U
#define TSR_EV_DECODE_ERR   6U
#define TSR_EV_GIVEUP       7U
#define TSR_EV_RESYNC       8U
#define TSR_EV_CTRL_ERR     9U
#define TSR_EV_EVICT        10U

/* ---- receiver.c -------------------------------------------------------------- */
/** Control connection is up (TCP handle valid, not closing). */
bool tsr_i_connected(const tsr_t *r);

/* ---- reasm.c ----------------------------------------------------------------- */
void    tsr_i_udp_drain(tsr_t *r, uint32_t now_ms);
/** One validated DATA packet (header already parsed and CRC-checked). */
void    tsr_i_rx_packet(tsr_t *r, const ts_hdr_t *h, const uint8_t *payload, uint32_t now_ms);
/** Decode every frame that is ready, apply the late-frame policy and abandon timeouts. */
void    tsr_i_decode_pass(tsr_t *r, uint32_t now_ms);
/** Slot index holding `frame_seq`, or -1. */
int32_t tsr_i_find_slot(const tsr_t *r, uint16_t frame_seq);
/** Forget all stream state (new stream_id or resync): slots, gaps, decoder reference. */
void    tsr_i_stream_reset(tsr_t *r, uint32_t now_ms);
/** Awaited-frame give-up time without progress (ms). */
uint32_t tsr_i_frame_timeout(const tsr_t *r);

/* ---- nack.c ------------------------------------------------------------------ */
/** Serial pkt_seq gap detection; fills gaps on late / retransmitted packets. */
void     tsr_i_gap_track(tsr_t *r, uint16_t pkt_seq, bool retransmit, uint32_t now_ms);
/** Remember that frame `frame_seq` occupies pkt_seq [base, base + count). */
void     tsr_i_frange_learn(tsr_t *r, uint16_t frame_seq, uint16_t base, uint8_t count);
/** Map a missing pkt_seq to (frame_seq, frag_idx); false when unknown or ambiguous. */
bool     tsr_i_map_pkt(const tsr_t *r, uint16_t pkt_seq, uint16_t *frame_seq, uint8_t *frag_idx);
/** Age the missing list: NACK rounds (chunked to TS_NACK_MAX), give-ups, expiry. */
void     tsr_i_nack_tick(tsr_t *r, uint32_t now_ms);
/** Empty NACK as UDP keep-alive when nothing reached the daemon for a while. */
void     tsr_i_keepalive(tsr_t *r, uint32_t now_ms);
/** NACK retry interval derived from the RTT estimate (~1 RTT + margin). */
uint32_t tsr_i_retry_ival(const tsr_t *r);
/** Drop all missing entries and frame ranges. */
void     tsr_i_nack_reset(tsr_t *r);

/* ---- metrics.c --------------------------------------------------------------- */
/**
 * A frame was decoded into r->x.scratch (r->frame still holds the previous
 * output): freeze end, fps window, latency, one-way delay, jitter, tiles,
 * truth / PSNR. The caller copies scratch into r->frame afterwards.
 */
void     tsr_i_on_decoded(tsr_t *r, const tsr_reasm_t *s, const ts_frame_info_t *info, uint32_t now_ms);
/** Per-tick metrics: uptime, freeze onset, 1 s windows, loss ratios, state. */
void     tsr_i_metrics_tick(tsr_t *r, uint32_t now_ms);
/** PSNR between two TS_FRAME_BYTES frames in centi-dB; 0 when identical. No libm. */
uint32_t tsr_i_psnr_cdb(const uint8_t *a, const uint8_t *b);
/** One-way delay rx_ms - ts_ms in serial arithmetic (ts_ms wraps at 65536). */
int32_t  tsr_i_owd_ms(uint32_t rx_ms, uint16_t ts_ms);
/** RFC 3550 style jitter step on a x16 fixed-point estimate: J += (|d| - J) / 16. */
uint32_t tsr_i_jitter_step(uint32_t jitter_x16, int32_t d_ms);

#ifdef __cplusplus
}
#endif

#endif /* TSR_INTERNAL_H */
