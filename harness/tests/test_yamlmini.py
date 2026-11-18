from pathlib import Path

import pytest

from harness.yamlmini import YamlError, load_mini

HARNESS = Path(__file__).resolve().parents[1]
SHIPPED = sorted([*HARNESS.glob("*.yaml"), *(HARNESS / "workflows").glob("*.yaml")])


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.name)
def test_shipped_config_parses_like_pyyaml(path: Path) -> None:
    yaml = pytest.importorskip("yaml")
    text = path.read_text(encoding="utf-8")
    assert load_mini(text) == yaml.safe_load(text)


def test_subset_constructs() -> None:
    doc = """
a: 1
b: [x, "y z", 3.5, {k: v, n: [1, 2]}]
multi: [one,
        two]
c:
  - one
  - two: 2
    three: null
d: |
  line1
    indented
e: >-
  folded one
  folded two
f: 'it''s'
url: https://x.y/z#frag   # trailing comment
j:
- same-indent
"""
    data = load_mini(doc)
    assert data["a"] == 1
    assert data["b"] == ["x", "y z", 3.5, {"k": "v", "n": [1, 2]}]
    assert data["multi"] == ["one", "two"]
    assert data["c"] == ["one", {"two": 2, "three": None}]
    assert data["d"] == "line1\n  indented\n"
    assert data["e"] == "folded one folded two"
    assert data["f"] == "it's"
    assert data["url"] == "https://x.y/z#frag"
    assert data["j"] == ["same-indent"]


@pytest.mark.parametrize("doc", ["a: &x 1\n", "a: 1\na: 2\n", "a: [1, 2\n", "a:\n\t- x\n"])
def test_unsupported_or_invalid_raises(doc: str) -> None:
    with pytest.raises(YamlError):
        load_mini(doc)
