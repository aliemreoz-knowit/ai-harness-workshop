/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- src/proto/proto.c
 * Error names, little-endian helpers, serial arithmetic (PROTOCOL.md 7),
 * DATA header (PROTOCOL.md 3) and NACK packet (PROTOCOL.md 4).
 * CRC32 lives in crc32.c, control TLVs and the accumulate parser in tlv.c.
 *
 * Rules: no dynamic memory, no stdio, single exit per function, every parser
 * bounded by the length it is given.
 */
#include <stddef.h>
#include <string.h>
#include "ts_proto.h"

/* DATA header field offsets (PROTOCOL.md section 3). */
#define HDR_OFF_MAGIC        0U
#define HDR_OFF_VER          2U
#define HDR_OFF_FLAGS        3U
#define HDR_OFF_FRAME_SEQ    4U
#define HDR_OFF_PKT_SEQ      6U
#define HDR_OFF_FRAG_IDX     8U
#define HDR_OFF_FRAG_COUNT   9U
#define HDR_OFF_FRAME_LEN    10U
#define HDR_OFF_PAYLOAD_LEN  14U
#define HDR_OFF_TS_MS        16U
#define HDR_OFF_RESERVED     18U
#define HDR_OFF_CRC          20U
#define HDR_CRC_COVER        20U   /* header bytes covered by the CRC */

#define HDR_FLAGS_RESERVED   0xFCU  /* bits 2..7, 0 on send */
_Static_assert((HDR_FLAGS_RESERVED & (TS_F_KEYFRAME | TS_F_RETRANSMIT)) == 0U, "flag bits disjoint");
_Static_assert((HDR_FLAGS_RESERVED | TS_F_KEYFRAME | TS_F_RETRANSMIT) == 0xFFU, "all flag bits covered");

/* NACK field offsets (PROTOCOL.md section 4). */
#define NACK_OFF_MAGIC       0U
#define NACK_OFF_COUNT       2U
#define NACK_OFF_RESERVED    3U
#define NACK_ENT_FRAME_SEQ   0U
#define NACK_ENT_FRAG_IDX    2U
#define NACK_ENT_PAD         3U

const char *ts_err_str(ts_err_t e)
{
    static const char *const names[TS_E_MAX_] = {
        "TS_OK", "TS_E_INVAL", "TS_E_NOMEM", "TS_E_BADMAGIC", "TS_E_BADVER",
        "TS_E_BADCRC", "TS_E_TRUNC", "TS_E_TOOLONG", "TS_E_FULL", "TS_E_NOTFOUND",
        "TS_E_TOO_OLD", "TS_E_AGAIN", "TS_E_IO", "TS_E_STATE", "TS_E_NOREF",
        "TS_E_CORRUPT", "TS_E_CLOSED", "TS_E_NOTIMPL"
    };
    const char *s = "TS_E_UNKNOWN";
    if ((e >= TS_OK) && (e < TS_E_MAX_)) {
        s = names[e];
    }
    return s;
}

/* ---- little-endian helpers ------------------------------------------------- */
uint16_t ts_rd16(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | (uint16_t)((uint16_t)p[1] << 8));
}

uint32_t ts_rd32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

void ts_wr16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v & 0xFFU);
    p[1] = (uint8_t)((v >> 8) & 0xFFU);
}

void ts_wr32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFFU);
    p[1] = (uint8_t)((v >> 8) & 0xFFU);
    p[2] = (uint8_t)((v >> 16) & 0xFFU);
    p[3] = (uint8_t)((v >> 24) & 0xFFU);
}

/* ---- serial arithmetic (PROTOCOL.md 7) ------------------------------------- */
int16_t ts_seq_diff(uint16_t a, uint16_t b)
{
    /* (uint16_t)(a - b) is the forward distance modulo 65536. Map it onto
     * [-32768, 32767] without relying on implementation-defined narrowing. */
    const uint16_t d = (uint16_t)((uint32_t)a - (uint32_t)b);
    int32_t s = (int32_t)d;
    if (d >= 0x8000U) {
        s = s - 65536;
    }
    return (int16_t)s;
}

bool ts_seq_after(uint16_t a, uint16_t b)
{
    return ts_seq_diff(a, b) > 0;
}

/* ---- DATA header (PROTOCOL.md 3) -------------------------------------------- */

/* Step 5 of PROTOCOL.md 3.1: fragment geometry. payload_len is already 1..MTU. */
static ts_err_t hdr_check_geometry(const ts_hdr_t *h)
{
    ts_err_t err = TS_OK;
    const uint32_t start = (uint32_t)h->frag_idx * TS_MTU_PAYLOAD;
    const uint32_t end = start + (uint32_t)h->payload_len;
    const bool last = ((uint32_t)h->frag_idx + 1U) == (uint32_t)h->frag_count;

    if ((h->frag_count == 0U) || ((uint32_t)h->frag_count > TS_MAX_FRAGS) ||
        (h->frag_idx >= h->frag_count)) {
        err = TS_E_INVAL;
    } else if ((h->frame_len == 0U) || (h->frame_len > TS_ENC_MAX)) {
        err = TS_E_INVAL;
    } else if (end > h->frame_len) {
        err = TS_E_INVAL;
    } else if ((!last) && ((uint32_t)h->payload_len != TS_MTU_PAYLOAD)) {
        err = TS_E_INVAL;
    } else if (last && (end != h->frame_len)) {
        err = TS_E_INVAL;
    } else {
        err = TS_OK;
    }
    return err;
}

static void hdr_write20(const ts_hdr_t *h, uint8_t *out)
{
    ts_wr16(&out[HDR_OFF_MAGIC], (uint16_t)TS_MAGIC_DATA);
    out[HDR_OFF_VER] = (uint8_t)TS_PROTOCOL_VERSION;
    out[HDR_OFF_FLAGS] = h->flags;
    ts_wr16(&out[HDR_OFF_FRAME_SEQ], h->frame_seq);
    ts_wr16(&out[HDR_OFF_PKT_SEQ], h->pkt_seq);
    out[HDR_OFF_FRAG_IDX] = h->frag_idx;
    out[HDR_OFF_FRAG_COUNT] = h->frag_count;
    ts_wr32(&out[HDR_OFF_FRAME_LEN], h->frame_len);
    ts_wr16(&out[HDR_OFF_PAYLOAD_LEN], h->payload_len);
    ts_wr16(&out[HDR_OFF_TS_MS], h->ts_ms);
    ts_wr16(&out[HDR_OFF_RESERVED], 0U);
}

static uint32_t hdr_crc(const uint8_t *hdr20, const uint8_t *payload, size_t payload_len)
{
    uint32_t c = ts_crc32_init();
    c = ts_crc32_update(c, hdr20, HDR_CRC_COVER);
    c = ts_crc32_update(c, payload, payload_len);
    return ts_crc32_final(c);
}

ts_err_t ts_hdr_pack(ts_hdr_t *h, const uint8_t *payload, uint8_t *out, size_t out_cap, size_t *out_len)
{
    ts_err_t err = TS_OK;

    if ((h == NULL) || (payload == NULL) || (out == NULL) || (out_len == NULL)) {
        err = TS_E_INVAL;
    } else if ((h->payload_len == 0U) || ((uint32_t)h->payload_len > TS_MTU_PAYLOAD)) {
        err = TS_E_INVAL;
    } else if (((uint32_t)h->flags & HDR_FLAGS_RESERVED) != 0U) {
        err = TS_E_INVAL;   /* reserved flag bits must be 0 on send */
    } else {
        err = hdr_check_geometry(h);
    }

    if (err == TS_OK) {
        const size_t total = (size_t)TS_HDR_LEN + (size_t)h->payload_len;
        if (out_cap < total) {
            err = TS_E_NOMEM;
        } else {
            /* memmove: the payload may already sit at out + TS_HDR_LEN
             * (retransmit re-stamps a stored packet in place). */
            (void)memmove(&out[TS_HDR_LEN], payload, (size_t)h->payload_len);
            h->ver = (uint8_t)TS_PROTOCOL_VERSION;
            hdr_write20(h, out);
            h->crc32 = hdr_crc(out, &out[TS_HDR_LEN], (size_t)h->payload_len);
            ts_wr32(&out[HDR_OFF_CRC], h->crc32);
            *out_len = total;
        }
    }
    return err;
}

ts_err_t ts_hdr_parse(const uint8_t *buf, size_t len, ts_hdr_t *h, const uint8_t **payload)
{
    ts_err_t err = TS_OK;

    if (payload != NULL) {
        *payload = NULL;
    }
    if ((buf == NULL) || (h == NULL) || (payload == NULL)) {
        err = TS_E_INVAL;
    } else if (len < (size_t)TS_HDR_LEN) {
        err = TS_E_TRUNC;                                       /* 3.1 step 1 */
    } else if (ts_rd16(&buf[HDR_OFF_MAGIC]) != (uint16_t)TS_MAGIC_DATA) {
        err = TS_E_BADMAGIC;                                    /* step 2 */
    } else if (buf[HDR_OFF_VER] != (uint8_t)TS_PROTOCOL_VERSION) {
        err = TS_E_BADVER;                                      /* step 3 */
    } else {
        h->ver = buf[HDR_OFF_VER];
        h->flags = buf[HDR_OFF_FLAGS];
        h->frame_seq = ts_rd16(&buf[HDR_OFF_FRAME_SEQ]);
        h->pkt_seq = ts_rd16(&buf[HDR_OFF_PKT_SEQ]);
        h->frag_idx = buf[HDR_OFF_FRAG_IDX];
        h->frag_count = buf[HDR_OFF_FRAG_COUNT];
        h->frame_len = ts_rd32(&buf[HDR_OFF_FRAME_LEN]);
        h->payload_len = ts_rd16(&buf[HDR_OFF_PAYLOAD_LEN]);
        h->ts_ms = ts_rd16(&buf[HDR_OFF_TS_MS]);
        h->crc32 = ts_rd32(&buf[HDR_OFF_CRC]);

        /* step 4: payload length vs. datagram length */
        if ((h->payload_len == 0U) || ((uint32_t)h->payload_len > TS_MTU_PAYLOAD)) {
            err = TS_E_INVAL;
        } else if (len < ((size_t)TS_HDR_LEN + (size_t)h->payload_len)) {
            err = TS_E_TRUNC;
        } else if (len != ((size_t)TS_HDR_LEN + (size_t)h->payload_len)) {
            err = TS_E_INVAL;
        } else {
            err = hdr_check_geometry(h);                        /* step 5 */
        }

        /* step 6: CRC over header[0..20) + payload, exact length */
        if (err == TS_OK) {
            const uint8_t *pl = &buf[TS_HDR_LEN];
            if (hdr_crc(buf, pl, (size_t)h->payload_len) != h->crc32) {
                err = TS_E_BADCRC;
            } else {
                *payload = pl;
            }
        }
    }
    return err;
}

/* ---- NACK (PROTOCOL.md 4) ---------------------------------------------------- */
ts_err_t ts_nack_pack(const ts_nack_t *n, uint8_t *out, size_t out_cap, size_t *out_len)
{
    ts_err_t err = TS_OK;

    if ((n == NULL) || (out == NULL) || (out_len == NULL)) {
        err = TS_E_INVAL;
    } else if ((uint32_t)n->count > TS_NACK_MAX) {
        err = TS_E_INVAL;
    } else {
        const size_t total = (size_t)TS_NACK_HDR_LEN + ((size_t)TS_NACK_ENTRY_LEN * (size_t)n->count);
        if (out_cap < total) {
            err = TS_E_NOMEM;
        } else {
            ts_wr16(&out[NACK_OFF_MAGIC], (uint16_t)TS_MAGIC_NACK);
            out[NACK_OFF_COUNT] = n->count;
            out[NACK_OFF_RESERVED] = 0U;
            for (size_t i = 0U; i < (size_t)n->count; i++) {
                uint8_t *p = &out[(size_t)TS_NACK_HDR_LEN + ((size_t)TS_NACK_ENTRY_LEN * i)];
                ts_wr16(&p[NACK_ENT_FRAME_SEQ], n->e[i].frame_seq);
                p[NACK_ENT_FRAG_IDX] = n->e[i].frag_idx;
                p[NACK_ENT_PAD] = 0U;
            }
            *out_len = total;
        }
    }
    return err;
}

ts_err_t ts_nack_parse(const uint8_t *buf, size_t len, ts_nack_t *n)
{
    ts_err_t err = TS_OK;

    if ((buf == NULL) || (n == NULL)) {
        err = TS_E_INVAL;
    } else if (len < (size_t)TS_NACK_HDR_LEN) {
        err = TS_E_TRUNC;
    } else if (ts_rd16(&buf[NACK_OFF_MAGIC]) != (uint16_t)TS_MAGIC_NACK) {
        err = TS_E_BADMAGIC;
    } else {
        const uint8_t count = buf[NACK_OFF_COUNT];
        const size_t need = (size_t)TS_NACK_HDR_LEN + ((size_t)TS_NACK_ENTRY_LEN * (size_t)count);
        if ((uint32_t)count > TS_NACK_MAX) {
            err = TS_E_INVAL;       /* never more than TS_NACK_MAX entries into n->e */
        } else if (len < need) {
            err = TS_E_TRUNC;
        } else if (len != need) {
            err = TS_E_INVAL;
        } else {
            n->count = count;
            for (size_t i = 0U; i < (size_t)count; i++) {
                const uint8_t *p = &buf[(size_t)TS_NACK_HDR_LEN + ((size_t)TS_NACK_ENTRY_LEN * i)];
                n->e[i].frame_seq = ts_rd16(&p[NACK_ENT_FRAME_SEQ]);
                n->e[i].frag_idx = p[NACK_ENT_FRAG_IDX];
            }
        }
    }
    return err;
}
