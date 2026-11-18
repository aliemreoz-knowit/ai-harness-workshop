# TS-101: Mission board needs per-viewer numbers from the daemon

**Type:** Feature  
**Component:** daemon  
**Reported by:** facilitator (mission board)

## Description

The mission board shows one tile per connected viewer (projector, board
thumbnail, laptops). It reads the daemon's STATS through `tsrecv`'s
`metrics.json`. The global numbers are there, but there is nothing per
viewer: `daemon.client_count` is 0 and `daemon.clients` is an empty list even
while two viewers are streaming. We need, per viewer, the numbers the protocol
already describes for the STATS per-client block (PROTOCOL.md 5.1): slot id,
state, UDP port, current rate, fresh packets and bytes sent, NACK entries
received, retransmissions sent and refused, keyframe requests and time since
the viewer was last heard.

## Steps to reproduce

1. On the Pi: `tinystreamd --source synth --seed 1`
2. On the laptop, viewer 1:
   `tsrecv --server 10.42.0.2:5601 --udp-port 5610 --truth synth:1 --metrics a.json --seconds 30`
3. On a second laptop (or a second terminal), viewer 2:
   `tsrecv --server 10.42.0.2:5601 --udp-port 5611 --metrics b.json --seconds 30`
4. In viewer 1 press `k` a few times; after 5 s open `a.json` and look at the
   `daemon` object.

## Expected

`daemon.client_count` is 2 and `daemon.clients` has one entry per viewer;
each viewer can find its own entry by its UDP port; the entry of viewer 1
shows its keyframe requests, the rates match what each viewer asked for, and
the per-viewer packet, byte, NACK and retransmission numbers add up to the
global ones.

## Actual

`"client_count":0,"clients":[]` in both files, although both viewers decode at
30 fps and receive STATS every second.

## Environment

- tinystream: ticket branch, synth source
- Pi 5, Raspberry Pi OS Lite 64-bit, direct Ethernet 10.42.0.x
- Laptop: Linux (or WSL2 with mirrored networking), tsrecv from the same tree
