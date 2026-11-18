/*
 * tinystream v2 -- hidden evaluation test: tests/unit/test_clients_second_viewer_eval.c
 * Viewer identity (PROTOCOL.md 4 and 5): up to TS_MAX_CLIENTS viewers on one
 * laptop share an IP and differ only in their UDP port. Lookups, liveness,
 * NACK accounting and closing must keep them apart. Client API only, virtual
 * clock; see meta.yaml for what this test evaluates.
 */
#include <string.h>
#include "unity.h"
#include "ts_config.h"
#include "ts_proto.h"
#include "ts_txq.h"
#include "ts_rate.h"
#include "ts_clients.h"

#define LAPTOP_IP  0x0A2A0005U   /* 10.42.0.5 */
#define OTHER_IP   0x0A2A0006U   /* 10.42.0.6 */
#define PORT0      7000U

static ts_clients_t T;
static ts_txq_t Q;
static uint8_t enc[TS_ENC_MAX];
static ts_client_t *v[TS_MAX_CLIENTS];

static ts_addr_t addr(uint32_t ip, uint16_t port)
{
    ts_addr_t a;
    a.ip = ip;
    a.port = port;
    return a;
}

static ts_client_t *viewer(ts_io_handle_t h, uint32_t ip, uint16_t udp_port, uint32_t now)
{
    ts_addr_t peer = addr(ip, (uint16_t)(43000U + (uint32_t)h));
    ts_client_t *c = ts_client_accept(&T, h, &peer, now);
    TEST_ASSERT_NOT_NULL(c);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_hello(&T, c, udp_port, 0U, &Q, now));
    return c;
}

/* All TS_MAX_CLIENTS viewers on the laptop, ports PORT0 + i, joined at t = 0. */
void setUp(void)
{
    uint8_t n = 0U;
    ts_clients_init(&T);
    ts_txq_init(&Q, 0U);
    for (uint8_t i = 0U; i < (uint8_t)TS_MAX_CLIENTS; i++) {
        v[i] = viewer((ts_io_handle_t)(10 + (int32_t)i), LAPTOP_IP, (uint16_t)(PORT0 + i), 0U);
    }
    (void)memset(enc, 0x5A, 4U * 1400U);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_txq_push_frame(&Q, 1U, 0U, 0U, enc, 4U * 1400U, &n));
}

void tearDown(void)
{
}

static void test_four_viewers_on_one_ip_are_four_clients(void)
{
    TEST_ASSERT_EQUAL_UINT8(TS_MAX_CLIENTS, ts_clients_active(&T));
    for (uint8_t i = 0U; i < (uint8_t)TS_MAX_CLIENTS; i++) {
        ts_addr_t k = addr(LAPTOP_IP, (uint16_t)(PORT0 + i));
        TEST_ASSERT_EQUAL_PTR_MESSAGE(v[i], ts_client_find(&T, &k), "lookup returned another viewer of the same IP");
        for (uint8_t j = 0U; j < i; j++) {
            TEST_ASSERT_TRUE(v[i] != v[j]);
        }
    }
    {
        ts_addr_t unknown = addr(LAPTOP_IP, (uint16_t)(PORT0 + TS_MAX_CLIENTS));
        ts_addr_t other = addr(OTHER_IP, PORT0);
        TEST_ASSERT_NULL_MESSAGE(ts_client_find(&T, &unknown), "unknown port on a known IP matched a viewer");
        TEST_ASSERT_NULL_MESSAGE(ts_client_find(&T, &other), "known port on an unknown IP matched a viewer");
    }
}

static void test_closed_viewer_is_not_found_through_its_neighbour(void)
{
    ts_addr_t k1 = addr(LAPTOP_IP, (uint16_t)(PORT0 + 1U));
    ts_addr_t k2 = addr(LAPTOP_IP, (uint16_t)(PORT0 + 2U));
    ts_client_close(&T, v[1]);
    TEST_ASSERT_NULL_MESSAGE(ts_client_find(&T, &k1), "a closed viewer's address still matches an active viewer");
    ts_client_free(&T, v[1]);
    TEST_ASSERT_NULL(ts_client_find(&T, &k1));
    TEST_ASSERT_EQUAL_PTR(v[2], ts_client_find(&T, &k2));
    TEST_ASSERT_EQUAL_UINT8(TS_MAX_CLIENTS - 1U, ts_clients_active(&T));
}

static void test_traffic_keeps_only_its_sender_alive(void)
{
    ts_addr_t k3 = addr(LAPTOP_IP, (uint16_t)(PORT0 + 3U));
    ts_client_t *c;
    ts_nack_t n;
    ts_retx_batch_t out;

    /* only the viewer on PORT0 + 3 sends NACKs, every 500 ms */
    (void)memset(&n, 0, sizeof(n));
    n.count = 1U;
    n.e[0].frame_seq = 1U;
    n.e[0].frag_idx = 2U;
    for (uint32_t t = 500U; t <= 3000U; t += 500U) {
        ts_clients_tick(&T, t);
        c = ts_client_find(&T, &k3);
        TEST_ASSERT_EQUAL_PTR_MESSAGE(v[3], c, "NACK sender resolved to another viewer");
        TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_nack(c, &n, &Q, NULL, 0U, NULL, t, &out));
        TEST_ASSERT_EQUAL_UINT8(1U, out.count);
    }
    TEST_ASSERT_EQUAL_UINT8_MESSAGE(TS_MAX_CLIENTS - 1U, ts_clients_expire(&T, 3000U, TS_CLIENT_TIMEOUT_MS),
                                    "silent viewers were kept alive by a neighbour's traffic");
    for (uint8_t i = 0U; i < 3U; i++) {
        TEST_ASSERT_EQUAL_INT(TS_CL_CLOSING, v[i]->state);
    }
    TEST_ASSERT_EQUAL_INT(TS_CL_ACTIVE, v[3]->state);
    TEST_ASSERT_EQUAL_PTR(v[3], ts_client_find(&T, &k3));
}

static void test_nack_from_one_port_does_not_spend_another_viewers_budget(void)
{
    ts_addr_t k0 = addr(LAPTOP_IP, PORT0);
    ts_addr_t k2 = addr(LAPTOP_IP, (uint16_t)(PORT0 + 2U));
    uint32_t tokens0 = ts_bucket_tokens(&v[0]->bucket);
    uint32_t tokens2 = ts_bucket_tokens(&v[2]->bucket);
    ts_nack_t n;
    ts_retx_batch_t out;
    ts_client_t *c;

    (void)memset(&n, 0, sizeof(n));
    n.count = 3U;
    for (uint8_t i = 0U; i < 3U; i++) {
        n.e[i].frame_seq = 1U;
        n.e[i].frag_idx = i;
    }
    c = ts_client_find(&T, &k2);
    TEST_ASSERT_EQUAL_PTR(v[2], c);
    TEST_ASSERT_EQUAL_INT(TS_OK, ts_client_nack(c, &n, &Q, NULL, 0U, NULL, 10U, &out));
    TEST_ASSERT_EQUAL_UINT8(3U, out.count);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(tokens0, ts_bucket_tokens(&v[0]->bucket), "viewer 0 paid for viewer 2's repairs");
    TEST_ASSERT_TRUE(ts_bucket_tokens(&v[2]->bucket) < tokens2);
    TEST_ASSERT_EQUAL_PTR(v[0], ts_client_find(&T, &k0));
}

static void test_restart_on_one_port_leaves_the_others(void)
{
    ts_addr_t k1 = addr(LAPTOP_IP, (uint16_t)(PORT0 + 1U));
    ts_client_t *neu;
    /* viewer on PORT0 + 1 restarts: closes its slot first (BYE), then a new connection says HELLO */
    ts_client_close(&T, v[1]);
    ts_client_free(&T, v[1]);
    neu = viewer(20, LAPTOP_IP, (uint16_t)(PORT0 + 1U), 100U);
    TEST_ASSERT_EQUAL_UINT8(TS_MAX_CLIENTS, ts_clients_active(&T));
    TEST_ASSERT_EQUAL_PTR(neu, ts_client_find(&T, &k1));
    for (uint8_t i = 0U; i < (uint8_t)TS_MAX_CLIENTS; i++) {
        if (i != 1U) {
            ts_addr_t k = addr(LAPTOP_IP, (uint16_t)(PORT0 + i));
            TEST_ASSERT_EQUAL_INT(TS_CL_ACTIVE, v[i]->state);
            TEST_ASSERT_EQUAL_PTR(v[i], ts_client_find(&T, &k));
        }
    }
}

int main(void)
{
    UNITY_BEGIN();
    RUN_TEST(test_four_viewers_on_one_ip_are_four_clients);
    RUN_TEST(test_closed_viewer_is_not_found_through_its_neighbour);
    RUN_TEST(test_traffic_keeps_only_its_sender_alive);
    RUN_TEST(test_nack_from_one_port_does_not_spend_another_viewers_budget);
    RUN_TEST(test_restart_on_one_port_leaves_the_others);
    return UNITY_END();
}
