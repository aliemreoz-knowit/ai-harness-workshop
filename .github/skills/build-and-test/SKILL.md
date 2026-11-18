---
name: build-and-test
description: Build tinystream and run its tests through the repository scripts (the dev container), pick the right preset and ctest label, and register a new test. Use for every build or test run; never call cmake, gcc, ctest or docker directly.
---

# Build and test

The host has no compiler. Everything runs in the `tinystream-dev:latest`
container through `scripts/`; on Windows run them from Git Bash with
`MSYS_NO_PATHCONV=1`.

```
scripts/build.sh host                    # configure + build a preset
scripts/test.sh host                     # build if needed, then every test of the preset
scripts/test.sh host -L unit             # one label: unit | property | loopback | budget
scripts/test.sh host -R test_txq         # one test executable (ctest regex)
scripts/test.sh asan -L unit             # the same under ASan + UBSan
```

| Preset | Use it for |
|---|---|
| `host` | the normal build and test run (fast, < 10 s) |
| `asan` | memory and undefined-behaviour errors (what the sanitizers gate runs) |
| `coverage` | per-module line coverage (what the coverage gate measures) |
| `pi` | the aarch64 cross build (part of the build gate) |
| `fuzz` | libFuzzer targets (clang, central harness only) |

Tests are Unity 2.7 with a virtual clock (`tests/support/`):

- `tests/unit/test_<module>.c` unit tests, `tests/unit/test_prop_<module>.c`
  property tests (labels `unit` + `property`), `tests/loopback/*.c` whole
  pipeline scenarios with budgets, `tests/budget/*.c` size and codec budgets.
- CMake globs these directories: a new file builds without a CMake change.
  List it in `scripts/test_manifest.txt` (`<ctest name> <label> ...`) in the
  same change, or the gates report it missing.
- A regression test fails without the fix and passes with it; assert the
  observable behaviour from the ticket, not internals. No ticket ids in test
  names, comments or data.

Reading a failure: ctest prints the failing executable; Unity lines read
`file:line:test_name:FAIL: message`. Rerun just that executable with
`-R <name>`; under ASan the first `ERROR: AddressSanitizer` or
`runtime error` line names the bad access.

Done is decided by the gates, not by a green `test.sh`: run the ladder with
the `run-gates` skill (`scripts/check.sh`) before you call a change finished.
Never weaken, skip or delete a test, a threshold or a budget to get a pass.
