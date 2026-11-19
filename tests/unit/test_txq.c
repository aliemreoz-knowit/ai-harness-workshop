/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/unit/test_txq.c
 * Transmit queue: fragmenter. Pure in-memory, no clock.
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

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_txq_init_empty);
    RUN_TEST(test_txq_push_rejects_bad_args);
    RUN_TEST(test_txq_fragment_counts);
    return UNITY_END();
}
