"""scripts/dockerbuild.sh: the run label and the read-only .git.

Most tests put a stub ``docker`` first on PATH that prints its arguments, one per line, so they
check what the script would start. The last two use the real docker (skipped without it, or without
the dev image: a test never builds it) and prove what a container can and cannot write.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from harness import envdetect

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dockerbuild.sh"
BASH = envdetect.find_bash()
IMAGE = "tinystream-dev:latest"      # the script's default; clean_env() drops TS_IMAGE

pytestmark = [
    pytest.mark.skipif(BASH is None, reason="no bash"),
    # in a container the script runs the command directly and never reaches docker
    pytest.mark.skipif(os.path.exists("/.dockerenv"), reason="already inside a container"),
]


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not (k.startswith("TS_") or k == "HARNESS_RUN_ID")}
    env.update(extra)
    return env


@pytest.fixture
def stub_docker(tmp_path: Path) -> dict[str, str]:
    """An environment whose ``docker`` knows the image and prints the arguments of ``docker run``."""
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text('#!/bin/sh\nif [ "$1" = "image" ]; then exit 0; fi\nprintf "%s\\n" "$@"\n',
                    encoding="utf-8", newline="\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return clean_env(PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])


def docker_run_args(workdir: Path, env: dict[str, str], *cmd: str) -> list[str]:
    done = subprocess.run([str(BASH), str(SCRIPT), *(cmd or ("echo", "hi"))], cwd=workdir, env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    args = done.stdout.splitlines()
    assert args[0] == "run", args
    return args


def mounts(args: list[str]) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == "-v"]


def work(tmp_path: Path) -> Path:
    w = tmp_path / "work"
    (w / "src").mkdir(parents=True)
    return w


# ---------------------------------------------------------------- the script itself ----
def test_the_script_parses() -> None:
    assert subprocess.run([str(BASH), "-n", str(SCRIPT)], capture_output=True, text=True).returncode == 0


def test_the_container_gets_the_run_label_when_there_is_a_run(tmp_path: Path, stub_docker: dict) -> None:
    w = work(tmp_path)
    args = docker_run_args(w, dict(stub_docker, HARNESS_RUN_ID="console-7"))
    assert args[args.index("--label") + 1] == "tinystream.run=console-7"
    assert "--label" not in docker_run_args(w, stub_docker)


def test_the_command_follows_the_image(tmp_path: Path, stub_docker: dict) -> None:
    args = docker_run_args(work(tmp_path), stub_docker, "echo", "a b")
    assert args[-3:] == [IMAGE, "echo", "a b"]


# ------------------------------------------------------------ .git is read-only ----
def test_a_git_directory_is_mounted_read_only_over_the_worktree(tmp_path: Path, stub_docker: dict) -> None:
    w = work(tmp_path)
    (w / ".git").mkdir()
    m = mounts(docker_run_args(w, stub_docker))
    assert len(m) == 2
    assert m[0].endswith(":/work") and m[1].endswith("/.git:/work/.git:ro")     # parent first, then the overlay
    assert Path(m[1].removesuffix(":/work/.git:ro")).resolve() == (w / ".git").resolve()


def test_a_git_file_is_mounted_read_only_too(tmp_path: Path, stub_docker: dict) -> None:
    # a linked worktree: .git is a file "gitdir: <main>/.git/worktrees/<name>"
    w = work(tmp_path)
    (w / ".git").write_text("gitdir: /somewhere/.git/worktrees/w\n", encoding="utf-8")
    m = mounts(docker_run_args(w, stub_docker))
    assert len(m) == 2 and m[1].endswith("/.git:/work/.git:ro")
    assert Path(m[1].removesuffix(":/work/.git:ro")).resolve() == (w / ".git").resolve()


def test_without_a_git_there_is_nothing_to_overlay(tmp_path: Path, stub_docker: dict) -> None:
    # docker would create a missing mount source on the host as an empty directory
    w = work(tmp_path)
    m = mounts(docker_run_args(w, stub_docker))
    assert len(m) == 1 and m[0].endswith(":/work")
    assert not (w / ".git").exists()


# ---------------------------------------------------- the same, with a real container ----
def real_docker_ready() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    try:
        return subprocess.run([docker, "image", "inspect", IMAGE], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


needs_docker = pytest.mark.skipif(not real_docker_ready(), reason=f"no docker, or no {IMAGE} image")


def in_container(w: Path, script: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(BASH), str(SCRIPT), "sh", "-c", script], cwd=w, env=clean_env(),
                          capture_output=True, text=True, timeout=300)


@needs_docker
def test_a_container_cannot_write_a_git_directory_but_can_write_sources(tmp_path: Path) -> None:
    w = work(tmp_path)
    (w / ".git").mkdir()
    head = w / ".git" / "HEAD"
    head.write_text("ref: refs/heads/main\n", encoding="utf-8")
    done = in_container(w, "touch /work/src/x && rm /work/src/x || exit 10;"        # sources stay writable
                           "touch /work/.git/planted && exit 11;"                  # no new file
                           "echo planted >> /work/.git/HEAD && exit 12;"           # no edit
                           "mkdir /work/.git/hooks && exit 13;"                    # no hooks
                           "rm -rf /work/.git && exit 14;"                         # no removal
                           "exit 0")
    assert done.returncode == 0, (done.returncode, done.stderr)
    assert head.read_text(encoding="utf-8") == "ref: refs/heads/main\n"
    assert sorted(p.name for p in (w / ".git").iterdir()) == ["HEAD"]


@needs_docker
def test_a_container_cannot_rewrite_a_gitfile(tmp_path: Path) -> None:
    w = work(tmp_path)
    gitfile = w / ".git"
    gitfile.write_text("gitdir: /somewhere/.git/worktrees/w\n", encoding="utf-8")
    done = in_container(w, "touch /work/src/x && rm /work/src/x || exit 10;"
                           "echo 'gitdir: /evil' > /work/.git && exit 11;"
                           "echo 'gitdir: /evil' >> /work/.git && exit 12;"
                           "rm -f /work/.git && exit 13;"
                           "mv /work/.git /work/.git.old && exit 14;"
                           "exit 0")
    assert done.returncode == 0, (done.returncode, done.stderr)
    assert gitfile.read_text(encoding="utf-8") == "gitdir: /somewhere/.git/worktrees/w\n"
    assert not (w / ".git.old").exists()
