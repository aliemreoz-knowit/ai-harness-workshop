/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- fuzz/fuzz_pkt_parse.c
 * libFuzzer target: DATA header + NACK parsers must never read out of bounds
 * or accept inconsistent lengths. Seeds: fuzz/corpus/pkt_parse (from PROTOCOL.md vectors).
 */
#include <stddef.h>
#include <stdint.h>
#include "ts_proto.h"

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    ts_hdr_t h;
    const uint8_t *payload = NULL;
    ts_nack_t n;
    (void)ts_hdr_parse(data, size, &h, &payload);
    (void)ts_nack_parse(data, size, &n);
    return 0;
}
