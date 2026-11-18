"""scripts/device.sh: the simulated device (daemon + relay + receiver + board server in one container)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = ROOT / "scripts" / "device.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")


def run(*args):
    return subprocess.run([BASH, "scripts/device.sh", *args], cwd=ROOT, capture_output=True, text=True,
                          timeout=60, env={**__import__("os").environ, "MSYS_NO_PATHCONV": "1"})


def docker_line(out: str) -> str:
    lines = [ln for ln in out.splitlines() if "docker run" in ln and not ln.startswith("build:")]
    assert len(lines) == 1, out
    return lines[0]


def test_device_script_dry_run():
    p = run("start", "--dry-run")
    assert p.returncode == 0, p.stderr
    line = docker_line(p.stdout)
    assert "--name ts-device" in line
    assert "-p 127.0.0.1:8000:8000" in line
    assert "poster_30f.y8" in line
    assert "open http://localhost:8000/tools/board/mission.html" in p.stdout


def test_dry_run_port_and_synth():
    p = run("start", "--dry-run", "--port", "8123", "--source", "synth")
    assert p.returncode == 0, p.stderr
    line = docker_line(p.stdout)
    assert "-p 127.0.0.1:8123:8000" in line and "--source synth" in line and "poster_30f" not in line


def test_dry_run_pipe_demo():
    p = run("pipe-demo", "--dry-run")
    assert p.returncode == 0, p.stderr
    line = docker_line(p.stdout)
    assert "--source pipe" in line


def test_usage_errors():
    assert run("bogus").returncode == 2
    assert run("start", "--source", "tape", "--dry-run").returncode == 2
    assert run("start", "--port", "x", "--dry-run").returncode == 2


def test_usage_lists_every_form():
    p = run("bogus")
    assert p.returncode == 2
    for form in ("device.sh start [", "--second-viewer [--viewer2-rate <kbps>]", "device.sh pipe-demo [",
                 "send keyframe [--dry-run]", "send keyframe --one-shot", "send set-rate=<kbps>",
                 "device.sh stop"):
        assert form in p.stderr, (form, p.stderr)


def test_serves_only_the_board_on_loopback():
    p = run("start", "--dry-run")
    assert "serving build-device/web" in p.stdout
    text = SCRIPT.read_text()
    assert "http.server 8000 --directory" in text          # never the repository root
    assert "-p 8000:8000" not in p.stdout and "-p 0.0.0.0" not in p.stdout


def exec_line(out: str) -> str:
    lines = [ln for ln in out.splitlines() if "docker exec ts-device" in ln]
    assert len(lines) == 1, out
    return lines[0]


def viewer_lines(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if "/build-host/tsrecv --server 127.0.0.1:5701" in ln]


def test_send_set_rate_dry_run():
    p = run("send", "set-rate=600", "--dry-run")
    assert p.returncode == 0, p.stderr
    line = exec_line(p.stdout)
    assert "/build-host/tsrecv --server 127.0.0.1:5701 --send set-rate=600" in line


def test_send_usage_errors():
    for bad in ([], ["rate=600"], ["set-rate=0"], ["set-rate=abc"], ["set-rate="], ["keyframe", "--port", "1"]):
        assert run("send", *bad, "--dry-run").returncode == 2, bad


def test_second_viewer_dry_run():
    p = run("start", "--second-viewer", "--dry-run")
    assert p.returncode == 0, p.stderr
    line = docker_line(p.stdout)
    assert "-p 127.0.0.1:8000:8000" in line and "--second-viewer" in line
    rx = viewer_lines(p.stdout)
    assert len(rx) == 2, p.stdout
    assert "--metrics build-device/web/.harness/board/metrics.json" in rx[0]
    assert "--frame build-device/web/.harness/board/frame.pgm" in rx[0]
    assert "--metrics build-device/web/.harness/board2/metrics.json" in rx[1]
    assert "--frame build-device/web/.harness/board2/frame.pgm" in rx[1]
    ports = [ln.split("--udp-port ")[1].split()[0] for ln in rx]
    assert ports[0] != ports[1], rx                         # two clients of the daemon on one host
    assert "serving build-device/web" in p.stdout            # both boards inside the one web root
    assert "open http://localhost:8000/tools/board/mission.html?feed2=../../.harness/board2" in p.stdout
    assert "-p 0.0.0.0" not in p.stdout and "-p 8000:8000" not in p.stdout
    one = run("start", "--dry-run")
    assert len(viewer_lines(one.stdout)) == 1 and "board2" not in one.stdout
    assert "--second-viewer" in docker_line(run("pipe-demo", "--second-viewer", "--dry-run").stdout)


def test_a_built_ref_is_not_built_again(tmp_path):
    """The build of a ref is made once per commit. The container's binaries have no exec bit a Windows
    checkout can see, so the cache looks for the files themselves."""
    import os
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, repo / "scripts" / "device.sh")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "device"], check=True)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                         text=True).stdout.strip()
    built = repo / "build-device" / sha[:12] / "build-host"
    built.mkdir(parents=True)
    for exe in ("tinystreamd", "tsrecv", "tsnetem"):
        (built / exe).write_bytes(b"\x7fELF")
        (built / exe).chmod(0o644)                          # as a Windows checkout sees them: no exec bit
    calls = tmp_path / "docker-calls.txt"
    fake = tmp_path / "bin" / "docker"                      # answers everything, records the calls
    fake.parent.mkdir()
    fake.write_text(f'#!/bin/sh\necho "$*" >> "{calls.as_posix()}"\nexit 0\n', encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    env = {**os.environ, "MSYS_NO_PATHCONV": "1", "PATH": str(fake.parent) + os.pathsep + os.environ["PATH"]}
    p = subprocess.run([BASH, "scripts/device.sh", "start", "--ref", "HEAD"], cwd=repo, env=env,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "[device] building" not in p.stdout, p.stdout
    runs = [ln for ln in calls.read_text(encoding="utf-8").splitlines() if ln.startswith("run ")]
    assert len(runs) == 1 and "--name ts-device" in runs[0], runs      # the device, no build container


def test_build_carries_the_host_user_off_windows():
    import os
    env = {**os.environ, "MSYS_NO_PATHCONV": "1", "TS_DEVICE_UNAME": "Linux"}
    for extra in ([], ["--ref", "HEAD"]):
        p = subprocess.run([BASH, "scripts/device.sh", "start", "--dry-run", *extra], cwd=ROOT,
                           capture_output=True, text=True, timeout=60, env=env)
        assert p.returncode == 0, p.stderr
        build = [ln for ln in p.stdout.splitlines() if ln.startswith("build: docker run")]
        assert len(build) == 1, p.stdout
        assert " -u " in build[0] and "dockerbuild" not in build[0]
        assert " -u " in docker_line("\n".join(ln for ln in p.stdout.splitlines() if not ln.startswith("build:")))
    p = subprocess.run([BASH, "scripts/device.sh", "start", "--dry-run"], cwd=ROOT, capture_output=True,
                       text=True, timeout=60, env={**env, "TS_DEVICE_UNAME": "MINGW64_NT"})
    assert " -u " not in p.stdout


def test_viewer_one_has_heartbeat_and_fifo_stdin():
    p = run("start", "--dry-run")
    assert p.returncode == 0, p.stderr
    rx = viewer_lines(p.stdout)
    assert len(rx) == 1, p.stdout
    assert "--heartbeat-ms 1000" in rx[0]
    assert "< /tmp/ts-keys" in rx[0] or "</tmp/ts-keys" in rx[0], rx[0]     # stdin from the FIFO in the container
    assert "/dev/null" not in rx[0]
    assert "mkfifo /tmp/ts-keys" in p.stdout


def test_send_keyframe_writes_k_into_the_fifo():
    p = run("send", "keyframe", "--dry-run")
    assert p.returncode == 0, p.stderr
    line = exec_line(p.stdout)
    assert "docker exec ts-device" in line and "/tmp/ts-keys" in line and "k" in line.split("/tmp/ts-keys")[0]
    assert "--send" not in line                              # not the one-shot


def test_send_keyframe_one_shot_keeps_the_old_line():
    p = run("send", "keyframe", "--one-shot", "--dry-run")
    assert p.returncode == 0, p.stderr
    line = exec_line(p.stdout)
    assert "/build-host/tsrecv --server 127.0.0.1:5701 --send keyframe" in line
    assert "/tmp/ts-keys" not in line
    assert run("send", "set-rate=600", "--one-shot", "--dry-run").returncode == 2    # keyframe only


def test_viewer_two_rate_option():
    p = run("start", "--second-viewer", "--viewer2-rate", "1000", "--dry-run")
    assert p.returncode == 0, p.stderr
    rx = viewer_lines(p.stdout)
    assert len(rx) == 2, p.stdout
    assert "--press" not in rx[0] and "--kbps" not in rx[0]  # viewer 1 is not affected
    assert "--udp-port 5611" in rx[1] and "--kbps 1000" in rx[1] and "--heartbeat-ms 1000" in rx[1], rx[1]
    assert "--press" not in rx[1]                            # a kept rate, not a rate that keeps halving
    assert "--viewer2-rate" in docker_line(p.stdout)         # reaches the container
    for bad in (["--viewer2-rate", "1000"], ["--second-viewer", "--viewer2-rate", "0"],
                ["--second-viewer", "--viewer2-rate", "x"], ["--second-viewer", "--viewer2-rate"]):
        assert run("start", *bad, "--dry-run").returncode == 2, bad


# Arrays that can be empty: under `set -u`, bash older than 4.4 (macOS ships 3.2) stops at "${A[@]}" of an empty
# array ("unbound variable"). bash 5 cannot show it (no option, `--posix` or BASH_COMPAT brings the old behaviour
# back), so every expansion of such an array is written ${A[@]+"${A[@]}"}, and a real bash 3.2 runs the dry run
# when its image is on this machine.
MAYBE_EMPTY = ("USER_ARGS", "INSIDE_ARGS", "PIDS")
OLD_BASH_IMAGE = "bash:3.2"


def test_arrays_that_can_be_empty_are_expanded_the_old_bash_way():
    import os
    import re
    text = SCRIPT.read_text()
    for name in MAYBE_EMPTY:
        bare = re.findall(r'(?<!\+)"\$\{' + name + r'\[[@*]\]\}"', text)
        assert bare == [], f"{name}: write ${{{name}[@]+\"${{{name}[@]}}\"}}"
        assert f'${{{name}[@]+"${{{name}[@]}}"}}' in text, name
    env = {**os.environ, "MSYS_NO_PATHCONV": "1", "TS_DEVICE_UNAME": "MINGW64_NT"}   # no -u, no second viewer
    p = subprocess.run([BASH, "scripts/device.sh", "start", "--dry-run"], cwd=ROOT, capture_output=True, text=True,
                       timeout=60, env=env)
    assert p.returncode == 0, p.stderr
    line = docker_line(p.stdout)
    assert "  " not in line and " -u " not in line and line.endswith("--source file:tests/data/poster_30f.y8")


def test_dry_run_under_bash_3_2():
    docker = shutil.which("docker")
    try:
        have = docker is not None and subprocess.run([docker, "image", "inspect", OLD_BASH_IMAGE], capture_output=True,
                                                     timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        have = False
    if not have:
        pytest.skip(f"no local {OLD_BASH_IMAGE} image (docker pull {OLD_BASH_IMAGE})")
    for uname, extra in (("MINGW64_NT", []), ("MINGW64_NT", ["--second-viewer", "--viewer2-rate", "900"]),
                         ("Linux", [])):  # no --ref: the image has no git
        p = subprocess.run([docker, "run", "--rm", "-v", f"{ROOT}:/work:ro", "-w", "/work", "-e",
                            f"TS_DEVICE_UNAME={uname}", OLD_BASH_IMAGE, "bash", "scripts/device.sh", "start",
                            "--dry-run", *extra], capture_output=True, text=True, timeout=120)
        assert p.returncode == 0 and "unbound variable" not in p.stderr, (uname, extra, p.stderr)
        assert "--name ts-device" in docker_line(p.stdout)
