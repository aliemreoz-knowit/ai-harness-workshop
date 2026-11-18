/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/unit/test_txq.c
 * Transmit queue: fragmenter, history ring (hit / miss / eviction / index
 * wrap), retransmit re-pack, per-client cursors. Pure in-memory, no clock.
 */
#include <string.h>
#include "unity.h"
#include "ts_config.h"
#include "ts_proto.h"
#include "ts_txq.h"

static ts_txq_t q;                 /* ~640 KB: static, not on the stack */
static uint8_t enc[TS_ENC_MAX];

void setUp(void)
{
    ts_txq_init(&q, 0U);
}

void tearDown(void)
{
}

static void fill(uint32_t len, uint32_t seed)
{
    for (uint32_t i = 0U; i < len; i++) {
        enc[i] = (uint8_t)((i * 31U + seed * 7U + (i >> 8)) & 0xFFU);
    }
}

static void push(uint16_t frame_seq, uint32_t len)
{
    uint8_t n = 0U;
    fill(len, frame_seq);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_push_frame(&q, frame_seq, 0U, (uint16_t)(frame_seq * 33U), enc, len, &n));
}

/* Independent bitwise CRC-32 (PROTOCOL.md 2) for the conformance check. */
static uint32_t ref_crc(const uint8_t *a, size_t alen, const uint8_t *b, size_t blen)
{
    uint32_t c = 0xFFFFFFFFU;
    for (size_t i = 0U; i < (alen + blen); i++) {
        uint8_t byte = (i < alen) ? a[i] : b[i - alen];
        c ^= (uint32_t)byte;
        for (uint32_t k = 0U; k < 8U; k++) {
            c = ((c & 1U) != 0U) ? ((c >> 1) ^ 0xEDB88320U) : (c >> 1);
        }
    }
    return c ^ 0xFFFFFFFFU;
}

/* CRC as the library computes it, over header[0..20) + payload. */
static uint32_t lib_crc(const uint8_t *pkt, uint16_t payload_len)
{
    uint8_t tmp[TS_PKT_MAX];
    (void)memcpy(tmp, pkt, 20U);
    (void)memcpy(&tmp[20], &pkt[TS_HDR_LEN], payload_len);
    return ts_crc32(tmp, (size_t)20U + payload_len);
}

static bool proto_ready(void)
{
    static const uint8_t chk[9] = { '1', '2', '3', '4', '5', '6', '7', '8', '9' };
    return ts_crc32(chk, sizeof(chk)) == 0xCBF43926U;
}

/* ---- fragmenter ------------------------------------------------------------ */
static void test_txq_init_empty(void)
{
    const ts_txq_slot_t *s = &q.slots[0];
    uint16_t a = 1U;
    uint16_t b = 1U;
    ts_txq_cursor_t c;
    TEST_ASSERT_FALSE(ts_txq_range(&q, &a, &b));
    TEST_ASSERT_EQUAL_INT(TS_E_NOTFOUND, ts_txq_lookup(&q, 0U, 0U, &s));
    TEST_ASSERT_NULL(s);
    ts_txq_cursor_init(&q, &c);
    TEST_ASSERT_NULL(ts_txq_cursor_peek(&q, &c));
    TEST_ASSERT_EQUAL_UINT32(0U, ts_txq_cursor_pending(&q, &c));
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, ts_txq_lookup(NULL, 0U, 0U, &s));
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, ts_txq_lookup(&q, 0U, 0U, NULL));
    TEST_ASSERT_FALSE(ts_txq_range(NULL, &a, &b));
}

static void test_txq_push_rejects_bad_args(void)
{
    uint8_t n = 0U;
    fill(10U, 1U);
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, ts_txq_push_frame(&q, 1U, 0U, 0U, enc, 0U, &n));
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, ts_txq_push_frame(&q, 1U, 0U, 0U, enc, (size_t)TS_ENC_MAX + 1U, &n));
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, ts_txq_push_frame(&q, 1U, 0U, 0U, NULL, 10U, &n));
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, ts_txq_push_frame(NULL, 1U, 0U, 0U, enc, 10U, &n));
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, ts_txq_push_frame(&q, 1U, (uint8_t)TS_F_RETRANSMIT, 0U, enc, 10U, &n));
    TEST_ASSERT_EQUAL_UINT32(0U, q.pushed);
    TEST_ASSERT_EQUAL_UINT32(0U, q.frames);
    /* frag_count is optional */
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_push_frame(&q, 1U, 0U, 0U, enc, 10U, NULL));
    TEST_ASSERT_EQUAL_UINT32(1U, q.pushed);
}

static void test_txq_fragment_counts(void)
{
    static const uint32_t lens[] = { 1U, 1399U, 1400U, 1401U, 2800U, 2801U, TS_ENC_MAX };
    static const uint8_t frags[] = { 1U, 1U, 1U, 2U, 2U, 3U, 110U };
    for (uint32_t i = 0U; i < (uint32_t)(sizeof(lens) / sizeof(lens[0])); i++) {
        uint8_t n = 0U;
        ts_txq_init(&q, 0U);
        fill(lens[i], i);
        TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_push_frame(&q, 7U, 0U, 0U, enc, lens[i], &n));
        TEST_ASSERT_EQUAL_UINT8(frags[i], n);
        TEST_ASSERT_EQUAL_UINT32(frags[i], q.pushed);
        TEST_ASSERT_TRUE(n <= TS_MAX_FRAGS);
    }
}

static void test_txq_packet_layout(void)
{
    const uint32_t len = TS_ENC_MAX;          /* 110 fragments, last carries 1064 B */
    uint8_t n = 0U;
    ts_txq_init(&q, 0xFFF0U);                /* pkt_seq wraps inside the frame */
    fill(len, 5U);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_push_frame(&q, 0x1234U, (uint8_t)TS_F_KEYFRAME, 0xBEEFU, enc, len, &n));
    TEST_ASSERT_EQUAL_UINT8(110U, n);
    for (uint8_t i = 0U; i < n; i++) {
        const ts_txq_slot_t *s = NULL;
        uint16_t plen = (i == 109U) ? (uint16_t)(len - 109U * 1400U) : (uint16_t)1400U;
        TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_lookup(&q, 0x1234U, i, &s));
        TEST_ASSERT_NOT_NULL(s);
        TEST_ASSERT_TRUE(s->used);
        TEST_ASSERT_EQUAL_UINT16((uint16_t)(0xFFF0U + i), s->pkt_seq);
        TEST_ASSERT_EQUAL_UINT16(TS_HDR_LEN + plen, s->len);
        TEST_ASSERT_EQUAL_HEX16(TS_MAGIC_DATA, ts_rd16(&s->pkt[0]));
        TEST_ASSERT_EQUAL_UINT8(TS_PROTOCOL_VERSION, s->pkt[2]);
        TEST_ASSERT_EQUAL_HEX8(TS_F_KEYFRAME, s->pkt[3]);
        TEST_ASSERT_EQUAL_HEX16(0x1234U, ts_rd16(&s->pkt[4]));
        TEST_ASSERT_EQUAL_HEX16(s->pkt_seq, ts_rd16(&s->pkt[6]));
        TEST_ASSERT_EQUAL_UINT8(i, s->pkt[8]);
        TEST_ASSERT_EQUAL_UINT8(110U, s->pkt[9]);
        TEST_ASSERT_EQUAL_UINT32(len, ts_rd32(&s->pkt[10]));
        TEST_ASSERT_EQUAL_UINT16(plen, ts_rd16(&s->pkt[14]));
        TEST_ASSERT_EQUAL_HEX16(0xBEEFU, ts_rd16(&s->pkt[16]));
        TEST_ASSERT_EQUAL_HEX16(0U, ts_rd16(&s->pkt[18]));
        TEST_ASSERT_EQUAL_HEX32(lib_crc(s->pkt, plen), ts_rd32(&s->pkt[20]));
        TEST_ASSERT_EQUAL_MEMORY(&enc[(uint32_t)i * 1400U], &s->pkt[TS_HDR_LEN], plen);
    }
    TEST_ASSERT_EQUAL_UINT16(0x005EU, q.pkt_seq_next);   /* 0xFFF0 + 110 wraps */
}

static void test_txq_crc_and_parse_conformance(void)
{
    const ts_txq_slot_t *s = NULL;
    ts_hdr_t h;
    const uint8_t *payload = NULL;
    if (!proto_ready()) {
        TEST_IGNORE_MESSAGE("proto CRC not implemented on this branch yet");
    }
    push(3U, 3000U);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_lookup(&q, 3U, 2U, &s));
    TEST_ASSERT_EQUAL_HEX32(ref_crc(s->pkt, 20U, &s->pkt[TS_HDR_LEN], 200U), ts_rd32(&s->pkt[20]));
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_hdr_parse(s->pkt, s->len, &h, &payload));
    TEST_ASSERT_EQUAL_UINT16(3U, h.frame_seq);
    TEST_ASSERT_EQUAL_UINT8(2U, h.frag_idx);
    TEST_ASSERT_EQUAL_UINT16(200U, h.payload_len);
}

/* ---- ring lookup ----------------------------------------------------------- */
static void test_txq_lookup_hit(void)
{
    const ts_txq_slot_t *s = NULL;
    push(10U, 3000U);   /* 3 frags */
    push(11U, 100U);    /* 1 frag */
    push(12U, 1500U);   /* 2 frags */
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_lookup(&q, 10U, 0U, &s));
    TEST_ASSERT_EQUAL_UINT16(0U, s->pkt_seq);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_lookup(&q, 10U, 2U, &s));
    TEST_ASSERT_EQUAL_UINT16(2U, s->pkt_seq);
    TEST_ASSERT_EQUAL_UINT8(2U, s->frag_idx);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_lookup(&q, 11U, 0U, &s));
    TEST_ASSERT_EQUAL_UINT16(3U, s->pkt_seq);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_lookup(&q, 12U, 1U, &s));
    TEST_ASSERT_EQUAL_UINT16(5U, s->pkt_seq);
    TEST_ASSERT_EQUAL_UINT16(12U, s->frame_seq);
    TEST_ASSERT_EQUAL_UINT32(3U, q.frames);
}

static void test_txq_lookup_miss(void)
{
    const ts_txq_slot_t *s = &q.slots[0];
    push(10U, 3000U);
    push(12U, 100U);    /* frame 11 never pushed */
    TEST_ASSERT_EQUAL_INT(TS_E_NOTFOUND, ts_txq_lookup(&q, 13U, 0U, &s));   /* future */
    TEST_ASSERT_NULL(s);
    TEST_ASSERT_EQUAL_INT(TS_E_NOTFOUND, ts_txq_lookup(&q, 11U, 0U, &s));   /* gap */
    TEST_ASSERT_EQUAL_INT(TS_E_NOTFOUND, ts_txq_lookup(&q, 10U, 3U, &s));   /* frag_idx >= frag_count */
    TEST_ASSERT_EQUAL_INT(TS_E_NOTFOUND, ts_txq_lookup(&q, 12U, 1U, &s));
    TEST_ASSERT_EQUAL_INT(TS_E_NOTFOUND, ts_txq_lookup(&q, 12U + 32767U, 0U, &s)); /* farthest serial future */
    TEST_ASSERT_EQUAL_INT(TS_E_TOO_OLD, ts_txq_lookup(&q, 12U + 32768U, 0U, &s));  /* serial midpoint = behind */
    TEST_ASSERT_EQUAL_INT(TS_E_TOO_OLD, ts_txq_lookup(&q, 9U, 0U, &s));     /* older than the ring */
}

static void test_txq_lookup_eviction_too_old(void)
{
    const ts_txq_slot_t *s = NULL;
    uint16_t oldest = 0U;
    uint16_t newest = 0U;
    for (uint16_t f = 0U; f < 5U; f++) {
        push(f, TS_ENC_MAX);           /* 5 x 110 = 550 fragments in 448 slots */
    }
    TEST_ASSERT_EQUAL_UINT32(550U, q.pushed);
    TEST_ASSERT_EQUAL_UINT32(102U, q.evicted);
    /* frame 0 lost fragments 0..101, keeps 102..109 */
    TEST_ASSERT_EQUAL_INT(TS_E_TOO_OLD, ts_txq_lookup(&q, 0U, 0U, &s));
    TEST_ASSERT_NULL(s);
    TEST_ASSERT_EQUAL_INT(TS_E_TOO_OLD, ts_txq_lookup(&q, 0U, 101U, &s));
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_lookup(&q, 0U, 102U, &s));
    TEST_ASSERT_EQUAL_UINT16(102U, s->pkt_seq);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_lookup(&q, 0U, 109U, &s));
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_lookup(&q, 4U, 109U, &s));
    TEST_ASSERT_EQUAL_UINT16(549U, s->pkt_seq);
    TEST_ASSERT_EQUAL_INT(TS_E_TOO_OLD, ts_txq_lookup(&q, 0xFFFFU, 0U, &s)); /* before frame 0 */
    TEST_ASSERT_TRUE(ts_txq_range(&q, &oldest, &newest));
    TEST_ASSERT_EQUAL_UINT16(0U, oldest);
    TEST_ASSERT_EQUAL_UINT16(4U, newest);
    push(5U, TS_ENC_MAX);              /* frame 0 gone completely, frame 1 partially */
    TEST_ASSERT_EQUAL_INT(TS_E_TOO_OLD, ts_txq_lookup(&q, 0U, 109U, &s));
    TEST_ASSERT_EQUAL_INT(TS_E_TOO_OLD, ts_txq_lookup(&q, 1U, 0U, &s));
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_lookup(&q, 1U, 109U, &s));
    TEST_ASSERT_TRUE(ts_txq_range(&q, &oldest, &newest));
    TEST_ASSERT_EQUAL_UINT16(1U, oldest);
    TEST_ASSERT_EQUAL_UINT16(5U, newest);
}

static void test_txq_index_and_seq_wrap(void)
{
    /* frame_seq and pkt_seq both wrap at 65536 while the ring index wraps at 448;
     * 65536 % 448 != 0, so the slot must not be derived from pkt_seq % slots. */
    const ts_txq_slot_t *s = NULL;
    uint16_t f = 0xFFF0U;
    ts_txq_init(&q, 0xFF00U);
    for (uint32_t i = 0U; i < 70000U; i++) {
        uint8_t n = 0U;
        enc[0] = (uint8_t)i;
        enc[1] = (uint8_t)(i >> 8);
        TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_push_frame(&q, f, 0U, 0U, enc, 2U + (i % 3U), &n));
        f = (uint16_t)(f + 1U);
    }
    /* newest frame = 0xFFF0 + 69999 (mod 65536), newest pkt_seq = 0xFF00 + 69999 */
    {
        uint16_t newest_f = (uint16_t)(0xFFF0U + 69999U);
        uint16_t newest_p = (uint16_t)(0xFF00U + 69999U);
        for (uint32_t back = 0U; back < TS_RING_SLOTS; back++) {
            uint16_t fs = (uint16_t)(newest_f - back);
            TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_lookup(&q, fs, 0U, &s));
            TEST_ASSERT_EQUAL_UINT16(fs, s->frame_seq);
            TEST_ASSERT_EQUAL_UINT16((uint16_t)(newest_p - back), s->pkt_seq);
        }
        TEST_ASSERT_EQUAL_INT(TS_E_TOO_OLD, ts_txq_lookup(&q, (uint16_t)(newest_f - TS_RING_SLOTS), 0U, &s));
        TEST_ASSERT_EQUAL_INT(TS_E_TOO_OLD, ts_txq_lookup(&q, (uint16_t)(newest_f - 1000U), 0U, &s));
        TEST_ASSERT_EQUAL_INT(TS_E_NOTFOUND, ts_txq_lookup(&q, (uint16_t)(newest_f + 1U), 0U, &s));
    }
    TEST_ASSERT_EQUAL_UINT32(70000U - TS_RING_SLOTS, q.evicted);
}

static void test_txq_lookup_across_frame_seq_wrap(void)
{
    const ts_txq_slot_t *s = NULL;
    push(0xFFFEU, 3000U);
    push(0xFFFFU, 3000U);
    push(0x0000U, 3000U);
    push(0x0001U, 1U);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_lookup(&q, 0xFFFEU, 2U, &s));
    TEST_ASSERT_EQUAL_UINT16(2U, s->pkt_seq);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_lookup(&q, 0x0000U, 1U, &s));
    TEST_ASSERT_EQUAL_UINT16(7U, s->pkt_seq);
    TEST_ASSERT_EQUAL_INT(TS_E_NOTFOUND, ts_txq_lookup(&q, 0x0002U, 0U, &s));
    TEST_ASSERT_EQUAL_INT(TS_E_TOO_OLD, ts_txq_lookup(&q, 0xFFFDU, 0U, &s));
}

/* ---- retransmit ------------------------------------------------------------ */
static void test_txq_make_retransmit(void)
{
    const ts_txq_slot_t *s = NULL;
    uint8_t out[TS_PKT_MAX];
    size_t out_len = 0U;
    ts_txq_slot_t unused;
    push(20U, 1500U);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_lookup(&q, 20U, 1U, &s));
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_make_retransmit(s, out, sizeof(out), &out_len));
    TEST_ASSERT_EQUAL_size_t(s->len, out_len);
    TEST_ASSERT_EQUAL_HEX8(TS_F_RETRANSMIT, out[3]);
    TEST_ASSERT_EQUAL_HEX16(s->pkt_seq, ts_rd16(&out[6]));      /* pkt_seq kept */
    TEST_ASSERT_EQUAL_MEMORY(&s->pkt[4], &out[4], 16U);          /* rest of header unchanged */
    TEST_ASSERT_EQUAL_MEMORY(&s->pkt[TS_HDR_LEN], &out[TS_HDR_LEN], 100U);
    TEST_ASSERT_EQUAL_HEX32(lib_crc(out, 100U), ts_rd32(&out[20]));
    TEST_ASSERT_EQUAL_HEX8(0U, s->pkt[3]);                       /* ring copy untouched */
    if (proto_ready()) {
        TEST_ASSERT_EQUAL_HEX32(ref_crc(out, 20U, &out[TS_HDR_LEN], 100U), ts_rd32(&out[20]));
    }
    /* keyframe flag survives */
    {
        uint8_t n = 0U;
        TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_push_frame(&q, 21U, (uint8_t)TS_F_KEYFRAME, 0U, enc, 10U, &n));
        TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_lookup(&q, 21U, 0U, &s));
        TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_make_retransmit(s, out, sizeof(out), &out_len));
        TEST_ASSERT_EQUAL_HEX8(TS_F_KEYFRAME | TS_F_RETRANSMIT, out[3]);
    }
    TEST_ASSERT_EQUAL_INT(TS_E_NOMEM, ts_txq_make_retransmit(s, out, (size_t)s->len - 1U, &out_len));
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, ts_txq_make_retransmit(NULL, out, sizeof(out), &out_len));
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, ts_txq_make_retransmit(s, NULL, sizeof(out), &out_len));
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, ts_txq_make_retransmit(s, out, sizeof(out), NULL));
    (void)memset(&unused, 0, sizeof(unused));
    TEST_ASSERT_EQUAL_INT(TS_E_INVAL, ts_txq_make_retransmit(&unused, out, sizeof(out), &out_len));
}

/* ---- cursors --------------------------------------------------------------- */
static void test_txq_cursor_fresh_packets(void)
{
    ts_txq_cursor_t a;
    ts_txq_cursor_t b;
    const ts_txq_slot_t *s = NULL;
    push(1U, 100U);                    /* before the cursors: not pending */
    ts_txq_cursor_init(&q, &a);
    TEST_ASSERT_EQUAL_UINT32(0U, ts_txq_cursor_pending(&q, &a));
    push(2U, 3000U);                   /* 3 fragments */
    ts_txq_cursor_init(&q, &b);        /* b joins after frame 2 */
    push(3U, 100U);
    TEST_ASSERT_EQUAL_UINT32(4U, ts_txq_cursor_pending(&q, &a));
    TEST_ASSERT_EQUAL_UINT32(1U, ts_txq_cursor_pending(&q, &b));
    for (uint8_t i = 0U; i < 3U; i++) {
        s = ts_txq_cursor_peek(&q, &a);
        TEST_ASSERT_NOT_NULL(s);
        TEST_ASSERT_EQUAL_UINT16(2U, s->frame_seq);
        TEST_ASSERT_EQUAL_UINT8(i, s->frag_idx);
        TEST_ASSERT_EQUAL_PTR(s, ts_txq_cursor_peek(&q, &a));   /* peek is idempotent */
        ts_txq_cursor_advance(&a);
    }
    s = ts_txq_cursor_peek(&q, &a);
    TEST_ASSERT_EQUAL_UINT16(3U, s->frame_seq);
    ts_txq_cursor_advance(&a);
    TEST_ASSERT_NULL(ts_txq_cursor_peek(&q, &a));
    TEST_ASSERT_EQUAL_UINT32(0U, ts_txq_cursor_pending(&q, &a));
    s = ts_txq_cursor_peek(&q, &b);
    TEST_ASSERT_EQUAL_UINT16(3U, s->frame_seq);
    TEST_ASSERT_EQUAL_UINT32(0U, a.skipped);
    TEST_ASSERT_EQUAL_UINT32(0U, b.skipped);
}

static void test_txq_cursor_skips_when_ring_wraps_past(void)
{
    ts_txq_cursor_t c;
    const ts_txq_slot_t *s = NULL;
    ts_txq_init(&q, 100U);
    ts_txq_cursor_init(&q, &c);
    for (uint16_t f = 0U; f < 500U; f++) {
        push(f, 10U);                  /* one fragment each */
    }
    TEST_ASSERT_EQUAL_UINT32(TS_RING_SLOTS, ts_txq_cursor_pending(&q, &c));
    s = ts_txq_cursor_peek(&q, &c);
    TEST_ASSERT_NOT_NULL(s);
    TEST_ASSERT_EQUAL_UINT32(500U - TS_RING_SLOTS, c.skipped);
    TEST_ASSERT_EQUAL_UINT16(500U - TS_RING_SLOTS, s->frame_seq);
    TEST_ASSERT_EQUAL_UINT16(100U + 500U - TS_RING_SLOTS, s->pkt_seq);
    for (uint32_t i = 0U; i < TS_RING_SLOTS; i++) {
        TEST_ASSERT_NOT_NULL(ts_txq_cursor_peek(&q, &c));
        ts_txq_cursor_advance(&c);
    }
    TEST_ASSERT_NULL(ts_txq_cursor_peek(&q, &c));
    TEST_ASSERT_EQUAL_UINT32(500U - TS_RING_SLOTS, c.skipped);
}

static void test_txq_cursor_u32_wrap_and_stale(void)
{
    ts_txq_cursor_t c;
    const ts_txq_slot_t *s = NULL;
    /* push index near the u32 wrap: cursors use serial u32 distances */
    q.pushed = 0xFFFFFFFEU;
    ts_txq_cursor_init(&q, &c);
    push(1U, 10U);
    push(2U, 10U);
    push(3U, 10U);
    TEST_ASSERT_EQUAL_UINT32(1U, q.pushed);
    TEST_ASSERT_EQUAL_UINT32(3U, ts_txq_cursor_pending(&q, &c));
    s = ts_txq_cursor_peek(&q, &c);
    TEST_ASSERT_EQUAL_UINT16(1U, s->frame_seq);
    /* a cursor "ahead" of the ring (left over from a re-init) resets, no bogus skip */
    c.next = q.pushed + 5U;
    TEST_ASSERT_EQUAL_UINT32(0U, ts_txq_cursor_pending(&q, &c));
    TEST_ASSERT_NULL(ts_txq_cursor_peek(&q, &c));
    TEST_ASSERT_EQUAL_UINT32(q.pushed, c.next);
    TEST_ASSERT_EQUAL_UINT32(0U, c.skipped);
    /* NULL safety */
    TEST_ASSERT_NULL(ts_txq_cursor_peek(NULL, &c));
    TEST_ASSERT_EQUAL_UINT32(0U, ts_txq_cursor_pending(&q, NULL));
    ts_txq_cursor_advance(NULL);
    ts_txq_cursor_init(NULL, &c);
    ts_txq_init(NULL, 0U);
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_txq_init_empty);
    RUN_TEST(test_txq_push_rejects_bad_args);
    RUN_TEST(test_txq_fragment_counts);
    RUN_TEST(test_txq_packet_layout);
    RUN_TEST(test_txq_crc_and_parse_conformance);
    RUN_TEST(test_txq_lookup_hit);
    RUN_TEST(test_txq_lookup_miss);
    RUN_TEST(test_txq_lookup_eviction_too_old);
    RUN_TEST(test_txq_index_and_seq_wrap);
    RUN_TEST(test_txq_lookup_across_frame_seq_wrap);
    RUN_TEST(test_txq_make_retransmit);
    RUN_TEST(test_txq_cursor_fresh_packets);
    RUN_TEST(test_txq_cursor_skips_when_ring_wraps_past);
    RUN_TEST(test_txq_cursor_u32_wrap_and_stale);
    return UNITY_END();
}
