#!/bin/bash
# The workshop's branches as local branches of this clone:
#   ticket/TS-1xx   one per ticket: only that ticket's defect is open (the others are fixed). An experiment starts
#                 every run of a ticket here, and the held-back tests in hidden-tests/ judge the result.
#   after-section-1 .. after-section-5   the harness and the fixes after each section, for the final demo (the release run on after-section-5).
# A clone from GitHub has them as origin/...; the experiments and the release run need them as local branches.
# Run it in the repository root (setup.sh runs it too). Safe to run again: it moves each branch to the published one.
set -uo pipefail
git rev-parse --git-dir >/dev/null 2>&1 || { echo "not a git clone: clone the repository (a Download ZIP has no branches)"; exit 1; }
git fetch -q origin '+refs/heads/ticket/*:refs/remotes/origin/ticket/*' '+refs/heads/after-section-*:refs/remotes/origin/after-section-*' 2>/dev/null \
    || echo "branches: could not fetch from origin, using what this clone has"
n=0; kept=""
for r in $(git for-each-ref --format='%(refname:strip=3)' refs/remotes/origin/ | grep -E '^(ticket/TS-[0-9]+|after-section-[1-5])$' | sort -u); do
    if git branch -f "$r" "origin/$r" >/dev/null 2>&1; then n=$((n + 1)); else kept="$kept $r"; fi
done
echo "branches: $n local (ticket/TS-1xx, after-section-1 .. after-section-5)${kept:+; left as they are (checked out somewhere):$kept}"
[ "$n" -gt 0 ] || [ -n "$kept" ] || { echo "branches: none found: is origin the workshop repository?"; exit 1; }
