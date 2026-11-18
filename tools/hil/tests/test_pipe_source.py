"""pipe_source.py writes rpicam-vid's byte stream: YUV420 frames, the clip in order, pauses skip time only."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HIL = Path(__file__).resolve().parent.parent
Y = 320 * 240
FRAME = Y * 3 // 2


def make_clip(path: Path, n: int) -> list[bytes]:
    frames = [bytes([(17 * k + 1) % 256]) * Y for k in range(n)]
    path.write_bytes(b"".join(frames))
    return frames


def run(args, timeout=20):
    return subprocess.run([sys.executable, str(HIL / "pipe_source.py")] + args, capture_output=True,
                          timeout=timeout)


def test_frames_are_yuv420_in_clip_order(tmp_path):
    clip = make_clip(tmp_path / "c.y8", 3)
    p = run(["--clip", str(tmp_path / "c.y8"), "--fps", "50", "--seconds", "0.3"])
    assert p.returncode == 0
    out = p.stdout
    assert len(out) % FRAME == 0 and len(out) // FRAME >= 8
    for i in range(len(out) // FRAME):
        f = out[i * FRAME:(i + 1) * FRAME]
        assert f[:Y] == clip[i % 3]
        assert f[Y:] == bytes([128]) * (Y // 2)


def test_pause_skips_time_not_frames(tmp_path):
    clip = make_clip(tmp_path / "c.y8", 2)
    p = run(["--clip", str(tmp_path / "c.y8"), "--fps", "50", "--seconds", "0.6", "--pause-at", "0.1",
             "--pause-s", "0.3", "--chunk", "65536"])
    n = len(p.stdout) // FRAME
    assert p.returncode == 0 and len(p.stdout) % FRAME == 0
    assert 8 <= n <= 20                     # 30 frames without the pause, 15 are skipped
    for i in range(n):                      # the clip continues where it stopped
        assert p.stdout[i * FRAME:i * FRAME + Y] == clip[i % 2]


def test_bad_clip(tmp_path):
    (tmp_path / "short.y8").write_bytes(b"x" * 100)
    p = run(["--clip", str(tmp_path / "short.y8"), "--seconds", "0.1"])
    assert p.returncode != 0 and b"no complete" in p.stderr
