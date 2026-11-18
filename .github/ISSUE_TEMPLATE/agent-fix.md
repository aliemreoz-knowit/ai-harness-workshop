---
name: Agent-ready fix
about: A ticket the agent harness can work on. Fill every section, then add the agent:fix label.
title: ""
labels: []
assignees: []
---

**Type:** Bug

## Description

What goes wrong, where it shows (projector, tsrecv status line, metrics.json, a gate), since when and how often. Describe what you observe, not a guess about the cause. Change the Type line above to Feature or Task if this is not a bug.

## Steps to reproduce

1. exact command, e.g. `tinystreamd --source synth --seed 1`
2. relay or viewer command with its options
3. what to do or how long to wait

## Expected

What should happen, with numbers where possible (fps, frames decoded, counters).

## Actual

What happens instead, with the numbers you measured.

## Environment

Branch or commit, Pi or host, link setup.

## Acceptance criteria

- [ ] a regression test reproduces the symptom and passes after the change
- [ ] a checkable statement on the stream or a gate

## Acceptance tests

- one test per line (Unity case or ctest name) that must pass after the change, existing or still to write; cover every side of the stream the symptom involves

## Out of scope

- what must not change (the public API in include/, PROTOCOL.md, ...)
