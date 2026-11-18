# TS-102: Stripes and crashes on detailed scenes

**Type:** Bug
**Component:** daemon
**Reported by:** stream operator (projector station)

## Description

On calm, flat scenes the stream looks fine, but as soon as the picture gets
busy -- a detailed poster, heavy motion, or the test pattern's periodic noise
burst -- horizontal stripes tear across the image, and on the noisiest frames
the daemon dies outright (systemd restarts it and the picture comes back a
couple of seconds later, then tears again on the next busy frame).

## Steps to reproduce

1. On the Pi: `tinystreamd --source synth --seed 1` (the synth source emits a
   full-frame noise burst 15 frames after every scene cut).
2. On the laptop:
   `tsrecv --server 10.42.0.2:5601 --udp-port 5610 --truth synth:1 --seconds 30`
3. Watch `frame.pgm` around each noise burst; also try a detailed recorded
   clip (`--source file`) or the camera on a cluttered scene.

## Expected

Every frame, however detailed, is shown intact; `frame.pgm` matches the source
and the daemon keeps running.

## Actual

Stripes (bands of wrong pixels) appear on detailed / noisy frames, and the
noise burst frame frequently kills the daemon (`frames_decoded` stalls, STATS
stop, the service restarts). Calm scenes are unaffected.

## Environment

- tinystream: ticket branch, synth source (file and camera sources show it too)
- Pi 5, Raspberry Pi OS Lite 64-bit, direct Ethernet 10.42.0.x
- Laptop: Ubuntu 24.04, tsrecv built from the same tree
