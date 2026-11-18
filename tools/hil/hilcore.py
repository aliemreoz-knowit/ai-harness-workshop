"""hilcore.py -- candidates, builds, targets and one HIL run (used by hil.py).

A run takes one candidate (a git ref of this repository), exports it with
`git archive` into <work_dir>/cand/<name>-<sha>, cross-compiles its daemon in
the dev container, asks for approval when the target wants a human, deploys
it, runs the checks of hil.yaml through measure.py, compares the summaries
with the budgets and writes <work_dir>/runs/<stamp>-<name>/report.md plus
run.json. tsnetem always comes from the station tree; tsrecv is the
candidate's own build (hil.yaml station.receiver: candidate, receiver-side
changes count) or the station's (--receiver station).
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import budgets as bud
import hilreport as rep

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


class HilError(RuntimeError):
    pass


# ---- configuration --------------------------------------------------------------------------

def load_config(path: Optional[str] = None) -> dict:
    p = Path(path) if path else HERE / "hil.yaml"
    text = p.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        cfg = yaml.safe_load(text)
    except ImportError:
        sys.path.insert(0, str(ROOT))
        from harness import yamlmini  # noqa: E402
        cfg = yamlmini.load(text)
    if not isinstance(cfg, dict) or cfg.get("version") != 1:
        raise HilError("%s: not a version 1 HIL configuration" % p)
    for chk in ("file", "camera"):
        if chk not in cfg.get("checks", {}) or chk not in cfg.get("budgets", {}):
            raise HilError("%s: checks.%s and budgets.%s are required" % (p, chk, chk))
    for chk, c in cfg["checks"].items():
        if chk not in cfg.get("budgets", {}):
            raise HilError("%s: budgets.%s is required for checks.%s" % (p, chk, chk))
        for key in cfg["budgets"][chk]:
            if key not in bud.RULES:
                raise HilError("%s: budgets.%s.%s is not a known budget" % (p, chk, key))
        if source_of(chk, c) not in SOURCES:
            raise HilError("%s: checks.%s.source must be one of %s" % (p, chk, ", ".join(SOURCES)))
        try:
            seq_start_of(c)
        except (KeyError, TypeError, ValueError) as exc:
            raise HilError("%s: checks.%s.wrap: %s" % (p, chk, exc))
    return cfg


# file: the clip (--source file:), pipe: rpicam-vid (Pi) or pipe_source.py (local), synth: the
# daemon's own test pattern (busier than the clip: it fills a tight budget; no reference)
SOURCES = ("file", "pipe", "synth")


def source_of(name: str, c: dict) -> str:
    return str(c.get("source") or ("pipe" if name == "camera" else "file"))


def seq_start_of(c: dict) -> int:
    """checks.<c>.seq_start, or derived from checks.<c>.wrap = {at, pkts_per_s, preroll_s}.

    The daemon numbers its packets from its own start (it fills the ring before the
    viewer joins), so pkt_seq at check time t is about seq_start + pkts_per_s * (t +
    preroll_s): the wrap lands at `at` when seq_start = 65536 - pkts_per_s * (at + preroll_s).
    """
    w = c.get("wrap")
    if not w:
        return int(c.get("seq_start", 0))
    n = int(round(float(w["pkts_per_s"]) * (float(w["at"]) + float(w.get("preroll_s", 0.0)))))
    if not 0 < n < 65536:
        raise ValueError("pkts_per_s * (at + preroll_s) = %d packets, must be 1..65535" % n)
    return 65536 - n


# ---- shell ------------------------------------------------------------------------------------

def find_bash(configured: str = "") -> str:
    try:
        sys.path.insert(0, str(ROOT))
        from harness.envdetect import find_bash as _fb  # noqa: E402
        b = _fb(configured)
    except ImportError:
        b = configured or None
    return b or "bash"


class Shell:
    """Runs commands from the station root and keeps a printable log.

    remote=True marks a command that touches the target (ssh, scp): in dry-run
    mode it is logged, not run. Local work (git, the container) always runs.
    """

    def __init__(self, root: Path = ROOT, dry_run: bool = False, bash: str = "",
                 echo: Optional[Callable[[str], None]] = None) -> None:
        self.root = root
        self.dry_run = dry_run
        self.bash = bash
        self.log: list[str] = []
        self.echo = echo

    def run(self, cmd: list[str], *, remote: bool = False, input: Optional[str] = None, check: bool = True,
            capture: bool = False, timeout: Optional[float] = None, binary: bool = False,
            env: Optional[dict] = None) -> subprocess.CompletedProcess:
        line = " ".join(shlex.quote(str(c)) for c in cmd)
        skip = remote and self.dry_run
        self.log.append(("[dry-run] " if skip else "") + line + (" <<< (stdin)" if input else ""))
        if self.echo:
            self.echo(self.log[-1])
        if skip:
            return subprocess.CompletedProcess(cmd, 0, b"" if binary else "", b"" if binary else "")
        full_env = dict(os.environ, MSYS_NO_PATHCONV="1")
        full_env.update(env or {})
        p = subprocess.run([str(c) for c in cmd], cwd=str(self.root), input=input, capture_output=capture or binary,
                           text=not binary, timeout=timeout, env=full_env)
        if check and p.returncode != 0:
            err = p.stderr if (capture and not binary) else ""
            raise HilError("command failed (%d): %s%s" % (p.returncode, line, ("\n" + err[-2000:]) if err else ""))
        return p

    def docker(self, args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        return self.run([self.bash or find_bash(), "scripts/dockerbuild.sh"] + args, **kw)


# ---- candidates -------------------------------------------------------------------------------

@dataclass
class Candidate:
    ref: str
    sha: str = ""
    name: str = ""
    note: str = ""
    pr: Optional[int] = None
    dir: str = ""          # posix, relative to the station root

    def as_dict(self) -> dict:
        return {"ref": self.ref, "sha": self.sha, "name": self.name, "note": self.note, "pr": self.pr,
                "dir": self.dir}


def slug(ref: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", ref).strip("-.").lower()
    return s[:40] or "candidate"


def resolve(shell: Shell, ref: str) -> str:
    p = shell.run(["git", "rev-parse", "--verify", "--quiet", ref + "^{commit}"], capture=True, check=False)
    if p.returncode != 0 or not p.stdout.strip():
        raise HilError("unknown git ref '%s'" % ref)
    return p.stdout.strip()


def prepare(shell: Shell, cfg: dict, cand: Candidate) -> Candidate:
    """Resolve the ref and export the tree (cached per sha)."""
    cand.sha = cand.sha or resolve(shell, cand.ref)
    cand.name = cand.name or slug(cand.ref)
    rel = "%s/cand/%s-%s" % (cfg["station"]["work_dir"], cand.name, cand.sha[:8])
    dest = shell.root / rel
    if not (dest / "CMakeLists.txt").exists():
        dest.mkdir(parents=True, exist_ok=True)
        p = shell.run(["git", "archive", "--format=tar", cand.sha], binary=True)
        with tarfile.open(fileobj=io.BytesIO(p.stdout)) as tf:
            if sys.version_info >= (3, 12):
                tf.extractall(dest, filter="data")
            else:
                tf.extractall(dest)
    cand.dir = rel
    return cand


def build(shell: Shell, tree: str, preset: str, targets: list[str]) -> None:
    shell.docker(["sh", "tools/hil/build.sh", tree, preset] + targets, capture=True)


def ensure_station_tools(shell: Shell, cfg: dict) -> str:
    preset = cfg["station"].get("tools_preset", "host")
    build(shell, ".", preset, ["tsrecv", "tsnetem"])
    return "build-" + preset


def ensure_clip(shell: Shell, cfg: dict) -> str:
    clip = cfg["station"]["clip"]
    if (shell.root / clip).exists():
        return clip
    alt = "%s/poster_30f.y8" % cfg["station"]["work_dir"]
    if not (shell.root / alt).exists():
        shell.docker(["python3", "scripts/make_clip.py", "--out", alt], capture=True)
    return alt


# ---- targets ----------------------------------------------------------------------------------

def check_spec(cfg: dict, check: str, run_dir: str, clip: str, tools_dir: str,
               rx_bin: Optional[str] = None) -> dict:
    c = cfg["checks"][check]
    ports = cfg["ports"]
    source = source_of(check, c)
    spec: dict[str, Any] = {"name": check, "seconds": c["seconds"], "seq_start": seq_start_of(c),
                            "source": source,
                            "out_dir": run_dir, "tools_dir": tools_dir, "tcp_port": ports["tcp"],
                            "rx_udp_port": ports["rx"], "relay_port": ports["relay"],
                            "receiver": "candidate" if rx_bin else "station"}
    if c.get("wrap"):
        spec["wrap_at"] = float(c["wrap"]["at"])
        spec["preroll_s"] = float(c["wrap"].get("preroll_s", 0.0))
    if rx_bin:
        spec["tsrecv"] = rx_bin
    if c.get("cut"):
        spec["cut"] = {"at": c["cut"]["at"], "s": c["cut"]["s"]}
    if c.get("scenario"):
        spec["scenario"] = [str(x) for x in c["scenario"]]
    if c.get("pause"):
        spec["pause"] = {"at": c["pause"]["at"], "s": c["pause"]["s"]}
    if c.get("press"):
        spec["press"] = str(c["press"])
        spec["heartbeat_ms"] = int(c.get("heartbeat_ms", 0))
    if c.get("kbps"):
        spec["kbps"] = int(c["kbps"])
    if source == "file":
        spec["ref"] = clip
    return spec


class LocalTarget:
    """The daemon runs in the dev container next to tsnetem and tsrecv."""

    name = "local"

    def __init__(self, cfg: dict, shell: Shell) -> None:
        self.cfg = cfg
        self.tcfg = cfg["targets"]["local"]
        self.shell = shell
        self.preset = self.tcfg.get("daemon_build", "pi")
        if self.preset not in ("pi", "host"):
            raise HilError("targets.local.daemon_build must be pi or host")

    def describe(self, cand: Candidate) -> str:
        how = "static aarch64 under qemu-aarch64-static" if self.preset == "pi" else "host build"
        return "%s/build-%s/tinystreamd (%s, dev container)" % (cand.dir, self.preset, how)

    def build(self, cand: Candidate) -> None:
        build(self.shell, cand.dir, self.preset, ["tinystreamd"])

    def run_check(self, cand: Candidate, check: str, run_dir: str, clip: str, tools_dir: str,
                  rx_bin: Optional[str] = None) -> dict:
        spec = check_spec(self.cfg, check, run_dir, clip, tools_dir, rx_bin)
        binary = "%s/build-%s/tinystreamd" % (cand.dir, self.preset)
        spec["mode"] = "local"
        spec["daemon"] = {"cmd": (["qemu-aarch64-static", binary] if self.preset == "pi" else [binary]),
                          "source": spec["source"], "clip": clip,
                          "udp_port": self.cfg["ports"]["udp"]}
        if spec["source"] == "pipe":
            spec["ref"] = clip          # the stand-in source has a reference
            spec["source_desc"] = "rpicam-vid stand-in (tools/hil/pipe_source.py | tinystreamd --source pipe)"
        elif spec["source"] == "synth":
            spec["source_desc"] = "tinystreamd --source synth"
        else:
            spec["source_desc"] = "tinystreamd --source file:%s" % clip
        spec_path = "%s/%s.spec.json" % (run_dir, check)
        _write_json(self.shell.root / spec_path, spec)
        self.shell.docker(["python3", "tools/hil/measure.py", "--spec", spec_path], check=False, capture=True,
                          timeout=float(spec["seconds"]) + 120.0)
        return _read_result(self.shell.root, run_dir, check, spec)


class PiTarget:
    """The daemon runs on the Pi under systemd; the receiver side runs on this laptop."""

    name = "pi"

    def __init__(self, cfg: dict, shell: Shell) -> None:
        self.cfg = cfg
        self.t = cfg["targets"]["pi"]
        self.shell = shell
        self.dest = "%s@%s" % (self.t["user"], self.t["host"]) if self.t.get("user") else self.t["host"]
        self.opts = [str(o) for o in self.t.get("ssh_opts", [])]

    def describe(self, cand: Candidate) -> str:
        return "%s/build-pi/tinystreamd -> %s:%s (systemd unit %s)" % (cand.dir, self.dest, self.t["remote_bin"],
                                                                      self.t["unit"])

    def ssh(self, command: str, input: Optional[str] = None) -> subprocess.CompletedProcess:
        return self.shell.run(["ssh"] + self.opts + [self.dest, command], remote=True, input=input,
                              timeout=60)

    def build(self, cand: Candidate) -> None:
        build(self.shell, cand.dir, "pi", ["tinystreamd"])

    def deploy(self, cand: Candidate, clip: Optional[str] = None) -> None:
        local_bin = "%s/build-pi/tinystreamd" % cand.dir
        self.shell.run(["scp"] + self.opts + [local_bin, "%s:/tmp/tinystreamd.hil" % self.dest], remote=True,
                       timeout=120)
        self.ssh("sudo install -m 0755 /tmp/tinystreamd.hil %s" % shlex.quote(self.t["remote_bin"]))
        if clip and self.t.get("push_clip", True):
            self.shell.run(["scp"] + self.opts + [clip, "%s:/tmp/clip.hil" % self.dest], remote=True, timeout=120)
            self.ssh("sudo install -m 0644 -D /tmp/clip.hil %s" % shlex.quote(self.t["remote_clip"]))

    def env_file(self, source: str, seq_start: int) -> str:
        ports = self.cfg["ports"]
        args = "--seq-start %d --udp-port %d --tcp-port %d" % (seq_start, ports["udp"], ports["tcp"])
        return ("# written by tools/hil/hil.py\nTSD_SOURCE=%s\nTSD_CLIP=%s\nTSD_ARGS=\"%s\"\nRPICAM_ARGS=\"%s\"\n"
                % (source, self.t["remote_clip"], args, self.t.get("rpicam_args", "")))

    def start(self, source: str, seq_start: int) -> None:
        self.ssh("sudo tee %s >/dev/null" % shlex.quote(self.t["env_file"]), input=self.env_file(source, seq_start))
        unit = shlex.quote(self.t["unit"])
        self.ssh("sudo systemctl restart %s && sleep 1 && systemctl is-active --quiet %s" % (unit, unit))

    def run_check(self, cand: Candidate, check: str, run_dir: str, clip: str, tools_dir: str,
                  rx_bin: Optional[str] = None) -> dict:
        c = self.cfg["checks"][check]
        rcv = self.t.get("receiver", {})
        spec = check_spec(self.cfg, check, run_dir, clip, rcv.get("tools_dir", tools_dir), rx_bin)
        source = {"pipe": "camera"}.get(spec["source"], spec["source"])   # TSD_SOURCE of the unit
        self.start(source, int(spec["seq_start"]))
        spec.update({"mode": "remote", "server": self.t["host"], "relay_bind": rcv.get("relay_bind", "0.0.0.0")})
        spec["source_desc"] = {"camera": "rpicam-vid | tinystreamd --source pipe on %s" % self.t["host"],
                               "synth": "tinystreamd --source synth on %s" % self.t["host"]}.get(
            source, "tinystreamd --source file:%s on %s" % (self.t["remote_clip"], self.t["host"]))
        if source == "camera" and c.get("pause"):
            at, s = float(c["pause"]["at"]), float(c["pause"]["s"])
            base = ["ssh"] + self.opts + [self.dest]
            spec["events"] = [{"at": at, "cmd": base + ["sudo pkill -STOP -x rpicam-vid"]},
                              {"at": at + s, "cmd": base + ["sudo pkill -CONT -x rpicam-vid"]}]
        spec_path = "%s/%s.spec.json" % (run_dir, check)
        _write_json(self.shell.root / spec_path, spec)
        if self.shell.dry_run:
            self.shell.run(["python3", "tools/hil/measure.py", "--spec", spec_path], remote=True)
            return {"name": check, "spec": spec, "summary": {}, "errors": ["dry run: not measured"], "dry_run": True}
        cmd = ["python3", "tools/hil/measure.py", "--spec", spec_path]
        if rcv.get("runner", "native") == "docker":
            self.shell.docker(cmd, check=False, capture=True, timeout=float(spec["seconds"]) + 120.0)
        else:
            self.shell.run([sys.executable] + cmd[1:], check=False, capture=True,
                           timeout=float(spec["seconds"]) + 120.0)
        return _read_result(self.shell.root, run_dir, check, spec)

    def finish(self) -> None:
        leave = self.t.get("leave_source")
        if leave:
            self.start(leave, 0)


def make_target(name: str, cfg: dict, shell: Shell) -> Any:
    if name == "local":
        return LocalTarget(cfg, shell)
    if name == "pi":
        return PiTarget(cfg, shell)
    raise HilError("unknown target '%s' (local | pi)" % name)


# ---- one run ----------------------------------------------------------------------------------

def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8", newline="\n")


def _read_result(root: Path, run_dir: str, check: str, spec: dict) -> dict:
    path = root / run_dir / ("%s.result.json" % check)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"name": check, "spec": spec, "summary": {}, "errors": ["no result from measure.py (%s)" % path]}


def evaluate_check(cfg: dict, check: str, result: dict) -> dict:
    spec = result.get("spec", {})
    extra: dict[str, tuple[bool, Any, str]] = {}
    if result.get("daemon_alive") is not None:
        extra["daemon_alive"] = (bool(result["daemon_alive"]), "yes" if result["daemon_alive"] else "no",
                                 "daemon still running at the end")
    errors = list(result.get("errors", []))
    extra["errors"] = (not errors, len(errors), "measurement errors")
    skip = {}
    if spec.get("receiver") == "candidate" and result.get("summary", {}).get("tear_frames") is None:
        skip["tear_frames"] = "the candidate's tsrecv has no tear detector"
    rows = bud.evaluate(result.get("summary", {}), cfg["budgets"][check], has_ref=bool(spec.get("ref")),
                        extra=extra, skip=skip)
    return {"name": check, "title": cfg["checks"][check].get("title", check), "spec": spec,
            "summary": result.get("summary", {}), "rows": rows, "verdict": bud.verdict(rows), "errors": errors}


def selected_checks(cfg: dict, wanted: Optional[list[str]]) -> list[str]:
    if wanted:
        bad = [c for c in wanted if c not in cfg["checks"]]
        if bad:
            raise HilError("unknown check(s): %s" % ", ".join(bad))
        return wanted
    return ["file"] + [n for n, c in cfg["checks"].items() if n != "file" and c.get("enabled", False)]


Approver = Callable[[Candidate, str], bool]


@dataclass
class RunResult:
    verdict: str                      # PASS | FAIL | DECLINED | ERROR | DRY-RUN
    run_dir: str
    report_path: str = ""
    report: str = ""
    checks: list[dict] = field(default_factory=list)
    error: str = ""


def station_desc(shell: Shell) -> str:
    p = shell.run(["git", "rev-parse", "--short=10", "HEAD"], capture=True, check=False)
    return "%s @ %s" % (shell.root.name, (p.stdout or "").strip() or "?")


def run_candidate(cfg: dict, cand: Candidate, target_name: str, shell: Shell, approve: Approver,
                  checks: Optional[list[str]] = None, log: Callable[[str], None] = print,
                  receiver: Optional[str] = None) -> RunResult:
    """One candidate on one target. receiver: station (default, hil.yaml station.receiver) | candidate."""
    t_start = time.time()
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    names = selected_checks(cfg, checks)
    target = make_target(target_name, cfg, shell)
    run_dir = ""
    run: dict[str, Any] = {"target": target_name, "started": _dt.datetime.now().isoformat(timespec="seconds"),
                           "dry_run": shell.dry_run, "checks": []}
    res = RunResult(verdict="ERROR", run_dir="")
    try:
        prepare(shell, cfg, cand)
        run_dir = "%s/runs/%s-%s" % (cfg["station"]["work_dir"], stamp, cand.name)
        (shell.root / run_dir).mkdir(parents=True, exist_ok=True)
        res.run_dir = run_dir
        run.update({"candidate": cand.as_dict(), "run_dir": run_dir, "daemon_desc": target.describe(cand),
                    "station": station_desc(shell)})
        log("hil: %s (%s) -> %s, checks %s" % (cand.ref, cand.sha[:10], target_name, ",".join(names)))
        tools_dir = ensure_station_tools(shell, cfg)
        clip = ensure_clip(shell, cfg)
        log("hil: building the daemon of %s" % cand.ref)
        target.build(cand)
        receiver = receiver or cfg["station"].get("receiver", "station")
        rx_bin = None
        if receiver == "candidate":
            # the candidate's own receiver (its receiver-side fixes are part of the change)
            build(shell, cand.dir, "host", ["tsrecv"])
            rx_bin = "%s/build-host/tsrecv" % cand.dir
        elif receiver != "station":
            raise HilError("receiver must be station or candidate, got '%s'" % receiver)
        run["receiver"] = receiver
        if not approve(cand, target_name):
            res.verdict = "DECLINED"
            run["verdict"] = "DECLINED"
            log("hil: deploy of %s not approved" % cand.ref)
            return res
        if target_name == "pi":
            target.deploy(cand, clip)
        for chk in names:
            log("hil: check %s" % chk)
            result = target.run_check(cand, chk, run_dir, clip, tools_dir, rx_bin)
            if result.get("dry_run"):
                ev = {"name": chk, "title": cfg["checks"][chk].get("title", chk), "spec": result["spec"],
                      "summary": {}, "rows": [], "verdict": "DRY-RUN", "errors": []}
            else:
                ev = evaluate_check(cfg, chk, result)
            run["checks"].append(ev)
            log("hil: check %s: %s" % (chk, ev["verdict"]))
        if target_name == "pi":
            target.finish()
        if shell.dry_run and target_name == "pi":
            res.verdict = "DRY-RUN"
        else:
            res.verdict = "FAIL" if any(c["verdict"] == "FAIL" for c in run["checks"]) else "PASS"
    except (HilError, subprocess.TimeoutExpired, OSError) as exc:
        res.verdict = "ERROR"
        res.error = str(exc)
        log("hil: error: %s" % exc)
    run["verdict"] = res.verdict
    run["duration_s"] = round(time.time() - t_start, 1)
    run["commands"] = list(shell.log)
    run.setdefault("candidate", cand.as_dict())
    if res.error:
        run["error"] = res.error
    res.checks = run["checks"]
    if run_dir:
        res.report = rep.render(run) + (("\n**Error:** %s\n" % res.error) if res.error else "")
        res.report_path = "%s/report.md" % run_dir
        (shell.root / res.report_path).write_text(res.report, encoding="utf-8", newline="\n")
        _write_json(shell.root / run_dir / "run.json", run)
    return res


# ---- approval (human in the loop) ---------------------------------------------------------------

def make_approver(cfg: dict, yes: bool = False, stdin: Any = None, out: Any = None) -> Approver:
    """approval: auto passes; human asks on the terminal and refuses without one (never hangs)."""
    stdin = stdin or sys.stdin
    out = out or sys.stdout

    def approve(cand: Candidate, target: str) -> bool:
        mode = cfg["targets"][target].get("approval", "human")
        if mode == "auto" or yes:
            return True
        if not (hasattr(stdin, "isatty") and stdin.isatty()):
            out.write("hil: approval needed to deploy %s (%s) to %s, but there is no terminal: not approved\n"
                      % (cand.ref, cand.sha[:10], target))
            return False
        what = ("PR #%d " % cand.pr) if cand.pr else ""
        out.write("hil: deploy %s%s (%s) to %s? [y/N] " % (what, cand.ref, cand.sha[:10], target))
        out.flush()
        answer = stdin.readline().strip().lower()
        return answer in ("y", "yes")

    return approve
