# TS-105: Viewer keys and rate changes are often ignored by the daemon

**Type:** Bug  
**Component:** daemon  
**Reported by:** stream operator (projector station)

## Description

Pressing `k` in the viewer to get a fresh keyframe often does nothing: the
smear after a loss stays until the next periodic keyframe. Changing the rate
with `+` / `-` is unreliable too; sometimes the new rate shows up in the
daemon's STATS, sometimes the daemon keeps the old one, roughly every other
attempt when keys are pressed quickly or while the viewer is busy repairing
losses. On the laptop behind the Wi-Fi bridge the viewer sometimes never gets
past the handshake: it reconnects every few seconds and `reconnects` keeps
counting up in `metrics.json` while the daemon runs normally for the other
viewer.

## Steps to reproduce

1. On the Pi: `tinystreamd --source synth --seed 1`
2. On the laptop (relay with a little loss so the viewer asks for repairs):
   `tsnetem --listen 0.0.0.0:5620 --forward 127.0.0.1:5610 --peer 10.42.0.2:5600 --spec loss=3`
3. On the laptop:
   `tsrecv --server 10.42.0.2:5601 --udp-port 5610 --announce-port 5620 --truth synth:1 --sdl`
4. Press `k`, then `+` and `-` a few times in quick succession; compare with
   the per-client `kbps` and `keyframe_reqs` in the daemon's STATS (shown by
   the viewer, also in `metrics.json`).
5. Repeat from a second laptop on a slow or congested link (Wi-Fi bridge,
   VPN) and watch `reconnects` in its `metrics.json`.

## Expected

Every `k` reaches the daemon (counted in STATS, keyframe within the cooldown),
every `+` / `-` changes the rate STATS reports, and a viewer completes the
handshake once and stays connected.

## Actual

`keyframe_reqs` in STATS grows more slowly than the number of key presses, the
STATS rate lags behind the requested one (only some changes take effect), and
on the slow link the viewer reconnects over and over (`reconnects` > 0, no
picture) although the daemon is fine.

## Environment

- tinystream: `m0-start` live stream (also on the ticket branch)
- Pi 5, Raspberry Pi OS Lite 64-bit, direct Ethernet 10.42.0.x; second laptop over Wi-Fi bridge
- Laptop: Ubuntu 24.04, tsrecv + tsnetem built from the same tree
