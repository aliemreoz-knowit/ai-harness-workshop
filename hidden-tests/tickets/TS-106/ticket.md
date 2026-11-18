# TS-106: Coarse picture and high NACK counter on a perfectly clean link

**Type:** Bug
**Component:** daemon
**Reported by:** stream operator (bring-up)

## Description

Even on a direct, clean cable with no relay in the path, the picture on the
projector looks coarse: diagonal edges turn into staircases and the small text
line at the bottom of the synth picture is unreadable. The frame rate stays at
about 30 fps, but the viewer's NACK, CRC-failure and retransmission counters
climb steadily, as if the link lost a large share of the packets, and the
received bitrate is about a third of what the same stream reaches on another
build. A plain `ping` shows no loss.

## Steps to reproduce

1. On the Pi: `tinystreamd --source synth --seed 1 --kbps 4000`
2. On the laptop, directly (no relay):
   `tsrecv --server 10.42.0.2:5601 --udp-port 5610 --truth synth:1 --seconds 30`
3. Read `metrics.json` and look at `frame.pgm`.

## Expected

On a clean link: `crc_fail` 0, `nacks_sent` near 0, `exact_match_pct` ~100,
bitrate ~1500 kbps, sharp edges.

## Actual

After 30 s: `crc_fail` ~690, `nacks_sent` ~685, `retx_rx` ~685 (about 23 per
second, nearly one per frame), `loss_pct` ~40, `kbps` ~580, `exact_match_pct`
~5, `psnr_db` ~18; `fps` 29.9, no freezes. `frame.pgm` shows staircase edges.

## Environment

- tinystream: ticket branch, synth source (camera shows it too)
- Pi 5, Raspberry Pi OS Lite 64-bit, direct Ethernet 10.42.0.x (no relay)
- Laptop: Ubuntu 24.04, tsrecv from the same tree
