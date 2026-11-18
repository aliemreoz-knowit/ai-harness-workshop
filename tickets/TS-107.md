# TS-107: Keyframe on request: the picture must come back right after a short outage

**Type:** Feature  
**Component:** daemon / receiver  
**Reported by:** stream operator (cable-pull demo)

## Description

When the link drops for a moment (cable pulled for half a second or a second,
a short loss burst on the relay) the picture on the projector stays frozen
long after the link is back: it only comes back at the next periodic
keyframe, which can be up to ten seconds later. Pressing `k` in the viewer
(or `tsrecv --send keyframe`) does not help either; the daemon does not send
a keyframe earlier because of it.

PROTOCOL.md already describes a viewer asking for a keyframe over the control
channel and the daemon answering it within the keyframe cooldown. We need that
to work end to end: a viewer that cannot decode any more asks for a keyframe
by itself, a viewer key press asks for one, and the daemon answers, without
letting a viewer (or several) flood the stream with keyframes.

## Steps to reproduce

1. On the Pi: `tinystreamd --source synth --seed 1`
2. On the laptop, relay with a short blackout 3 s into the run:
   `tsnetem --listen 0.0.0.0:5620 --forward 127.0.0.1:5610 --peer 10.42.0.2:5600 --scenario pull.txt`
   with `pull.txt` containing the line `t=3s blackout 1s`
3. On the laptop:
   `tsrecv --server 10.42.0.2:5601 --udp-port 5610 --announce-port 5620 --truth synth:1 --seconds 15 --sdl`
4. Watch the picture after the blackout ends; then press `k` a few times during
   a normal run and compare `keyframes` in `metrics.json` with a run without
   pressing it.
5. Same with `--spec burst=50` (one 50-packet burst) instead of the blackout.

## Expected

- Within 1 s after the cable is back a fresh, correct picture is shown and the
  stream is back at full frame rate.
- `k` / `--send keyframe` produces a keyframe within a fraction of a second
  (at most one per second, however often it is pressed).

## Actual

- After a 0.5 s or 1 s blackout the picture stays frozen for several seconds
  (until the next periodic keyframe); `freezes` counts it, `keyframe_reqs` in
  `metrics.json` stays 0.
- `k` has no visible effect; `keyframes` grows only every 10 s.
- With 5 % random loss there are whole seconds without a new frame.

## Environment

- tinystream: ticket branch, synth source (camera source shows the same)
- Pi 5, Raspberry Pi OS Lite 64-bit, direct Ethernet 10.42.0.x
- Laptop: Ubuntu 24.04, tsnetem + tsrecv from the same tree
