# TS-112: Stream crawls after the viewer changes its rate

**Type:** Bug  
**Component:** daemon  
**Reported by:** stream operator

## Description

The viewer starts smooth at 30 fps. As soon as the rate is changed from the
viewer (key `-` halves it, `+` doubles it, both send SET_RATE) the picture
drops to a fraction of 30 fps (roughly 5-15 fps, at times close to standstill),
freezes of one to three seconds in longer runs,
and it does not get better while the viewer keeps running. The STATS shown by
the viewer report the new rate correctly. Starting a fresh viewer with
`--kbps` set to the same value gives a smooth stream.

## Steps to reproduce

1. On the Pi: `tinystreamd --source synth --seed 1`
2. On the laptop:
   `tsrecv --server 10.42.0.2:5601 --udp-port 5610 --truth synth:1 --metrics m.json --seconds 15`
3. After a second press `-` in the viewer (rate 4000 -> 2000 kbit/s).
4. Look at `fps`, `kbps`, `freezes` and `daemon.clients[0].kbps` in `m.json`.
5. Compare with `tsrecv ... --kbps 2000` started fresh.

## Expected

After the change the stream keeps about 30 fps (quality may drop), the
received bitrate stays within the new rate (+10 %) and not far below it, no
freezes.

## Actual

10 s after pressing `-`: 120-150 frames decoded instead of about 300, final
`fps` anywhere between 1 and 19, received `kbps` about 250, 2 freezes, 356 packets lost, exact match
63 %. `daemon.clients[0].kbps` says 2000.

## Environment

- tinystream: ticket branch, synth source
- Pi 5, Raspberry Pi OS Lite 64-bit, direct Ethernet 10.42.0.x
- Laptop: Linux (or WSL2 with mirrored networking), tsrecv from the same tree
