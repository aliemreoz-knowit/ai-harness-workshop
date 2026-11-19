# tinystream v2 -- architecture

Practice project of the AI Harness Workshop. A C11 daemon (`tinystreamd`)
streams 320x240 gray8 frames over UDP with NACK-based repair and a TCP control
channel; `tsrecv` receives, decodes and measures; `tsnetem` breaks the link on
purpose. Everything builds and runs inside the devcontainer; the whole pipeline
also runs **in-process** in tests with a virtual clock.

The wire format is specified in [`PROTOCOL.md`](../PROTOCOL.md). The public
API is `include/*.h` (frozen, see section 5).

## 1. Module map

```
include/            frozen API, one header per module (ts_<module>.h)
src/proto/          ts_proto.h   DATA header, NACK, TLV parser, CRC32, sequence helpers              -> libtsproto
src/codec/          ts_codec.h   keyframe RLE, P-frame XOR delta vs reference, quality step, tiles      -> libtsproto
src/txq/            ts_txq.h     fragmenter, 448-slot history ring, per-client cursor, NACK lookup        -> libtsd
src/rate/           ts_rate.h    token buckets, loss adaptation, congestion control, keyframe scheduler -> libtsd
src/clients/        ts_clients.h static table of 4 viewers, lifecycle                                -> libtsd
src/net/            ts_daemon.h  daemon core: one tick = capture, encode, send, NACKs, TLVs, STATS      -> libtsd
src/io/             ts_io.h      I/O vtable: POSIX sockets + poll (io_posix.c), in-memory fake (io_fake.c) -> libtsd
src/capture/        ts_capture.h synth | file | pipe (rpicam-vid YUV420) | v4l2                         -> libtsd
src/pool/           ts_pool.h    the daemon storage pool, seal() for the malloc guard               -> libtsd
src/log/            ts_log.h     numeric ring log, compiled out of the daemon                            -> libtsd
src/main.c          argument parsing, init order, poll loop, SIGTERM                                   -> tinystreamd
src/malloc_guard.c  --wrap=malloc/calloc/realloc/free: abort after ts_pool_seal()                       -> tinystreamd
tools/tsrecv/lib/   ts_receiver.h reassembly (8 in flight), NACK generator, decoder, TCP client, metrics -> libtsr
tools/tsrecv/       viewer / metrics.json / frame.pgm / --send one-shots (SDL2 optional, TS_WITH_SDL)   -> tsrecv
tools/tsnetem/lib/  ts_netem.h   chaos filter + in-process shim + scenario timeline                     -> libtsnetem_shim
tools/tsnetem/      userspace UDP relay with a live loss slider                                         -> tsnetem
tools/hil/          hil.py: HIL station (hardware gate) -- candidate build, Pi or local qemu stand-in, file,
                    camera and viewer-key checks with budgets, report / PR comment;
                    tools/hil/pi/ provisioning (Python)
tools/bridge/       bridge.py: label-triggered team bridge (agent:fix -> harness run -> PR, hil-ready
                    -> HIL queue), --dry-run against recorded GitHub/Jira responses (Python)
tests/unit/         test_<module>.c (label unit), test_prop_<module>.c (labels unit+property), Unity 2.7.0
tests/loopback/     daemon + receiver(s) through the shim, virtual clock (label loopback)
tests/budget/       codec budgets of the budgets gate: ratio, PSNR, TS_ENC_MAX (label budget)
tests/support/      ts_loop.{h,c} harness glue, ts_lbcheck.{h,c} loopback checker, ts_vectors_gen.c
tests/vectors/      generated conformance vectors (json + ts_vectors.{h,c}) from scripts/protocol_vectors.py
fuzz/               libFuzzer targets (fuzz_pkt_parse, fuzz_ctrl_parse, fuzz_codec_decode) + corpus seeds
scripts/            dockerbuild.sh, build.sh, test.sh, check.sh (gate ladder), smoke.sh (real sockets),
                    opening_demo.sh (projector stream for the board), protocol_vectors.py,
                    make_clip.py (file-source clip), misra.json,
                    cppcheck-suppressions.txt, coverage_thresholds.txt, test_manifest.txt,
                    misra_canary/canary.c, no_heap_canary/canary.c (misra / no-heap self-tests, not built, not scanned)
cmake/aarch64.cmake Pi cross toolchain (static, qemu-aarch64-static as test emulator)
```

Library dependency order: `tsproto` <- `tsd` <- `tsr`, `tsnetem_shim` <- tests.
The daemon binary links `tsd` + `tsproto` only.

## 2. Data flow

```
Pi / daemon tick (tsd_tick, no blocking, no heap):
  ts_cap_read ──frame──► ts_enc_encode ──enc──► ts_txq_push_frame ──► ring slots
       ▲                      ▲ quality (ts_loss_update, ts_cc_update: stream vs slowest viewer)
       │ injected now_ms      ▲ keyframe (ts_keysched_decide)     ▼ per client: ts_txq_cursor_peek -> ts_bucket_take -> udp_send
  ts_io_t vtable ◄─────────── udp_recv NACK ──► ts_nack_parse ──► ts_client_find ──► ts_txq_lookup ──► retransmit
                 ◄─────────── tcp_accept/recv ──► ts_tlv_acc_push/next ──► HELLO/KEYFRAME_REQ/SET_RATE/BYE ──► ts_client_*
                 ────────────► tcp_send WELCOME / STATS (1 s)

Laptop / receiver tick (tsr_tick):
  udp_recv ──► ts_hdr_parse (CRC, bounds) ──► reassembly table (8 frames, dup drop, late policy)
           ──► pkt_seq gap ──► NACK list (<= 32) ──► udp_send
  complete frame ──► ts_dec_decode (TS_E_NOREF -> KEYFRAME_REQ) ──► frame + metrics
  tcp: connect -> HELLO -> WELCOME; STATS in; KEYFRAME_REQ / SET_RATE / BYE out
```

The rules every module follows on the wire (sequence numbers, codec
references, rates) are specified in PROTOCOL.md.

## 3. Build and test

Everything runs inside the image `tinystream-dev:latest`
(`.devcontainer/Dockerfile`, Debian bookworm: gcc 12.2, clang-14, cmake 3.25,
ninja, cppcheck 2.10, gcovr 5.2, qemu-user-static 7.2, aarch64 cross gcc 12.2,
Node 22 + Copilot CLI 1.0.100, Python 3.11 venv with github-copilot-sdk 1.0.23).

```
scripts/dockerbuild.sh <cmd...>   # run anything in the container, cwd mounted at /work
scripts/build.sh <preset>         # cmake --preset + build
scripts/test.sh  <preset> [ctest args]
scripts/check.sh [gate ...]       # the gate ladder (section 4)
python3 scripts/protocol_vectors.py   # regenerate tests/vectors + fuzz/corpus after a PROTOCOL.md change
```

Presets (`CMakePresets.json`, build dir `build-<preset>`):

| preset | compiler / flags | purpose |
|---|---|---|
| host | gcc -O1 -g | daily build, unit + loopback tests |
| asan | gcc -fsanitize=address,undefined -fno-sanitize-recover=all, linked `-no-pie` | sanitizers |
| fuzz | clang-14 -fsanitize=fuzzer,address; libs + fuzz/ targets only, linked `-no-pie` | fuzz |
| pi | aarch64-linux-gnu-gcc -static; ctest runs under qemu-aarch64-static | build, HIL |
| coverage | gcc --coverage -O0 | coverage |

All our code compiles with `-std=c11 -Wall -Wextra -Wpedantic -Werror -Wshadow
-Wconversion -Wsign-conversion -Wstrict-prototypes -Wvla -Wundef -Wformat=2`;
libraries additionally with `-Wmissing-prototypes`. Unity is compiled with `-w`.

CMake globs every directory (`CONFIGURE_DEPENDS`): add a file under
`src/<module>/`, `tests/unit/test_*.c`, `tests/loopback/*.c`, `tests/budget/*.c`
or `fuzz/fuzz_*.c` and it is picked up; **module implementers never edit
CMakeLists.txt**. `tsproto`, `tsd` and `tinystreamd` compile with
`-fstack-usage` (the `.su` files feed the budgets gate).

**Why `-no-pie` for the sanitizer presets.** Docker Desktop's WSL2 kernel
(6.x) runs with `vm.mmap_rnd_bits=32`; with that much ASLR entropy a PIE
executable built with gcc 12's libasan (and clang 14's) sometimes lands where
the shadow memory must go, and the binary hangs before `main` in an endless
`AddressSanitizer:DEADLYSIGNAL` loop (gcc) or segfaults (clang). Measured:
22 of 60 runs of `test_harness` hung as PIE, 0 of 60 with `-no-pie`. A fixed
load address avoids the collision without touching the VM (`sysctl`) or the
container seccomp profile (`setarch -R`). Sanitizer coverage is unchanged.

Measurements of the integrated golden tree (28-CPU host, Docker Desktop,
WSL2): clean host + pi build 11.7 s (build gate); host ctest (25 tests) about 2.5 s
wall; asan ctest about 6 s; real-socket `scripts/smoke.sh` 10 s. The
seq-wrap scenario wraps `pkt_seq` and `frame_seq` once in 20 virtual seconds
(0.2 s).

## 4. Gate ladder (`scripts/check.sh`)

Times are wall clock from `scripts/check.sh` on the integrated golden tree:
first number with all build dirs removed beforehand (each gate pays its own
build), second number incremental. Every gate line also prints its budget
(design doc 3.3, adjusted where noted) and flags `OVER BUDGET`.

Exit status: 0 only when every requested gate PASSed; the fuzz gate is the
only one that may SKIP (fuzz runs in the central harness). Any FAIL exits 1;
any other SKIP prints `LADDER INCOMPLETE` and exits 3. A gate function signals
SKIP with code 77, so a failing command can never pass as a SKIP. Missing tests
are a FAIL: `scripts/test_manifest.txt` lists every test executable the ladder
requires and unit-tests, property-tests, sanitizers, loopback, budgets and
coverage check it (tests are globbed by CMake, so a deleted test file would
otherwise just disappear).

Without a usable Docker, `check.sh` on a host hands off to the Docker-free
ladder (`scripts/lite_gates.py`, zig cc): build, unit-tests, property-tests,
loopback, budgets (codec budgets) and diff-policy run there, the gates that
need the container SKIP with `needs Docker (not run here)`, and it exits 0
when nothing FAILed. The harness counts those SKIPs like the fuzz gate's ("not
run here", not an incomplete ladder) and names them:
`not run here: fuzz coverage`.

| gate | what | state | clean / incr. |
|---|---|---|---|
| build | host + pi build, -Werror | PASS | 11.7 s / 0.4 s |
| misra | (0) no `TS-[0-9]{3}` ticket id in `src include tests tools fuzz PROTOCOL.md`; (1) canary `scripts/misra_canary/canary.c` must trip 21.3, 21.6, 21.8 and 21.21; (2) `cppcheck --addon=misra` subset 10.x, 11.x, 15.x, 17.7, 21.3, 21.6, 21.8 + 21.21 (`scripts/misra.json`; cppcheck 2.10 checks `system()` under 21.21, not 21.8) over `src include tools tests/support` with the log compiled out; (3) a second pass over `tools tests/support src/log` with `TS_LOG_ENABLED=1`. File-level exceptions in `scripts/cppcheck-suppressions.txt`, inline `cppcheck-suppress` only with a justification (container-of casts, BSD `sockaddr` casts, `mmap` `void *`). Runs on a container-local copy of the tree (bind-mount reads made it 25 s) | PASS | 4.6 s |
| no-heap | `nm -u` on tsproto/tsd/tinystreamd objects: no allocator or allocating libc call (malloc family, strdup/strndup, posix_memalign/aligned_alloc/memalign/valloc, asprintf, getline/getdelim, open_memstream/fmemopen, fopen/fdopen/freopen/tmpfile, opendir/scandir) and no process creation or program loading (exec* family, fork, vfork, posix_spawn/posix_spawnp, popen, system, dlopen); a canary object (`scripts/no_heap_canary/canary.c`) must trip the list for each of them first; then `tinystreamd` (host, and the pi binary under qemu-aarch64-static) runs 1 s on free ports and must stop cleanly on SIGTERM (the `--wrap` malloc guard aborts with 134). Budget 3 s (1 s of it is the run) | PASS | 1.7 s |
| unit-tests | ctest label `unit` (host): 13 executables, 251 cases | PASS | 0.7 s |
| property-tests | ctest label `property` (`test_prop_*.c`) | PASS | 0.4 s |
| sanitizers | every test (unit, property, loopback, budget) in the asan preset | PASS | 6 s |
| fuzz | 3 fuzz targets x `FUZZ_SECS` (`TS_FUZZ=1`) | SKIP by default; PASS with `TS_FUZZ=1 FUZZ_SECS=10` | 34 s |
| loopback | ctest label `loopback`: clean, 5 % loss, burst 50, reorder/dup, corrupt (bit flip, truncation), cable pull, two clients, seq wrap (short), SET_RATE (incl. a rate below the stream bitrate), tight link under loss, worst-case frames, file source | PASS | 0.8 s |
| budgets | see below (budget 2 s: two incremental builds + the budget test) | PASS | 1.6 s |
| coverage | gcovr line coverage per module on its own tests (`scripts/coverage_thresholds.txt`), budget 15 s (one coverage run per module) | PASS | 14.3 s |
| compliance | checks the change (working tree against the merge base with `TS_BASE_REF`, default `HEAD`): Jira key in the last commit message when `TS_REQUIRE_TICKET=1` (`TS_TICKET` names the expected key), `SPDX-License-Identifier: MIT` in the first 5 lines of every new or changed C file, `PROTOCOL_VERSION` equal in PROTOCOL.md and `ts_config.h` and bumped whenever the DATA header packing (PROTOCOL.md 3 layout, `ts_hdr_t`, header constants, `HDR_*` offsets, `hdr_write20()`) or PROTOCOL.md itself changed, every changed path inside `TS_ALLOWED_PATHS` (globs, when set) | PASS | < 0.1 s |
| diff-policy | checks the same change: no path of `scripts/protected_paths.txt` touched (unless `TS_ALLOW_PROTECTED` approves it), no added `system`/`popen`/`exec*`/`fork`/`posix_spawn*`/`dlopen` call in any C file, no new TLV type (enumerator or `#define` with a value, or a new row in the PROTOCOL.md 5 table) | PASS | < 0.1 s |
| style | style of the added lines (`harness/form.py`, Python, run by the harness): C files under `src tools tests fuzz`, no tab, no trailing whitespace, at most 120 columns, final newline | PASS | < 0.1 s |
| comments | comments & docs of the added code (`harness/form.py`): a new function of 15+ body lines has a comment directly above it (`test_*` excepted), a new file's header comment says what it is for, no `TODO`/`FIXME`/`XXX` in new comments | PASS | < 0.1 s |
| hardware | HIL (`tools/hil/hil.py`: cross build -> scp -> systemctl -> file check with a blackout across the pkt_seq wrap + camera check with a source pause + viewer-key check; `--target local` runs the Pi binary under qemu) | facilitator station | ~1 min local |

Full ladder: about 27 s incremental on the bind mount (coverage and
sanitizers dominate), 20 s from a clean container-local checkout (`git archive
HEAD` into /tmp). It was 60-86 s while the misra gate read the bind-mounted
tree.

**Budgets.** `tests/budget/test_budget_codec.c` (label `budget`) encodes and
decodes 600 synth frames (two scene cuts, two noise bursts) with the daemon's
keyframe policy and prints `BUDGET` lines; `check.sh budgets` then measures
sizes and stack frames of the daemon objects (`tsproto`, `tsd` minus the test-only
`io_fake.c`, `tinystreamd`) for host and pi. Limits can be overridden with
`TS_BUDGET_TEXT_MAX`, `TS_BUDGET_BSS_MAX`, `TS_BUDGET_STACK_MAX`.

| budget | limit | golden |
|---|---|---|
| compression ratio, quality 1 | >= 10.0 | 11.64 |
| mean PSNR, quality 2 / 4 | >= 22 dB / >= 20 dB | 23.9 dB / 22.1 dB (q1 is lossless and must stay exact) |
| decoder output == encoder reference | every frame | every frame |
| largest encoded frame (noise, adversarial patterns, q1/2/4, K and P) | <= 5n/3+2+8 = 128010 <= TS_ENC_MAX (153664), guard bytes intact | 77414 B (56 fragments of 110) |
| raw-sized output buffer | refused with TS_E_NOMEM, reference unchanged | yes |
| `.text` (Berkeley text = code + rodata) | <= 64 KiB | host 36613 B, pi 39683 B |
| `.bss` | <= 1.5 MiB (same bound as the pool `_Static_assert`) | 1042500 B (the static pool and the encoded frame buffer) |
| largest stack frame (`-fstack-usage`) | <= 2048 B, no unbounded `dynamic` frame | 736 B (`main`) |

**The coverage gate** measures every module on **its own unit tests only**:
for each line of `scripts/coverage_thresholds.txt` the `.gcda` counters are
reset, `ctest -R '^test_(prop_)?<module>$'` runs, and gcovr checks the
module's own object directory with `--fail-under-line`. Coverage that loopback
scenarios or other modules' tests happen to produce does not count (with it,
txq stayed at 85 % with every txq unit test deleted). A module without its own
test fails. The all-tests coverage is printed afterwards as information only.
Scanning the whole build tree is avoided on purpose: on a Docker Desktop bind
mount it takes minutes, and merging the header-inline functions of the test
TUs trips an assertion in gcovr 5.2. Thresholds and golden values (lines,
own tests; all tests in brackets):

| module | dir | min | golden |
|---|---|---|---|
| proto | src/proto/ | 85 % | 100.0 % (100.0) |
| codec | src/codec/ | 85 % | 99.7 % (99.7) |
| txq | src/txq/ | 80 % | 99.4 % (99.4) |
| rate | src/rate/ | 80 % | 100.0 % (100.0) |
| clients | src/clients/ | 80 % | 99.5 % (100.0) |
| capture | src/capture/ | 50 % | 52.7 % (52.7; V4L2 needs a device) |
| net | src/net/ | 60 % | 90.6 % (91.2) |
| tsrecv | tools/tsrecv/lib/ | 60 % | 95.7 % (96.5) |
| netem | tools/tsnetem/lib/ | 60 % | 97.9 % (98.1) |

**Baseline-aware gates in the harness.** `scripts/check.sh` stays absolute.
The agent harness (`harness/baseline.py`, `loop.baseline: auto`) records the
test gates unit-tests, property-tests, sanitizers, loopback and coverage once
on the untouched base of a run. When that base already fails tests (a branch
with other open defects), a test gate passes for the change when no test that
passed on the base fails now and the ticket's `Acceptance tests` pass (no list:
at least one base failure passes now); the coverage gate when no module drops
below its base coverage or its threshold, whichever is lower. Build, static and
policy gates stay absolute.

**Hidden tests.** Evaluation-only tests (the held-back-tests gate) live in
`hidden-tests/` and are kept out of the agent's copy (never on a `ticket/*`
branch); `hidden-tests/run.sh` builds them against a checkout in the dev image. They
compile against the frozen API (section 5) and the public test support only.

**Code size** (`git ls-files`, C sources and headers, lines): src 5243,
include 1890, tools/tsrecv 2698, tools/tsnetem 1860, tests/unit 7829,
tests/loopback 1182, tests/support 627, tests/budget 234, fuzz 134; about
21.7 k in total (plus 1.1 k of generated vectors), against the design
estimate of about 6 k. The difference is mostly tests (9.9 k) and the two
tools (4.6 k against 1.35 k planned: metrics.json, PGM/SDL output, scenario
timeline, live slider). Block timings and context budgets in the design doc
use the real numbers.

A gate never reports PASS for work it did not do: missing tests FAIL, and only the fuzz gate may report **SKIP with a reason** (plus, on the Docker-free ladder, the gates that need the container: `needs Docker (not run here)`).

## 5. The frozen API rule

`include/*.h` and the structure of `PROTOCOL.md` are protected paths: hidden
evaluation tests compile against them, so they must keep compiling on every
branch. Allowed: **additive, non-breaking** changes (new functions, new enum
values at the end, new struct members at the end, new constants) accompanied by
a note in the PR and, when the wire format changes, a `PROTOCOL_VERSION` bump
in both `PROTOCOL.md` and `ts_config.h`. Not allowed: renaming, removing,
changing signatures, reordering struct members, changing constant values.

## 6. Loopback tests: daemon + receiver through the shim

`tests/support/ts_loop.h` wires one daemon and up to two receivers:

```
ts_io_fake_t io_d (10.0.0.1)        ts_io_fake_t io_r[i] (10.0.0.32+i : 6000+i)
   daemon  --udp_out-->  ts_netem_pump_star(filter)  --udp_in-->  receiver i
   daemon  <--udp_in--   ts_netem_pump_star(filter)  <--udp_out-- receiver i   (NACKs)
   TCP: ts_io_fake_link_tcp(io_r[i], io_d): tcp_connect lands in io_d's accept queue,
        bytes move directly between the two fakes (reliable, not filtered);
        io_fake.tcp_send_limit caps the bytes one tcp_send() accepts.
```

One `ts_loop_step()` is one virtual millisecond (configurable): set clocks ->
`tsd_tick(now)` -> pump -> `tsr_tick(now)` for each receiver -> pump -> `now +=
tick`. Nothing sleeps and nothing touches a socket, so every scenario is
bit-reproducible (`ts_netem_cfg.seed`); about 600 virtual seconds cost about
6.5 s of CPU (host, -O1), and every scenario in this tree takes well under a
second. Scenarios change the link at run time with
`ts_loop_set_netem()` or `ts_netem_scenario_start()` (cable pull = `cable_pulled`
for a window). Budgets (fps, NACK ratio, recovery time, freezes) are read from
`tsr_metrics()` and `tsd_stats()`.

The same `ts_netem_filter()` runs inside the `tsnetem` relay on the HIL station,
so a scenario that passes in loopback means the same thing on the Pi link.

## 7. Implementer contract (for the module agents)

1. **Scope**: you own exactly `src/<module>/` (or `tools/<tool>/lib/`) and
   `tests/unit/test_<module>*.c`. The header in `include/` is the contract: every
   function it declares must exist and behave as documented and as PROTOCOL.md
   says. Do not touch other modules; if the header is insufficient, propose an
   *additive* change and list it in your report.
2. **Rules**: C11, warnings as errors with the flags in section 3; no dynamic
   memory anywhere in `src/` (the receiver may `calloc` once in `tsr_create`);
   no stdio in `src/` except `main.c` and `log.c` under `TS_LOG_ENABLED`;
   single-threaded; explicit widths (`uint16_t`, not `unsigned`), no implicit
   conversions (`-Wconversion` is on), every function prototyped (`static` or in
   the header), clean under the MISRA subset of the misra gate (inline
   suppressions need a comment).
3. **Determinism**: time comes in as `now_ms`; randomness comes from a seeded
   xorshift32; never call `time()`, `rand()`, `sleep()` or a socket from a
   module. The `ts_io_t` vtable is the only way to the network.
4. **Tables**: every table is static and has a documented full/overflow
   behaviour (`TS_E_FULL`, `TS_E_TOO_OLD`, ...).
5. **Tests first**: use the vectors (`tests/vectors/ts_vectors.h`, generated from
   PROTOCOL.md by an independent Python implementation) and the virtual clock;
   property tests go to `test_prop_<module>.c`; aim at the coverage threshold.
6. **Done means**:
   `scripts/check.sh build misra no-heap unit-tests property-tests sanitizers`
   green, your module's coverage threshold met, no new suppressions without
   justification, the report lists any header change (expected: none).
