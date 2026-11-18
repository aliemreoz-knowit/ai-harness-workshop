# TS-113: Tear line runs down the camera picture, stream stops when the camera pauses

**Type:** Bug  
**Component:** daemon (camera source)  
**Reported by:** HIL station operator

## Description

Only with the real Pi camera (`--source pipe`): a horizontal tear line slowly
wanders down the picture; above it the picture is from a newer frame than
below. With the synth or file source the picture is clean.

When the camera process pauses for a moment, the whole daemon stops: no new
frames, no STATS, the `k` key is ignored and after two seconds the viewer
reconnects. When the camera continues the stream comes back, often with a torn
first frame. Expected is that the last picture just stays on screen while the
camera is paused and the control channel keeps working.

## Steps to reproduce

1. On the Pi:
   `rpicam-vid -t 0 -n --codec yuv420 --width 320 --height 240 --framerate 30 -o - | tinystreamd --source pipe`
2. On the laptop: `tsrecv --server 10.42.0.2:5601 --udp-port 5610 --sdl --scale 2`
3. Point the camera at the poster with a slowly moving object; watch for the tear line.
4. On the Pi: `kill -STOP $(pidof rpicam-vid)`, wait 3 s, press `k` in the viewer,
   then `kill -CONT $(pidof rpicam-vid)`.

## Expected

No tear line. While the camera is paused: last frame stays, STATS continue,
`k` is answered, no reconnect. After `-CONT` the stream continues cleanly.

## Actual

Tear line wandering down. During the pause no STATS and no reaction to `k`,
viewer re-HELLOs after 2 s; first frame after the pause is torn.

## Environment

- tinystream: ticket branch, Pi build (`scripts/build.sh pi`)
- Pi 5 + Camera Module, Raspberry Pi OS Lite 64-bit, rpicam-vid (libcamera)
- Laptop: Ubuntu 24.04, tsrecv with SDL2
- Not reproducible with `--source synth` or `--source file:...`
