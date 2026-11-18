"""setup.ps1 and setup.sh at the top of the repository parse: a syntax error stops every participant's setup."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PS_PARSE = (
    "$e = $null; $t = $null; "
    "[void][System.Management.Automation.Language.Parser]::ParseFile($env:TS_SETUP_PS1, [ref]$t, [ref]$e); "
    "$e | ForEach-Object { \"$($_.Extent.StartLineNumber): $($_.Message)\" }"
)


def test_setup_ps1_parses():
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if shell is None:
        pytest.skip("no PowerShell here")
    script = ROOT / "setup.ps1"
    done = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", PS_PARSE], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120,
                          env={**os.environ, "TS_SETUP_PS1": str(script)})
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "", f"setup.ps1 does not parse:\n{done.stdout}"


def test_setup_sh_parses():
    bash = shutil.which("bash")
    if bash is None or "system32" in bash.lower():
        pytest.skip("no bash here (WSL's bash does not count)")
    done = subprocess.run([bash, "-n", (ROOT / "setup.sh").as_posix()], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
