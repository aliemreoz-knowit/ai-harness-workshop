/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- fuzz/fuzz_codec_decode.c
 * libFuzzer target: the decoder must reject any malformed bitstream without
 * writing outside the frame buffer, on every path: keyframes, header checks,
 * TS_E_NOREF, and P-frames decoded against a real reference (the XOR sink of
 * the RLE decoder). Seeds: fuzz/corpus/codec_decode.
 *
 * Each input is decoded twice:
 *   1. as is, on a fresh decoder (keyframe path, header checks, NOREF);
 *   2. as the frame that follows a valid keyframe: the decoder is primed with
 *      a fixed keyframe (a constant frame, frame_seq FUZZ_REF_SEQ, encoded
 *      once) and the input's codec header ref_seq is patched to FUZZ_REF_SEQ,
 *      so a P-frame input reaches the reference path; bit 0 of the input's
 *      frame_seq byte forces kind = P so keyframe seeds mutate into P-frames.
 */
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include "ts_config.h"
#include "ts_proto.h"
#include "ts_codec.h"

#define FUZZ_REF_SEQ    0x0101U
#define FUZZ_IN_MAX     (TS_ENC_MAX + 64U)
#define FUZZ_OFF_KIND   0U        /* PROTOCOL.md 6.1 codec header */
#define FUZZ_OFF_SEQ    2U
#define FUZZ_OFF_REF    4U

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

static uint8_t g_prime[TS_ENC_MAX];
static size_t  g_prime_len;
static bool    g_have_prime;
static uint8_t g_patched[FUZZ_IN_MAX];
static uint8_t g_frame[TS_FRAME_BYTES];
static ts_enc_t g_enc;
static ts_dec_t g_dec;
static uint8_t g_out[TS_FRAME_BYTES];

static void prime_build(void)
{
    ts_frame_info_t info;
    (void)memset(g_frame, 0x80, sizeof(g_frame));
    ts_enc_init(&g_enc);
    if (ts_enc_encode(&g_enc, g_frame, (uint16_t)FUZZ_REF_SEQ, g_prime, sizeof(g_prime), &info) == TS_OK) {
        g_prime_len = (size_t)info.enc_len;
        g_have_prime = true;
    }
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    ts_frame_info_t info;
    if (!g_have_prime) {
        prime_build();
    }
    /* 1. the raw input on a fresh decoder */
    ts_dec_init(&g_dec);
    (void)ts_dec_decode(&g_dec, data, size, g_out, &info);

    /* 2. the input as the frame after a valid keyframe */
    if (g_have_prime && (size >= (size_t)TS_CODEC_HDR_LEN) && (size <= sizeof(g_patched))) {
        ts_dec_init(&g_dec);
        if (ts_dec_decode(&g_dec, g_prime, g_prime_len, g_out, &info) == TS_OK) {
            (void)memcpy(g_patched, data, size);
            if ((data[FUZZ_OFF_SEQ] & 1U) != 0U) {
                g_patched[FUZZ_OFF_KIND] = (uint8_t)TS_CODEC_KIND_PFRAME;
            }
            ts_wr16(&g_patched[FUZZ_OFF_REF], (uint16_t)FUZZ_REF_SEQ);
            (void)ts_dec_decode(&g_dec, g_patched, size, g_out, &info);
        }
    }
    return 0;
}
