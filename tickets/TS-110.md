# TS-110: Picture almost stops when the link loses a few packets

**Type:** Bug  
**Component:** daemon  
**Reported by:** stream operator (loss slider demo)

## Description

On a clean link the stream is smooth. As soon as the relay drops around 5 % of
the packets the frame rate on the projector falls to about 2 fps within a few
seconds, the latency shown in the viewer keeps climbing and the picture lags
further and further behind. It does not recover while the loss is on; when the
slider goes back to 0 % it takes several seconds to catch up. It is worst when
the viewer's budget is close to what the link can carry.

## Steps to reproduce

1. On the Pi: `tinystreamd --source synth --seed 1 --kbps 1000`
2. On the laptop, relay with a tight link and loss:
   `tsnetem --listen 0.0.0.0:5620 --forward 127.0.0.1:5610 --peer 10.42.0.2:5600 --spec loss=5,throttle=1100 --slider`
   (or `--scenario tools/tsnetem/scenarios/tight_link.txt`)
3. On the laptop:
   `tsrecv --server 10.42.0.2:5601 --udp-port 5610 --announce-port 5620 --truth synth:1 --seconds 30`
4. Compare `metrics.json` with the same run at `--spec loss=0,throttle=1100`.

## Expected

With 5 % loss the stream keeps at least 20 fps every second, latency stays
flat, and the daemon stays within the viewer's budget (+10 %).

## Actual

`fps` around 2 after a few seconds, latency and jitter keep growing, NACK and
retransmission counters very high, bitrate at the link limit the whole time.

## Environment

- tinystream: ticket branch, synth source (camera source shows the same)
- Pi 5, Raspberry Pi OS Lite 64-bit, direct Ethernet 10.42.0.x
- Laptop: Ubuntu 24.04, tsnetem in the path, tsrecv from the same tree
