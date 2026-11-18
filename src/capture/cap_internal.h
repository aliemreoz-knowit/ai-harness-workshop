/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- src/capture/cap_internal.h
 * Private glue between the capture HAL files (capture.c, cap_v4l2.c). Not part
 * of the public API; nothing outside src/capture/ includes it.
 */
#ifndef TS_CAP_INTERNAL_H
#define TS_CAP_INTERNAL_H

#include <stdint.h>
#include "ts_capture.h"

#ifdef __cplusplus
extern "C" {
#endif

/** Open the V4L2 device `c->cfg.path` and start streaming. Errors: TS_E_IO, TS_E_NOTIMPL (not Linux). */
ts_err_t ts_cap_v4l2_open(ts_cap_t *c);
/** Dequeue one frame and convert it to gray8 into `frame`. TS_OK / TS_E_AGAIN / TS_E_IO. */
ts_err_t ts_cap_v4l2_read(ts_cap_t *c, uint8_t *frame);
/** Stop streaming, unmap the buffers and close the device. */
void     ts_cap_v4l2_close(ts_cap_t *c);

#ifdef __cplusplus
}
#endif

#endif /* TS_CAP_INTERNAL_H */
