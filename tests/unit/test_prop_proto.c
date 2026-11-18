/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/unit/test_prop_proto.c
 * Property tests for src/proto (labels unit + property, gate property-tests):
 *   - DATA header round-trip for random valid headers (pack -> parse == input),
 *   - NACK round-trip for random entry lists,
 *   - TLV streams of random messages fed in random chunk sizes come out intact,
 *   - serial arithmetic: antisymmetry and order across the u16 wrap.
 * Deterministic: xorshift32 with fixed seeds, no clock, no sockets.
 */
#include <string.h>
#include "unity.h"
#include "ts_config.h"
#include "ts_proto.h"

void setUp(void)
{
}

void tearDown(void)
{
}

static uint32_t rnd(uint32_t *s)
{
    uint32_t x = *s;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    *s = x;
    return x;
}

static void test_prop_hdr_roundtrip(void)
{
    static uint8_t payload[TS_MTU_PAYLOAD];
    static uint8_t pkt[TS_PKT_MAX];
    uint32_t s = 0xC0FFEEU;
    for (uint32_t it = 0U; it < 5000U; it++) {
        ts_hdr_t h;
        ts_hdr_t p;
        const uint8_t *pl = NULL;
        size_t len = 0U;
        const uint32_t frame_len = 1U + (rnd(&s) % TS_ENC_MAX);
        const uint32_t count = (frame_len + TS_MTU_PAYLOAD - 1U) / TS_MTU_PAYLOAD;
        const uint32_t idx = rnd(&s) % count;
        const uint32_t plen = (idx + 1U < count) ? TS_MTU_PAYLOAD : (frame_len - (idx * TS_MTU_PAYLOAD));
        for (uint32_t i = 0U; i < plen; i++) {
            payload[i] = (uint8_t)rnd(&s);
        }
        (void)memset(&h, 0, sizeof(h));
        h.flags = (uint8_t)(rnd(&s) & 3U);
        h.frame_seq = (uint16_t)rnd(&s);
        h.pkt_seq = (uint16_t)rnd(&s);
        h.frag_idx = (uint8_t)idx;
        h.frag_count = (uint8_t)count;
        h.frame_len = frame_len;
        h.payload_len = (uint16_t)plen;
        h.ts_ms = (uint16_t)rnd(&s);
        TEST_ASSERT_EQUAL(TS_OK, ts_hdr_pack(&h, payload, pkt, sizeof(pkt), &len));
        TEST_ASSERT_EQUAL_size_t(TS_HDR_LEN + plen, len);
        (void)memset(&p, 0, sizeof(p));
        TEST_ASSERT_EQUAL(TS_OK, ts_hdr_parse(pkt, len, &p, &pl));
        TEST_ASSERT_EQUAL_MEMORY(&h, &p, sizeof(h));
        TEST_ASSERT_EQUAL_MEMORY(payload, pl, plen);
        /* any single corrupted byte is rejected */
        {
            const size_t at = (size_t)(rnd(&s) % (uint32_t)len);
            pkt[at] = (uint8_t)(pkt[at] ^ (uint8_t)(1U + (rnd(&s) % 255U)));
            TEST_ASSERT_NOT_EQUAL(TS_OK, ts_hdr_parse(pkt, len, &p, &pl));
            TEST_ASSERT_NULL(pl);
        }
    }
}

static void test_prop_nack_roundtrip(void)
{
    static uint8_t pkt[TS_NACK_PKT_MAX];
    uint32_t s = 0xBADC0DEU;
    for (uint32_t it = 0U; it < 2000U; it++) {
        ts_nack_t n;
        ts_nack_t m;
        size_t len = 0U;
        (void)memset(&n, 0, sizeof(n));
        n.count = (uint8_t)(rnd(&s) % (TS_NACK_MAX + 1U));
        for (uint32_t i = 0U; i < n.count; i++) {
            n.e[i].frame_seq = (uint16_t)rnd(&s);
            n.e[i].frag_idx = (uint8_t)(rnd(&s) % TS_MAX_FRAGS);
        }
        TEST_ASSERT_EQUAL(TS_OK, ts_nack_pack(&n, pkt, sizeof(pkt), &len));
        TEST_ASSERT_EQUAL_size_t(4U + (4U * (size_t)n.count), len);
        (void)memset(&m, 0xFF, sizeof(m));
        TEST_ASSERT_EQUAL(TS_OK, ts_nack_parse(pkt, len, &m));
        TEST_ASSERT_EQUAL_UINT8(n.count, m.count);
        for (uint32_t i = 0U; i < n.count; i++) {
            TEST_ASSERT_EQUAL_UINT16(n.e[i].frame_seq, m.e[i].frame_seq);
            TEST_ASSERT_EQUAL_UINT8(n.e[i].frag_idx, m.e[i].frag_idx);
        }
        if (len > 4U) {
            TEST_ASSERT_EQUAL(TS_E_TRUNC, ts_nack_parse(pkt, len - 1U, &m));
        }
    }
}

typedef struct msg {
    uint8_t  type;
    uint16_t len;
    uint32_t tag;
} msg_t;

static void test_prop_tlv_stream_random_chunks(void)
{
    static ts_tlv_acc_t acc;
    static uint8_t stream[64U * (TS_TLV_HDR_LEN + TS_TLV_MAX_LEN)];
    static uint8_t value[TS_TLV_MAX_LEN];
    static msg_t msgs[64];
    uint32_t s = 0x5EED1U;
    for (uint32_t it = 0U; it < 100U; it++) {
        const uint32_t nmsg = 1U + (rnd(&s) % 64U);
        size_t n = 0U;
        size_t pos = 0U;
        uint32_t got = 0U;
        ts_tlv_t t;
        for (uint32_t i = 0U; i < nmsg; i++) {
            size_t k = 0U;
            msgs[i].type = (uint8_t)(1U + (rnd(&s) % 6U));
            msgs[i].len = (uint16_t)(((rnd(&s) % 4U) == 0U) ? (rnd(&s) % (TS_TLV_MAX_LEN + 1U)) : (rnd(&s) % 16U));
            msgs[i].tag = rnd(&s);
            for (uint32_t b = 0U; b < msgs[i].len; b++) {
                value[b] = (uint8_t)(msgs[i].tag + b);
            }
            TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack(msgs[i].type, (uint8_t)rnd(&s), value, msgs[i].len,
                                                 &stream[n], sizeof(stream) - n, &k));
            n += k;
        }
        ts_tlv_acc_init(&acc);
        while (pos < n) {
            const size_t room = (size_t)TS_TLV_ACC_CAP - (size_t)ts_tlv_acc_pending(&acc);
            size_t chunk = 1U + (size_t)(rnd(&s) % 700U);
            if (chunk > (n - pos)) {
                chunk = n - pos;
            }
            if (chunk > room) {
                chunk = room;      /* a recv() never asks for more than the free space */
            }
            TEST_ASSERT_EQUAL(TS_OK, ts_tlv_acc_push(&acc, &stream[pos], chunk));
            pos += chunk;
            while (ts_tlv_acc_next(&acc, &t) == TS_OK) {
                TEST_ASSERT_TRUE(got < nmsg);
                TEST_ASSERT_EQUAL_UINT8(msgs[got].type, t.type);
                TEST_ASSERT_EQUAL_UINT16(msgs[got].len, t.len);
                for (uint32_t b = 0U; b < t.len; b++) {
                    TEST_ASSERT_EQUAL_HEX8((uint8_t)(msgs[got].tag + b), t.value[b]);
                }
                got++;
            }
        }
        TEST_ASSERT_EQUAL_UINT32(nmsg, got);
        TEST_ASSERT_EQUAL_UINT16(0U, ts_tlv_acc_pending(&acc));
    }
}

static void test_prop_seq_serial_order(void)
{
    uint32_t s = 0xABCDEFU;
    for (uint32_t it = 0U; it < 200000U; it++) {
        const uint16_t a = (uint16_t)rnd(&s);
        const uint16_t step = (uint16_t)(1U + (rnd(&s) % 32767U));   /* 1 .. 32767 ahead */
        const uint16_t b = (uint16_t)(a + step);
        const int16_t d = ts_seq_diff(b, a);
        TEST_ASSERT_EQUAL_INT32((int32_t)step, (int32_t)d);
        TEST_ASSERT_EQUAL_INT32(-(int32_t)d, (int32_t)ts_seq_diff(a, b));
        TEST_ASSERT_TRUE(ts_seq_after(b, a));
        TEST_ASSERT_FALSE(ts_seq_after(a, b));
    }
}

static void test_prop_pkt_seq_stream_across_wrap(void)
{
    /* A receiver tracking the highest pkt_seq with ts_seq_after over 200k
     * packets starting near the wrap. */
    uint16_t highest = 65000U;
    uint32_t advanced = 0U;
    for (uint32_t i = 1U; i <= 200000U; i++) {
        const uint16_t seq = (uint16_t)(65000U + i);
        const uint16_t late = (uint16_t)(seq - 5U);          /* reordered packet */
        TEST_ASSERT_TRUE(ts_seq_after(seq, highest));
        TEST_ASSERT_FALSE(ts_seq_after(late, seq));
        highest = seq;
        advanced++;
    }
    TEST_ASSERT_EQUAL_UINT32(200000U, advanced);
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_prop_hdr_roundtrip);
    RUN_TEST(test_prop_nack_roundtrip);
    RUN_TEST(test_prop_tlv_stream_random_chunks);
    RUN_TEST(test_prop_seq_serial_order);
    RUN_TEST(test_prop_pkt_seq_stream_across_wrap);
    return UNITY_END();
}
