"""overlay_from: a variant takes its harness structure from another directory (a unit's worktree).

The overlay is applied after the eval worktree is made from its seed and before the run starts, and it is
committed, so the agent starts on a clean tree and the gates (diff-policy, compliance) see only the agent's change.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from harness import api
from harness.eval import results, runner, variants
from harness.eval import worktree as wt

OVERLAY_FILES = {
    "AGENTS.md": "# unit rules\n",
    ".github/agents/planner.agent.md": "planner\n",
    ".github/skills/triage/SKILL.md": "skill\n",
    ".github/skills/triage/config.yml": "triage: true\n",
    ".github/skills/triage/NOTES.MD": "the extension is matched without regard to case\n",
    ".github/instructions/c.instructions.md": "c rules\n",
    ".github/copilot-instructions.md": "copilot\n",
    ".github/hooks/guard.json": "{}\n",
    "harness/harness.yaml": "version: 1\n",
    "harness/gates.yaml": "gates: {}\n",
    "harness/workflows/jira-to-pr.yaml": "name: jira-to-pr\n",
    "harness/prompts/top.md": "top prompt\n",
    "harness/prompts/v1/planner.md": "prompt\n",
}
# under the overlay paths, but not text the harness reads (.md .yaml .yml .json): skipped, and listed in the record
SKIPPED_FILES = {
    ".github/skills/triage/scripts/run.sh": "#!/bin/sh\necho triage\n",
    ".github/skills/triage/scripts/run.py": "print('triage')\n",
    ".github/skills/triage/bin/tool.exe": "MZ\n",
    ".github/skills/triage/LICENSE": "no extension\n",
    ".github/skills/triage/tricky.md.sh": "echo not markdown\n",
    ".github/agents/helper.js": "console.log(1)\n",
    ".github/instructions/setup.bat": "echo setup\n",
    "harness/prompts/notes.txt": "a text file, but not one the harness reads\n",
    "harness/prompts/v1/fetch.ps1": "Write-Host fetch\n",
}
# in the overlay source, but outside the overlay paths: never copied
NOT_OVERLAY = {
    "README.md": "unit readme\n",
    "harness/orchestrator.py": "print('edited')\n",
    "harness/prompts-extra.md": "not under prompts/\n",
    ".github/hooks/notes.txt": "only hooks/*.json\n",
    ".github/hooks/guard.sh": "only hooks/*.json\n",
    ".github/hooks/sub/deep.json": "{}\n",
    ".github/workflows/ci.yml": "name: ci\n",
    ".github/mcp.json": "{}\n",
    "scripts/check.sh": "echo weakened\n",
    "src/b.c": "int b;\n",
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _commit_on(repo: Path, branch: str, files: dict[str, str]) -> None:
    """Add files to a ticket branch (the fixture repo sits on main)."""
    _git(repo, "checkout", "-q", branch)
    _write_tree(repo, files)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed harness files")
    _git(repo, "checkout", "-q", "main")


def _worktree(repo: Path, tmp_path: Path, ticket: str = "EV-1") -> tuple[Path, str]:
    path = tmp_path / "wt" / "x"
    return path, wt.create_worktree(repo, path, "eval/x", f"ticket/{ticket}")


def _symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")


# ------------------------------------------------------------- apply_overlay --

def test_overlay_copies_the_overlay_paths_and_commits_once(repo, tmp_path):
    src = _write_tree(tmp_path / "unit-7", {**OVERLAY_FILES, **NOT_OVERLAY, "src/a.c": "int a; /* edited */\n"})
    work, base = _worktree(repo, tmp_path)
    sha = wt.apply_overlay(work, src)
    for rel, text in OVERLAY_FILES.items():
        assert (work / rel).read_bytes() == text.encode(), rel
    for rel in NOT_OVERLAY:
        assert not (work / rel).exists(), rel
    assert "edited" not in (work / "src" / "a.c").read_text(encoding="utf-8")      # not an overlay path
    # one extra commit with the overlay name in the message, the repo identity, a clean tree
    assert sha == wt.head(work) and wt.commits_since(work, base) == 1
    assert _git(work, "log", "-1", "--format=%s") == "eval: harness overlay from unit-7"
    assert _git(work, "log", "-1", "--format=%an <%ae>") == "Eval Test <eval-test@example.invalid>"
    assert _git(work, "status", "--porcelain") == ""
    assert sorted(_git(work, "show", "--name-only", "--format=", "HEAD").splitlines()) == sorted(OVERLAY_FILES)


def test_overlay_copies_only_text_the_harness_reads_and_lists_the_rest(repo, tmp_path):
    src = _write_tree(tmp_path / "unit-7", {**OVERLAY_FILES, **SKIPPED_FILES, **NOT_OVERLAY})
    work, base = _worktree(repo, tmp_path)
    skipped: list[str] = []
    assert wt.apply_overlay(work, src, skipped)
    for rel in SKIPPED_FILES:
        assert not os.path.lexists(work / rel), rel
    assert sorted(_git(work, "show", "--name-only", "--format=", "HEAD").splitlines()) == sorted(OVERLAY_FILES)
    # the skipped files are named, sorted, as paths relative to the source; what lies outside the overlay
    # paths (scripts/check.sh, .github/hooks/guard.sh next to hooks/*.json) is no overlay file to skip
    assert skipped == sorted(SKIPPED_FILES)
    assert _git(work, "status", "--porcelain") == ""


@pytest.mark.parametrize("name, copied", [
    ("a.md", True), ("a.yaml", True), ("a.yml", True), ("a.json", True), ("A.MD", True), ("a.Json", True),
    ("a.txt", False), ("a.sh", False), ("a", False), (".md", False), ("a.md.sh", False), ("a.json.bak", False),
    ("a.mdx", False), ("a.yaml~", False), ("a.md.exe", False),
])
def test_overlay_extension_rule(repo, tmp_path, name, copied):
    rel = f"harness/prompts/{name}"
    src = _write_tree(tmp_path / "unit-7", {rel: "content\n", "AGENTS.md": "# rules\n"})
    work, base = _worktree(repo, tmp_path)
    skipped: list[str] = []
    wt.apply_overlay(work, src, skipped)
    assert (work / rel).exists() is copied
    assert skipped == ([] if copied else [rel])


def test_overlay_that_copies_nothing_still_lists_what_it_skipped(repo, tmp_path):
    src = _write_tree(tmp_path / "unit-7", {".github/skills/x/run.sh": "echo\n", "harness/prompts/a.txt": "a\n"})
    work, base = _worktree(repo, tmp_path)
    skipped: list[str] = []
    assert wt.apply_overlay(work, src, skipped) is None
    assert skipped == [".github/skills/x/run.sh", "harness/prompts/a.txt"]
    assert wt.head(work) == base and _git(work, "status", "--porcelain") == ""


def test_overlay_skipped_list_is_capped_at_fifty_names(repo, tmp_path):
    assert wt.OVERLAY_SKIPPED_MAX == 50
    names = [f"harness/prompts/s{i:03d}.sh" for i in range(60)]
    src = _write_tree(tmp_path / "unit-7", {**{n: "echo\n" for n in names}, "AGENTS.md": "# rules\n"})
    work, base = _worktree(repo, tmp_path)
    skipped: list[str] = []
    assert wt.apply_overlay(work, src, skipped)
    assert skipped == names[:50]                                    # the first fifty, in order
    assert not (work / "harness" / "prompts").exists()


def test_overlay_counts_what_it_does_not_name(repo, tmp_path):
    names = [f"harness/prompts/s{i:03d}.sh" for i in range(120)]
    src = _write_tree(tmp_path / "unit-7", {**{n: "echo\n" for n in names}, "AGENTS.md": "# rules\n"})
    work, base = _worktree(repo, tmp_path)
    skipped = wt.SkippedNames()
    assert wt.apply_overlay(work, src, skipped)
    assert skipped == names[:50] and skipped.total == 120           # fifty names, and all of them counted
    plain: list[str] = []                                           # a plain list still gets the names
    assert wt.apply_overlay(work, src, plain) is None
    assert plain == names[:50]


def test_the_skipped_total_is_the_number_of_files_left_out(repo, tmp_path):
    src = _write_tree(tmp_path / "unit-7", {**OVERLAY_FILES, **SKIPPED_FILES, **NOT_OVERLAY})
    work, base = _worktree(repo, tmp_path)
    skipped = wt.SkippedNames()
    assert wt.apply_overlay(work, src, skipped)
    assert skipped == sorted(SKIPPED_FILES) and skipped.total == len(SKIPPED_FILES)
    only_text = _write_tree(tmp_path / "unit-8", OVERLAY_FILES)
    work2 = tmp_path / "wt" / "y"
    wt.create_worktree(repo, work2, "eval/y", "ticket/EV-1")
    none = wt.SkippedNames()
    assert wt.apply_overlay(work2, only_text, none) and none == [] and none.total == 0
    nothing = _write_tree(tmp_path / "unit-9", {".github/skills/x/run.sh": "echo\n", "harness/prompts/a.txt": "a\n"})
    work3 = tmp_path / "wt" / "z"
    wt.create_worktree(repo, work3, "eval/z", "ticket/EV-1")
    left = wt.SkippedNames()
    assert wt.apply_overlay(work3, nothing, left) is None and len(left) == left.total == 2


def test_the_total_adds_up_over_several_overlays_like_the_names_do(repo, tmp_path):
    first = _write_tree(tmp_path / "unit-1", {"AGENTS.md": "# a\n", "harness/prompts/a.sh": "echo\n",
                                              "harness/prompts/b.sh": "echo\n", "harness/prompts/c.sh": "echo\n"})
    second = _write_tree(tmp_path / "unit-2", {"harness/prompts/d.md": "d\n", "harness/prompts/e.sh": "echo\n"})
    work, base = _worktree(repo, tmp_path)
    skipped = wt.SkippedNames()
    wt.apply_overlay(work, first, skipped)
    assert skipped.total == 3 == len(skipped)
    wt.apply_overlay(work, second, skipped)                          # the same object again: 1 more
    assert skipped.total == 4 == len(skipped)
    assert skipped == ["harness/prompts/a.sh", "harness/prompts/b.sh", "harness/prompts/c.sh",
                       "harness/prompts/e.sh"]
    assert wt.SkippedNames().total == 0                              # a fresh one starts at zero, not shared


def test_an_overlay_refused_by_the_scan_counts_nothing(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(wt, "OVERLAY_MAX_FILES", 1)
    src = _write_tree(tmp_path / "unit-7", {"AGENTS.md": "# rules\n", "harness/prompts/a.md": "a\n",
                                            "harness/prompts/b.sh": "echo\n"})
    work, base = _worktree(repo, tmp_path)
    skipped = wt.SkippedNames()
    with pytest.raises(wt.OverlayError, match="more than 1 files"):
        wt.apply_overlay(work, src, skipped)
    assert skipped == [] and skipped.total == 0


def test_the_scan_keeps_the_first_names_in_sorted_order_and_counts_all(tmp_path, monkeypatch):
    monkeypatch.setattr(wt, "OVERLAY_SKIPPED_MAX", 5)
    names = [f"harness/prompts/s{i:02d}.sh" for i in range(12)]
    src = _write_tree(tmp_path / "unit-7", {n: "echo\n" for n in names})
    files, kept, total = wt._overlay_files(src)
    assert files == [] and kept == names[:5] and total == 12


def test_first_names_hold_at_most_the_limit_however_many_are_added():
    first = wt._FirstNames(5)
    added = [f"n{i:04d}" for i in range(1000)][::-1]                # the smallest names come last
    for name in added:
        first.add(name)
        assert len(first.names) <= 5
    assert first.names == sorted(added)[:5] and first.total == 1000
    few = wt._FirstNames(5)
    for name in ("b", "a"):
        few.add(name)
    assert few.names == ["a", "b"] and few.total == 2
    none = wt._FirstNames(0)
    none.add("a")
    assert none.names == [] and none.total == 1


def test_skipped_files_do_not_count_against_the_overlay_caps(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(wt, "OVERLAY_MAX_FILES", 3)
    monkeypatch.setattr(wt, "OVERLAY_MAX_BYTES", 100)
    files = {f"harness/prompts/s{i}.sh": "x" * 500 for i in range(10)}      # 10 files, 5000 bytes: not copied
    src = _write_tree(tmp_path / "unit-7", {**files, "AGENTS.md": "# rules\n"})
    work, base = _worktree(repo, tmp_path)
    skipped: list[str] = []
    assert wt.apply_overlay(work, src, skipped)                     # no OverlayError
    assert len(skipped) == 10 and not (work / "harness").exists()


def test_overlay_skips_by_extension_before_looking_at_the_file(repo, tmp_path):
    outside = _write_tree(tmp_path / "outside", {"x.sh": "echo\n"})
    src = _write_tree(tmp_path / "unit-7", {"AGENTS.md": "# rules\n", "harness/prompts/b.sh": "echo b\n"})
    _symlink(src / "harness" / "prompts" / "a.sh", outside / "x.sh")       # not a regular file, and not text
    work, base = _worktree(repo, tmp_path)
    skipped: list[str] = []
    assert wt.apply_overlay(work, src, skipped)
    assert skipped == ["harness/prompts/a.sh", "harness/prompts/b.sh"]     # listed, never followed or copied
    assert not os.path.lexists(work / "harness" / "prompts" / "a.sh")


def test_overlay_skipped_is_optional(repo, tmp_path):
    src = _write_tree(tmp_path / "unit-7", {"AGENTS.md": "# rules\n", "harness/prompts/a.sh": "echo\n"})
    work, base = _worktree(repo, tmp_path)
    assert wt.apply_overlay(work, src)                              # a caller that does not ask gets no list
    assert not (work / "harness" / "prompts" / "a.sh").exists()


def test_overlay_adds_and_replaces_but_never_deletes(repo, tmp_path):
    _commit_on(repo, "ticket/EV-1", {"AGENTS.md": "# seed rules\n", "harness/prompts/old.md": "old\n",
                                    ".github/agents/keep.agent.md": "keep\n"})
    src = _write_tree(tmp_path / "unit-7", {"AGENTS.md": "# unit rules\n", "harness/prompts/new.md": "new\n"})
    work, base = _worktree(repo, tmp_path)
    assert wt.apply_overlay(work, src)
    assert (work / "AGENTS.md").read_text(encoding="utf-8") == "# unit rules\n"       # replaced
    assert (work / "harness" / "prompts" / "new.md").is_file()                        # added
    assert (work / "harness" / "prompts" / "old.md").read_text(encoding="utf-8") == "old\n"       # left alone
    assert (work / ".github" / "agents" / "keep.agent.md").is_file()
    assert sorted(_git(work, "diff", "--name-only", base, "HEAD").splitlines()) == ["AGENTS.md",
                                                                                      "harness/prompts/new.md"]


@pytest.mark.parametrize("seeded", [False, True], ids=["empty source", "source equals the seed"])
def test_overlay_that_changes_nothing_makes_no_commit(repo, tmp_path, seeded):
    files = {"AGENTS.md": "# seed rules\n", "harness/gates.yaml": "gates: {}\n"}
    if seeded:
        _commit_on(repo, "ticket/EV-1", files)
    src = _write_tree(tmp_path / "unit-7", files if seeded else {})
    work, base = _worktree(repo, tmp_path)
    assert wt.apply_overlay(work, src) is None
    assert wt.head(work) == base and _git(work, "status", "--porcelain") == ""


def test_overlay_twice_is_idempotent(repo, tmp_path):
    src = _write_tree(tmp_path / "unit-7", OVERLAY_FILES)
    work, base = _worktree(repo, tmp_path)
    assert wt.apply_overlay(work, src)
    assert wt.apply_overlay(work, src) is None
    assert wt.commits_since(work, base) == 1


def test_overlay_source_must_be_a_directory(repo, tmp_path):
    work, base = _worktree(repo, tmp_path)
    with pytest.raises(wt.OverlayError, match="not a directory"):
        wt.apply_overlay(work, tmp_path / "missing")
    (tmp_path / "file").write_text("x", encoding="utf-8")
    with pytest.raises(wt.OverlayError, match="not a directory"):
        wt.apply_overlay(work, tmp_path / "file")
    assert wt.head(work) == base


def test_overlay_skips_symlinks(repo, tmp_path):
    outside = _write_tree(tmp_path / "outside", {"secret.md": "secret\n", "tree/more.md": "more\n"})
    src = _write_tree(tmp_path / "unit-7", {"AGENTS.md": "# unit rules\n"})
    _symlink(src / ".github" / "agents" / "link.agent.md", outside / "secret.md")       # a linked file
    _symlink(src / ".github" / "skills" / "evil", outside / "tree")                      # a linked directory
    _symlink(src / ".github" / "copilot-instructions.md", outside / "secret.md")        # a linked literal path
    work, base = _worktree(repo, tmp_path)
    assert wt.apply_overlay(work, src)
    assert (work / "AGENTS.md").is_file()
    for rel in (".github/agents/link.agent.md", ".github/skills/evil", ".github/copilot-instructions.md"):
        assert not os.path.lexists(work / rel), rel
    assert _git(work, "show", "--name-only", "--format=", "HEAD").splitlines() == ["AGENTS.md"]


def test_overlay_refuses_a_path_that_escapes_the_source(repo, tmp_path):
    outside = _write_tree(tmp_path / "outside", {"p.md": "from outside\n"})
    src = _write_tree(tmp_path / "unit-7", {"AGENTS.md": "# unit rules\n"})
    _symlink(src / "harness" / "prompts", outside)                  # an overlay root that leaves the source
    work, base = _worktree(repo, tmp_path)
    with pytest.raises(wt.OverlayError, match="escapes"):
        wt.apply_overlay(work, src)
    # refused before anything was copied
    assert wt.head(work) == base and _git(work, "status", "--porcelain") == ""
    assert not (work / "AGENTS.md").exists()


def test_overlay_leaves_git_entries_behind(repo, tmp_path):
    src = _write_tree(tmp_path / "unit-7", {"AGENTS.md": "# unit rules\n",
                                            ".github/skills/foo/SKILL.md": "foo skill\n",
                                            ".github/skills/bar/SKILL.md": "bar skill\n",
                                            ".github/skills/baz/SKILL.md": "baz skill\n",
                                            ".github/skills/baz/.GIT/config": "[core]\n",
                                            ".github/agents/a.agent.md": "a\n",
                                            ".github/agents/.git": "gitdir: /participant/worktree\n"})
    subprocess.run(["git", "init", "-q", str(src / ".github" / "skills" / "foo")], check=True)   # a nested repository
    (src / ".github" / "skills" / "bar" / ".git").write_text("gitdir: /somewhere/else\n", encoding="utf-8")
    work, base = _worktree(repo, tmp_path)
    assert wt.apply_overlay(work, src)
    # the files next to a .git entry are overlay files like any other; the .git entries are not
    wanted = [".github/agents/a.agent.md", ".github/skills/bar/SKILL.md", ".github/skills/baz/SKILL.md",
              ".github/skills/foo/SKILL.md", "AGENTS.md"]
    for rel in wanted:
        assert (work / rel).is_file(), rel
    assert [p for p in work.rglob("*") if p.name.lower() == ".git" and p.parent != work] == []
    assert _git(work, "status", "--porcelain") == ""
    assert sorted(_git(work, "show", "--name-only", "--format=", "HEAD").splitlines()) == wanted
    assert " commit " not in _git(work, "ls-tree", "-r", "HEAD")        # no gitlink (mode 160000 is "commit")


def test_overlay_fails_when_it_leaves_the_worktree_dirty(repo, tmp_path, monkeypatch):
    src = _write_tree(tmp_path / "unit-7", {"AGENTS.md": "# unit rules\n"})
    work, base = _worktree(repo, tmp_path)
    real_git = wt.git

    def commit_then_dirty(cwd, *args, **kw):
        out = real_git(cwd, *args, **kw)
        if "commit" in args:
            (Path(cwd) / "stray.txt").write_text("left behind\n", encoding="utf-8")
        return out
    monkeypatch.setattr(wt, "git", commit_then_dirty)
    with pytest.raises(wt.OverlayError, match=r"not clean.*stray\.txt"):
        wt.apply_overlay(work, src)


def test_overlay_refuses_hard_links(repo, tmp_path):
    src = _write_tree(tmp_path / "unit-7", {"AGENTS.md": "# unit rules\n", "harness/prompts/a.md": "a\n"})
    try:
        os.link(src / "harness" / "prompts" / "a.md", tmp_path / "elsewhere.md")
    except (OSError, NotImplementedError):
        pytest.skip("hard links are not available here")
    work, base = _worktree(repo, tmp_path)
    with pytest.raises(wt.OverlayError, match=r"hard link.*harness/prompts/a\.md"):
        wt.apply_overlay(work, src)
    assert wt.head(work) == base and not (work / "AGENTS.md").exists()


def test_overlay_is_capped_in_files_and_bytes(repo, tmp_path, monkeypatch):
    assert (wt.OVERLAY_MAX_FILES, wt.OVERLAY_MAX_BYTES) == (500, 5 * 1024 * 1024)
    work, base = _worktree(repo, tmp_path)
    many = _write_tree(tmp_path / "many", {f"harness/prompts/p{i}.md": "x\n" for i in range(4)})
    monkeypatch.setattr(wt, "OVERLAY_MAX_FILES", 3)
    with pytest.raises(wt.OverlayError, match="more than 3 files"):
        wt.apply_overlay(work, many)
    big = _write_tree(tmp_path / "big", {"AGENTS.md": "x" * 60, "harness/prompts/a.md": "y" * 60})
    monkeypatch.setattr(wt, "OVERLAY_MAX_BYTES", 100)
    with pytest.raises(wt.OverlayError, match="limit of 100 bytes"):
        wt.apply_overlay(work, big)
    assert wt.head(work) == base and not (work / "harness").exists()    # the refused overlays copied nothing
    small = _write_tree(tmp_path / "small", {"AGENTS.md": "x" * 60})
    assert wt.apply_overlay(work, small)                                 # under both caps


def test_overlay_checks_each_file_again_right_before_copying_it(repo, tmp_path, monkeypatch):
    outside = _write_tree(tmp_path / "outside", {"secret.md": "secret\n"})
    src = _write_tree(tmp_path / "unit-7", {"AGENTS.md": "# unit rules\n", "harness/prompts/a.md": "a\n"})
    scan = wt._overlay_files

    def scan_then_swap(source):
        files = scan(source)
        (source / "harness" / "prompts" / "a.md").unlink()              # a file turns into a link after the scan
        _symlink(source / "harness" / "prompts" / "a.md", outside / "secret.md")
        return files
    monkeypatch.setattr(wt, "_overlay_files", scan_then_swap)
    work, base = _worktree(repo, tmp_path)
    with pytest.raises(wt.OverlayError, match=r"changed.*harness/prompts/a\.md"):
        wt.apply_overlay(work, src)
    assert not (work / "harness" / "prompts" / "a.md").exists()


def test_overlay_is_never_partial_when_a_directory_cannot_be_read(repo, tmp_path, monkeypatch):
    src = _write_tree(tmp_path / "unit-7", {"AGENTS.md": "# unit rules\n", "harness/prompts/a.md": "a\n"})

    def unreadable(top, topdown=True, onerror=None, followlinks=False):
        onerror(PermissionError(13, "Permission denied", str(top)))
        return iter(())
    monkeypatch.setattr(os, "walk", unreadable)
    work, base = _worktree(repo, tmp_path)
    with pytest.raises(wt.OverlayError, match="cannot read the overlay source.*Permission denied"):
        wt.apply_overlay(work, src)
    assert wt.head(work) == base and not (work / "AGENTS.md").exists()


# ------------------------------------------------------------------ variants --

def _matrix(tmp_path: Path, body: str, name: str = "variants.yaml") -> variants.Matrix:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("version: 1\ndefaults:\n  roles: {implementer: true}\n  models: {implementer: m}\n"
                 "variants:\n" + textwrap.indent(textwrap.dedent(body), "  "), encoding="utf-8")
    return variants.load_matrix(p)


def test_overlay_from_is_kept_as_given_and_resolved_against_the_variants_file(tmp_path):
    unit = tmp_path / "units" / "u1"
    unit.mkdir(parents=True)
    m = _matrix(tmp_path / "cfg", f"""\
        A: {{label: plain}}
        ABS: {{overlay_from: "{unit.as_posix()}"}}
        REL: {{overlay_from: ../units/u1}}
        """)
    assert (m.variants["A"].overlay_from, m.variants["A"].overlay_dir) == (None, None)
    assert (m.variants["ABS"].overlay_from, m.variants["ABS"].overlay_dir) == (unit.as_posix(), unit.resolve())
    assert (m.variants["REL"].overlay_from, m.variants["REL"].overlay_dir) == ("../units/u1", unit.resolve())


def test_overlay_from_is_inherited_and_can_be_cleared(tmp_path):
    (tmp_path / "u1").mkdir()
    m = _matrix(tmp_path, f"""\
        A: {{overlay_from: "{(tmp_path / 'u1').as_posix()}"}}
        B: {{extends: A, label: inherits}}
        C: {{extends: A, overlay_from: null}}
        """)
    assert m.variants["B"].overlay_dir == (tmp_path / "u1").resolve()
    assert m.variants["C"].overlay_from is None and m.variants["C"].overlay_dir is None


@pytest.mark.parametrize("value, msg", [
    ('"does-not-exist"', "is not an existing directory"),
    ('"a-file"', "is not an existing directory"),
    ("123", "must be a non-empty path string"),
    ("[a, b]", "must be a non-empty path string"),
    ('""', "must be a non-empty path string"),
], ids=["missing", "file", "number", "list", "empty"])
def test_overlay_from_must_be_an_existing_directory(tmp_path, value, msg):
    (tmp_path / "a-file").write_text("x", encoding="utf-8")
    with pytest.raises(variants.VariantError, match=rf"^team-small: overlay_from .*{msg}"):
        _matrix(tmp_path, f"team-small: {{overlay_from: {value}}}\n")


def test_the_overlaid_harness_config_is_what_the_run_is_told_to_load(tmp_path):
    (tmp_path / "u1").mkdir()
    m = _matrix(tmp_path, f"""\
        A: {{label: plain, extra: {{runtime_mode: facilitator}}}}
        O: {{extends: A, overlay_from: "{(tmp_path / 'u1').as_posix()}"}}
        """)
    work = tmp_path / "wt" / "O-EV-1-r0"
    s = m.variants["O"].run_settings(str(work), "/r")
    # the backend reads harness.yaml (and gates.yaml, workflows/, prompts/ next to it) from the worktree
    assert s.extra == {"runtime_mode": "facilitator", "config": str(work / "harness" / "harness.yaml"),
                       "applied_knobs": []}           # the variant sets no knob itself (and the file no cap)
    assert "overlay_from" not in s.to_dict()
    plain = m.variants["A"].run_settings(str(work), "/r")
    assert plain.extra == {"runtime_mode": "facilitator"}               # no overlay: the package's config, as before


def test_overlay_from_and_an_explicit_config_exclude_each_other(tmp_path):
    (tmp_path / "u1").mkdir()
    with pytest.raises(variants.VariantError, match=r"^team-small: overlay_from .*extra\.config"):
        _matrix(tmp_path, f'team-small: {{overlay_from: "{(tmp_path / "u1").as_posix()}", extra: {{config: other.yaml}}}}\n')


# ------------------------------------------------------------- applied knobs --

def _knob_matrix(tmp_path: Path, body: str) -> variants.Matrix:
    """A variants file whose defaults block sets every run setting: what an overlay must not inherit."""
    (tmp_path / "u1").mkdir(exist_ok=True)
    p = tmp_path / "variants.yaml"
    p.write_text("version: 1\n"
                 "defaults:\n"
                 "  roles: {implementer: true}\n"
                 "  models: {planner: m-plan, implementer: m-impl, reviewer: m-rev}\n"
                 "  retry_budget: 3\n"
                 "  custom_instructions: true\n"
                 "  prompt_template: v1\n"
                 "  autonomy: human\n"
                 "  gates: true\n"
                 "  max_ai_credits: 7\n"
                 "  timeout_s: 100\n"
                 "variants:\n" + textwrap.indent(textwrap.dedent(body), "  "), encoding="utf-8")
    return variants.load_matrix(p)


def test_an_overlay_variant_applies_only_the_knobs_it_sets_itself(tmp_path):
    unit = (tmp_path / "u1").as_posix()
    m = _knob_matrix(tmp_path, f"""\
        P: {{label: plain}}
        O: {{overlay_from: "{unit}"}}
        S: {{overlay_from: "{unit}", retry_budget: 2, gates: false, models: {{implementer: m-new}},
            roles: {{reviewer: false}}, max_ai_credits: 3}}
        """)
    assert m.variants["P"].applied_knobs is None                       # no overlay: every setting applies
    assert m.variants["O"].applied_knobs == ["max_ai_credits"]         # the cap always applies, nothing else
    assert m.variants["S"].applied_knobs == ["gates", "max_ai_credits", "models.implementer", "retry_budget",
                                             "roles.reviewer"]
    # the merged settings stay what they were; the backend is told which of them to leave out
    assert m.variants["O"].settings["retry_budget"] == 3 and m.variants["O"].settings["max_ai_credits"] == 7
    s = m.variants["O"].run_settings("/w", "/r")
    assert s.extra["applied_knobs"] == ["max_ai_credits"] and s.retry_budget == 3
    assert "applied_knobs" not in m.variants["P"].run_settings("/w", "/r").extra
    s.extra["applied_knobs"].append("x")
    assert m.variants["O"].applied_knobs == ["max_ai_credits"]         # a copy per run


def test_extends_counts_as_the_variants_own_but_defaults_do_not(tmp_path):
    unit = (tmp_path / "u1").as_posix()
    m = _knob_matrix(tmp_path, f"""\
        A: {{retry_budget: 1, roles: {{planner: true}}, models: {{planner: m-plan2}}}}
        O: {{extends: A, overlay_from: "{unit}", prompt_template: v2}}
        """)
    assert m.variants["O"].applied_knobs == ["max_ai_credits", "models.planner", "prompt_template", "retry_budget",
                                             "roles.planner"]
    assert m.variants["A"].applied_knobs is None


def test_the_model_of_a_role_the_variant_does_not_switch_still_applies(tmp_path):
    unit = (tmp_path / "u1").as_posix()
    m = _knob_matrix(tmp_path, f"""\
        O: {{overlay_from: "{unit}", models: {{planner: m-plan2}}}}
        SWITCHED: {{overlay_from: "{unit}", roles: {{planner: false}}, models: {{planner: m-plan2}}}}
        """)
    # whether the planner runs is the unit's workflow's call, so its model is the variant's to set ...
    assert m.variants["O"].applied_knobs == ["max_ai_credits", "models.planner"]
    assert m.variants["O"].settings["models"]["planner"] == "m-plan2"
    # ... but a role the variant itself switches off has no model to apply
    assert m.variants["SWITCHED"].applied_knobs == ["max_ai_credits", "roles.planner"]
    assert "planner" not in m.variants["SWITCHED"].settings["models"]


def test_an_overlay_variant_without_any_cap_applies_none(tmp_path):
    (tmp_path / "u1").mkdir()
    m = _matrix(tmp_path, f'O: {{overlay_from: "{(tmp_path / "u1").as_posix()}"}}\n')
    assert m.variants["O"].applied_knobs == []          # nothing set by the variant: the unit's file decides


def test_applied_knobs_is_not_for_a_variants_file_to_set(tmp_path):
    with pytest.raises(variants.VariantError, match=r"^team-small: extra\.applied_knobs"):
        _matrix(tmp_path, "team-small: {extra: {applied_knobs: [retry_budget]}}\n")


# -------------------------------------------------------------------- runner --

def _cfg(repo: Path, hidden: Path, matrix: variants.Matrix, tmp_path: Path, **kw) -> runner.EvalConfig:
    base = dict(repo=repo, hidden_dir=hidden, variants=matrix.select(None), tickets=["EV-1"],
                experiment="ov", results_dir=tmp_path / "results", worktree_root=tmp_path / "wt",
                gate_weights=matrix.gate_weights, parallel=1, repeats=1, golden_ref="golden",
                skip_hidden=True, log=lambda s: None)
    base.update(kw)
    return runner.EvalConfig(**base)


def _agent(seen: dict, commit: bool = False):
    """A backend that records what the agent starts on, then fixes src/a.c (and commits like the harness)."""
    def backend(workflow, ticket, settings, mode="eval"):
        work = Path(settings.workdir)
        seen[settings.variant] = {
            "status": _git(work, "status", "--porcelain"),
            "agents_md": (work / "AGENTS.md").is_file(),
            "ahead": int(_git(work, "rev-list", "--count", f"ticket/{ticket}..HEAD")),
            "head": _git(work, "rev-parse", "HEAD"),
            "config": settings.extra.get("config"),
            "knobs": settings.extra.get("applied_knobs"),
        }
        (work / "src" / "a.c").write_text("int a; /* fixed */\n", encoding="utf-8")
        if commit:
            _git(work, "checkout", "-q", "-b", f"agent/{ticket.lower()}-abcd")
            _git(work, "add", "src")
            _git(work, "commit", "-q", "-m", f"{ticket}: fix")
        return api.RunResult(status="completed", success=True, run_id="r1", ai_credits=0.1)
    return backend


@pytest.fixture
def unit_dir(tmp_path) -> Path:
    return _write_tree(tmp_path / "unit-7", {**OVERLAY_FILES, **SKIPPED_FILES, **NOT_OVERLAY})


def _two_variants(tmp_path: Path, unit: Path) -> variants.Matrix:
    return _matrix(tmp_path, f"""\
        A: {{label: plain}}
        O: {{label: overlaid, overlay_from: "{unit.as_posix()}"}}
        """)


@pytest.mark.parametrize("commit", [False, True])
def test_the_agent_starts_on_a_clean_tree_with_the_overlay_committed(repo, hidden, tmp_path, unit_dir, commit):
    m = _two_variants(tmp_path, unit_dir)
    seen: dict = {}
    recs = {r["variant"]: r for r in runner.Runner(_cfg(repo, hidden, m, tmp_path, backend=_agent(seen, commit))).run()}
    o, a = recs["O"], recs["A"]
    assert o["run_status"] == "done", o.get("error")
    # at the start: overlay file there, exactly one commit above the seed, nothing uncommitted
    assert seen["O"]["agents_md"] and seen["O"]["ahead"] == 1 and seen["O"]["status"] == ""
    assert not seen["A"]["agents_md"] and seen["A"]["ahead"] == 0
    seed = _git(repo, "rev-parse", "ticket/EV-1")
    assert o["overlay_from"] == unit_dir.as_posix() and o["overlay_commit"] == seen["O"]["head"] != seed
    assert o["seed_commit"] == seed and wt.commits_since(Path(o["worktree"]), o["overlay_commit"]) == (1 if commit else 0)
    assert (a["overlay_from"], a["overlay_commit"]) == (None, None)
    # what the overlay left out (not text the harness reads) is in the record; no overlay, nothing skipped
    assert o["overlay_skipped"] == sorted(SKIPPED_FILES) and a["overlay_skipped"] == []
    assert o["overlay_skipped_total"] == len(SKIPPED_FILES) and a["overlay_skipped_total"] == 0
    assert not any(os.path.lexists(Path(o["worktree"]) / rel) for rel in SKIPPED_FILES)
    # the run loads the overlaid harness.yaml of its own worktree; a variant without overlay keeps the package's
    assert seen["O"]["config"] == str(Path(o["worktree"]) / "harness" / "harness.yaml")
    assert seen["A"]["config"] is None and o["settings"]["extra"]["config"] == seen["O"]["config"]
    # the record measures the agent's change only: overlay files are no diff, no path violation, no commit
    assert o["changed_files"] == ["src/a.c"] and o["path_violations"] == []
    assert o["workflow_committed"] is commit and o["commits"] == (1 if commit else 0)
    assert o["commit"] == (o["head_commit"] if commit else None)
    patch = (Path(o["run_dir"]) / "diff.patch").read_text(encoding="utf-8")
    assert "+int a; /* fixed */" in patch and "AGENTS.md" not in patch
    assert o["diff"] == a["diff"] and o["golden_diff"] == a["golden_diff"]
    # the overlay commit stays in the eval branch's history
    assert _git(repo, "rev-list", "--count", f"ticket/EV-1..{o['branch']}") == ("2" if commit else "1")


def test_overlay_that_changes_nothing_records_null(repo, hidden, tmp_path):
    empty = _write_tree(tmp_path / "empty-unit", {})
    m = _matrix(tmp_path, f'O: {{overlay_from: "{empty.as_posix()}"}}\n')
    seen: dict = {}
    (rec,) = runner.Runner(_cfg(repo, hidden, m, tmp_path, backend=_agent(seen))).run()
    assert rec["run_status"] == "done" and seen["O"]["ahead"] == 0
    assert rec["overlay_from"] == empty.as_posix() and rec["overlay_commit"] is None
    assert rec["overlay_skipped"] == [] and rec["overlay_skipped_total"] == 0


def test_the_record_lists_at_most_fifty_skipped_files_and_counts_all_of_them(repo, hidden, tmp_path):
    unit = _write_tree(tmp_path / "unit-9", {"AGENTS.md": "# rules\n",
                                             **{f"harness/prompts/s{i:03d}.sh": "echo\n" for i in range(120)}})
    m = _matrix(tmp_path, f'O: {{overlay_from: "{unit.as_posix()}"}}\n')
    (rec,) = runner.Runner(_cfg(repo, hidden, m, tmp_path, backend=_agent({}))).run()
    assert rec["run_status"] == "done"
    assert rec["overlay_skipped"] == [f"harness/prompts/s{i:03d}.sh" for i in range(50)]
    assert rec["overlay_skipped_total"] == 120
    assert type(rec["overlay_skipped"]) is list                      # a plain list in the record, as written to jsonl


def test_what_the_scan_found_stays_in_the_record_when_the_overlay_fails_afterwards(repo, hidden, tmp_path,
                                                                                    monkeypatch):
    outside = _write_tree(tmp_path / "outside", {"secret.md": "secret\n"})
    unit = _write_tree(tmp_path / "unit-9", {"AGENTS.md": "# rules\n", "harness/prompts/a.md": "a\n",
                                             "harness/prompts/b.sh": "echo\n", "harness/prompts/c.txt": "c\n"})
    scan = wt._overlay_files

    def scan_then_swap(source):
        found = scan(source)
        (source / "harness" / "prompts" / "a.md").unlink()           # a file turns into a link after the scan
        _symlink(source / "harness" / "prompts" / "a.md", outside / "secret.md")
        return found
    monkeypatch.setattr(wt, "_overlay_files", scan_then_swap)
    m = _matrix(tmp_path, f'O: {{overlay_from: "{unit.as_posix()}"}}\n')
    (rec,) = runner.Runner(_cfg(repo, hidden, m, tmp_path, backend=_agent({}))).run()
    assert rec["run_status"] == "setup_error" and "overlay" in rec["error"]
    assert rec["overlay_skipped"] == ["harness/prompts/b.sh", "harness/prompts/c.txt"]
    assert rec["overlay_skipped_total"] == 2


def test_the_record_says_which_knobs_the_variant_applied(repo, hidden, tmp_path, unit_dir):
    m = _knob_matrix(tmp_path, f"""        A: {{label: plain}}
        O: {{overlay_from: "{unit_dir.as_posix()}", retry_budget: 2}}
        """)
    seen: dict = {}
    recs = {r["variant"]: r for r in runner.Runner(_cfg(repo, hidden, m, tmp_path, backend=_agent(seen))).run()}
    assert recs["O"]["applied_knobs"] == ["max_ai_credits", "retry_budget"]
    assert recs["O"]["settings"]["extra"]["applied_knobs"] == ["max_ai_credits", "retry_budget"]
    assert seen["O"]["knobs"] == ["max_ai_credits", "retry_budget"]            # what the backend was told
    assert recs["A"]["applied_knobs"] is None and seen["A"]["knobs"] is None    # no overlay: every setting applies
    assert "applied_knobs" not in recs["A"]["settings"]["extra"]


def test_a_failed_overlay_is_a_setup_error_and_the_rest_runs(repo, hidden, tmp_path, unit_dir):
    m = _two_variants(tmp_path, unit_dir)
    shutil.rmtree(unit_dir)                                  # the unit's worktree went away after the file was read
    seen: dict = {}
    recs = {r["variant"]: r for r in runner.Runner(_cfg(repo, hidden, m, tmp_path, backend=_agent(seen))).run()}
    assert recs["O"]["run_status"] == "setup_error" and "overlay" in recs["O"]["error"]
    assert recs["O"]["overlay_from"] == unit_dir.as_posix() and recs["O"]["overlay_commit"] is None
    assert recs["O"]["overlay_skipped"] == [] and recs["O"]["overlay_skipped_total"] == 0
    assert recs["O"]["applied_knobs"] == []
    assert "O" not in seen                                   # the agent never started
    assert recs["A"]["run_status"] == "done"


# --------------------------------------------------------------- board rows --

def _rec(variant: str, ticket: str, **kw) -> dict:
    return {"schema": results.SCHEMA, "experiment": "e", "variant": variant, "ticket": ticket, "repeat": 0,
            "run_status": "done", "success": True, "ai_credits": 1.0, **kw}


def test_board_rows_carry_the_applied_knobs_of_their_variant():
    recs = [_rec("A", "T-1"),
            _rec("O", "T-1", overlay_from="/units/unit-7", applied_knobs=["max_ai_credits"]),
            _rec("O", "T-2", overlay_from="/units/unit-7", applied_knobs=["max_ai_credits", "retry_budget"])]
    rows = {r["variant"]: r for r in results.aggregate(recs)}
    assert rows["A"]["applied_knobs"] is None                                   # old records and plain variants
    assert rows["O"]["applied_knobs"] == ["max_ai_credits", "retry_budget"]     # one value: the last run's
    runs = {(r["variant"], r["ticket"]): r for r in results.build_board(recs)["runs"]}
    assert runs["A", "T-1"]["applied_knobs"] is None
    assert runs["O", "T-1"]["applied_knobs"] == ["max_ai_credits"]
    assert runs["O", "T-2"]["applied_knobs"] == ["max_ai_credits", "retry_budget"]


def test_board_rows_carry_the_overlay_of_their_variant_and_nothing_else_changes():
    recs = [_rec("A", "T-1"),
            _rec("O", "T-1", overlay_from="/units/unit-7", overlay_commit="abc123"),
            _rec("O", "T-2", overlay_from="/units/unit-7", overlay_commit="def456")]
    rows = {r["variant"]: r for r in results.aggregate(recs)}
    assert (rows["A"]["overlay_from"], rows["A"]["overlay_commit"]) == (None, None)       # old records: no fields
    assert (rows["O"]["overlay_from"], rows["O"]["overlay_commit"]) == ("/units/unit-7", "def456")   # one value
    assert rows["O"]["runs"] == 2 and rows["O"]["pass_rate"] == 1.0

    def numbers(rs):
        return [{k: v for k, v in r.items() if not k.startswith("overlay_")} for r in rs]
    plain = [{k: v for k, v in r.items() if not k.startswith("overlay_")} for r in recs]
    assert numbers(results.aggregate(recs)) == numbers(results.aggregate(plain))
    board = results.build_board(recs)
    assert [r["variant"] for r in board["variants"]] == [r["variant"] for r in results.aggregate(recs)]
    # the per-run rows carry their own run's values
    runs = {(r["variant"], r["ticket"]): r for r in board["runs"]}
    assert (runs["A", "T-1"]["overlay_from"], runs["A", "T-1"]["overlay_commit"]) == (None, None)
    assert (runs["O", "T-1"]["overlay_from"], runs["O", "T-1"]["overlay_commit"]) == ("/units/unit-7", "abc123")
    assert (runs["O", "T-2"]["overlay_from"], runs["O", "T-2"]["overlay_commit"]) == ("/units/unit-7", "def456")
