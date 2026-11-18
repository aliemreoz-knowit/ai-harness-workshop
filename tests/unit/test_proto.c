/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/unit/test_proto.c
 * Unit tests for libtsproto wire format (src/proto): CRC32, serial arithmetic,
 * DATA header, NACK, TLV packers/parsers and the accumulate parser. Every
 * vector in tests/vectors/ts_vectors.h (generated from PROTOCOL.md by the
 * independent Python implementation) is checked in both directions.
 */
#include <string.h>
#include "unity.h"
#include "ts_config.h"
#include "ts_proto.h"
#include "ts_vectors.h"

void setUp(void)
{
}

void tearDown(void)
{
}

static const char *err_name(const char *vec_error)
{
    return (vec_error != NULL) ? vec_error : "TS_OK";
}

/* ---- CRC32 ----------------------------------------------------------------- */
static void test_crc_vectors(void)
{
    for (size_t i = 0U; i < tsv_crc_count; i++) {
        const tsv_crc_t *v = &tsv_crc[i];
        TEST_ASSERT_EQUAL_HEX32_MESSAGE(v->crc, ts_crc32(v->data, v->len), v->name);
    }
}

static void test_crc_streaming_any_split(void)
{
    for (size_t i = 0U; i < tsv_crc_count; i++) {
        const tsv_crc_t *v = &tsv_crc[i];
        for (size_t cut = 0U; cut <= v->len; cut++) {
            uint32_t s = ts_crc32_init();
            s = ts_crc32_update(s, v->data, cut);
            s = ts_crc32_update(s, &v->data[cut], v->len - cut);
            TEST_ASSERT_EQUAL_HEX32_MESSAGE(v->crc, ts_crc32_final(s), v->name);
        }
    }
}

static void test_crc_lengths_and_empty_input(void)
{
    /* ts_crc32 over two lengths of one buffer, and over no data at all */
    const uint8_t five[8] = { 1U, 2U, 3U, 4U, 5U, 0U, 0U, 0U };
    TEST_ASSERT_NOT_EQUAL(ts_crc32(five, 5U), ts_crc32(five, 8U));
    TEST_ASSERT_EQUAL_HEX32(0U, ts_crc32(NULL, 0U));
    TEST_ASSERT_EQUAL_HEX32(0xFFFFFFFFU, ts_crc32_update(0xFFFFFFFFU, NULL, 10U));
}

/* ---- serial arithmetic ------------------------------------------------------ */
static void test_seq_examples(void)
{
    TEST_ASSERT_TRUE(ts_seq_after(0x0001U, 0xFFFFU));
    TEST_ASSERT_EQUAL_INT16(2, ts_seq_diff(0x0001U, 0xFFFFU));
    TEST_ASSERT_FALSE(ts_seq_after(0xFFFFU, 0x0001U));
    TEST_ASSERT_EQUAL_INT16(-2, ts_seq_diff(0xFFFFU, 0x0001U));
    TEST_ASSERT_EQUAL_INT16(0, ts_seq_diff(1234U, 1234U));
    TEST_ASSERT_FALSE(ts_seq_after(1234U, 1234U));
    /* midpoint: exactly 32768 apart is -32768 in both directions, never "after" */
    TEST_ASSERT_EQUAL_INT16(-32768, ts_seq_diff(32768U, 0U));
    TEST_ASSERT_EQUAL_INT16(-32768, ts_seq_diff(0U, 32768U));
    TEST_ASSERT_FALSE(ts_seq_after(32768U, 0U));
    TEST_ASSERT_FALSE(ts_seq_after(0U, 32768U));
    TEST_ASSERT_EQUAL_INT16(32767, ts_seq_diff(32767U, 0U));
    TEST_ASSERT_TRUE(ts_seq_after(32767U, 0U));
    TEST_ASSERT_EQUAL_INT16(-32767, ts_seq_diff(0U, 32767U));
}

static void test_seq_wrap_every_value(void)
{
    /* For every counter value: the successor is after it, across the wrap. */
    for (uint32_t a = 0U; a <= 0xFFFFU; a++) {
        const uint16_t x = (uint16_t)a;
        const uint16_t next = (uint16_t)(a + 1U);
        const uint16_t far = (uint16_t)(a + 32767U);
        const uint16_t mid = (uint16_t)(a + 32768U);
        TEST_ASSERT_TRUE(ts_seq_after(next, x));
        TEST_ASSERT_FALSE(ts_seq_after(x, next));
        TEST_ASSERT_EQUAL_INT16(1, ts_seq_diff(next, x));
        TEST_ASSERT_EQUAL_INT16(-1, ts_seq_diff(x, next));
        TEST_ASSERT_EQUAL_INT16(32767, ts_seq_diff(far, x));
        TEST_ASSERT_EQUAL_INT16(-32768, ts_seq_diff(mid, x));
        TEST_ASSERT_FALSE(ts_seq_after(mid, x));
    }
}

static void test_seq_ts_ms_latency_across_wrap(void)
{
    /* ts_ms latency = (uint16_t)(rx_ms - ts_ms) through ts_seq_diff */
    const uint16_t ts_ms = 65530U;
    const uint16_t rx_ms = 5U;          /* 11 ms later, after the wrap */
    TEST_ASSERT_EQUAL_INT16(11, ts_seq_diff(rx_ms, ts_ms));
    TEST_ASSERT_TRUE(ts_seq_after(rx_ms, ts_ms));
    /* a stale timestamp from "the future" is negative, not 65k ms */
    TEST_ASSERT_EQUAL_INT16(-11, ts_seq_diff(ts_ms, rx_ms));
}

/* ---- DATA header ------------------------------------------------------------- */
static void test_hdr_vectors_parse(void)
{
    for (size_t i = 0U; i < tsv_hdr_count; i++) {
        const tsv_hdr_t *v = &tsv_hdr[i];
        ts_hdr_t h;
        const uint8_t *pl = (const uint8_t *)1;   /* must be overwritten */
        const ts_err_t e = ts_hdr_parse(v->pkt, v->len, &h, &pl);
        TEST_ASSERT_EQUAL_STRING_MESSAGE(err_name(v->error), ts_err_str(e), v->name);
        if (v->error == NULL) {
            TEST_ASSERT_EQUAL_UINT8_MESSAGE(v->ver, h.ver, v->name);
            TEST_ASSERT_EQUAL_UINT8_MESSAGE(v->flags, h.flags, v->name);
            TEST_ASSERT_EQUAL_UINT16_MESSAGE(v->frame_seq, h.frame_seq, v->name);
            TEST_ASSERT_EQUAL_UINT16_MESSAGE(v->pkt_seq, h.pkt_seq, v->name);
            TEST_ASSERT_EQUAL_UINT8_MESSAGE(v->frag_idx, h.frag_idx, v->name);
            TEST_ASSERT_EQUAL_UINT8_MESSAGE(v->frag_count, h.frag_count, v->name);
            TEST_ASSERT_EQUAL_UINT32_MESSAGE(v->frame_len, h.frame_len, v->name);
            TEST_ASSERT_EQUAL_UINT16_MESSAGE(v->payload_len, h.payload_len, v->name);
            TEST_ASSERT_EQUAL_UINT16_MESSAGE(v->ts_ms, h.ts_ms, v->name);
            TEST_ASSERT_EQUAL_HEX32_MESSAGE(v->crc32, h.crc32, v->name);
            TEST_ASSERT_TRUE_MESSAGE(pl == &v->pkt[TS_HDR_LEN], v->name);
        } else {
            TEST_ASSERT_NULL_MESSAGE(pl, v->name);
        }
    }
}

static void test_hdr_vectors_pack_bit_exact(void)
{
    static uint8_t out[TS_PKT_MAX];
    for (size_t i = 0U; i < tsv_hdr_count; i++) {
        const tsv_hdr_t *v = &tsv_hdr[i];
        ts_hdr_t h;
        size_t out_len = 0U;
        if (v->error != NULL) {
            continue;
        }
        (void)memset(&h, 0, sizeof(h));
        h.flags = v->flags;
        h.frame_seq = v->frame_seq;
        h.pkt_seq = v->pkt_seq;
        h.frag_idx = v->frag_idx;
        h.frag_count = v->frag_count;
        h.frame_len = v->frame_len;
        h.payload_len = v->payload_len;
        h.ts_ms = v->ts_ms;
        TEST_ASSERT_EQUAL_STRING_MESSAGE("TS_OK",
            ts_err_str(ts_hdr_pack(&h, &v->pkt[TS_HDR_LEN], out, sizeof(out), &out_len)), v->name);
        TEST_ASSERT_EQUAL_size_t_MESSAGE(v->len, out_len, v->name);
        TEST_ASSERT_EQUAL_UINT8(TS_PROTOCOL_VERSION, h.ver);
        if (strcmp(v->name, "reserved_nonzero_ignored") == 0) {
            /* we always send reserved = 0, so only the reserved bytes and the CRC differ */
            TEST_ASSERT_EQUAL_MEMORY(v->pkt, out, 18U);
            TEST_ASSERT_EQUAL_HEX8(0U, out[18]);
            TEST_ASSERT_EQUAL_HEX8(0U, out[19]);
            TEST_ASSERT_EQUAL_MEMORY(&v->pkt[TS_HDR_LEN], &out[TS_HDR_LEN], v->payload_len);
        } else {
            TEST_ASSERT_EQUAL_HEX8_ARRAY_MESSAGE(v->pkt, out, v->len, v->name);
            TEST_ASSERT_EQUAL_HEX32_MESSAGE(v->crc32, h.crc32, v->name);
        }
    }
}

static void fill_hdr(ts_hdr_t *h, uint8_t idx, uint8_t count, uint32_t frame_len, uint16_t payload_len)
{
    (void)memset(h, 0, sizeof(*h));
    h->flags = TS_F_KEYFRAME;
    h->frame_seq = 10U;
    h->pkt_seq = 20U;
    h->frag_idx = idx;
    h->frag_count = count;
    h->frame_len = frame_len;
    h->payload_len = payload_len;
    h->ts_ms = 30U;
}

static void test_hdr_pack_rejects_bad_fields(void)
{
    static uint8_t payload[TS_MTU_PAYLOAD + 1U];
    static uint8_t out[TS_PKT_MAX + 8U];
    ts_hdr_t h;
    size_t n = 0U;

    fill_hdr(&h, 0U, 1U, 10U, 10U);
    TEST_ASSERT_EQUAL(TS_OK, ts_hdr_pack(&h, payload, out, sizeof(out), &n));
    TEST_ASSERT_EQUAL_size_t(34U, n);

    fill_hdr(&h, 0U, 1U, 10U, 0U);       /* payload_len 0 */
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_hdr_pack(&h, payload, out, sizeof(out), &n));
    fill_hdr(&h, 0U, 1U, 1401U, 1401U);  /* payload_len > MTU */
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_hdr_pack(&h, payload, out, sizeof(out), &n));
    fill_hdr(&h, 0U, 0U, 10U, 10U);      /* frag_count 0 */
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_hdr_pack(&h, payload, out, sizeof(out), &n));
    fill_hdr(&h, 0U, 111U, 10U, 1400U);  /* frag_count > max */
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_hdr_pack(&h, payload, out, sizeof(out), &n));
    fill_hdr(&h, 1U, 1U, 10U, 10U);      /* idx >= count */
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_hdr_pack(&h, payload, out, sizeof(out), &n));
    fill_hdr(&h, 0U, 1U, 0U, 10U);       /* frame_len 0 */
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_hdr_pack(&h, payload, out, sizeof(out), &n));
    fill_hdr(&h, 0U, 1U, TS_ENC_MAX + 1U, 10U);
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_hdr_pack(&h, payload, out, sizeof(out), &n));
    fill_hdr(&h, 0U, 2U, 3000U, 100U);   /* non-last fragment short */
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_hdr_pack(&h, payload, out, sizeof(out), &n));
    fill_hdr(&h, 1U, 2U, 3000U, 100U);   /* last fragment does not end the frame */
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_hdr_pack(&h, payload, out, sizeof(out), &n));
    fill_hdr(&h, 0U, 1U, 5U, 10U);       /* fragment beyond frame_len */
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_hdr_pack(&h, payload, out, sizeof(out), &n));
    fill_hdr(&h, 0U, 1U, 10U, 10U);
    h.flags = 0x04U;                     /* reserved flag bit */
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_hdr_pack(&h, payload, out, sizeof(out), &n));
    fill_hdr(&h, 0U, 1U, 10U, 10U);
    TEST_ASSERT_EQUAL(TS_E_NOMEM, ts_hdr_pack(&h, payload, out, 33U, &n));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_hdr_pack(NULL, payload, out, sizeof(out), &n));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_hdr_pack(&h, NULL, out, sizeof(out), &n));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_hdr_pack(&h, payload, NULL, sizeof(out), &n));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_hdr_pack(&h, payload, out, sizeof(out), NULL));
}

static void test_hdr_pack_max_frame_geometry(void)
{
    /* the largest legal frame: TS_ENC_MAX bytes in 110 fragments */
    static uint8_t payload[TS_MTU_PAYLOAD];
    static uint8_t out[TS_PKT_MAX];
    const uint32_t last_len = TS_ENC_MAX - (109U * TS_MTU_PAYLOAD);
    ts_hdr_t h;
    ts_hdr_t p;
    const uint8_t *pl = NULL;
    size_t n = 0U;
    fill_hdr(&h, 108U, 110U, TS_ENC_MAX, (uint16_t)TS_MTU_PAYLOAD);
    TEST_ASSERT_EQUAL(TS_OK, ts_hdr_pack(&h, payload, out, sizeof(out), &n));
    fill_hdr(&h, 109U, 110U, TS_ENC_MAX, (uint16_t)last_len);
    TEST_ASSERT_EQUAL(TS_OK, ts_hdr_pack(&h, payload, out, sizeof(out), &n));
    TEST_ASSERT_EQUAL(TS_OK, ts_hdr_parse(out, n, &p, &pl));
    TEST_ASSERT_EQUAL_UINT32(TS_ENC_MAX, p.frame_len);
    TEST_ASSERT_EQUAL_UINT8(109U, p.frag_idx);
}

static void test_hdr_pack_payload_in_place_retransmit(void)
{
    /* the txq re-stamps a stored packet in place: payload == out + 24 */
    static uint8_t pkt[TS_PKT_MAX];
    ts_hdr_t h;
    ts_hdr_t p;
    const uint8_t *pl = NULL;
    size_t n = 0U;
    uint32_t crc_first;
    for (size_t i = 0U; i < 100U; i++) {
        pkt[TS_HDR_LEN + i] = (uint8_t)(i * 7U);
    }
    fill_hdr(&h, 0U, 1U, 100U, 100U);
    TEST_ASSERT_EQUAL(TS_OK, ts_hdr_pack(&h, &pkt[TS_HDR_LEN], pkt, sizeof(pkt), &n));
    crc_first = h.crc32;
    h.flags = (uint8_t)(h.flags | TS_F_RETRANSMIT);
    TEST_ASSERT_EQUAL(TS_OK, ts_hdr_pack(&h, &pkt[TS_HDR_LEN], pkt, sizeof(pkt), &n));
    TEST_ASSERT_NOT_EQUAL(crc_first, h.crc32);   /* flags changed -> CRC recomputed */
    TEST_ASSERT_EQUAL(TS_OK, ts_hdr_parse(pkt, n, &p, &pl));
    TEST_ASSERT_EQUAL_HEX8(TS_F_KEYFRAME | TS_F_RETRANSMIT, p.flags);
    TEST_ASSERT_EQUAL_UINT16(20U, p.pkt_seq);
    for (size_t i = 0U; i < 100U; i++) {
        TEST_ASSERT_EQUAL_HEX8((uint8_t)(i * 7U), pl[i]);
    }
}

static void test_hdr_parse_every_truncation_and_bitflip(void)
{
    const tsv_hdr_t *v = &tsv_hdr[2];   /* retransmit_single, 34 bytes */
    static uint8_t buf[64];
    ts_hdr_t h;
    const uint8_t *pl = NULL;
    TEST_ASSERT_NULL(v->error);
    for (size_t len = 0U; len < v->len; len++) {
        TEST_ASSERT_NOT_EQUAL(TS_OK, ts_hdr_parse(v->pkt, len, &h, &pl));
        TEST_ASSERT_NULL(pl);
    }
    for (size_t bit = 0U; bit < (v->len * 8U); bit++) {
        (void)memcpy(buf, v->pkt, v->len);
        buf[bit / 8U] = (uint8_t)(buf[bit / 8U] ^ (uint8_t)(1U << (bit % 8U)));
        if ((bit / 8U) == 18U || (bit / 8U) == 19U) {
            /* reserved: ignored, but CRC-covered */
            TEST_ASSERT_EQUAL(TS_E_BADCRC, ts_hdr_parse(buf, v->len, &h, &pl));
        } else {
            TEST_ASSERT_NOT_EQUAL(TS_OK, ts_hdr_parse(buf, v->len, &h, &pl));
        }
    }
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_hdr_parse(NULL, 34U, &h, &pl));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_hdr_parse(v->pkt, v->len, NULL, &pl));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_hdr_parse(v->pkt, v->len, &h, NULL));
}

/* ---- NACK --------------------------------------------------------------------- */
static void test_nack_vectors(void)
{
    static uint8_t out[TS_NACK_PKT_MAX];
    for (size_t i = 0U; i < tsv_nack_count; i++) {
        const tsv_nack_t *v = &tsv_nack[i];
        ts_nack_t n;
        const ts_err_t e = ts_nack_parse(v->pkt, v->len, &n);
        TEST_ASSERT_EQUAL_STRING_MESSAGE(err_name(v->error), ts_err_str(e), v->name);
        if (v->error == NULL) {
            size_t out_len = 0U;
            TEST_ASSERT_EQUAL_UINT8_MESSAGE(v->count, n.count, v->name);
            for (size_t k = 0U; k < v->count; k++) {
                TEST_ASSERT_EQUAL_UINT16_MESSAGE(v->entries[k].frame_seq, n.e[k].frame_seq, v->name);
                TEST_ASSERT_EQUAL_UINT8_MESSAGE(v->entries[k].frag_idx, n.e[k].frag_idx, v->name);
            }
            TEST_ASSERT_EQUAL(TS_OK, ts_nack_pack(&n, out, sizeof(out), &out_len));
            TEST_ASSERT_EQUAL_size_t_MESSAGE(v->len, out_len, v->name);
            TEST_ASSERT_EQUAL_HEX8_ARRAY_MESSAGE(v->pkt, out, v->len, v->name);
        }
    }
}

static void test_nack_pack_errors(void)
{
    static uint8_t out[TS_NACK_PKT_MAX];
    ts_nack_t n;
    size_t len = 0U;
    (void)memset(&n, 0, sizeof(n));
    n.count = 33U;
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_nack_pack(&n, out, sizeof(out), &len));
    n.count = 32U;
    TEST_ASSERT_EQUAL(TS_E_NOMEM, ts_nack_pack(&n, out, 131U, &len));
    TEST_ASSERT_EQUAL(TS_OK, ts_nack_pack(&n, out, sizeof(out), &len));
    TEST_ASSERT_EQUAL_size_t(TS_NACK_PKT_MAX, len);
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_nack_pack(NULL, out, sizeof(out), &len));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_nack_parse(NULL, 4U, &n));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_nack_parse(out, 4U, NULL));
}

static void test_nack_parse_huge_count_never_overflows(void)
{
    /* count 255 with a matching long datagram must not overflow e[32] */
    static uint8_t pkt[4U + (4U * 255U)];
    ts_nack_t n;
    (void)memset(pkt, 0xAB, sizeof(pkt));
    ts_wr16(pkt, (uint16_t)TS_MAGIC_NACK);
    pkt[2] = 255U;
    pkt[3] = 0U;
    (void)memset(&n, 0, sizeof(n));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_nack_parse(pkt, sizeof(pkt), &n));
    TEST_ASSERT_EQUAL_UINT8(0U, n.count);
}

/* ---- TLV ------------------------------------------------------------------------ */
static void test_tlv_vectors_via_acc(void)
{
    static ts_tlv_acc_t acc;
    for (size_t i = 0U; i < tsv_tlv_count; i++) {
        const tsv_tlv_t *v = &tsv_tlv[i];
        ts_tlv_t t;
        ts_tlv_acc_init(&acc);
        TEST_ASSERT_EQUAL(TS_OK, ts_tlv_acc_push(&acc, v->data, v->len));
        if (v->error != NULL) {
            TEST_ASSERT_EQUAL_STRING_MESSAGE(v->error, ts_err_str(ts_tlv_acc_next(&acc, &t)), v->name);
        } else {
            TEST_ASSERT_EQUAL_MESSAGE(TS_OK, ts_tlv_acc_next(&acc, &t), v->name);
            TEST_ASSERT_EQUAL_UINT8_MESSAGE(v->type, t.type, v->name);
            TEST_ASSERT_EQUAL_UINT16_MESSAGE(v->vlen, t.len, v->name);
            TEST_ASSERT_TRUE(t.value == &acc.buf[TS_TLV_HDR_LEN]);
            TEST_ASSERT_EQUAL_UINT16(0U, ts_tlv_acc_pending(&acc));
            TEST_ASSERT_EQUAL(TS_E_AGAIN, ts_tlv_acc_next(&acc, &t));
        }
    }
}

static const tsv_tlv_t *tlv_vec(const char *name)
{
    const tsv_tlv_t *r = NULL;
    for (size_t i = 0U; i < tsv_tlv_count; i++) {
        if (strcmp(tsv_tlv[i].name, name) == 0) {
            r = &tsv_tlv[i];
        }
    }
    TEST_ASSERT_NOT_NULL(r);
    return r;
}

static ts_tlv_t tlv_of(const tsv_tlv_t *v)
{
    ts_tlv_t t;
    t.type = v->data[0];
    t.flags = v->data[1];
    t.len = ts_rd16(&v->data[2]);
    t.value = &v->data[TS_TLV_HDR_LEN];
    return t;
}

static void test_tlv_typed_roundtrip(void)
{
    static uint8_t out[TS_TLV_HDR_LEN + TS_TLV_MAX_LEN];
    size_t n = 0U;
    ts_tlv_t t;
    {
        const tsv_tlv_t *v = tlv_vec("hello");
        ts_tlv_hello_t x;
        t = tlv_of(v);
        TEST_ASSERT_EQUAL(TS_OK, ts_tlv_parse_hello(&t, &x));
        TEST_ASSERT_EQUAL_UINT16(6000U, x.udp_port);
        TEST_ASSERT_EQUAL_UINT32(4000U, x.kbps);
        TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack_hello(&x, out, sizeof(out), &n));
        TEST_ASSERT_EQUAL_size_t(v->len, n);
        TEST_ASSERT_EQUAL_HEX8_ARRAY(v->data, out, v->len);
    }
    {
        const tsv_tlv_t *v = tlv_vec("welcome");
        ts_tlv_welcome_t x;
        t = tlv_of(v);
        TEST_ASSERT_EQUAL(TS_OK, ts_tlv_parse_welcome(&t, &x));
        TEST_ASSERT_EQUAL_UINT16(TS_WIDTH, x.w);
        TEST_ASSERT_EQUAL_UINT16(TS_HEIGHT, x.h);
        TEST_ASSERT_EQUAL_UINT16(TS_MTU_PAYLOAD, x.mtu);
        TEST_ASSERT_EQUAL_HEX32(0xC0FFEE42U, x.stream_id);
        TEST_ASSERT_EQUAL_UINT16(TS_RING_SLOTS, x.ring_slots);
        TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack_welcome(&x, out, sizeof(out), &n));
        TEST_ASSERT_EQUAL_size_t(v->len, n);
        TEST_ASSERT_EQUAL_HEX8_ARRAY(v->data, out, v->len);
    }
    {
        const tsv_tlv_t *v = tlv_vec("set_rate");
        ts_tlv_set_rate_t x;
        t = tlv_of(v);
        TEST_ASSERT_EQUAL(TS_OK, ts_tlv_parse_set_rate(&t, &x));
        TEST_ASSERT_EQUAL_UINT32(1500U, x.kbps);   /* kbps, not bps */
        TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack_set_rate(&x, out, sizeof(out), &n));
        TEST_ASSERT_EQUAL_size_t(v->len, n);
        TEST_ASSERT_EQUAL_HEX8_ARRAY(v->data, out, v->len);
    }
    {
        const tsv_tlv_t *v = tlv_vec("keyframe_req");
        TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack_keyframe_req(out, sizeof(out), &n));
        TEST_ASSERT_EQUAL_size_t(v->len, n);
        TEST_ASSERT_EQUAL_HEX8_ARRAY(v->data, out, v->len);
    }
    {
        const tsv_tlv_t *v = tlv_vec("bye");
        TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack_bye(out, sizeof(out), &n));
        TEST_ASSERT_EQUAL_size_t(v->len, n);
        TEST_ASSERT_EQUAL_HEX8_ARRAY(v->data, out, v->len);
    }
    {
        const tsv_tlv_t *v = tlv_vec("max_len_ok");
        TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack(99U, 0U, &v->data[TS_TLV_HDR_LEN], (uint16_t)TS_TLV_MAX_LEN,
                                             out, sizeof(out), &n));
        TEST_ASSERT_EQUAL_size_t(v->len, n);
        TEST_ASSERT_EQUAL_HEX8_ARRAY(v->data, out, v->len);
    }
}

static void test_tlv_stats_roundtrip(void)
{
    static uint8_t out[TS_TLV_HDR_LEN + TS_STATS_MAX_LEN];
    const char *names[2] = { "stats_no_clients", "stats_two_clients" };
    for (size_t i = 0U; i < 2U; i++) {
        const tsv_tlv_t *v = tlv_vec(names[i]);
        ts_tlv_stats_t s;
        ts_tlv_t t = tlv_of(v);
        size_t n = 0U;
        (void)memset(&s, 0, sizeof(s));
        TEST_ASSERT_EQUAL(TS_OK, ts_tlv_parse_stats(&t, &s));
        TEST_ASSERT_EQUAL_UINT32(12345U, s.g.uptime_ms);
        TEST_ASSERT_EQUAL_UINT32(5000000U, s.g.bytes_sent);
        TEST_ASSERT_EQUAL_UINT8(i * 2U, s.g.client_count);
        TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack_stats(&s, out, sizeof(out), &n));
        TEST_ASSERT_EQUAL_size_t(v->len, n);
        TEST_ASSERT_EQUAL_HEX8_ARRAY(v->data, out, v->len);
        if (i == 1U) {
            TEST_ASSERT_EQUAL_UINT8(2U, s.c[1].id);
            TEST_ASSERT_EQUAL_UINT8(2U, s.c[1].state);
            TEST_ASSERT_EQUAL_UINT16(6001U, s.c[1].udp_port);
            TEST_ASSERT_EQUAL_UINT32(1999U, s.c[1].last_seen_ms);
            TEST_ASSERT_EQUAL_UINT32(1U, s.c[1].retx_dropped);
        }
    }
}

static void test_tlv_typed_parse_rejects(void)
{
    static uint8_t buf[TS_TLV_HDR_LEN + TS_STATS_MAX_LEN + 36U];
    ts_tlv_hello_t hello;
    ts_tlv_welcome_t welcome;
    ts_tlv_set_rate_t rate;
    ts_tlv_stats_t stats;
    ts_tlv_t t;
    const tsv_tlv_t *hv = tlv_vec("hello");

    t = tlv_of(hv);
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_tlv_parse_welcome(&t, &welcome));   /* wrong type */
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_tlv_parse_set_rate(&t, &rate));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_tlv_parse_stats(&t, &stats));
    t.len = 5U;                                                           /* wrong length */
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_tlv_parse_hello(&t, &hello));
    t.len = 7U;
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_tlv_parse_hello(&t, &hello));
    t.len = 6U;
    t.value = NULL;
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_tlv_parse_hello(&t, &hello));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_tlv_parse_hello(NULL, &hello));
    t = tlv_of(hv);
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_tlv_parse_hello(&t, NULL));

    /* STATS: client_count must match the length and be <= 4 */
    (void)memset(buf, 0, sizeof(buf));
    t.type = (uint8_t)TS_TLV_STATS;
    t.flags = 0U;
    t.value = buf;
    buf[40] = 1U;
    t.len = (uint16_t)TS_STATS_GLOBAL_LEN;                               /* says 1, carries 0 */
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_tlv_parse_stats(&t, &stats));
    t.len = (uint16_t)(TS_STATS_GLOBAL_LEN + TS_STATS_CLIENT_LEN);
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_parse_stats(&t, &stats));
    buf[40] = 5U;
    t.len = (uint16_t)(TS_STATS_GLOBAL_LEN + (5U * TS_STATS_CLIENT_LEN));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_tlv_parse_stats(&t, &stats));
    t.len = 43U;
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_tlv_parse_stats(&t, &stats));
    stats.g.client_count = 5U;
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_tlv_pack_stats(&stats, buf, sizeof(buf), &(size_t){ 0U }));
}

static void test_tlv_pack_errors(void)
{
    static uint8_t value[TS_TLV_MAX_LEN + 1U];
    static uint8_t out[TS_TLV_ACC_CAP];
    size_t n = 0U;
    ts_tlv_hello_t hello = { 6000U, 0U };
    TEST_ASSERT_EQUAL(TS_E_TOOLONG, ts_tlv_pack(1U, 0U, value, (uint16_t)(TS_TLV_MAX_LEN + 1U), out, sizeof(out), &n));
    TEST_ASSERT_EQUAL(TS_E_NOMEM, ts_tlv_pack(1U, 0U, value, 10U, out, 13U, &n));
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack(1U, 0U, value, 10U, out, 14U, &n));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_tlv_pack(1U, 0U, NULL, 10U, out, sizeof(out), &n));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_tlv_pack(1U, 0U, value, 10U, NULL, sizeof(out), &n));
    TEST_ASSERT_EQUAL(TS_E_NOMEM, ts_tlv_pack_hello(&hello, out, 9U, &n));
    TEST_ASSERT_EQUAL(TS_E_NOMEM, ts_tlv_pack_bye(out, 3U, &n));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_tlv_pack_hello(NULL, out, sizeof(out), &n));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_tlv_pack_welcome(NULL, out, sizeof(out), &n));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_tlv_pack_set_rate(NULL, out, sizeof(out), &n));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_tlv_pack_stats(NULL, out, sizeof(out), &n));
}

/* ---- accumulate parser ------------------------------------------------------------ */
static void test_acc_glued_stream_vector(void)
{
    static ts_tlv_acc_t acc;
    for (size_t s = 0U; s < tsv_tlv_stream_count; s++) {
        const tsv_tlv_stream_t *v = &tsv_tlv_stream[s];
        size_t pos = 0U;
        size_t got = 0U;
        ts_tlv_t t;
        ts_tlv_acc_init(&acc);
        /* the listed chunk sizes, then whatever is left as one final chunk */
        for (size_t c = 0U; c <= v->nchunks; c++) {
            const size_t len = (c < v->nchunks) ? (size_t)v->chunks[c] : (v->len - pos);
            TEST_ASSERT_TRUE(len <= (v->len - pos));
            TEST_ASSERT_EQUAL(TS_OK, ts_tlv_acc_push(&acc, &v->data[pos], len));
            pos += len;
            while (ts_tlv_acc_next(&acc, &t) == TS_OK) {
                TEST_ASSERT_TRUE(got < v->nexpect);
                TEST_ASSERT_EQUAL_UINT8(v->expect[got].type, t.type);
                TEST_ASSERT_EQUAL_UINT16(v->expect[got].len, t.len);
                got++;
            }
        }
        TEST_ASSERT_EQUAL_size_t(v->len, pos);
        TEST_ASSERT_EQUAL_size_t(v->nexpect, got);
        TEST_ASSERT_EQUAL_UINT16(0U, ts_tlv_acc_pending(&acc));
    }
}

static void test_acc_one_byte_at_a_time(void)
{
    static ts_tlv_acc_t acc;
    const tsv_tlv_stream_t *v = &tsv_tlv_stream[0];
    size_t got = 0U;
    ts_tlv_t t;
    ts_tlv_acc_init(&acc);
    for (size_t i = 0U; i < v->len; i++) {
        ts_err_t e;
        TEST_ASSERT_EQUAL(TS_OK, ts_tlv_acc_push(&acc, &v->data[i], 1U));
        e = ts_tlv_acc_next(&acc, &t);
        if (e == TS_OK) {
            TEST_ASSERT_EQUAL_UINT8(v->expect[got].type, t.type);
            TEST_ASSERT_EQUAL_UINT16(v->expect[got].len, t.len);
            got++;
            TEST_ASSERT_EQUAL(TS_E_AGAIN, ts_tlv_acc_next(&acc, &t));
        } else {
            TEST_ASSERT_EQUAL(TS_E_AGAIN, e);
        }
    }
    TEST_ASSERT_EQUAL_size_t(v->nexpect, got);
}

static void test_acc_three_tlvs_in_one_buffer(void)
{
    static ts_tlv_acc_t acc;
    static uint8_t buf[64];
    size_t n = 0U;
    size_t k = 0U;
    ts_tlv_t t;
    ts_tlv_hello_t hello = { 6000U, 2500U };
    ts_tlv_set_rate_t rate = { 3000U };
    ts_tlv_hello_t hx;
    ts_tlv_set_rate_t rx;

    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack_hello(&hello, &buf[n], sizeof(buf) - n, &k));
    n += k;
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack_keyframe_req(&buf[n], sizeof(buf) - n, &k));
    n += k;
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack_set_rate(&rate, &buf[n], sizeof(buf) - n, &k));
    n += k;

    ts_tlv_acc_init(&acc);
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_acc_push(&acc, buf, n));
    TEST_ASSERT_EQUAL_UINT16(n, ts_tlv_acc_pending(&acc));
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_acc_next(&acc, &t));
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_parse_hello(&t, &hx));
    TEST_ASSERT_EQUAL_UINT32(2500U, hx.kbps);
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_acc_next(&acc, &t));
    TEST_ASSERT_EQUAL_UINT8(TS_TLV_KEYFRAME_REQ, t.type);
    TEST_ASSERT_EQUAL_UINT16(0U, t.len);
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_acc_next(&acc, &t));
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_parse_set_rate(&t, &rx));
    TEST_ASSERT_EQUAL_UINT32(3000U, rx.kbps);
    TEST_ASSERT_EQUAL(TS_E_AGAIN, ts_tlv_acc_next(&acc, &t));
    TEST_ASSERT_EQUAL_UINT16(0U, ts_tlv_acc_pending(&acc));
}

static void test_acc_compaction_long_stream(void)
{
    /* 300 HELLOs (3000 bytes) in odd chunk sizes: the buffer must compact and
     * keep yielding TLVs in order without ever reporting FULL. */
    static ts_tlv_acc_t acc;
    static uint8_t stream[3000];
    size_t n = 0U;
    size_t pos = 0U;
    uint16_t want = 0U;
    ts_tlv_t t;
    for (uint16_t i = 0U; i < 300U; i++) {
        ts_tlv_hello_t h = { i, (uint32_t)i * 3U };
        size_t k = 0U;
        TEST_ASSERT_EQUAL(TS_OK, ts_tlv_pack_hello(&h, &stream[n], sizeof(stream) - n, &k));
        n += k;
    }
    ts_tlv_acc_init(&acc);
    while (pos < n) {
        size_t chunk = 1U + ((pos * 7U) % 97U);
        if (chunk > (n - pos)) {
            chunk = n - pos;
        }
        TEST_ASSERT_EQUAL(TS_OK, ts_tlv_acc_push(&acc, &stream[pos], chunk));
        pos += chunk;
        while (ts_tlv_acc_next(&acc, &t) == TS_OK) {
            ts_tlv_hello_t h;
            TEST_ASSERT_EQUAL(TS_OK, ts_tlv_parse_hello(&t, &h));
            TEST_ASSERT_EQUAL_UINT16(want, h.udp_port);
            TEST_ASSERT_EQUAL_UINT32((uint32_t)want * 3U, h.kbps);
            want++;
        }
    }
    TEST_ASSERT_EQUAL_UINT16(300U, want);
}

static void test_acc_full_and_toolong(void)
{
    static ts_tlv_acc_t acc;
    static uint8_t junk[TS_TLV_ACC_CAP + 1U];
    const uint8_t over[4] = { 1U, 0U, 0x01U, 0x02U };   /* len 513 */
    ts_tlv_t t;

    ts_tlv_acc_init(&acc);
    TEST_ASSERT_EQUAL(TS_E_FULL, ts_tlv_acc_push(&acc, junk, sizeof(junk)));
    TEST_ASSERT_EQUAL_UINT16(0U, ts_tlv_acc_pending(&acc));
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_acc_push(&acc, junk, 1000U));
    TEST_ASSERT_EQUAL(TS_E_FULL, ts_tlv_acc_push(&acc, junk, 25U));
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_acc_push(&acc, junk, 24U));
    TEST_ASSERT_EQUAL_UINT16(TS_TLV_ACC_CAP, ts_tlv_acc_pending(&acc));
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_acc_push(&acc, junk, 0U));

    /* TOOLONG is reported as soon as the header is in, before the value */
    ts_tlv_acc_init(&acc);
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_acc_push(&acc, over, 3U));
    TEST_ASSERT_EQUAL(TS_E_AGAIN, ts_tlv_acc_next(&acc, &t));
    TEST_ASSERT_EQUAL(TS_OK, ts_tlv_acc_push(&acc, &over[3], 1U));
    TEST_ASSERT_EQUAL(TS_E_TOOLONG, ts_tlv_acc_next(&acc, &t));
    TEST_ASSERT_EQUAL(TS_E_TOOLONG, ts_tlv_acc_next(&acc, &t));

    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_tlv_acc_push(NULL, junk, 1U));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_tlv_acc_push(&acc, NULL, 1U));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_tlv_acc_next(NULL, &t));
    TEST_ASSERT_EQUAL(TS_E_INVAL, ts_tlv_acc_next(&acc, NULL));
    TEST_ASSERT_EQUAL_UINT16(0U, ts_tlv_acc_pending(NULL));
    ts_tlv_acc_init(NULL);
}

static void test_err_str_all(void)
{
    for (int i = 0; i < (int)TS_E_MAX_; i++) {
        const char *s = ts_err_str((ts_err_t)i);
        TEST_ASSERT_NOT_NULL(s);
        TEST_ASSERT_EQUAL_INT(0, strncmp(s, "TS_", 3U));
    }
    TEST_ASSERT_EQUAL_STRING("TS_E_CORRUPT", ts_err_str(TS_E_CORRUPT));
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_crc_vectors);
    RUN_TEST(test_crc_streaming_any_split);
    RUN_TEST(test_crc_lengths_and_empty_input);
    RUN_TEST(test_seq_examples);
    RUN_TEST(test_seq_wrap_every_value);
    RUN_TEST(test_seq_ts_ms_latency_across_wrap);
    RUN_TEST(test_hdr_vectors_parse);
    RUN_TEST(test_hdr_vectors_pack_bit_exact);
    RUN_TEST(test_hdr_pack_rejects_bad_fields);
    RUN_TEST(test_hdr_pack_max_frame_geometry);
    RUN_TEST(test_hdr_pack_payload_in_place_retransmit);
    RUN_TEST(test_hdr_parse_every_truncation_and_bitflip);
    RUN_TEST(test_nack_vectors);
    RUN_TEST(test_nack_pack_errors);
    RUN_TEST(test_nack_parse_huge_count_never_overflows);
    RUN_TEST(test_tlv_vectors_via_acc);
    RUN_TEST(test_tlv_typed_roundtrip);
    RUN_TEST(test_tlv_stats_roundtrip);
    RUN_TEST(test_tlv_typed_parse_rejects);
    RUN_TEST(test_tlv_pack_errors);
    RUN_TEST(test_acc_glued_stream_vector);
    RUN_TEST(test_acc_one_byte_at_a_time);
    RUN_TEST(test_acc_three_tlvs_in_one_buffer);
    RUN_TEST(test_acc_compaction_long_stream);
    RUN_TEST(test_acc_full_and_toolong);
    RUN_TEST(test_err_str_all);
    return UNITY_END();
}
