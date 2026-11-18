#!/bin/bash
# Removes what the experiments of one day left behind, so that the same commands start from zero again: after a
# rehearsal (the day's commands carry the day's date in their experiment names, and an experiment skips runs it
# has already done) or after the workshop.
#
#   bash "$WORKSHOP/clean-day.sh" 20261120          the result files, eval branches and worktrees of that day
#   bash "$WORKSHOP/clean-day.sh" 20261120 --dry    only list them
# Run it where the experiments ran: on the stage that is the console's copy (source workshop/stage-shell.sh); in a
# clone without the console, its root. It never touches main, the ticket branches or after-section-1 .. after-section-5.
set -uo pipefail
DAY="${1:-}"; DRY="${2:-}"
case "$DAY" in [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;; *) echo "usage: bash \"\$WORKSHOP/clean-day.sh\" YYYYMMDD [--dry]"; exit 2 ;; esac
[ -f harness/eval/cli.py ] || { echo "run this where the experiments ran: the console's copy (source workshop/stage-shell.sh)"; exit 2; }
WT="../$(basename "$(pwd)")-eval/worktrees"
files=$(ls results/*-"$DAY"*.jsonl 2>/dev/null; ls -d results/*-"$DAY" 2>/dev/null)
branches=$(git for-each-ref --format='%(refname:short)' "refs/heads/eval/" | grep -- "-$DAY/" || true)
dirs=$(ls -d "$WT"/*-"$DAY" 2>/dev/null || true)
echo "results:  $(echo $files | wc -w)   eval branches: $(echo $branches | wc -w)   worktree folders: $(echo $dirs | wc -w)"
[ "$DRY" = "--dry" ] && { printf '%s\n' $files $branches $dirs; exit 0; }
for d in $dirs; do for w in "$d"/*; do [ -d "$w" ] && git worktree remove --force "$w" >/dev/null 2>&1; done; rm -rf "$d"; done
git worktree prune
for b in $branches; do git branch -D "$b" >/dev/null 2>&1; done
rm -rf $files
python -m harness experiment leaderboard results/*.jsonl >/dev/null 2>&1 || true   # rebuilds results/results.json from what is left
echo "clean: the experiments of $DAY are gone"
