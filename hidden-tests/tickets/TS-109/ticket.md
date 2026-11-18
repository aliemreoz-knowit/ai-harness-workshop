# TS-109: Second viewer on the same laptop freezes and keeps reconnecting

**Type:** Bug  
**Component:** daemon  
**Reported by:** facilitator B (projector + board thumbnail on one laptop)

## Description

Facilitator B's laptop runs two viewers: the projector view and the small
thumbnail on the mission board. Each one alone is smooth. With both running
the one started second shows a picture that stops every couple of seconds,
the viewer reconnects ("re-HELLO") again and again and its frame rate in
`metrics.json` sits around 2 fps. When the loss slider is turned up the
second viewer hardly shows anything any more and the first one starts to
stutter as well. Two viewers on two different laptops work fine.

## Steps to reproduce

1. On the Pi: `tinystreamd --source synth --seed 1`
2. On the laptop, viewer 1:
   `tsrecv --server 10.42.0.2:5601 --udp-port 5610 --truth synth:1 --metrics a.json --seconds 20`
3. On the same laptop, 2 s later, viewer 2:
   `tsrecv --server 10.42.0.2:5601 --udp-port 5611 --truth synth:1 --metrics b.json --seconds 15`
4. Compare `fps`, `freezes` and `reconnects` in `a.json` and `b.json`.
   Optional: put both viewers behind `tsnetem --spec loss=3` and repeat.

## Expected

Both viewers decode at about 30 fps, no freezes, no reconnects, also with a
few percent loss on the link.

## Actual

Viewer 1: 29.9 fps, no freezes. Viewer 2: about 150 frames in 7 s, fps 2,
2 freezes (2.1 s in total), 2 reconnects. With 3 % loss viewer 2 decodes
almost nothing and viewer 1 freezes once.

## Environment

- tinystream: ticket branch, synth source
- Pi 5, Raspberry Pi OS Lite 64-bit, direct Ethernet 10.42.0.x
- Laptop: Linux (or WSL2 with mirrored networking), both tsrecv from the same tree
