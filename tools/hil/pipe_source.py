#!/usr/bin/env python3
"""pipe_source.py -- scripted stand-in for rpicam-vid on the HIL station.

The camera source of tinystreamd is `--source pipe`: rpicam-vid writes
320x240 YUV420 frames (Y plane, then U and V at quarter size) back to back to
stdout, the daemon keeps the Y plane. This script writes the same byte stream
from a gray8 clip (tests/data/poster_30f.y8 or the make_clip.py stand-in), so
the pipe path can be checked without a Pi or a camera:

    python3 tools/hil/pipe_source.py --clip CLIP --fps 30 --seconds 10 \\
        --pause-at 4 --pause-s 3 | tinystreamd --source pipe ...

Like the camera it is not polite to the reader:
  * every frame goes out in one write() of 115200 bytes (or in --chunk sized
    pieces with --chunk-gap-ms between them), so a reader sees short reads at
    the pipe capacity (64 KiB on Linux) and between the chunks;
  * --pause-at / --pause-s stop the output for a while (what `kill -STOP` on
    rpicam-vid does on the Pi); the frames due during the pause are skipped,
    the stream then continues on the frame clock (the camera does the same).
The U and V planes are 128 (no colour). The k-th frame written is clip frame
k % N (a pause does not advance the clip), so `tsrecv --ref CLIP --seq-start S` compares the right frames as long
as the daemon encodes every frame it reads.

Times can be absolute (--t0 EPOCH: --pause-at counts from that wall-clock
instant, the measurement uses it to line the pause up with its own clock).
Exit status 0 at the end of --seconds, or when the reader goes away.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

W, H = 320, 240
Y_BYTES = W * H
UV_BYTES = Y_BYTES // 2
FRAME_BYTES = Y_BYTES + UV_BYTES


def load_clip(path: str) -> list[bytes]:
    with open(path, "rb") as f:
        data = f.read()
    n = len(data) // Y_BYTES
    if n == 0:
        raise SystemExit("pipe_source: %s holds no complete 320x240 gray8 frame" % path)
    return [data[i * Y_BYTES:(i + 1) * Y_BYTES] for i in range(n)]


def yuv420(y: bytes) -> bytes:
    return y + bytes([128]) * UV_BYTES


def write_all(fd: int, buf: bytes) -> None:
    view = memoryview(buf)
    while len(view) > 0:
        n = os.write(fd, view)
        view = view[n:]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clip", required=True, help="raw 320x240 gray8 frames, back to back")
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--seconds", type=float, default=10.0, help="stop after this long (0 = forever)")
    ap.add_argument("--pause-at", type=float, default=0.0, help="pause the output at t (s); 0 = no pause")
    ap.add_argument("--pause-s", type=float, default=0.0, help="length of the pause (s)")
    ap.add_argument("--chunk", type=int, default=0, help="bytes per write() (0 = one write per frame)")
    ap.add_argument("--chunk-gap-ms", type=float, default=0.0, help="sleep between the chunks of a frame")
    ap.add_argument("--t0", type=float, default=0.0, help="wall-clock origin of --pause-at (default: start)")
    a = ap.parse_args(argv)

    frames = [yuv420(y) for y in load_clip(a.clip)]
    fd = sys.stdout.fileno()
    t0 = a.t0 if a.t0 > 0.0 else time.time()
    period = 1.0 / a.fps
    k = 0           # frame clock
    out = 0         # frames written
    try:
        while True:
            due = t0 + k * period
            now = time.time()
            if a.seconds > 0.0 and now - t0 >= a.seconds:
                break
            if a.pause_s > 0.0 and a.pause_at > 0.0 and a.pause_at <= (due - t0) < a.pause_at + a.pause_s:
                k += 1          # the camera is stopped: this frame never exists
                continue
            if due > now:
                time.sleep(due - now)
            buf = frames[out % len(frames)]
            if a.chunk > 0:
                for off in range(0, FRAME_BYTES, a.chunk):
                    write_all(fd, buf[off:off + a.chunk])
                    if a.chunk_gap_ms > 0.0:
                        time.sleep(a.chunk_gap_ms / 1000.0)
            else:
                write_all(fd, buf)
            k += 1
            out += 1
    except BrokenPipeError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
