# KEY-123: <one line: what the user sees, not what you think is wrong>

**Type:** Bug | Feature | Test gap
**Component:** daemon | receiver | relay | camera | harness
**Priority:** <Low | Medium | High>

<!--
Agent-ready ticket template. Copy to tickets/<KEY>.md and fill every section.
Run it with:   python -m harness run --workflow jira-to-pr --ticket tickets/<KEY>.md
The harness reads the "# KEY-123: title" line and the **Field:** lines; every
"## Section" below reaches the agents verbatim (inside an untrusted-data block).
Write what is observable and checkable. Leave guesses about the cause out:
a wrong guess in the ticket costs more agent turns than no guess.
-->

## Context

<Where the problem shows up (projector, tsrecv status line, metrics.json, a
gate), since when, how often. One or two sentences on why it matters.>

## Steps to reproduce

1. <exact command, e.g. `tinystreamd --source synth --seed 1`>
2. <relay or viewer command with its options>
3. <what to do or how long to wait>

## Inputs

<Files, options, recordings or scenarios the work starts from (e.g. a loopback
scenario in tests/loopback/, a section of PROTOCOL.md, a metrics.json excerpt).>

## Expected

<What should happen, with numbers where possible (fps, kbps, counters).>

## Actual

<What happens instead, with the numbers you measured.>

## Acceptance criteria

- [ ] <checkable statement, e.g. "a regression test reproduces the symptom and passes after the change">
- [ ] <checkable statement tied to a gate, e.g. "scripts/check.sh unit-tests sanitizers loopback pass">
- [ ] <checkable statement on the stream, e.g. "metrics.json shows 0 NACKs on a clean link for 30 s">

## Acceptance tests

- <one test per line: a ctest name or Unity case, test_<module>_<case>, existing
  or the regression test still to write, that must pass after the change>

<Why: when the branch already fails other tests, the harness gates judge the
change by these tests (no new failure + these pass) instead of an all-green
ladder; without a list, fixing any one of the failures the branch had counts.>

## Out of scope

- <what must not change: e.g. the public API in include/, PROTOCOL.md, the receiver>
- <related problems that get their own ticket>

## Definition of done

- Gate profile `ladder` passes; new or changed tests are listed in scripts/test_manifest.txt.
- The change touches only the paths this ticket needs; the reviewer approved it.
- The commit subject starts with the ticket key.
