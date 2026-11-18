/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/loopback/test_lb_enc_max.c
 * budgets/sanitizers TS_ENC_MAX through the pipeline (the RLE worst case is larger than
 * the raw frame; every buffer on the path must hold TS_ENC_MAX).
 *  1. Worst-case frames (noise, alternating, "x yyy" runs, ...) at every
 *     quality step go encoder -> txq fragmenter -> fake UDP through the netem
 *     shim (reorder + duplicates) -> ts_hdr_parse -> reassembly in a
 *     TS_ENC_MAX buffer -> decoder, and must come out bit-exact. An encode
 *     into a raw-sized buffer must fail with TS_E_NOMEM, never overflow.
 *  2. The full daemon + receiver loop on the synth source with a scene cut
 *     every second (one noise frame 15 frames after each cut) stays exact.
 */
#include "lb_common.h"
#include "ts_codec.h"
#include "ts_txq.h"
#include "ts_proto.h"

#define PATTERNS 6U

static ts_enc_t     g_enc;
static ts_dec_t     g_dec;
static ts_txq_t     g_txq;
static ts_netem_t   g_nm;
static ts_io_fake_t g_tx;
static ts_io_fake_t g_rx;
static uint8_t      g_frame[TS_FRAME_BYTES];
static uint8_t      g_encbuf[TS_ENC_MAX];
static uint8_t      g_small[TS_FRAME_BYTES];     /* deliberately raw-sized */
static uint8_t      g_reasm[TS_ENC_MAX];
static uint8_t      g_out[TS_FRAME_BYTES];
static uint8_t      g_have[TS_MAX_FRAGS];

static const ts_addr_t TX_ADDR = { 0x0A000001U, 5600U };
static const ts_addr_t RX_ADDR = { 0x0A000020U, 6000U };

void setUp(void)
{
}

void tearDown(void)
{
}

static void make_pattern(uint32_t p, uint32_t salt)
{
    uint32_t s = 0x9E3779B9U ^ salt;
    for (uint32_t i = 0U; i < TS_FRAME_BYTES; i++) {
        uint8_t v;
        switch (p) {
        case 0U:   /* noise */
            v = (uint8_t)(ts_netem_rand(&s) >> 24);
            break;
        case 1U:   /* alternating extremes: literal-only stream */
            v = ((i & 1U) != 0U) ? 0xFFU : 0x00U;
            break;
        case 2U:   /* "x y y y": 1-symbol literal + 3-run, worst case of the canonical encoder */
            v = ((i % 4U) == 0U) ? (uint8_t)(i / 4U) : (uint8_t)((i / 4U) + 128U);
            break;
        case 3U:   /* pairs: runs of exactly 2 */
            v = (uint8_t)((i / 2U) * 37U);
            break;
        case 4U:   /* horizontal gradient */
            v = (uint8_t)((i % TS_WIDTH) + salt);
            break;
        default:   /* flat */
            v = (uint8_t)salt;
            break;
        }
        g_frame[i] = v;
    }
}

/** Fragment, relay through the shim, reassemble, decode; returns the fragment count. */
static uint8_t ship_frame(uint16_t frame_seq, const ts_frame_info_t *info, uint32_t *now)
{
    ts_txq_cursor_t cur;
    uint8_t frag_count = 0U;
    uint8_t got = 0U;
    uint8_t flags = info->keyframe ? (uint8_t)TS_F_KEYFRAME : 0U;
    ts_frame_info_t dinfo;

    ts_txq_cursor_init(&g_txq, &cur);
    TEST_ASSERT_EQUAL(TS_OK, ts_txq_push_frame(&g_txq, frame_seq, flags, (uint16_t)*now, g_encbuf,
                                               (size_t)info->enc_len, &frag_count));
    TEST_ASSERT_TRUE(frag_count >= 1U);
    TEST_ASSERT_TRUE(frag_count <= TS_MAX_FRAGS);
    TEST_ASSERT_EQUAL_UINT32((info->enc_len + TS_MTU_PAYLOAD - 1U) / TS_MTU_PAYLOAD, (uint32_t)frag_count);
    for (const ts_txq_slot_t *s = ts_txq_cursor_peek(&g_txq, &cur); s != NULL; s = ts_txq_cursor_peek(&g_txq, &cur)) {
        TEST_ASSERT_TRUE(s->len <= TS_PKT_MAX);
        TEST_ASSERT_EQUAL_INT32((int32_t)s->len, ts_io_udp_send(&g_tx.io, &RX_ADDR, s->pkt, s->len));
        ts_txq_cursor_advance(&cur);
    }
    (void)memset(g_have, 0, sizeof(g_have));
    for (uint32_t k = 0U; (k < 50U) && (got < frag_count); k++) {
        uint8_t buf[TS_IO_DGRAM_MAX];
        int32_t n;
        ts_netem_pump(&g_nm, &g_tx, &g_rx, *now);
        for (n = ts_io_udp_recv(&g_rx.io, NULL, buf, sizeof(buf)); n > 0; n = ts_io_udp_recv(&g_rx.io, NULL, buf, sizeof(buf))) {
            ts_hdr_t h;
            const uint8_t *payload = NULL;
            TEST_ASSERT_EQUAL(TS_OK, ts_hdr_parse(buf, (size_t)n, &h, &payload));
            TEST_ASSERT_EQUAL_UINT16(frame_seq, h.frame_seq);
            TEST_ASSERT_EQUAL_UINT32(info->enc_len, h.frame_len);
            TEST_ASSERT_EQUAL_UINT8(frag_count, h.frag_count);
            if (g_have[h.frag_idx] == 0U) {    /* duplicate drop */
                (void)memcpy(&g_reasm[(size_t)h.frag_idx * TS_MTU_PAYLOAD], payload, h.payload_len);
                g_have[h.frag_idx] = 1U;
                got++;
            }
        }
        *now += 1U;
    }
    TEST_ASSERT_EQUAL_UINT8_MESSAGE(frag_count, got, "fragments lost on a loss-free link");
    TEST_ASSERT_EQUAL(TS_OK, ts_dec_decode(&g_dec, g_reasm, (size_t)info->enc_len, g_out, &dinfo));
    TEST_ASSERT_EQUAL_MEMORY(g_enc.ref, g_out, TS_FRAME_BYTES);
    TEST_ASSERT_EQUAL(info->keyframe, dinfo.keyframe);
    return frag_count;
}

static void test_worst_case_frames_through_the_shim(void)
{
    static const uint8_t qualities[3] = { 1U, 2U, 4U };
    ts_netem_cfg_t nc;
    uint32_t now = 1000U;
    uint16_t seq = 65530U;          /* wraps during the test */
    uint32_t max_len = 0U;
    uint8_t max_frags = 0U;

    (void)memset(&nc, 0, sizeof(nc));
    nc.reorder_pct = 20U;
    nc.reorder_delay_ms = 3U;
    nc.dup_pct = 5U;
    nc.seed = 0xE11CU;
    ts_io_fake_init(&g_tx, TX_ADDR);
    ts_io_fake_init(&g_rx, RX_ADDR);
    ts_netem_init(&g_nm, &nc, now);
    ts_enc_init(&g_enc);
    ts_dec_init(&g_dec);
    ts_txq_init(&g_txq, 65500U);

    for (uint32_t qi = 0U; qi < 3U; qi++) {
        TEST_ASSERT_EQUAL(TS_OK, ts_enc_set_quality(&g_enc, qualities[qi]));
        for (uint32_t p = 0U; p < PATTERNS; p++) {
            for (uint32_t salt = 0U; salt < 2U; salt++) {
                ts_frame_info_t info;
                uint8_t frags;
                make_pattern(p, salt + (qi * 7U));
                if (salt == 0U) {
                    ts_enc_force_keyframe(&g_enc);   /* keyframe = the pattern itself; then a P-frame */
                }
                TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc, g_frame, seq, g_encbuf, sizeof(g_encbuf), &info));
                TEST_ASSERT_TRUE(info.enc_len > TS_CODEC_HDR_LEN);
                TEST_ASSERT_TRUE(info.enc_len <= TS_ENC_MAX);
                frags = ship_frame(seq, &info, &now);
                if (qualities[qi] == 1U) {
                    TEST_ASSERT_EQUAL_MEMORY(g_frame, g_out, TS_FRAME_BYTES);   /* lossless at q1 */
                }
                if (info.enc_len > max_len) {
                    max_len = info.enc_len;
                }
                if (frags > max_frags) {
                    max_frags = frags;
                }
                seq++;
            }
        }
    }
    (void)printf("  [enc_max] largest frame %lu bytes (TS_ENC_MAX %lu, raw %lu) in %u fragments\n",
                 (unsigned long)max_len, (unsigned long)TS_ENC_MAX, (unsigned long)TS_FRAME_BYTES, (unsigned)max_frags);
    TEST_ASSERT_TRUE_MESSAGE(max_len > TS_FRAME_BYTES, "worst-case patterns did not exceed the raw frame size");
    TEST_ASSERT_EQUAL_UINT32(0U, ts_netem_stats(&g_nm)->held_overflow);
    TEST_ASSERT_TRUE(ts_netem_stats(&g_nm)->duplicated > 0U);
    TEST_ASSERT_TRUE(ts_netem_stats(&g_nm)->reordered > 0U);
}

static void test_raw_sized_buffer_is_refused_not_overflowed(void)
{
    ts_frame_info_t info;
    ts_enc_init(&g_enc);
    make_pattern(2U, 0U);                  /* encodes to more than TS_FRAME_BYTES */
    TEST_ASSERT_EQUAL(TS_E_NOMEM, ts_enc_encode(&g_enc, g_frame, 1U, g_small, sizeof(g_small), &info));
    /* the reference is unchanged: the next encode is still a keyframe and succeeds with a proper buffer */
    TEST_ASSERT_EQUAL(TS_OK, ts_enc_encode(&g_enc, g_frame, 2U, g_encbuf, sizeof(g_encbuf), &info));
    TEST_ASSERT_TRUE(info.keyframe);
    TEST_ASSERT_TRUE(info.enc_len > TS_FRAME_BYTES);
}

static void test_daemon_noise_bursts_stay_exact(void)
{
    ts_loop_cfg_t c;
    ts_lb_window_t w;
    lb_cfg(&c);
    c.daemon.kbps_client = TS_GLOBAL_KBPS;
    c.daemon.capture.scene_cut_ms = 1000U;   /* noise frame every second */
    lb_start(&c);
    ts_lb_run(&g_lb, 1000U);
    ts_lb_window(&g_lb, 6000U, &w);
    lb_print_window("enc_max/daemon", &w);
    lb_print_rx("enc_max/daemon", 0U);

    lb_assert_invariants(1U);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(0U, tsr_metrics(g_L.rx[0])->decode_errors, "decode errors on noise frames");
    TEST_ASSERT_TRUE_MESSAGE(g_lb.rx[0].compared >= 100U, "too few frames decoded with noise bursts");
    TEST_ASSERT_TRUE_MESSAGE(w.min_fps_1s[0] > 0U, "a whole second without a decoded frame");
    lb_stop();
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_worst_case_frames_through_the_shim);
    RUN_TEST(test_raw_sized_buffer_is_refused_not_overflowed);
    RUN_TEST(test_daemon_noise_bursts_stay_exact);
    return UNITY_END();
}
