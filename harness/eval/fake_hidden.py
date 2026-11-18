"""Stand-in for <hidden-tests>/run.sh with the same output format (no docker).

    python fake_hidden.py <checkout> <preset> <ticket>

PASS when the fake orchestrator left its marker file (.eval-fake-fix) in the
checkout, FAIL otherwise. FAKE_HIDDEN_EXIT=2 simulates a build error.
Standalone on purpose: run as a script, imports nothing from the package.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

MARKER = ".eval-fake-fix"


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: fake_hidden.py <checkout> <preset> <ticket>", file=sys.stderr)
        return 2
    checkout, preset, ticket = Path(argv[0]), argv[1], argv[2]
    forced = os.environ.get("FAKE_HIDDEN_EXIT")
    if forced == "2":
        print("[hidden] build failed", file=sys.stderr)
        return 2
    name = "test_fake_" + ticket.lower().replace("-", "_")
    print(f"[hidden] preset {preset}, tickets: {ticket}, tests: {name}")
    if (checkout / MARKER).is_file():
        print(f"PASS  {ticket:<8} {name:<34} 0.01 sec")
        print("[hidden] PASS (1 of 1)")
        return 0
    print(f"FAIL  {ticket:<8} {name:<34} 0.01 sec")
    print("---- ctest output ----\n(fake)\n[hidden] FAIL (1 of 1)")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
