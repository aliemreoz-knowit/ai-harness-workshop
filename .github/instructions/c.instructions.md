---
applyTo: "src/**,tools/**"
---

# C rules for `src/` and `tools/`

- C11, no extensions; the build uses `-Wall -Wextra -Wpedantic -Werror -Wshadow
  -Wconversion -Wsign-conversion -Wstrict-prototypes -Wvla -Wundef -Wformat=2`
  (libraries also `-Wmissing-prototypes`). Write code that is clean under all of
  them: explicit casts between widths and signedness, no VLAs, `(void)` in empty
  parameter lists.
- Fixed-width types (`uint8_t`, `uint16_t`, `uint32_t`, `size_t` for sizes);
  no plain `int`/`unsigned` for protocol fields or counters.
- Every function is `static` or declared in its module header. Do not change
  `include/*.h`; if you need more, describe the additive change in your report.
- No heap in `src/`: no `malloc`/`calloc`/`realloc`/`free`, no allocating libc
  calls (`strdup`, `asprintf`, `getline`, `fopen`, `opendir`, ...). Tables are
  static with a documented full/overflow result (`TS_E_FULL`, `TS_E_NOMEM`, ...).
  `tools/` may allocate once at start-up (`tsr_create`).
- No `system`, `popen`, `exec*`, `fork`, `posix_spawn*`, `dlopen`, anywhere.
- No stdio in `src/` except `main.c` and `log.c` under `TS_LOG_ENABLED`.
- No `time()`, `rand()`, `sleep()` or direct socket calls in a module: time
  comes in as `now_ms`, randomness from the seeded xorshift32, I/O through the
  `ts_io_t` vtable.
- Every parser is bounded by the length it is given, as in the existing modules.
- MISRA subset of the misra gate (10.x, 11.x, 15.x, 17.7, 21.3, 21.6, 21.8,
  21.21): use the return value of non-void calls or cast it to `(void)`, single
  exit per function where the module already does so. An inline `cppcheck-suppress`
  needs a justification comment on the same or previous line.
- Wire behaviour (layouts, constants, sequence, codec and rate rules) comes
  from `PROTOCOL.md`; cite the section in a comment when you implement a rule.
- New or changed files start with `/* SPDX-License-Identifier: MIT */`, then
  the usual header comment (`tinystream v2 -- <path>`, what the file does).
- No ticket ids in code, comments or test data.
