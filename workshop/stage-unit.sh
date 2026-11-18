#!/bin/bash
# The stage. Puts the console's own copy of this repository on ticket/TS-104, the tree where only TS-104 is open: the
# start of the live run of section 1. On main every ticket is open, so the full ladder is red before the agent starts
# and the run ends "escalated" whatever the agent does. It also removes ticket versions saved in a rehearsal, so the
# right pane of TS-104 is empty again, and makes that tree the console's start point ("Changes since start").
#
# The copy is where the whole stage runs: the console's Today runs its commands there and the terminals work there
# (source workshop/stage-shell.sh). So this also gives the copy the workshop's branches as local branches, as
# branches.sh does for this clone: ticket/TS-1xx (the experiments start every run there) and after-section-1 .. after-section-5 (the
# final demo). Their source is this clone's own branches (run bash workshop/branches.sh first).
#
# Run it in the repository root with the console stopped:   bash workshop/stage-unit.sh
# Back to main (every ticket open):                         bash workshop/stage-unit.sh --undo
# A console on another port than 8765:                      CONSOLE_PORT=8771 bash workshop/stage-unit.sh
set -euo pipefail
U=.console-data/units/unit-01/repo
S=.console-data/source
PORT="${CONSOLE_PORT:-8765}"
[ -d "$U" ] || { echo "no console copy yet: start the console once (bash setup.sh), stop it, run this again"; exit 1; }
if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then echo "the console is running on port $PORT: stop it first"; exit 1; fi
[ "$(git -C "$U" rev-parse --abbrev-ref HEAD)" = "unit/01" ] || { echo "the console copy is not on branch unit/01: not touching it"; exit 1; }
REF=ticket/TS-104; [ "${1:-}" = "--undo" ] && REF=main
git rev-parse -q --verify "refs/heads/$REF" >/dev/null || { echo "no branch $REF in this clone: run bash workshop/branches.sh first"; exit 1; }
git -C "$S" fetch -q origin "+refs/heads/$REF:refs/remotes/origin/$REF"
T="$(git -C "$S" rev-parse "refs/remotes/origin/$REF")"
git -C "$U" reset -q --hard "$T"
git -C "$U" clean -fdq tickets
if [ "$(git -C "$S" rev-parse --abbrev-ref HEAD)" = "main" ]; then git -C "$S" reset -q --hard "$T"
else echo "WARNING: the console's clone is not on main: 'Changes since start' will list the difference to main"; fi
echo "console copy on $REF: $(git -C "$U" log --oneline -1)"

# the workshop's branches in the copy (its clone holds the branches; the copy is a worktree of it): from this clone's
git -C "$S" fetch -q origin '+refs/heads/ticket/*:refs/remotes/origin/ticket/*' '+refs/heads/after-section-*:refs/remotes/origin/after-section-*' \
    || echo "WARNING: could not fetch the ticket and after-section branches into the console's clone"
n=0; kept=""
for r in $(git -C "$S" for-each-ref --format='%(refname:strip=3)' refs/remotes/origin/ | grep -E '^(ticket/TS-[0-9]+|after-section-[1-5])$' | sort -u); do
    if git -C "$S" branch -f --no-track "$r" "origin/$r" >/dev/null 2>&1; then n=$((n + 1)); else kept="$kept $r"; fi
done
echo "console copy: $n workshop branches (ticket/TS-1xx, after-section-1 .. after-section-5)${kept:+; left as they are (checked out somewhere):$kept}"
[ "$n" -gt 0 ] || [ -n "$kept" ] || echo "WARNING: no ticket branches in the console's copy: an experiment started there cannot run (bash workshop/branches.sh, then this again)"

[ "$REF" = main ] || echo "check: source workshop/stage-shell.sh && python -m harness gates ladder   (expect unit-tests, sanitizers and loopback red: the three faces of TS-104)"
