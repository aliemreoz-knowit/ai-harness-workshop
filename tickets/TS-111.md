# TS-111: Smeared picture after a lossy period, cleans up only every few seconds

**Type:** Bug  
**Component:** daemon / receiver  
**Reported by:** stream operator (loss slider demo)

## Description

When the relay loses packets for a while the stream gets blurrier, which is
expected. But when the loss slider goes back to 0 % the picture does not get
sharp again: fine detail (poster text, edges of the moving object) stays
smeared and ghost copies of edges stay where they were, also on parts of the
scene that do not move at all. Every few seconds the picture suddenly "pops"
back to sharp, and the next lossy period starts the smear again. The frame
rate stays normal the whole time; nothing freezes.

## Steps to reproduce

1. On the Pi: `tinystreamd --source synth --seed 1`
2. On the laptop, relay with a lossy period followed by a clean link:
   `tsnetem --listen 0.0.0.0:5620 --forward 127.0.0.1:5610 --peer 10.42.0.2:5600 --scenario step.txt`
   with `step.txt`:
   ```
   t=0s loss=15%
   t=4s loss=0
   ```
   (or move the `--slider` from 15 % to 0 % by hand)
3. On the laptop:
   `tsrecv --server 10.42.0.2:5601 --udp-port 5610 --announce-port 5620 --truth synth:1 --seconds 9 --sdl`
4. Compare `exact_match_pct`, `truth_exact` and `psnr_db` in `metrics.json`
   with a run of the same length on a clean link.

## Expected

Once the link is clean again the picture is sharp and identical to the source
within a second (exact match back on every frame), and stays that way.

## Actual

After the lossy period the picture stays smeared until a keyframe arrives.
9 s run as above: `exact_match_pct` 2 (5 of ~215 frames exact), last frame
`psnr_db` about 22 dB against the source although the link has been clean for
5 s. Frame rate 30 fps, no freezes.

## Environment

- tinystream: ticket branch, synth source (camera source shows the same)
- Pi 5, Raspberry Pi OS Lite 64-bit, direct Ethernet 10.42.0.x
- Laptop: Ubuntu 24.04, tsnetem + tsrecv from the same tree
