# TS-103: Daemon and viewer crash on malformed input from the network

**Type:** Bug (security)
**Component:** daemon / receiver
**Reported by:** security review

## Description

During a security review of the wire handling, the reviewers made both the
daemon and the viewer (`tsrecv`) crash by sending them malformed datagrams.
On the Pi the daemon is restarted by systemd, which blacks out the picture for
about two seconds; the viewer simply dies. Under AddressSanitizer both crashes
show a memory error instead of a clean rejection. The reviewers' inputs are
attached to this ticket by the facilitator; the ordinary relay impairments
(loss, bursts, bit flips) do not trigger it, because damaged packets are
already dropped before they are used.

## Steps to reproduce

1. Build the asan preset (`scripts/build.sh asan`).
2. Replay the reviewers' inputs (facilitator) against the daemon and the
   viewer, or run the public test and fuzz suites under ASan.
3. Watch for AddressSanitizer reports.

## Expected

Any malformed or hostile datagram is rejected and counted; neither the viewer
nor the daemon ever crashes, whatever arrives on its sockets.

## Actual

Both processes crash with an AddressSanitizer memory error on the reviewers'
inputs; without ASan the daemon dies and is restarted, the viewer exits.

## Environment

- tinystream: ticket branch, synth source
- Pi 5, Raspberry Pi OS Lite 64-bit, direct Ethernet 10.42.0.x
- Laptop: Ubuntu 24.04, tsrecv built from the same tree
