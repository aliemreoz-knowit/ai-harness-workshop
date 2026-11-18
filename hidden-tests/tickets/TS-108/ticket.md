# TS-108: Transmit queue is below the coverage bar

**Type:** Task  
**Component:** daemon (transmit queue)  
**Reported by:** CI / release owner

## Description

The gate ladder is red on the coverage gate: the transmit queue
(`src/txq/`) is measured at about 61 % line coverage from its own unit tests,
the bar for this module is 80 %. Nothing is visibly wrong with the stream
today, but the transmit queue is what answers every repair request from the
viewers and what keeps the history of sent packets, so we do not want it to
change without tests that would notice.

Bring the transmit queue's own unit tests back above the bar with tests that
check behaviour (what a viewer and the daemon rely on), not just lines that
happen to run. The module itself is not to be changed for this task.

## Steps to reproduce

1. `scripts/check.sh coverage`
2. Look at the `txq` entry of the coverage line (and `build-check/coverage.log`).

## Expected

coverage green; `txq` at or above 80 % from `tests/unit/test_txq.c` alone; the rest
of the ladder stays green.

## Actual

`coverage FAIL ... txq=60.9` (every other module meets its threshold); the build, misra, no-heap, test and budgets gates pass.

## Environment

- tinystream: ticket branch, dev container (`scripts/check.sh`)
