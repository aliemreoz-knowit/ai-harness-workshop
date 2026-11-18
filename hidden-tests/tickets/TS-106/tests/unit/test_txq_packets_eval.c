/*
 * tinystream v2 -- hidden evaluation test: tests/unit/test_txq_packets_eval.c
 *
 * Every DATA packet the txq stores must carry a CRC over header[0..20) plus
 * exactly payload_len payload bytes (PROTOCOL.md 2, 3), so the receiver's
 * ts_hdr_parse() accepts it on a clean link. A sender that computes the CRC
 * over the payload length rounded up (e.g. to a multiple of 4) produces a CRC
 * the receiver cannot reproduce for any fragment whose payload_len is not
 * already aligned -- typically the last fragment of every frame -- so those
 * packets are dropped as TS_E_BADCRC even with no corruption on the wire.
 *
 * This drives the frozen API only: push frames with deliberately unaligned
 * fragment lengths, then parse every stored packet (and its retransmit) with
 * the receiver's parser.
 *
 * The defect: see the ticket.
 */
#include "unity.h"
#include "ts_txq.h"
#include "ts_proto.h"
#include <string.h>

static ts_txq_t g_q;
static uint8_t  g_enc[TS_ENC_MAX];
static uint8_t  g_retx[TS_PKT_MAX];

/* enc_len values whose last fragment is NOT a multiple of 4 bytes
 * (single-fragment frames and multi-fragment frames alike). */
static const uint32_t K_ENC_LENS[] = { 9U, 10U, 11U, 50U, 1399U, 1402U, 2001U, 4003U };
#define N_LENS (sizeof(K_ENC_LENS) / sizeof(K_ENC_LENS[0]))

void setUp(void)
{
    for (uint32_t i = 0U; i < TS_ENC_MAX; i++) {
        g_enc[i] = (uint8_t)((i * 37U) + 11U);
    }
}

void tearDown(void)
{
}

static void parse_slot_must_be_ok(const ts_txq_slot_t *s)
{
    ts_hdr_t h;
    const uint8_t *pl = NULL;
    size_t rlen = 0U;
    ts_err_t rc = ts_hdr_parse(s->pkt, (size_t)s->len, &h, &pl);
    TEST_ASSERT_EQUAL_MESSAGE(TS_OK, rc, "a stored DATA packet failed the receiver's CRC/length check");

    /* a retransmit of the same slot must parse too */
    TEST_ASSERT_EQUAL(TS_OK, ts_txq_make_retransmit(s, g_retx, sizeof(g_retx), &rlen));
    rc = ts_hdr_parse(g_retx, rlen, &h, &pl);
    TEST_ASSERT_EQUAL_MESSAGE(TS_OK, rc, "a retransmitted DATA packet failed the receiver's CRC/length check");
}

static void test_every_stored_packet_parses_on_a_clean_link(void)
{
    ts_txq_cursor_t cur;
    const ts_txq_slot_t *s;
    uint32_t parsed = 0U;

    ts_txq_init(&g_q, 40000U);
    ts_txq_cursor_init(&g_q, &cur);          /* cursor at the (empty) end: sees everything we push next */

    for (uint32_t i = 0U; i < N_LENS; i++) {
        uint8_t frags = 0U;
        uint8_t flags = ((i & 1U) != 0U) ? (uint8_t)TS_F_KEYFRAME : 0U;
        TEST_ASSERT_EQUAL(TS_OK, ts_txq_push_frame(&g_q, (uint16_t)(100U + i), flags,
                                                   (uint16_t)(i * 3U), g_enc, (size_t)K_ENC_LENS[i], &frags));
        TEST_ASSERT_TRUE(frags >= 1U);
    }

    while ((s = ts_txq_cursor_peek(&g_q, &cur)) != NULL) {
        parse_slot_must_be_ok(s);
        ts_txq_cursor_advance(&cur);
        parsed++;
    }
    TEST_ASSERT_TRUE_MESSAGE(parsed >= N_LENS, "cursor saw fewer packets than were pushed");
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_every_stored_packet_parses_on_a_clean_link);
    return UNITY_END();
}
