#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run .github/config-tests/cases.json against the guardrail hooks.

    python3 .github/config-tests/run_cases.py            # guardrails.py (rules of the bash variant)
    python3 .github/config-tests/run_cases.py --sh       # through guardrails.sh, as the CLI calls it
    python3 .github/config-tests/run_cases.py --ps       # guardrails.ps1 (PowerShell variant, needs powershell/pwsh)
    python3 .github/config-tests/run_cases.py --config   # only check that guardrails.json references existing scripts

Also checks .github/hooks/guardrails.json: valid JSON, known events, every
command hook has bash and powershell variants and both scripts exist.
Exit 0 when every case gives the expected decision.
"""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
EVENTS = {"sessionStart", "sessionEnd", "userPromptSubmitted", "preToolUse", "postToolUse",
          "postToolUseFailure", "errorOccurred", "agentStop", "subagentStart", "subagentStop"}


def check_config():
    errs = []
    path = os.path.join(REPO, ".github", "hooks", "guardrails.json")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if cfg.get("version") != 1:
        errs.append("version must be 1")
    hooks = cfg.get("hooks")
    if not isinstance(hooks, dict) or not hooks:
        errs.append("hooks must be a non-empty object")
        hooks = {}
    for ev, items in hooks.items():
        if ev not in EVENTS:
            errs.append("unknown hook event %s" % ev)
        if not isinstance(items, list):
            errs.append("%s must be a list" % ev)
            continue
        for k, h in enumerate(items):
            where = "%s[%d]" % (ev, k)
            if h.get("type") != "command":
                errs.append(where + ": type must be command")
            for shell in ("bash", "powershell"):
                cmd = h.get(shell)
                if not isinstance(cmd, str) or not cmd:
                    errs.append("%s: missing %s variant" % (where, shell))
                    continue
                for tok in cmd.replace("'", " ").replace('"', " ").split():
                    if tok.startswith(".github/") and not os.path.isfile(os.path.join(REPO, tok)):
                        errs.append("%s: %s references missing %s" % (where, shell, tok))
            if not isinstance(h.get("timeoutSec", 30), int):
                errs.append(where + ": timeoutSec must be an integer")
            if "matcher" in h:
                import re
                re.compile(h["matcher"])
    return errs


def runner(ps):
    if "--sh" in sys.argv:       # exactly the bash variant the CLI runs (wrapper + guardrails.py)
        bash = os.environ.get("CHECK_BASH") or "bash"   # check_config.sh: the Git Bash it runs in (never WSL's)
        return [bash, os.path.join(REPO, ".github", "hooks", "scripts", "guardrails.sh")]
    if not ps:
        return [sys.executable, os.path.join(REPO, ".github", "hooks", "scripts", "guardrails.py")]
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if not exe:
        sys.exit("no powershell / pwsh on PATH")
    return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
            os.path.join(REPO, ".github", "hooks", "scripts", "guardrails.ps1")]


def decision(out):
    out = out.strip()
    if not out:
        return "none"
    o = json.loads(out)
    if o.get("permissionDecision") == "deny":
        return "deny"
    ctx = o.get("additionalContext", "")
    if ctx:
        return "flagged" if "INJECTION HEURISTICS" in ctx else "context"
    return "other:" + out


def main():
    errs = check_config()
    for e in errs:
        print("CONFIG " + e)
    if "--config" in sys.argv:
        return 1 if errs else 0
    ps = "--ps" in sys.argv
    base = runner(ps)
    with open(os.path.join(HERE, "cases.json"), encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else ""
    cases = [c for c in cases if only.lower() in c["name"].lower()]
    fails = 0
    for c in cases:
        env = dict(os.environ)
        for k in ("TS_AGENT_ROLE", "TS_ALLOW_PROTECTED", "MSYS_NO_PATHCONV"):   # Git Bash: the hook script path stays convertible
            env.pop(k, None)
        env.update(c.get("env", {}))
        env["TS_REPO_ROOT"] = REPO
        env["TS_HOOK_LOG"] = os.path.join(REPO, "build-check", "hook-cases.jsonl")
        p = subprocess.run(base + [c["mode"]], input=json.dumps(c["input"]).encode("utf-8"),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=REPO, env=env, timeout=60)
        try:
            got = decision(p.stdout.decode("utf-8", "replace")) if p.returncode == 0 else "exit %d" % p.returncode
        except ValueError:
            got = "bad json: " + p.stdout.decode("utf-8", "replace")[:200]
        ok = got == c["expect"]
        fails += 0 if ok else 1
        if "-v" in sys.argv:
            print("  stdout: " + p.stdout.decode("utf-8", "replace").strip())
        print("%-4s %-5s %-50s expect %-8s got %s%s" % ("ok" if ok else "FAIL", c["mode"], c["name"][:50],
                                                       c["expect"], got,
                                                       "" if ok else "  " + p.stderr.decode("utf-8", "replace")[-300:]))
    label = "guardrails.ps1" if ps else "guardrails.sh" if "--sh" in sys.argv else "guardrails.py"
    print("---- %s: %d of %d cases ok%s" % (label, len(cases) - fails,
                                           len(cases), ", config errors: %d" % len(errs) if errs else ""))
    return 1 if fails or errs else 0


if __name__ == "__main__":
    sys.exit(main())
