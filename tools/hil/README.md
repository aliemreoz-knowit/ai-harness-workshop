# tools/hil -- the HIL station (the hardware gate)

`hil.py` takes a candidate (a git ref), cross-compiles its daemon, puts it on
the Pi (or runs it locally under qemu), measures the stream through the same
`tsnetem` and `tsrecv` every time, compares with the budgets in `hil.yaml` and
writes a markdown report. From the queue it also takes GitHub PRs labelled
`hil-ready` and posts the report back. Python 3.9+, standard library only
(PyYAML is used when present, else `harness/yamlmini.py`).

```
python tools/hil/hil.py run --ref main                          # local: dev container, qemu, ~1 min
python tools/hil/hil.py run --ref main --checks file,keys       # only these checks
python tools/hil/hil.py run --ref agent/x-1 --target pi         # the Pi (asks facilitator B first)
python tools/hil/hil.py run --ref main --target pi --dry-run --yes -v   # list the ssh/scp/systemctl commands only
python tools/hil/hil.py queue --target pi --github --watch 30   # queue file + hil-ready PRs
python tools/hil/hil.py queue --github --dry-run --recorded tools/hil/tests/fixtures/github \
       --repo example-org/tinystream --file /dev/null           # the PR path without network
python tools/hil/hil.py check-config
python -m pytest tools/hil/tests -q                             # unit tests + end to end (dev image)
```

Exit status: 0 PASS, 1 FAIL or DECLINED, 2 error.

## One run

```
git archive <ref> -> build-hil/cand/<name>-<sha>/      (cached per commit)
dev container: cmake --preset pi --target tinystreamd   (static aarch64; local can use the host build)
station tools: build-host/tsrecv, build-host/tsnetem    (from THIS tree: tsnetem always; tsrecv with --receiver station)
candidate rx  <cand>/build-host/tsrecv                  (station.receiver: candidate, the default in hil.yaml)
approval      targets.<t>.approval: human asks on the terminal, refuses without one; auto passes
deploy        pi: scp -> sudo install -> /etc/default/tinystreamd -> systemctl restart (Restart=always)
check file    10 s, --source file:<clip>, --seq-start derived so that pkt_seq wraps inside the cut
              (hil.yaml checks.file.wrap), tsnetem blackout 1 s at t=3 s, 2 % loss from t=5 s,
              tsrecv --ref <clip>: fps, NACK ratio + CRC failures on the clean link, freezes,
              recovery after the cut, fps from t=6.5 s, torn frames, exact match, NACKs answered TOO_OLD
check camera  10 s, --source pipe fed by rpicam-vid (pi) or tools/hil/pipe_source.py (local),
              the source pauses 3 s at t=4 s (kill -STOP on the Pi): fps, freezes, torn frames,
              longest gap between daemon STATS, reconnects, exact match (local only)
check keys    10 s, file source, clean link, tsrecv --press k:2000 with the rate heartbeat on
              (--heartbeat-ms 1000): keys sent, presses answered with a keyframe within 1.25 s,
              presses ignored, fps, freezes, exact match
report        build-hil/runs/<stamp>-<name>/report.md + run.json + every spec, result, log
```

The packet rate decides where pkt_seq wraps: the daemon numbers its packets
from its own start, so `checks.file.wrap` derives `--seq-start` from the
measured packets/s and the daemon's head start (`seq_start = 65536 - pkts_per_s
* (at + preroll_s)`). On a new target or with another clip, read "packets/s"
in the report, re-derive, and confirm on a known-good ref (the wrap estimate is
in the report too).

Every number comes from `tsrecv`'s `metrics.json`, sampled every 50 ms by
`measure.py` (it runs where the receiver runs: in the dev container for
`--target local`, on the station laptop for `--target pi`). The tear
detector is in `tools/tsrecv/rx_tear.h`: a torn frame has 1..32 row pairs
whose step is new against the previous frame (a static edge is not new; a
whole new picture is not a tear). `--receiver candidate` measures with the
candidate's own `tsrecv` instead (receiver-side changes count; its missing
tear detector is a SKIP, never a PASS). `hil.yaml` sets `station.receiver:
candidate`; `--receiver station` measures one run with the station's build.

## Files

| file | what |
|---|---|
| `hil.py` | CLI: `run`, `queue`, `check-config` |
| `hil.yaml` | checks (file, camera, keys; source file / pipe / synth, scenario, key presses, viewer budget), budgets, ports, targets (Pi address, ssh, approval), queue, GitHub |
| `hilcore.py` | candidates, builds, `LocalTarget` / `PiTarget`, one run, approval |
| `measure.py` | one check: processes, scenario, sampling, `summarize()` |
| `pipe_source.py` | rpicam-vid stand-in: YUV420 frames from a clip, short reads, scripted pause |
| `budgets.py`, `hilreport.py` | budget rows and verdict; markdown report / PR comment |
| `hilqueue.py`, `ghclient.py` | queue file, the team bridge queue (`.harness/hil/queue.jsonl`) + state; GitHub REST (reads; writes skipped in `--dry-run`) |
| `build.sh` | `cmake --preset` + build of a few targets inside the container |
| `pi/` | systemd unit, run wrapper, defaults, `setup-pi.sh`, `CHECKLIST.md` |
| `tests/` | pytest: pure functions, recorded GitHub responses, Pi dry run, end to end (marker `docker`) |

## GitHub

`hil.yaml github.repo` and a token in `HIL_GITHUB_TOKEN` (fine-grained, this
repository only: pull requests read, issues read and write). The client never
reads other credentials. `--dry-run` lists the PRs and records every write in
`build-hil/github-dryrun.jsonl`; `--recorded DIR` answers the reads from
recorded responses (no network at all). PR heads are fetched with
`git fetch <remote> pull/<n>/head:refs/hil/pr-<n>`; nothing is ever pushed.

## Simulated device

Without a Pi, `scripts/device.sh` runs the stream of the projector on this
laptop, in one dev-image container (Git Bash on Windows: `MSYS_NO_PATHCONV=1`):

```
scripts/device.sh start [--source file|synth] [--ref REF] [--port 8000]   # then open the printed address
scripts/device.sh start --second-viewer    # plus a second viewer, the "rx 2" thumbnail on the board
scripts/device.sh pipe-demo [--ref REF]    # camera stand-in: torn frames, a 3 s stall at t=4 s
scripts/device.sh start --second-viewer --viewer2-rate 1000   # viewer 2 keeps a rate of 1000 kbit/s
scripts/device.sh send keyframe            # (another terminal) a 'k' key of the running viewer
scripts/device.sh send keyframe --one-shot # a keyframe request from a client of its own
scripts/device.sh send set-rate=600        # (another terminal) a rate change, in kbit/s
scripts/device.sh stop
scripts/device.sh start --dry-run          # print the docker lines and the viewers, start nothing
```

Inside `ts-device`: `tinystreamd --source file:tests/data/poster_30f.y8` (the
`make_clip.py` stand-in when the clip is missing) -> `tsnetem --slider` on the
attached terminal (`5`, `0`, `+`, `-`, `burst 50`, `blackout 1s`, `pull`,
`plug`, `flip=2`, `s`, `q`; `q` ends the device) -> `tsrecv` writing
`.harness/board/metrics.json` and `frame.pgm`, plus `python -m http.server 8000`
serving only `build-device/web` (`mission.html` and the board files) on
`127.0.0.1:<port>`, for `http://localhost:<port>/tools/board/mission.html`. The
daemon, relay and receiver are the host build of `--ref` (`git archive` into
`build-device/<sha>`, built once per commit); default is the working tree. Logs:
`build-device/logs/`. `hil.py run --ref REF` (target `local`) is unchanged.

Viewer 1 (behind the slider's relay) runs with `--heartbeat-ms 1000`, as the
hil keys check does, and reads its keys from a FIFO inside the container
(`/tmp/ts-keys`). `send keyframe` writes `k` into it, and the board's "viewer
keys" tile counts it (k sent, answered, ignored). Send it three times: a
daemon that answers keys shows 3 answered, one that ignores them shows 3
ignored. `send keyframe
--one-shot` and `send set-rate=<kbps>` instead run tsrecv's one-shot `--send` in
`ts-device` (`docker exec`, the device's own build of the ref): a client of its
own that connects for about a second and prints what it sent and, for a rate,
the rate the daemon reports for it. A rate is the rate of that client only, so
the viewers keep theirs; `--viewer2-rate <kbps>` (with `--second-viewer`) is the
way to give a viewer a rate it keeps: viewer 2 runs `--kbps <kbps>
--heartbeat-ms 1000`, a lowered rate repeated every second (the "rx 2"
thumbnail then shows what the daemon does with a viewer on a low rate: 30 fps
on a good daemon, a crawl on a broken one). `--second-viewer` (with `start` or `pipe-demo`) adds a second
`tsrecv` in the container: a second client of the daemon from the same host, on
udp 5611 straight to the daemon (the slider's relay carries the first viewer
only), writing `.harness/board2/`. The printed address carries
`?feed2=../../.harness/board2`, which shows it as the "rx 2" thumbnail;
`?data=../../.harness/board2` shows it as the whole board.
