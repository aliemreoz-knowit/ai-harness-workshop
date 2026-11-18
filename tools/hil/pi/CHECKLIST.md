# HIL station checklist

The station is the Pi (daemon + camera) on a direct Ethernet cable to the
station laptop (tsnetem, tsrecv, `hil.py`). Facilitator B owns it.

Two refs are used below; set them for the checkout the station runs from:

- `GOOD` -- a ref known to pass every check (the station's reference build,
  e.g. its release tag: `git tag --list 'v*'`). Not `main` by default: in a
  workshop copy `main` is the start state.
- `BAD` -- a ref known to fail the file check (the workshop start state:
  `main` in a workshop copy, the start branch in the station's own checkout).

## Once (about a week before)

- [ ] Flash Raspberry Pi OS Lite 64-bit (Bookworm) with Raspberry Pi Imager:
      hostname `tinystream-pi`, an admin user, SSH on, Wi-Fi only if needed for apt.
- [ ] Camera Module on the CAM/DISP 0 connector, ribbon contacts the right way round.
- [ ] Station laptop: an SSH key for the deploy user (`ssh-keygen -t ed25519 -f ~/.ssh/hil`),
      `~/.ssh/config` entry `Host 10.42.0.2` with `User hil` and `IdentityFile ~/.ssh/hil`.
- [ ] Copy and run the setup on the Pi (it is idempotent):
      ```
      scp -r tools/hil/pi admin@tinystream-pi:/tmp/hil-pi
      ssh admin@tinystream-pi "sudo HIL_PUBKEY='$(cat ~/.ssh/hil.pub)' sh /tmp/hil-pi/setup-pi.sh"
      ```
      Expect `camera OK: one 320x240 YUV420 frame = 115200 bytes`.
- [ ] Laptop Ethernet: static `10.42.0.1/24`, no gateway. `ping 10.42.0.2` answers.
- [ ] Receiver side runs where UDP from the Pi arrives: Linux / macOS natively, or
      WSL2 with mirrored networking (`networkingMode=mirrored` in `.wslconfig`). Build
      the station tools there (`scripts/build.sh host`) and keep
      `targets.pi.receiver.runner: native` in `tools/hil/hil.yaml`.
      Docker Desktop's NAT does not forward the Pi's UDP into a container.
- [ ] Record the HIL clip on the Pi (detailed poster + slow object, good light):
      ```
      ssh hil@10.42.0.2 "sudo systemctl stop tinystreamd; rpicam-vid -n --frames 30 --codec yuv420 \
          --width 320 --height 240 --framerate 30 -o /tmp/poster.yuv"
      scp hil@10.42.0.2:/tmp/poster.yuv . && python3 scripts/make_clip.py --from-yuv420 poster.yuv
      ```
      (until then `hil.py` uses the generated stand-in; `push_clip: true` copies the
      station clip to the Pi so both ends compare the same frames).
- [ ] First deploy: `python tools/hil/hil.py run --target pi --ref $GOOD --checks file,camera,keys`
      must PASS. Read "packets/s" in the report and re-derive `checks.file.wrap` in
      `hil.yaml` (`pkts_per_s`; `preroll_s` = the daemon's head start before the viewer
      joins) so that "pkt_seq wraps at" (setup line of the file check) falls into the
      1 s link cut; run once more and compare with the estimate under "Also measured".
- [ ] Rehearse a FAIL: `python tools/hil/hil.py run --target pi --ref $BAD` must FAIL
      (CRC failures on a clean link, NACK ratio, exact match).

## On the day (before 09:00)

- [ ] Cable, power, camera pointed at the poster, light on.
- [ ] `ssh hil@10.42.0.2 systemctl is-active tinystreamd` -> `active`.
- [ ] `python tools/hil/hil.py check-config` and one `run --target pi --ref $GOOD` (about 1 min).
- [ ] Projector viewer: `tsrecv --server 10.42.0.2:5701 --sdl` shows the camera.
- [ ] Queue file `build-hil/queue.txt` empty; GitHub token for the queue exported
      (`HIL_GITHUB_TOKEN`, fine-grained, this repository only: pull requests read,
      issues read/write for the comment and labels).
- [ ] Dry run of the PR path: `python tools/hil/hil.py queue --target pi --github --dry-run`
      lists the `hil-ready` PRs and the comment it would post, and writes nothing.

## HIL window (10 to 15 min each)

1. `python tools/hil/hil.py queue --target pi --github` (or `--watch 30`).
2. For every candidate the station asks `deploy ... to pi? [y/N]`: B checks the
   PR (green ladder, review approved) and answers. A `n` is recorded as DECLINED
   and asked again only when the PR gets a new commit.
3. The report goes to the PR as a comment (`hil-pass` / `hil-fail` label);
   the projector shows the camera again afterwards (`leave_source: camera`).

## Fallbacks

| Problem | Do this |
|---|---|
| Camera dead / dark | `checks.camera.enabled: false`; the file check still runs; a UVC webcam with `TSD_SOURCE=synth` keeps the projector alive |
| Pi does not boot | second SD card with the same image (cloned after the setup above) |
| Direct link down | `hil.py run --target local`: the same checks in the dev container, the aarch64 binary under qemu |
| Queue / GitHub down | put refs into `build-hil/queue.txt` by hand; paste `report.md` into the PR |
| A candidate kills the Pi stream | `Restart=always` brings it back after 2 s; `hil.py run --target pi --ref $GOOD` restores the reference build |
| Camera stall demo | `ssh hil@10.42.0.2 sudo pkill -STOP -x rpicam-vid` ... `-CONT` |
