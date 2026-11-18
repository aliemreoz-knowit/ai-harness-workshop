/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- ts_receiver.h
 * Receiver core (libtsr, tools/tsrecv/lib): TCP control client (HELLO ->
 * WELCOME, STATS, KEYFRAME_REQ, SET_RATE, BYE), UDP reassembly of up to
 * TS_REASM_FRAMES frames in flight, duplicate drop, late-frame policy, NACK
 * generation, decoder and metrics. Like the daemon it
 * only touches the network through ts_io_t and time through `now_ms`.
 *
 * FROZEN API: additive, non-breaking changes only.
 * The receiver is a laptop tool: it may use the heap in tsr_create(), but
 * never in tsr_tick(); tsr_init() places it in caller storage for tests.
 */
#ifndef TS_RECEIVER_H
#define TS_RECEIVER_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "ts_config.h"
#include "ts_proto.h"
#include "ts_codec.h"
#include "ts_io.h"
#include "ts_log.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum tsr_late_policy {
    TSR_LATE_DROP = 0,   /**< a frame older than the last decoded one is dropped */
    TSR_LATE_DECODE = 1  /**< decode anyway if it is the reference the decoder waits for */
} tsr_late_policy_t;

typedef struct tsr_cfg {
    ts_addr_t         server;           /**< daemon TCP control address */
    uint16_t          udp_port;         /**< local UDP port announced in HELLO */
    uint32_t          kbps;             /**< requested rate (0 = daemon default) */
    uint32_t          nack_delay_ms;    /**< 0 = TS_NACK_DELAY_MS */
    uint32_t          nack_retry_ms;    /**< 0 = TS_NACK_RETRY_MS */
    uint32_t          reconnect_ms;     /**< TCP reconnect backoff, 0 = 1000 */
    uint8_t           max_nack_per_tick;/**< 0 = TS_NACK_MAX */
    tsr_late_policy_t late_policy;
    bool              no_nack;          /**< disable NACKs (measure raw loss) */
} tsr_cfg_t;

typedef enum tsr_state {
    TSR_DISCONNECTED = 0,
    TSR_CONNECTING,
    TSR_HELLO_SENT,
    TSR_STREAMING,
    TSR_CLOSED
} tsr_state_t;

/** One frame being reassembled. */
typedef struct tsr_reasm {
    bool     used;
    bool     complete;
    uint16_t frame_seq;
    uint8_t  frag_count;
    uint8_t  frags_have;
    uint8_t  flags;
    uint16_t ts_ms;
    uint32_t frame_len;
    uint32_t first_rx_ms;
    uint32_t last_rx_ms;
    uint32_t nack_sent_ms;              /**< last NACK for this frame (0 = none) */
    uint8_t  have[TS_MAX_FRAGS];        /**< 1 when fragment i arrived */
    uint16_t pkt_seq[TS_MAX_FRAGS];     /**< pkt_seq of fragment i (for gap detection) */
    uint8_t  data[TS_ENC_MAX];
} tsr_reasm_t;

/** Metrics exported as metrics.json by tsrecv. Rates are over the last second. */
typedef struct tsr_metrics {
    uint32_t fps_x100;         /**< decoded frames per second x100 */
    uint32_t kbps;             /**< received UDP bytes over the last second */
    uint32_t pkts_rx;
    uint32_t pkts_lost;        /**< pkt_seq gaps never filled */
    uint32_t dups;
    uint32_t crc_fail;
    uint32_t parse_fail;
    uint32_t nacks_sent;       /**< NACK entries sent */
    uint32_t retx_rx;          /**< packets with TS_F_RETRANSMIT */
    uint32_t frames_complete;
    uint32_t frames_decoded;
    uint32_t frames_dropped;   /**< late or evicted incomplete */
    uint32_t decode_errors;
    uint32_t keyframes;
    uint32_t keyframe_reqs;    /**< KEYFRAME_REQ sent */
    uint32_t freezes;          /**< gaps > TS_FREEZE_MS without a decoded frame */
    uint32_t freeze_ms_total;
    uint32_t latency_ms;       /**< first fragment -> decode, last frame */
    uint32_t latency_avg_ms;
    uint32_t jitter_ms;        /**< mean abs deviation of inter-frame arrival */
    uint32_t exact_match_pct;  /**< vs reference set by tsr_set_truth (0 when none) */
    uint32_t psnr_cdb;         /**< PSNR in centi-dB vs truth, 0 when none/identical */
    uint16_t changed_tiles;    /**< last frame */
    uint8_t  tile_bitmap[TS_TILE_BITMAP_BYTES];
    uint32_t stats_rx;         /**< STATS TLVs received */
    ts_tlv_stats_t last_stats; /**< last daemon STATS */
    uint32_t reconnects;
    uint32_t ctrl_errors;
    /* ---- additive (receiver implementation): appended, never reordered ---- */
    uint32_t uptime_ms;        /**< since the first tsr_tick() */
    uint32_t pkts_expected;    /**< pkt_seq span covered so far (received + gaps) */
    uint32_t pkts_missing;     /**< pkt_seq gaps detected (before repair) */
    uint32_t loss_pct_x100;    /**< raw link loss: pkts_missing / pkts_expected, x100 */
    uint32_t residual_loss_pct_x100; /**< after repair: pkts_lost / pkts_expected, x100 */
    uint32_t late_pkts;        /**< packets for frames already decoded or skipped */
    uint32_t nack_giveups;     /**< missing packets abandoned after the last NACK round */
    uint32_t rtt_ms;           /**< NACK -> retransmit round-trip estimate */
    int32_t  owd_ms;           /**< arrival - ts_ms of the last decoded frame (serial; meaningful with a shared clock) */
    uint32_t freeze_ms_current;/**< length of the ongoing freeze, 0 when none */
    uint32_t truth_frames;     /**< decoded frames compared against ground truth */
    uint32_t truth_exact;      /**< ... of which bit-exact */
    uint32_t stream_id;        /**< from WELCOME (0 before) */
    uint8_t  state;            /**< tsr_state_t at the last tick */
    uint8_t  psnr_src;         /**< TSR_PSNR_NONE / _TRUTH / _KEYFRAME */
} tsr_metrics_t;

/** Source of tsr_metrics_t.psnr_cdb (additive). */
#define TSR_PSNR_NONE      0U
#define TSR_PSNR_TRUTH     1U   /**< vs ground truth (tsr_set_truth / tsr_set_truth_fn) */
#define TSR_PSNR_KEYFRAME  2U   /**< last P-frame output vs the keyframe that followed it */

/* ---- Receiver-private state (additive; appended to tsr_t as `x`) ---------- */
#define TSR_MISS_CAP     512U   /**< tracked missing pkt_seq entries */
#define TSR_FRANGE_CAP   32U    /**< remembered frame_seq -> pkt_seq ranges */
#define TSR_TX_CAP       256U   /**< queued control bytes (TLVs) */

/**
 * Ground-truth provider (additive): fill `truth` (TS_FRAME_BYTES) with the
 * expected content of decoded frame `frame_seq`; return false when unknown.
 */
typedef bool (*tsr_truth_fn_t)(void *ctx, uint16_t frame_seq, uint8_t *truth);

/** One missing pkt_seq waiting for repair. */
typedef struct tsr_miss {
    uint16_t pkt_seq;
    uint8_t  rounds;          /**< NACK rounds sent (0..2) */
    uint8_t  open;            /**< 1 = still missing, 0 = filled / resolved */
    uint32_t detect_ms;       /**< when the gap was seen */
    uint32_t nack_ms;         /**< last NACK for it */
} tsr_miss_t;

/** pkt_seq range [base, base + count) of the fresh fragments of one frame. */
typedef struct tsr_frange {
    uint16_t frame_seq;
    uint16_t base;
    uint8_t  count;
    uint8_t  used;
} tsr_frange_t;

/** Implementation state of the receiver (additive, appended to tsr_t). */
typedef struct tsr_ext {
    bool           started;          /**< first tick seen */
    bool           ever_connected;
    bool           have_play;        /**< play_seq valid */
    bool           bye_pending;      /**< close after the BYE is flushed */
    bool           have_data_from;
    bool           in_freeze;
    bool           have_jitter_ref;
    bool           abandon_next;     /**< NACK give-up for a frame without a slot */
    uint16_t       abandon_seq;
    uint16_t       play_seq;         /**< last frame consumed (decoded or skipped) */
    uint16_t       prev_ts_ms;       /**< jitter reference */
    uint32_t       prev_arrival_ms;
    uint32_t       jitter_x16;
    bool           kreq_sent;        /**< at least one automatic KEYFRAME_REQ */
    uint32_t       last_kreq_ms;
    uint32_t       last_udp_tx_ms;
    uint32_t       rtt_ms;
    uint32_t       late_streak;
    uint32_t       latency_n;
    uint64_t       latency_sum_ms;
    ts_addr_t      data_from;        /**< daemon UDP address (NACK destination) */
    tsr_truth_fn_t truth_fn;
    void          *truth_ctx;
    uint16_t       tx_len;
    uint8_t        tx_buf[TSR_TX_CAP];
    uint16_t       miss_head;
    uint16_t       miss_count;
    tsr_miss_t     miss[TSR_MISS_CAP];
    uint8_t        frange_next;
    tsr_frange_t   frange[TSR_FRANGE_CAP];
    uint8_t        slot_giveup[TS_REASM_FRAMES];
    uint32_t       last_alive_ms;    /**< last UDP packet or TLV from the daemon (silence watchdog) */
    uint8_t        scratch[TS_FRAME_BYTES];   /**< decoder output before it becomes `frame` */
} tsr_ext_t;

typedef struct tsr {
    tsr_cfg_t        cfg;
    tsr_state_t      state;
    ts_io_t         *io;
    ts_io_handle_t   tcp;
    uint32_t         start_ms;
    uint32_t         state_ms;          /**< when the state was entered */
    uint32_t         last_rx_ms;        /**< last UDP packet */
    uint32_t         last_decoded_ms;
    uint32_t         last_bye_ms;
    ts_tlv_welcome_t welcome;
    bool             have_welcome;
    ts_tlv_acc_t     acc;
    tsr_reasm_t      reasm[TS_REASM_FRAMES];
    bool             have_pkt_seq;
    uint16_t         pkt_seq_expect;    /**< next expected pkt_seq */
    uint16_t         last_decoded_seq;
    bool             have_decoded;
    bool             need_keyframe;
    ts_dec_t         dec;
    uint8_t          frame[TS_FRAME_BYTES];   /**< last decoded frame */
    uint8_t          truth[TS_FRAME_BYTES];   /**< optional ground truth for exact-match/PSNR */
    bool             have_truth;
    tsr_metrics_t    m;
    uint32_t         window_start_ms;   /**< 1 s metrics window */
    uint32_t         window_frames;
    uint32_t         window_bytes;
    ts_log_t         log;
    uint8_t          pkt_buf[TS_IO_DGRAM_MAX];
    uint8_t          ctl_buf[TS_TLV_HDR_LEN + TS_TLV_MAX_LEN];
    tsr_ext_t        x;                 /**< additive: implementation state */
} tsr_t;

/** Heap-allocate and initialise (tools). NULL on failure. */
tsr_t   *tsr_create(const tsr_cfg_t *cfg, ts_io_t *io);
/** Initialise caller-owned storage (tests, no heap). Errors: TS_E_INVAL. */
ts_err_t tsr_init(tsr_t *r, const tsr_cfg_t *cfg, ts_io_t *io);
/** Free a tsr_create()d instance (closes the control connection first). */
void     tsr_destroy(tsr_t *r);
/** One iteration at `now_ms`: connect/HELLO, drain UDP, NACK, decode, TLVs, metrics. */
ts_err_t tsr_tick(tsr_t *r, uint32_t now_ms);
/** Last decoded frame (TS_FRAME_BYTES) or NULL before the first one. */
const uint8_t *tsr_frame(const tsr_t *r);
const tsr_metrics_t *tsr_metrics(const tsr_t *r);
tsr_state_t tsr_state(const tsr_t *r);
const ts_tlv_welcome_t *tsr_welcome(const tsr_t *r);   /**< NULL until received */
/** Queue control messages (sent on the next tick). Errors: TS_E_STATE when not connected. */
ts_err_t tsr_send_keyframe_req(tsr_t *r);
ts_err_t tsr_send_set_rate(tsr_t *r, uint32_t kbps);
ts_err_t tsr_send_bye(tsr_t *r);
/** Provide the ground truth of the NEXT decoded frame for exact-match / PSNR metrics. */
void     tsr_set_truth(tsr_t *r, const uint8_t *truth);
void     tsr_cfg_default(tsr_cfg_t *cfg);
/** Write metrics as JSON into `buf` (NUL-terminated). Returns bytes written (excluding NUL) or 0 on overflow. */
size_t   tsr_metrics_json(const tsr_metrics_t *m, char *buf, size_t cap);

/* ---- additive API ---------------------------------------------------------- */
/** Ground truth looked up per decoded frame_seq (exact-match / PSNR); fn NULL disables. */
void     tsr_set_truth_fn(tsr_t *r, tsr_truth_fn_t fn, void *ctx);
/** Zero the counters of tsr_metrics() (connection and stream state are kept). */
void     tsr_reset_metrics(tsr_t *r);

#ifdef __cplusplus
}
#endif

#endif /* TS_RECEIVER_H */
