#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Ask the pinned Copilot CLI what it loads from this repository, without a prompt.

    scripts/dockerbuild.sh python3 .github/config-tests/discover.py

Starts the CLI through the Copilot SDK (COPILOT_CLI_PATH, as in the dev image)
with a throw-away HOME whose config trusts the repository, and calls only the
discovery RPCs (hooks, agents, skills, MCP servers, instructions). No session
is created, no model is called, no credentials are needed. Exit 0 when every
expected item is discovered and the CLI reports no errors or warnings.
"""
import asyncio
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
EXPECT = {
    "hooks": {("preToolUse", ".github/hooks/guardrails.json"), ("postToolUse", ".github/hooks/guardrails.json")},
    "agents": {"planner", "implementer", "test-writer", "reviewer"},
    "skills": {"build-and-test", "run-gates", "protocol"},
    "mcp": {"github", "atlassian"},
    "instructions": {".github/copilot-instructions.md", "AGENTS.md", ".github/instructions/c.instructions.md"},
}


async def discover():
    from copilot import CopilotClient
    from copilot.generated import rpc as R
    client = CopilotClient()
    await client.start()
    try:
        return {
            "hooks": (await client.rpc.hooks.discover(R.HooksDiscoverRequest(project_paths=[REPO]))).to_dict(),
            "agents": (await client.rpc.agents.discover(R.AgentsDiscoverRequest(project_paths=[REPO]))).to_dict(),
            "skills": (await client.rpc.skills.discover(R.SkillsDiscoverRequest(project_paths=[REPO]))).to_dict(),
            "mcp": (await client.rpc.mcp.discover(R.MCPDiscoverRequest(working_directory=REPO))).to_dict(),
            "instructions": (await client.rpc.instructions.discover(
                R.InstructionsDiscoverRequest(project_paths=[REPO]))).to_dict(),
        }
    finally:
        await client.stop()


def main():
    home = tempfile.mkdtemp(prefix="copilot-home-")
    os.makedirs(os.path.join(home, ".copilot"))
    with open(os.path.join(home, ".copilot", "config.json"), "w", encoding="utf-8") as f:
        json.dump({"trustedFolders": [REPO]}, f)
    os.environ["HOME"] = home
    os.environ.setdefault("COPILOT_AUTO_UPDATE", "false")
    d = asyncio.run(discover())
    got = {
        "hooks": {(h["hookType"], h.get("source", "")) for h in d["hooks"].get("hooks", []) if h.get("enabled")},
        "agents": {a["name"] for a in d["agents"].get("agents", []) if a.get("source") == "project"},
        "skills": {s["name"] for s in d["skills"].get("skills", []) if s.get("source") == "project" and s.get("enabled")},
        "mcp": {m["name"] for m in d["mcp"].get("servers", []) if m.get("source") == "workspace" and m.get("enabled")},
        "instructions": {i.get("sourcePath", "") for i in d["instructions"].get("sources", [])},
    }
    rc = 0
    for kind, want in EXPECT.items():
        miss = want - got[kind]
        print("%-4s %-12s %s" % ("ok" if not miss else "FAIL", kind,
                                 ", ".join(sorted(str(x[0]) if isinstance(x, tuple) else x for x in got[kind]))
                                 + ("  MISSING: %s" % sorted(miss) if miss else "")))
        rc |= 1 if miss else 0
    for a in d["agents"].get("agents", []):
        if a.get("source") == "project":
            print("     agent %-12s tools: %s" % (a["name"], ", ".join(a.get("tools") or ["(all)"])))
    problems = d["hooks"].get("errors", []) + d["hooks"].get("warnings", []) + d["skills"].get("errors", [])
    for p in problems:
        print("FAIL CLI reported: %s" % p)
        rc = 1
    print("---- copilot discovery: %s" % ("PASS" if rc == 0 else "FAIL"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
