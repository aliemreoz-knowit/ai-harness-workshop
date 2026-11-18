import subprocess
from pathlib import Path

from harness import form
from harness.config import HarnessConfig
from harness.gates import GateContext

NEW_HEADER = "/* SPDX-License-Identifier: MIT */\n/*\n * tinystream v2 -- {path}\n * {what}\n */\n"


def gates(cfg: HarnessConfig, repo: Path):
    ctx = GateContext(repo, [], cfg.policy)
    return form.style(ctx, cfg.gates.gates["style"]), form.docs(ctx, cfg.gates.gates["comments"])


def rules(result) -> set[tuple[str, str]]:
    return {(i.rule, i.file) for i in result.items}


def long_fn(name: str, comment: str = "") -> str:
    body = "".join(f"    x += {i}U;\n" for i in range(form.LONG_FUNCTION))
    return f"{comment}static uint32_t {name}(uint32_t x)\n{{\n{body}    return x;\n}}\n"


def test_clean_tree_passes(cfg: HarnessConfig, repo: Path) -> None:
    style, docs = gates(cfg, repo)
    assert (style.status, docs.status) == ("PASS", "PASS")


def test_style_judges_added_lines_only(cfg: HarnessConfig, repo: Path) -> None:
    src = repo / "src" / "proto" / "proto.c"
    old = src.read_text(encoding="utf-8")
    src.write_text(old + "int a;\t/* tab */\nint b;   \nint c = " + "1 + " * 40 + "1;\nint d;", encoding="utf-8")
    style, _ = gates(cfg, repo)
    got = {(i.rule, i.line) for i in style.items}
    n = len(old.splitlines())
    assert got == {("tab", n + 1), ("trailing-space", n + 2), ("line-length", n + 3), ("final-newline", n + 4)}


def test_docs_wants_comments_on_new_long_functions(cfg: HarnessConfig, repo: Path) -> None:
    src = repo / "src" / "proto" / "proto.c"
    src.write_text(src.read_text(encoding="utf-8") + "\n" + long_fn("bare") + "\n"
                   + long_fn("said", "/* Adds the first few integers to x. */\n") + "\n"
                   + long_fn("test_named_for_what_it_checks") + "\n"
                   + "static int short_one(void)\n{\n    return 1;\n}\n"
                   + "/* TODO: handle the wrap */\n", encoding="utf-8")
    _, docs = gates(cfg, repo)
    assert docs.status == "FAIL"
    assert {(i.rule, i.message.split("(")[0]) for i in docs.items} == {
        ("function-comment", "new function bare"), ("todo", "TODO left in a comment: finish it, or name what is left in your report")}


def test_docs_new_file_says_what_it_is_for(cfg: HarnessConfig, repo: Path) -> None:
    good = "tests/unit/test_more.c"
    bad = "tests/unit/test_bare.c"
    (repo / good).write_text(NEW_HEADER.format(path=good, what="Parser cases at the length limits.") + "int x;\n",
                             encoding="utf-8")
    (repo / bad).write_text("/* SPDX-License-Identifier: MIT */\n/*\n * tinystream v2 -- tests/unit/test_bare.c\n */\n"
                            "int y;\n", encoding="utf-8")
    style, docs = gates(cfg, repo)
    assert style.status == "PASS"
    assert rules(docs) == {("file-description", bad)}


def test_a_changed_signature_is_not_a_new_function(cfg: HarnessConfig, repo: Path) -> None:
    src = repo / "src" / "proto" / "proto.c"
    body = "".join(f"    x += {i};\n" for i in range(form.LONG_FUNCTION))
    src.write_text("/*\n * tinystream v2 -- src/proto/proto.c\n */\nint ts_parse(int x)\n{\n" + body
                   + "    return x + 1;\n}\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "base"], cwd=repo, check=True)
    src.write_text(src.read_text(encoding="utf-8").replace("int ts_parse(int x)", "int ts_parse(long x)"),
                   encoding="utf-8")
    _, docs = gates(cfg, repo)
    assert docs.status == "PASS", docs.items


def test_only_c_sources_in_the_editable_trees(cfg: HarnessConfig, repo: Path) -> None:
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "notes.md").write_text("a\tb   \n", encoding="utf-8")
    style, docs = gates(cfg, repo)
    assert (style.status, docs.status) == ("PASS", "PASS")


def test_a_renamed_file_is_judged_on_its_changed_lines_only(cfg: HarnessConfig, repo: Path) -> None:
    src = repo / "src" / "proto" / "proto.c"
    src.write_text(src.read_text(encoding="utf-8") + "\n" + long_fn("old_and_bare") + "int wide = 1;   \n",
                   encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    subprocess.run(["git", "mv", "src/proto/proto.c", "src/proto/proto2.c"], cwd=repo, check=True)
    style, docs = gates(cfg, repo)
    assert (style.status, docs.status) == ("PASS", "PASS"), (style.items, docs.items)


def test_a_one_line_function_does_not_swallow_the_next(cfg: HarnessConfig, repo: Path) -> None:
    src = repo / "src" / "proto" / "proto.c"
    src.write_text(src.read_text(encoding="utf-8") + "\nstatic int one(int x) { return x; }\n\n" + long_fn("next_bare"),
                   encoding="utf-8")
    _, docs = gates(cfg, repo)
    assert [i.message.split("(")[0] for i in docs.items] == ["new function next_bare"]


def test_a_staged_new_file_still_says_what_it_is_for(cfg: HarnessConfig, repo: Path) -> None:
    bad = "tests/unit/test_staged.c"
    (repo / bad).write_text("/* SPDX-License-Identifier: MIT */\n/*\n * tinystream v2 -- tests/unit/test_staged.c\n */\n"
                            "int y;\n", encoding="utf-8")
    subprocess.run(["git", "add", bad], cwd=repo, check=True)
    _, docs = gates(cfg, repo)
    assert rules(docs) == {("file-description", bad)}
