from pathlib import Path

from harness import compliance
from harness.config import HarnessConfig
from harness.gates import GateContext

ALLOWED = ["src/**", "tools/**", "tests/**", "PROTOCOL.md"]


def run(cfg: HarnessConfig, repo: Path, **kw: object):
    return compliance.check(GateContext(repo, ALLOWED, cfg.policy, **kw), cfg.gates.gates["compliance"])


def rules(result) -> set[str]:
    return {i.rule for i in result.items}


def test_clean_tree_passes(cfg: HarnessConfig, repo: Path) -> None:
    assert run(cfg, repo).status == "PASS"
    assert rules(run(cfg, repo, expect_changes=True)) == {"no-change"}


def test_paths_calls_header_and_protocol(cfg: HarnessConfig, repo: Path) -> None:
    (repo / "include" / "ts_proto.h").write_text("int ts_parse(long x);\n", encoding="utf-8")
    (repo / "docs" / "ARCHITECTURE.md").write_text("edited\n", encoding="utf-8")
    src = repo / "src" / "proto" / "proto.c"
    src.write_text(src.read_text(encoding="utf-8") + "void *p(void) { return malloc(4); }\n"
                   "/* system() in a comment is fine */\nconst char *s = \"popen( in a string\";\n",
                   encoding="utf-8")
    (repo / "src" / "proto" / "new.c").write_text("int y;\n", encoding="utf-8")
    proto = repo / "PROTOCOL.md"
    proto.write_text(proto.read_text(encoding="utf-8").replace("body", "body changed"), encoding="utf-8")
    r = run(cfg, repo)
    assert r.status == "FAIL"
    got = {(i.rule, i.file) for i in r.items}
    assert ("protected-path", "include/ts_proto.h") in got
    assert ("allowed-paths", "docs/ARCHITECTURE.md") in got
    assert ("forbidden-call:malloc", "src/proto/proto.c") in got
    assert ("license-header", "src/proto/new.c") in got
    assert ("protocol-version", "PROTOCOL.md") in got
    calls = [i for i in r.items if i.rule.startswith("forbidden-call")]
    assert len(calls) == 1 and calls[0].line == 8      # comment and string literal are ignored


def test_protocol_bump_and_header_ok(cfg: HarnessConfig, repo: Path) -> None:
    proto = repo / "PROTOCOL.md"
    proto.write_text(proto.read_text(encoding="utf-8").replace("PROTOCOL_VERSION: 2", "PROTOCOL_VERSION: 3"),
                     encoding="utf-8")
    (repo / "tests" / "unit" / "test_new.c").write_text(
        "/* SPDX-License-Identifier: MIT */\n/*\n * tinystream v2 -- tests/unit/test_new.c\n */\n", encoding="utf-8")
    assert run(cfg, repo).status == "PASS"


def test_new_file_without_spdx_fails(cfg: HarnessConfig, repo: Path) -> None:
    (repo / "src" / "proto" / "extra.c").write_text("/*\n * tinystream v2 -- src/proto/extra.c\n */\nint z;\n",
                                                    encoding="utf-8")
    r = run(cfg, repo)
    assert r.status == "FAIL" and {(i.rule, i.file) for i in r.items} == {("license-spdx", "src/proto/extra.c")}


def test_test_manifest_is_the_protected_exception(cfg: HarnessConfig, repo: Path) -> None:
    (repo / "scripts").mkdir(exist_ok=True)
    (repo / "scripts" / "test_manifest.txt").write_text("test_new unit\n", encoding="utf-8")
    (repo / "scripts" / "check.sh").write_text("exit 0\n", encoding="utf-8")
    r = compliance.check(GateContext(repo, ALLOWED + ["scripts/test_manifest.txt"], cfg.policy),
                         cfg.gates.gates["compliance"])
    got = {(i.rule, i.file) for i in r.items}
    assert ("protected-path", "scripts/check.sh") in got
    assert not any(f == "scripts/test_manifest.txt" for _, f in got)
