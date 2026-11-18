/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- fuzz/fuzz_ctrl_parse.c
 * libFuzzer target: TLV accumulate parser fed in random chunk sizes, then the
 * typed parsers. Seeds: fuzz/corpus/ctrl_parse.
 */
#include <stddef.h>
#include <stdint.h>
#include "ts_proto.h"

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    static ts_tlv_acc_t acc;
    ts_tlv_t tlv;
    size_t pos = 0U;
    ts_tlv_acc_init(&acc);
    while (pos < size) {
        size_t chunk = ((size_t)data[pos] % 7U) + 1U;
        if (chunk > (size - pos)) {
            chunk = size - pos;
        }
        if (ts_tlv_acc_push(&acc, &data[pos], chunk) != TS_OK) {
            break;
        }
        pos += chunk;
        while (ts_tlv_acc_next(&acc, &tlv) == TS_OK) {
            ts_tlv_hello_t hello;
            ts_tlv_welcome_t welcome;
            ts_tlv_stats_t stats;
            ts_tlv_set_rate_t rate;
            (void)ts_tlv_parse_hello(&tlv, &hello);
            (void)ts_tlv_parse_welcome(&tlv, &welcome);
            (void)ts_tlv_parse_stats(&tlv, &stats);
            (void)ts_tlv_parse_set_rate(&tlv, &rate);
        }
    }
    return 0;
}
