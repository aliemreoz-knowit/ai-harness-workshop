/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- ts_daemon.h
 * The daemon core: one tick = capture -> encode -> fragment -> send to every
 * client (bucket-paced) -> serve NACKs -> handle control TLVs -> expire
 * clients -> periodic STATS. The network is reached only through ts_io_t and
 * the clock only through `now_ms`, so the same code runs under the fake I/O
 * in loopback tests and under POSIX sockets in src/main.c.
 *
 * FROZEN API: additive, non-breaking changes only.
 * Rules: no dynamic memory after tsd_create (static storage only), no stdio,
 * single-threaded.
 */
#ifndef TS_DAEMON_H
#define TS_DAEMON_H

#include <stdint.h>
#include <stdbool.h>
#include "ts_config.h"
#include "ts_proto.h"
#include "ts_codec.h"
#include "ts_txq.h"
#include "ts_rate.h"
#include "ts_clients.h"
#include "ts_capture.h"
#include "ts_io.h"
#include "ts_log.h"

#ifdef __cplusplus
extern "C" {
#endif

struct ts_pool;   /* ts_pool.h embeds tsd_t, so only a forward declaration here */

typedef struct tsd_cfg {
    uint16_t     udp_port;            /**< informational; the io object is already bound */
    uint16_t     tcp_port;
    uint32_t     stream_id;           /**< sent in WELCOME; 0 = derive from now_ms */
    uint32_t     fps;                 /**< capture pacing, 0 = TS_DEFAULT_FPS */
    uint32_t     kbps_global;         /**< 0 = TS_GLOBAL_KBPS */
    uint32_t     kbps_client;         /**< default per-client rate, 0 = TS_DEFAULT_KBPS */
    uint16_t     seq_start;           /**< initial frame_seq and pkt_seq (HIL: 65000) */
    uint32_t     keyframe_period_ms;  /**< 0 = TS_KEYFRAME_PERIOD_MS */
    uint32_t     client_timeout_ms;   /**< 0 = TS_CLIENT_TIMEOUT_MS */
    uint8_t      retx_per_tick;       /**< 0 = TS_RETX_PER_TICK */
    bool         loss_adapt;          /**< enable NACK-ratio quality adaptation */
    ts_cap_cfg_t capture;
} tsd_cfg_t;

typedef enum tsd_state {
    TSD_INIT = 0,
    TSD_RUNNING,
    TSD_STOPPED
} tsd_state_t;

/** Daemon instance. Large members (ring, references, buffers) make this ~1 MB; it lives in the pool. */
typedef struct tsd {
    tsd_cfg_t         cfg;
    tsd_state_t       state;
    ts_io_t          *io;
    struct ts_pool   *pool;
    uint32_t          start_ms;
    uint32_t          last_tick_ms;
    uint32_t          last_stats_ms;
    uint32_t          last_loss_ms;
    uint16_t          frame_seq_next;
    ts_cap_t          cap;
    ts_enc_t          enc;
    ts_txq_t          txq;
    ts_clients_t      clients;
    ts_bucket_t       global_bucket;
    ts_loss_t         loss;
    ts_keysched_t     keysched;
    ts_stats_global_t stats;
    ts_log_t          log;
    uint8_t           frame[TS_FRAME_BYTES];   /**< last captured frame */
    uint8_t           pkt_buf[TS_PKT_MAX];     /**< retransmit / receive scratch */
    uint8_t           ctl_buf[TS_TLV_HDR_LEN + TS_TLV_MAX_LEN]; /**< TLV send scratch */
    uint32_t          key_push_idx;   /**< (additive) txq push index of the newest keyframe's first fragment */
    bool              have_key_push;  /**< (additive) key_push_idx is valid */
    ts_cc_t           cc;             /**< (additive) congestion control: stream vs slowest viewer */
} tsd_t;

/**
 * Initialise the daemon inside `pool` (returns `&pool->daemon`), open the
 * capture source and seal the pool (no allocation afterwards). NULL on
 * failure (bad cfg, capture open failed). `io` must outlive the daemon.
 */
tsd_t *tsd_create(const tsd_cfg_t *cfg, ts_io_t *io, struct ts_pool *pool);
/**
 * One iteration at `now_ms`. Never blocks; the caller paces it (poll() in
 * main, the virtual clock in tests). Errors: TS_E_STATE (not running),
 * TS_E_IO (fatal I/O), otherwise TS_OK even when nothing happened.
 */
ts_err_t tsd_tick(tsd_t *d, uint32_t now_ms);
/** Global counters (same content as the STATS TLV global block). */
const ts_stats_global_t *tsd_stats(const tsd_t *d);
/** Close all clients, close the capture, state = STOPPED. */
void tsd_stop(tsd_t *d);
/** Next time (ms) the daemon wants a tick: next frame due or STATS due. */
uint32_t tsd_next_due_ms(const tsd_t *d);
/** Default configuration (synth source, default ports/rates). */
void tsd_cfg_default(tsd_cfg_t *cfg);

#ifdef __cplusplus
}
#endif

#endif /* TS_DAEMON_H */
