/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/unit/test_harness.c
 * Proves the test harness: Unity runs, headers compile under -Werror, the
 * trivial final modules (config, err strings, LE helpers, log ring, pool)
 * behave, and the vectors header is reachable. Module tests go to
 * tests/unit/test_<module>.c (property tests: test_prop_<module>.c).
 */
#include <string.h>
#include "unity.h"
#include "ts_config.h"
#include "ts_proto.h"
#include "ts_log.h"
#include "ts_pool.h"
#include "ts_loop.h"
#include "ts_vectors.h"

void setUp(void)
{
}

void tearDown(void)
{
}

static void test_config_constants(void)
{
    TEST_ASSERT_EQUAL_UINT32(76800U, TS_FRAME_BYTES);
    TEST_ASSERT_EQUAL_UINT32(2U * TS_WIDTH * TS_HEIGHT + 64U, TS_ENC_MAX);
    TEST_ASSERT_TRUE(TS_MAX_FRAGS * TS_MTU_PAYLOAD >= TS_ENC_MAX);
    TEST_ASSERT_EQUAL_UINT32(2U, TS_PROTOCOL_VERSION);
    TEST_ASSERT_EQUAL_UINT32(TS_PROTOCOL_VERSION, TSV_PROTOCOL_VERSION);
}

static void test_err_str(void)
{
    TEST_ASSERT_EQUAL_STRING("TS_OK", ts_err_str(TS_OK));
    TEST_ASSERT_EQUAL_STRING("TS_E_NOTIMPL", ts_err_str(TS_E_NOTIMPL));
    TEST_ASSERT_EQUAL_STRING("TS_E_UNKNOWN", ts_err_str(TS_E_MAX_));
}

static void test_le_helpers_roundtrip(void)
{
    uint8_t b[4];
    ts_wr16(b, 0x5453U);
    TEST_ASSERT_EQUAL_HEX8(0x53U, b[0]);
    TEST_ASSERT_EQUAL_HEX8(0x54U, b[1]);
    TEST_ASSERT_EQUAL_HEX16(0x5453U, ts_rd16(b));
    ts_wr32(b, 0xDEADBEEFU);
    TEST_ASSERT_EQUAL_HEX8(0xEFU, b[0]);
    TEST_ASSERT_EQUAL_HEX32(0xDEADBEEFU, ts_rd32(b));
}

static void test_log_ring(void)
{
    static ts_log_t log;
    ts_log_entry_t e;
    ts_log_init(&log, TS_LOG_INFO);
    ts_log_push(&log, 10U, TS_LOG_DBG, TS_MOD_TXQ, 1U, 0U, 0U);   /* filtered */
    TEST_ASSERT_EQUAL_UINT16(0U, ts_log_count(&log));
    for (uint32_t i = 0U; i < TS_LOG_RING + 5U; i++) {
        ts_log_push(&log, i, TS_LOG_INFO, TS_MOD_DAEMON, (uint16_t)i, i, 0U);
    }
    TEST_ASSERT_EQUAL_UINT16(TS_LOG_RING, ts_log_count(&log));
    TEST_ASSERT_EQUAL_UINT32(5U, log.dropped);
    TEST_ASSERT_TRUE(ts_log_pop(&log, &e));
    TEST_ASSERT_EQUAL_UINT32(5U, e.ms);  /* oldest surviving entry */
    TEST_ASSERT_EQUAL_STRING("daemon", ts_log_module_name((ts_module_t)e.module));
}

static void test_pool_seal(void)
{
    ts_pool_t *p = ts_pool_get();
    ts_pool_init(p);
    TEST_ASSERT_EQUAL_HEX32(TS_POOL_MAGIC, p->magic);
    TEST_ASSERT_FALSE(ts_pool_sealed(p));
    ts_pool_seal(p);
    TEST_ASSERT_TRUE(ts_pool_sealed(p));
    TEST_ASSERT_TRUE(ts_pool_size() > TS_RING_SLOTS * TS_PKT_MAX);
}

static void test_vectors_present(void)
{
    /* The generated header is reachable and carries the CRC check value. */
    TEST_ASSERT_TRUE(TSV_CRC_COUNT >= 2U);
    TEST_ASSERT_EQUAL_HEX32(0xCBF43926U, tsv_crc[1].crc);
    TEST_ASSERT_EQUAL_size_t(9U, tsv_crc[1].len);
    TEST_ASSERT_EQUAL_MEMORY("123456789", tsv_crc[1].data, 9U);
}

static void test_loop_harness_links(void)
{
    /* Glue only: with stubs tsd_create() returns NULL, the harness must cope. */
    static ts_loop_t L;
    ts_loop_cfg_t c;
    ts_loop_cfg_default(&c);
    ts_loop_init(&L, &c);
    ts_loop_run(&L, 10U);
    TEST_ASSERT_EQUAL_UINT32(10U, L.steps);
    TEST_ASSERT_EQUAL_UINT32(1010U, L.now_ms);
    ts_loop_close(&L);
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_config_constants);
    RUN_TEST(test_err_str);
    RUN_TEST(test_le_helpers_roundtrip);
    RUN_TEST(test_log_ring);
    RUN_TEST(test_pool_seal);
    RUN_TEST(test_vectors_present);
    RUN_TEST(test_loop_harness_links);
    return UNITY_END();
}
