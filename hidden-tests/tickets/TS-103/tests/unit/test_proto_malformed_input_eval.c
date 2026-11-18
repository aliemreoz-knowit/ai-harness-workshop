/*
 * tinystream v2 -- hidden evaluation test: tests/unit/test_proto_malformed_input_eval.c
 *
 * ts_nack_parse() must be bounded by TS_NACK_MAX (ts_proto.h: "never writes
 * more than TS_NACK_MAX entries"): a NACK datagram whose count byte claims more
 * than TS_NACK_MAX entries -- even when the datagram actually carries them, so
 * the length is consistent -- must be rejected with TS_E_INVAL and nothing
 * written past ts_nack_t.e[TS_NACK_MAX]. A parser that trusts the count byte
 * writes count entries into the fixed 32-slot array (ASan: buffer overflow;
 * on the host it returns TS_OK for an over-long count).
 *
 * The defect: see the ticket.
 */
#include "unity.h"
#include "ts_proto.h"
#include <string.h>

#define NK_MAGIC   0U
#define NK_COUNT   2U
#define NK_ENTRY0  4U

void setUp(void)
{
}

void tearDown(void)
{
}

static size_t build_nack(uint8_t *buf, size_t cap, uint16_t count)
{
    size_t need = (size_t)TS_NACK_HDR_LEN + ((size_t)TS_NACK_ENTRY_LEN * (size_t)count);
    TEST_ASSERT_TRUE(need <= cap);
    (void)memset(buf, 0, need);
    ts_wr16(&buf[NK_MAGIC], (uint16_t)TS_MAGIC_NACK);
    buf[NK_COUNT] = (uint8_t)count;
    for (uint16_t k = 0U; k < count; k++) {
        ts_wr16(&buf[NK_ENTRY0 + (4U * (size_t)k)], (uint16_t)(0x1000U + k));
        buf[NK_ENTRY0 + (4U * (size_t)k) + 2U] = (uint8_t)(k & 0xFFU);
    }
    return need;
}

static void test_overlong_count_is_rejected_not_written(void)
{
    static uint8_t buf[4U + (4U * 255U)];
    ts_nack_t n;
    size_t len;

    len = build_nack(buf, sizeof(buf), (uint16_t)(TS_NACK_MAX + 1U));
    (void)memset(&n, 0, sizeof(n));
    TEST_ASSERT_EQUAL_MESSAGE(TS_E_INVAL, ts_nack_parse(buf, len, &n),
                              "count = TS_NACK_MAX+1 accepted (array overflow)");

    len = build_nack(buf, sizeof(buf), 255U);
    (void)memset(&n, 0, sizeof(n));
    TEST_ASSERT_EQUAL_MESSAGE(TS_E_INVAL, ts_nack_parse(buf, len, &n),
                              "count = 255 accepted (array overflow)");
}

static void test_max_count_still_parses(void)
{
    static uint8_t buf[4U + (4U * 255U)];
    ts_nack_t n;
    size_t len = build_nack(buf, sizeof(buf), (uint16_t)TS_NACK_MAX);
    (void)memset(&n, 0, sizeof(n));
    TEST_ASSERT_EQUAL(TS_OK, ts_nack_parse(buf, len, &n));
    TEST_ASSERT_EQUAL_UINT8((uint8_t)TS_NACK_MAX, n.count);
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_overlong_count_is_rejected_not_written);
    RUN_TEST(test_max_count_still_parses);
    return UNITY_END();
}
