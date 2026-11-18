#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# check_config.sh -- verify the Copilot config layer without sending a prompt:
#   1. JSON files parse (hooks, MCP, repo settings)
#   2. guardrail hook regression cases (bash/Python variant; --ps also runs the
#      PowerShell variant, which needs powershell on the host)
#   3. the pinned Copilot CLI discovers the hooks, agents, skills, MCP servers
#      and instructions with no errors (discovery RPCs only, no model call)
# Runs in the dev container (scripts/dockerbuild.sh); usage from the repo root:
#   MSYS_NO_PATHCONV=1 .github/config-tests/check_config.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
if [ "${TS_IN_CONTAINER:-0}" != "1" ] && [ ! -f /.dockerenv ]; then
    cd "$REPO" && exec "$REPO/scripts/dockerbuild.sh" .github/config-tests/check_config.sh "$@"
fi
cd "$REPO" || exit 2
rc=0
for f in .github/hooks/guardrails.json .github/mcp.json .github/copilot/settings.json .github/config-tests/cases.json; do
    if python3 -c 'import json, sys; json.load(open(sys.argv[1], encoding="utf-8"))' "$f"; then
        echo "ok   json         $f"
    else
        echo "FAIL json         $f"; rc=1
    fi
done
out=$(python3 .github/config-tests/run_cases.py --sh) || { rc=1; echo "$out" | grep -v '^ok '; }
echo "$out" | tail -1
/opt/venv/bin/python .github/config-tests/discover.py || rc=1
exit $rc
