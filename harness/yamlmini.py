"""Minimal YAML subset loader, used when PyYAML is not installed.

The devcontainer venv pins only the Copilot SDK and its dependencies, so the
harness cannot rely on PyYAML being present. This loader understands exactly
the subset the harness configuration files use:

- block mappings and block sequences (``- item``, ``- key: value``),
- plain, single- and double-quoted scalars (null, bool, int, float, str),
- flow sequences ``[a, b]`` and flow mappings ``{a: 1, b: [x]}``,
- literal ``|`` and folded ``>`` block scalars (with ``-`` / ``+`` chomping),
- ``#`` comments.

Anchors, tags, multi-document streams and complex keys are not supported and
raise :class:`YamlError`. ``load()`` prefers PyYAML when it is importable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

__all__ = ["YamlError", "load", "load_mini"]


class YamlError(ValueError):
    """Raised on input outside the supported subset."""


@dataclass
class _Line:
    no: int  # 0-based index into the raw lines
    indent: int
    text: str  # content without indentation and trailing comment


_INT_RE = re.compile(r"^[-+]?(0|[1-9][0-9_]*)$")
_FLOAT_RE = re.compile(r"^[-+]?(\d[\d_]*)?\.\d+([eE][-+]?\d+)?$")  # YAML 1.1: a dot is required


def load(text: str) -> Any:
    """Parse YAML text with PyYAML when available, else with the subset loader."""
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        return load_mini(text)
    return yaml.safe_load(text)


def load_mini(text: str) -> Any:
    """Parse YAML text with the built-in subset loader."""
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[_Line] = []
    for no, line in enumerate(raw):
        if "\t" in line[: len(line) - len(line.lstrip())]:
            raise YamlError(f"line {no + 1}: tabs are not allowed for indentation")
        stripped = _strip_comment(line).rstrip()
        if not stripped.strip():
            continue
        if stripped.strip() in ("---", "..."):
            if lines:
                raise YamlError(f"line {no + 1}: multi-document streams are not supported")
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        lines.append(_Line(no, indent, stripped.strip()))
    if not lines:
        return None
    parser = _Parser(raw, lines)
    value, pos = parser.block(0, lines[0].indent)
    if pos != len(lines):
        bad = lines[pos]
        raise YamlError(f"line {bad.no + 1}: unexpected indentation or content: {bad.text!r}")
    return value


def _strip_comment(line: str) -> str:
    """Remove a trailing ``# comment`` that is outside quotes."""
    quote: str | None = None
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            # a quote only opens a quoted scalar at a token start
            if i == 0 or line[i - 1] in " \t[{,:-":
                quote = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
        i += 1
    return line


def _split_key(text: str) -> tuple[str, str] | None:
    """Split ``key: value`` at the first ``:`` (followed by space or EOL) outside quotes."""
    quote: str | None = None
    depth = 0
    for i, c in enumerate(text):
        if quote:
            if c == quote:
                quote = None
            continue
        if c in "\"'" and (i == 0 or text[i - 1] in " [{,"):
            quote = c
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
        elif c == ":" and depth == 0 and (i + 1 == len(text) or text[i + 1] == " "):
            return text[:i].strip(), text[i + 1 :].strip()
    return None


class _Parser:
    def __init__(self, raw: list[str], lines: list[_Line]) -> None:
        self.raw = raw
        self.lines = lines

    # -- block structure ------------------------------------------------------
    def block(self, pos: int, indent: int) -> tuple[Any, int]:
        line = self.lines[pos]
        if line.indent != indent:
            raise YamlError(f"line {line.no + 1}: bad indentation")
        if _is_seq_item(line.text):
            return self.sequence(pos, indent)
        if _split_key(line.text) is not None:
            return self.mapping(pos, indent)
        # a lone scalar document / value
        return self.scalar_or_flow(line.text, line.no), pos + 1

    def sequence(self, pos: int, indent: int) -> tuple[list[Any], int]:
        out: list[Any] = []
        while pos < len(self.lines):
            line = self.lines[pos]
            if line.indent < indent:
                break
            if line.indent > indent:
                raise YamlError(f"line {line.no + 1}: bad indentation in sequence")
            if not _is_seq_item(line.text):
                break
            rest = line.text[1:].lstrip(" ")
            if not rest:
                pos += 1
                if pos < len(self.lines) and self.lines[pos].indent > indent:
                    value, pos = self.block(pos, self.lines[pos].indent)
                else:
                    value = None
                out.append(value)
                continue
            child_indent = indent + (len(line.text) - len(rest))
            if _is_seq_item(rest) or (_split_key(rest) is not None and not _is_flow(rest)):
                # "- key: value" / "- - x": re-read the rest as a nested block
                self.lines[pos] = _Line(line.no, child_indent, rest)
                value, pos = self.block(pos, child_indent)
                out.append(value)
                continue
            if rest.startswith(("|", ">")):
                value, pos = self.block_scalar(pos, rest, indent)
                out.append(value)
                continue
            rest, pos = self.join_flow(rest, pos)
            out.append(self.scalar_or_flow(rest, line.no))
            pos += 1
        return out, pos

    def mapping(self, pos: int, indent: int) -> tuple[dict[str, Any], int]:
        out: dict[str, Any] = {}
        while pos < len(self.lines):
            line = self.lines[pos]
            if line.indent < indent:
                break
            if line.indent > indent:
                raise YamlError(f"line {line.no + 1}: bad indentation in mapping")
            if _is_seq_item(line.text):
                break
            kv = _split_key(line.text)
            if kv is None:
                raise YamlError(f"line {line.no + 1}: expected 'key: value', got {line.text!r}")
            key_text, rest = kv
            key = self.scalar_or_flow(key_text, line.no)
            if isinstance(key, (dict, list)):
                raise YamlError(f"line {line.no + 1}: complex keys are not supported")
            key = str(key) if not isinstance(key, str) else key
            if key in out:
                raise YamlError(f"line {line.no + 1}: duplicate key {key!r}")
            if rest.startswith(("&", "*", "!")):
                raise YamlError(f"line {line.no + 1}: anchors, aliases and tags are not supported")
            if rest == "":
                pos += 1
                if pos < len(self.lines) and self.lines[pos].indent > indent:
                    value, pos = self.block(pos, self.lines[pos].indent)
                elif (pos < len(self.lines) and self.lines[pos].indent == indent
                      and _is_seq_item(self.lines[pos].text)):
                    value, pos = self.sequence(pos, indent)
                else:
                    value = None
            elif rest.startswith(("|", ">")):
                value, pos = self.block_scalar(pos, rest, indent)
            else:
                rest, pos = self.join_flow(rest, pos)
                value = self.scalar_or_flow(rest, line.no)
                pos += 1
            out[key] = value
        return out, pos

    def join_flow(self, text: str, pos: int) -> tuple[str, int]:
        """A flow collection may continue on the following lines: join them until balanced."""
        if not _is_flow(text):
            return text, pos
        while _depth(text) > 0 and pos + 1 < len(self.lines):
            pos += 1
            text = text + " " + self.lines[pos].text
        return text, pos

    def block_scalar(self, pos: int, header: str, parent_indent: int) -> tuple[str, int]:
        line = self.lines[pos]
        m = re.fullmatch(r"([|>])([-+]?)(\d?)", header.strip())
        if not m:
            raise YamlError(f"line {line.no + 1}: bad block scalar header {header!r}")
        style, chomp, explicit = m.group(1), m.group(2), m.group(3)
        # collect raw lines until a non-blank line with indent <= parent_indent
        body: list[str] = []
        no = line.no + 1
        while no < len(self.raw):
            text = self.raw[no]
            if text.strip() and (len(text) - len(text.lstrip(" "))) <= parent_indent:
                break
            body.append(text)
            no += 1
        # skip the parsed lines that belong to the scalar
        nxt = pos + 1
        while nxt < len(self.lines) and self.lines[nxt].no < no:
            nxt += 1
        content_lines = [b for b in body if b.strip()]
        if explicit:
            ind = parent_indent + int(explicit)
        elif content_lines:
            ind = min(len(b) - len(b.lstrip(" ")) for b in content_lines)
        else:
            ind = parent_indent + 1
        texts = [b[ind:] if len(b) >= ind else "" for b in body]
        # trailing blank lines are handled by chomping
        trailing = 0
        while texts and not texts[-1].strip():
            texts.pop()
            trailing += 1
        if style == "|":
            value = "\n".join(texts)
        else:
            value = _fold(texts)
        if not texts:
            return "", nxt
        if chomp == "-":
            pass
        elif chomp == "+":
            value += "\n" * (1 + trailing)
        else:
            value += "\n"
        return value, nxt

    # -- scalars --------------------------------------------------------------
    def scalar_or_flow(self, text: str, no: int) -> Any:
        text = text.strip()
        if _is_flow(text):
            value, end = _parse_flow(text, 0, no)
            if text[end:].strip():
                raise YamlError(f"line {no + 1}: trailing content after flow collection")
            return value
        return _scalar(text, no)


def _fold(texts: list[str]) -> str:
    """Fold a ``>`` block: single newlines become spaces, blank lines become newlines."""
    out = ""
    breaks = 0
    prev: str | None = None
    for t in texts:
        if not t.strip():
            breaks += 1
            continue
        if prev is None or breaks:
            out += "\n" * breaks + t
        elif t.startswith(" ") or prev.startswith(" "):
            out += "\n" + t
        else:
            out += " " + t
        prev = t
        breaks = 0
    return out


def _depth(text: str) -> int:
    """Open-bracket depth of a flow collection, ignoring quoted text."""
    depth = 0
    quote: str | None = None
    for i, c in enumerate(text):
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'" and (i == 0 or text[i - 1] in " [{,:"):
            quote = c
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
    return depth


def _is_seq_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


def _is_flow(text: str) -> bool:
    return text.startswith(("[", "{"))


def _scalar(text: str, no: int) -> Any:
    if text == "":
        return None
    if text[0] == '"':
        if len(text) < 2 or text[-1] != '"':
            raise YamlError(f"line {no + 1}: unterminated double-quoted string")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise YamlError(f"line {no + 1}: bad double-quoted string: {exc}") from None
    if text[0] == "'":
        if len(text) < 2 or text[-1] != "'":
            raise YamlError(f"line {no + 1}: unterminated single-quoted string")
        return text[1:-1].replace("''", "'")
    if text[0] in "&*!|>%@`":
        raise YamlError(f"line {no + 1}: unsupported YAML syntax {text!r}")
    low = text.lower()
    if low in ("null", "~"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if _INT_RE.match(text):
        return int(text.replace("_", ""))
    if _FLOAT_RE.match(text):
        return float(text.replace("_", ""))
    return text


def _parse_flow(text: str, i: int, no: int) -> tuple[Any, int]:
    """Parse a flow collection starting at text[i] ('[' or '{'); return (value, end index)."""
    opener = text[i]
    closer = "]" if opener == "[" else "}"
    items: list[Any] = []
    mapping: dict[str, Any] = {}
    i += 1
    while True:
        i = _skip_ws(text, i)
        if i >= len(text):
            raise YamlError(f"line {no + 1}: unterminated flow collection")
        if text[i] == closer:
            return (items if opener == "[" else mapping), i + 1
        # one entry
        if text[i] in "[{":
            value, i = _parse_flow(text, i, no)
            key_text = None
        else:
            start = i
            quote: str | None = None
            while i < len(text):
                c = text[i]
                if quote:
                    if c == quote:
                        quote = None
                elif c in "\"'" and i == start:
                    quote = c
                elif c in ",]}" or (opener == "{" and c == ":" and
                                    (i + 1 == len(text) or text[i + 1] in " ,}")):
                    break
                i += 1
            key_text = text[start:i].strip()
            value = _scalar(key_text, no)
        i = _skip_ws(text, i)
        if opener == "{":
            if i < len(text) and text[i] == ":":
                i = _skip_ws(text, i + 1)
                if i < len(text) and text[i] in "[{":
                    val, i = _parse_flow(text, i, no)
                else:
                    start = i
                    quote = None
                    while i < len(text):
                        c = text[i]
                        if quote:
                            if c == quote:
                                quote = None
                        elif c in "\"'" and i == start:
                            quote = c
                        elif c in ",}":
                            break
                        i += 1
                    val = _scalar(text[start:i].strip(), no)
            else:
                val = None
            if isinstance(value, (dict, list)):
                raise YamlError(f"line {no + 1}: complex keys are not supported")
            mapping[str(value)] = val
        else:
            items.append(value)
        i = _skip_ws(text, i)
        if i < len(text) and text[i] == ",":
            i += 1
        elif i < len(text) and text[i] == closer:
            continue
        else:
            raise YamlError(f"line {no + 1}: expected ',' or '{closer}' in flow collection")


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t":
        i += 1
    return i
