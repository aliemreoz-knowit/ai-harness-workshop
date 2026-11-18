/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- ts_config.h
 * Compile-time constants shared by every module. Mirrors PROTOCOL.md section 1.
 *
 * FROZEN API: additive, non-breaking changes only (see docs/ARCHITECTURE.md).
 * Every constant here is a plain integer macro so it can be used in array sizes
 * and _Static_assert expressions. Values that are part of the wire format MUST
 * match PROTOCOL.md; changing one requires a PROTOCOL_VERSION bump.
 */
#ifndef TS_CONFIG_H
#define TS_CONFIG_H

#include <stdint.h>
#include <stddef.h>
#include <assert.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---- Protocol / wire format -------------------------------------------- */
#define TS_PROTOCOL_VERSION     2U      /**< DATA header `ver` byte */

#define TS_WIDTH                320U    /**< frame width (pixels) */
#define TS_HEIGHT               240U    /**< frame height (pixels) */
#define TS_FRAME_BYTES          (TS_WIDTH * TS_HEIGHT)   /**< gray8, one byte per pixel */

#define TS_MTU_PAYLOAD          1400U   /**< max DATA payload bytes per UDP packet */
#define TS_HDR_LEN              24U     /**< DATA header length */
#define TS_PKT_MAX              (TS_HDR_LEN + TS_MTU_PAYLOAD) /**< max DATA datagram */
#define TS_MAX_FRAGS            110U    /**< max fragments per frame */
#define TS_ENC_MAX              (2U * TS_WIDTH * TS_HEIGHT + 64U) /**< encoded frame bound */
#define TS_RING_SLOTS           448U    /**< sender history ring slots */
#define TS_MAX_CLIENTS          4U      /**< concurrent viewers */

#define TS_NACK_MAX             32U     /**< entries per NACK packet */
#define TS_NACK_HDR_LEN         4U
#define TS_NACK_ENTRY_LEN       4U
#define TS_NACK_PKT_MAX         (TS_NACK_HDR_LEN + TS_NACK_ENTRY_LEN * TS_NACK_MAX)

#define TS_TLV_HDR_LEN          4U      /**< type u8, flags u8, len u16 */
#define TS_TLV_MAX_LEN          512U    /**< max TLV value length */
#define TS_TLV_ACC_CAP          1024U   /**< per-connection accumulate buffer */

#define TS_MAGIC_DATA           0x5453U /**< DATA packet magic */
#define TS_MAGIC_NACK           0x4E4BU /**< NACK packet magic */

/* DATA header flag bits */
#define TS_F_KEYFRAME           0x01U
#define TS_F_RETRANSMIT         0x02U

/* ---- Codec ----------------------------------------------------------------- */
#define TS_TILE                 16U     /**< tile edge */
#define TS_TILES_X              (TS_WIDTH / TS_TILE)
#define TS_TILES_Y              (TS_HEIGHT / TS_TILE)
#define TS_TILES                (TS_TILES_X * TS_TILES_Y)   /**< 300 */
#define TS_TILE_BITMAP_BYTES    ((TS_TILES + 7U) / 8U)      /**< 38 */
#define TS_TILE_THRESH          16U     /**< abs pixel diff above which a tile is "changed" */
#define TS_QUALITY_MAX          4U      /**< quality steps: 1, 2, 4 */
#define TS_CODEC_HDR_LEN        8U      /**< codec header in front of the RLE stream */
#define TS_CODEC_KIND_KEY       0x4BU   /**< 'K' */
#define TS_CODEC_KIND_PFRAME    0x50U   /**< 'P' */
#define TS_SCENE_CHANGE_PCT     60U     /**< changed tiles (%) that force a keyframe */

/* ---- Timing (ms) ----------------------------------------------------------- */
#define TS_KEYFRAME_PERIOD_MS   10000U
#define TS_KEYFRAME_COOLDOWN_MS 1000U
#define TS_CLIENT_TIMEOUT_MS    2000U
#define TS_STATS_PERIOD_MS      1000U
#define TS_NACK_DELAY_MS        20U
#define TS_NACK_RETRY_MS        200U
#define TS_FREEZE_MS            500U
#define TS_LOSS_WINDOW_MS       1000U   /**< loss-adaptation measurement window */
#define TS_LOSS_STEP_DOWN_PCT   10U     /**< NACK ratio above this steps quality down */

/* ---- Rate / queues --------------------------------------------------------- */
#define TS_RETX_PER_TICK        16U     /**< max retransmits per client per tick */
#define TS_DEFAULT_KBPS         4000U   /**< per-client default rate */
#define TS_GLOBAL_KBPS          16000U  /**< global cap */
#define TS_REASM_FRAMES         8U      /**< receiver frames in flight */
#define TS_DEFAULT_FPS          30U

/* ---- Default ports --------------------------------------------------------- */
#define TS_DEFAULT_UDP_PORT     5600U
#define TS_DEFAULT_TCP_PORT     5601U

/* ---- Sanity ---------------------------------------------------------------- */
_Static_assert(TS_HDR_LEN == 24U, "DATA header is 24 bytes");
_Static_assert(TS_ENC_MAX == 153664U, "TS_ENC_MAX = 2*W*H + 64");
_Static_assert(TS_MAX_FRAGS * TS_MTU_PAYLOAD >= TS_ENC_MAX, "a frame must fit in TS_MAX_FRAGS fragments");
_Static_assert(TS_MAX_FRAGS <= 255U, "frag_idx/frag_count are u8");
_Static_assert(TS_RING_SLOTS >= 4U * TS_MAX_FRAGS, "ring holds at least 4 frames");
_Static_assert(TS_RING_SLOTS <= 65535U, "ring index is u16");
_Static_assert(TS_TLV_ACC_CAP >= TS_TLV_HDR_LEN + TS_TLV_MAX_LEN, "accumulate buffer holds one max TLV");
_Static_assert(TS_NACK_PKT_MAX == 132U, "NACK packet max 132 bytes");
_Static_assert((TS_WIDTH % TS_TILE) == 0U && (TS_HEIGHT % TS_TILE) == 0U, "tiles divide the frame");
_Static_assert(TS_TILES == 300U, "300 tiles");
_Static_assert((TS_HEIGHT % TS_QUALITY_MAX) == 0U, "quality steps divide the height");
_Static_assert(TS_MAX_CLIENTS <= 255U, "client id is u8");
_Static_assert(TS_PKT_MAX <= 1500U, "DATA datagram fits a standard MTU");

#ifdef __cplusplus
}
#endif

#endif /* TS_CONFIG_H */
