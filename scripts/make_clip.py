#!/usr/bin/env python3
"""make_clip.py -- the 30-frame gray8 clip for the `file:` capture source.

The HIL clip `tests/data/poster_30f.y8` (30 frames, 320x240, gray8, frames
back to back, no header) is recorded once on the Pi:

    rpicam-vid -n --frames 30 --codec yuv420 --width 320 --height 240 \
               --framerate 30 -o poster.yuv
    python3 scripts/make_clip.py --from-yuv420 poster.yuv     # keeps the Y planes

Until that recording exists, a deterministic stand-in with the same geometry
can be generated (detailed static "poster" plus a slowly moving object, the
content the HIL scene is meant to have):

    python3 scripts/make_clip.py                               # -> tests/data/poster_30f.y8

Options: --out PATH (default tests/data/poster_30f.y8), --frames N (default 30).
The tests do not need the file: tests/loopback/test_lb_file_clip.c writes its
own clip with the same generator to a temporary file.
"""
import argparse
import os
import sys

W, H = 320, 240
FRAME = W * H
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, "tests", "data", "poster_30f.y8")


def poster_pixel(x, y):
    """Static detailed background: diagonal gradient, text-like blocks, fine stripes."""
    v = (x + 2 * y) & 0xFF
    if 24 <= y < 72 and 16 <= x < 304:
        # "text": 6x8 glyph cells with pseudo-random strokes
        cx, cy = (x - 16) // 6, (y - 24) // 8
        stroke = (cx * 7 + cy * 13 + ((x - 16) % 6) * 3 + ((y - 24) % 8) * 5) % 7
        v = 230 if stroke < 3 else 30
    elif 200 <= y < 232:
        v = 200 if ((y // 2) % 2) == 0 else 60     # 2-row stripes (detail at q2/q4)
    return v


def frame(k):
    """Frame k: the poster plus a 48x48 checkerboard object moving 3 px / frame."""
    out = bytearray(FRAME)
    for y in range(H):
        row = y * W
        for x in range(W):
            out[row + x] = poster_pixel(x, y)
    ox, oy = (40 + 3 * k) % (W - 48), 100
    for y in range(oy, oy + 48):
        for x in range(ox, ox + 48):
            out[y * W + x] = 255 if (((x - ox) // 8 + (y - oy) // 8) % 2) == 0 else 0
    return bytes(out)


def from_yuv420(path, frames):
    yuv = W * H * 3 // 2
    data = open(path, "rb").read()
    n = min(frames, len(data) // yuv)
    if n == 0:
        raise SystemExit("make_clip: %s holds no complete 320x240 YUV420 frame" % path)
    return b"".join(data[i * yuv:i * yuv + FRAME] for i in range(n))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--frames", type=int, default=30)
    ap.add_argument("--from-yuv420", metavar="FILE", help="keep the Y planes of an rpicam-vid yuv420 recording")
    a = ap.parse_args()
    if a.from_yuv420:
        clip = from_yuv420(a.from_yuv420, a.frames)
    else:
        clip = b"".join(frame(k) for k in range(a.frames))
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "wb") as f:
        f.write(clip)
    print("make_clip: %s, %d frames of %dx%d gray8" % (a.out, len(clip) // FRAME, W, H))
    return 0


if __name__ == "__main__":
    sys.exit(main())
