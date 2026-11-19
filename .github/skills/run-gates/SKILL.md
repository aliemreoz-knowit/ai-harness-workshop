---
name: run-gates
description: Run the tinystream gate ladder (scripts/check.sh build, misra, no-heap, unit-tests, property-tests, sanitizers, fuzz, loopback, budgets, coverage, compliance, diff-policy), read a failing gate's log and turn it into a fix. Use before declaring a change done.
---

# Run the gates

```
MSYS_NO_PATHCONV=1 scripts/check.sh                                # the whole ladder
MSYS_NO_PATHCONV=1 scripts/check.sh build unit-tests loopback      # selected gates
TS_BASE_REF=<base branch> scripts/check.sh compliance diff-policy  # your change since the base branch
TS_REQUIRE_TICKET=1 TS_TICKET=<KEY> TS_ALLOWED_PATHS="src/txq/** tests/unit/**" \
TS_BASE_REF=<base branch> scripts/check.sh compliance              # as the harness runs it for a ticket
```

Each gate prints one line: `<gate> PASS|FAIL|SKIP <ms> (budget) <name> <reason>`,
then a summary. Exit 0 = all PASS (fuzz may SKIP), 1 = a FAIL, 3 = another gate
SKIPped (ladder incomplete). Full logs: `build-check/<gate>.log`.

check.sh runs the gate ladder (`scripts/lite_gates.py`, zig cc): every reason says `zig cc`,
and fuzz and coverage say `SKIP ... not run here` (exit 0 when nothing FAILed). The harness counts
those SKIPs like the fuzz gate's, not as an incomplete ladder.
On Windows `test_capture` and `test_lb_file_clip` show as `SKIP` lines in the
log. The container ladder is the reference: clang (zig) and gcc can warn
differently.

| Gate | Fails when | Look at |
|---|---|---|
| build | a warning or error on host or aarch64 | first `error:` in build.log |
| misra | a ticket id in a source tree, a MISRA finding, the canary not tripping | `file:line: ... [misra-c2012-x.y]` |
| no-heap | an allocating / process call in a daemon object, or the daemon aborts (exit 134) | `forbidden calls referenced`, `no-heap-host.txt` |
| unit-tests / property-tests | a unit / property test fails or a manifest test is missing | Unity `FAIL` lines |
| sanitizers | ASan / UBSan report in any test | first `ERROR: AddressSanitizer` / `runtime error` |
| fuzz | fuzz crash (only with `TS_FUZZ=1`) | `fuzz-*.txt`, `crash-*` input |
| loopback | a loopback budget (fps, NACK ratio, freezes, recovery) | the scenario's budget lines |
| budgets | ratio, PSNR, encoded size, `.text`/`.bss`, stack frame | `BUDGET`, `SIZE`, `STACK` lines |
| coverage | a module below its own-test coverage threshold | `coverage-<module>.txt` |
| compliance | no Jira key in the last commit (`TS_REQUIRE_TICKET=1`), missing SPDX line, wire format or PROTOCOL.md changed without a `PROTOCOL_VERSION` bump, a path outside `TS_ALLOWED_PATHS` | the `ticket:`, `license:`, `protocol:`, `paths:` lines |
| style | a tab, trailing whitespace, a line over 120 columns or no final newline in a line you added | `file:line [line-length]` (rule in brackets) |
| comments | a new 15+ line function without a comment above it, a new file whose header does not say what it is for, a `TODO`/`FIXME`/`XXX` in a new comment | `file:line [function-comment]` (rule in brackets) |
| diff-policy | a protected path touched, a new `system`/`popen`/`exec*`/`fork`/`posix_spawn`/`dlopen` call, a new TLV type | the `protected:`, `calls:`, `tlv:` lines |

Fix loop: take the first failing gate, read the smallest excerpt that names
file, line and rule, fix the cause, rerun only that gate, then the full list.
Never edit the gate scripts, thresholds, budgets, the manifest's existing
lines or `scripts/cppcheck-suppressions.txt` to get a PASS. The compliance and
diff-policy gates are about the change, so they need `TS_BASE_REF` set to the
branch you started from (default: uncommitted changes only).
