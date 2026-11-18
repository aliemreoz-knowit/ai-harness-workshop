/*
 * tinystream v2 -- hidden evaluation test: tests/unit/test_codec_malformed_input_eval.c
 *
 * ts_rle_decode() must check every run and literal length against the
 * remaining symbol count before writing (PROTOCOL.md 6.4, ts_codec.h: "Rejects
 * runs/literals that would exceed n"). A run token whose length is larger than
 * the symbols still owed must be rejected with TS_E_CORRUPT and must not write
 * past the caller's buffer. A decoder that trusts the run length writes the
 * whole run (ASan: buffer overflow past the exactly-sized sink; on the host it
 * returns something other than TS_E_CORRUPT).
 *
 * The defect: see the ticket.
 */
#include "unity.h"
#include "ts_codec.h"
#include "ts_proto.h"

#define RLE_ESCAPE  0x00U
#define SINK_N      16U

/* Exactly-sized sinks: a redzone (ASan) sits immediately after, so a run that
 * overruns n is a global-buffer-overflow, not a write into slack. */
static uint8_t g_sink_run[SINK_N];
static uint8_t g_sink_lit[SINK_N];

void setUp(void)
{
}

void tearDown(void)
{
}

static void test_run_length_over_n_is_rejected(void)
{
    /* one run token of length 255 while only SINK_N symbols are owed */
    const uint8_t in[2] = { 255U, 0xABU };
    size_t consumed = 0U;
    TEST_ASSERT_EQUAL_MESSAGE(TS_E_CORRUPT,
                              ts_rle_decode(in, sizeof(in), g_sink_run, (size_t)SINK_N, &consumed),
                              "run length > n accepted (buffer overrun)");
}

static void test_literal_length_over_n_is_rejected(void)
{
    /* escape + literal length 200 while only SINK_N symbols are owed */
    uint8_t in[2U + 200U];
    size_t consumed = 0U;
    in[0] = RLE_ESCAPE;
    in[1] = 200U;
    for (uint32_t i = 0U; i < 200U; i++) {
        in[2U + i] = (uint8_t)(i & 0xFFU);
    }
    TEST_ASSERT_EQUAL_MESSAGE(TS_E_CORRUPT,
                              ts_rle_decode(in, sizeof(in), g_sink_lit, (size_t)SINK_N, &consumed),
                              "literal length > n accepted (buffer overrun)");
}

static void test_exact_run_still_decodes(void)
{
    /* a run of exactly SINK_N is valid and fills the sink */
    const uint8_t in[2] = { (uint8_t)SINK_N, 0x42U };
    size_t consumed = 0U;
    TEST_ASSERT_EQUAL(TS_OK, ts_rle_decode(in, sizeof(in), g_sink_run, (size_t)SINK_N, &consumed));
    TEST_ASSERT_EQUAL_size_t(sizeof(in), consumed);
    for (uint32_t i = 0U; i < SINK_N; i++) {
        TEST_ASSERT_EQUAL_UINT8(0x42U, g_sink_run[i]);
    }
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_run_length_over_n_is_rejected);
    RUN_TEST(test_literal_length_over_n_is_rejected);
    RUN_TEST(test_exact_run_still_decodes);
    return UNITY_END();
}
