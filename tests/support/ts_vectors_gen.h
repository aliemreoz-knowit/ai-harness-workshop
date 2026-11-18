/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tests/support/ts_vectors_gen.h
 * C replica of the frame generators used by scripts/protocol_vectors.py so
 * that codec tests can rebuild the input of every tsv_frame[] vector.
 */
#ifndef TS_VECTORS_GEN_H
#define TS_VECTORS_GEN_H

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/** Fill `frame` (TS_FRAME_BYTES) from a generator name ("gradient0", "noise7", "bars8"). false = unknown. */
bool tsv_gen_frame(const char *generator, uint8_t *frame);

#ifdef __cplusplus
}
#endif

#endif /* TS_VECTORS_GEN_H */
