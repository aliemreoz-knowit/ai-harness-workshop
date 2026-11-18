"""Files of a unit worktree: the path guard, tree, read, write (one save = one commit), diff and ``commit_files``.

This module is the boundary between a browser and the facilitator's disk. A path from the browser is
never used before :func:`resolve` (through ``tree``, ``read`` or ``write``) has accepted it:

* Only relative paths. ``..``, ``.git`` (any depth), drive letters, ``:`` (alternate data streams), wildcard
  and control characters, trailing dots or spaces and Windows device names (``nul``, ``aux.c`` ...) are refused.
* The path must lead to where it says. Its real location (symlinks and junctions followed) has to be the
  same folders, compared without case, as the path that was asked for; so a link that leads out of the
  worktree, or in to another folder of it (``src/alias`` -> ``harness``), is refused, and so is a short name.
* ``is_editable`` decides on the path as it is on disk, compared without case, so ``HARNESS/CLI.PY`` is as
  read-only as ``harness/cli.py``.
* Run artefacts and build output are not served: a top-level ``.harness``, ``results`` or ``build*`` folder
  and any ``__pycache__`` are refused like read-only paths ("not part of the exercise").

The agent and the participants' code in the gate container can change anything inside the worktree at any
moment (swap a folder for a link, make a hard link, rewrite the worktree's ``.git`` file), so the guard on the
path is only the first line. ``docs/threat-model.md`` names the actors; the two lists below are the same as there.

What this module guarantees, for a request from the browser, whatever happens in the worktree meanwhile:

1. A browser path reaches only files of the unit worktree: never ``.git``, run or build output, or a path
   through a link or a short name; a read-only path is never written.
2. Nothing outside the worktree is read, and no existing file outside is overwritten or deleted, through
   ``read``, ``write`` or a rollback. The open handle is what is checked, never a second look at the path
   (POSIX: walked down from a folder fd with ``O_NOFOLLOW``; Windows: the handle's final path must be the pinned
   worktree path plus the guarded parts); it must be a regular file with one name (no hard link); the worktree
   folder must be the one pinned on first use (its real path and its device and inode).
3. A save commits exactly the bytes it wrote (git never reads the worktree file: ``hash-object --stdin``, a
   scratch index in a console temp folder, ``commit-tree``), to the unit's own branch, and only if the branch is
   still where the save found it (``update-ref`` with the old HEAD). A file whose HEAD version has CRLF keeps it.
4. Git is never pointed at a repo by the worktree's ``.git`` file. Every call names the unit's admin dir
   (``<data>/source/.git/worktrees/<name>``, found by ``git_location`` from the ``gitdir`` files there,
   outside the worktree) and the work tree. So the config, hooks and filter drivers git uses are the clone's,
   which file writes in a worktree cannot reach; on top, hooks point at an empty folder, fsmonitor is off, and
   ``diff`` runs no textconv or external diff. (``units.py`` names the git dirs the same way for reset and ``git``.)
5. Saves to one worktree are serialised with each other and with a catch-up reset (``worktree_lock``); a save
   based on a stale version is a ``Conflict``, never a silent overwrite or merge.

What stays open (accepted):

1. Windows: a new file, and missing parent folders, are created by path and checked afterwards. A link
   swapped in at that instant can leave an empty file or empty folders where it led; nothing is written to them
   and the save is refused. (POSIX creates through the folder fds: no such gap.)
2. ``tree`` lists a folder by path: such a link can show the names (not the contents) of a folder outside.
3. A save writes in place, not atomically: a hard kill between the truncate and the end of the write leaves a
   cut-off file (the last commit has the old version; an uncommitted agent edit of that file is lost).
4. The update-ref to index gap: the branch moves first, then the real index gets the entry. A commit made from
   the index in between (an agent's ``git commit``) lacks the save and undoes it in its own commit; if the index
   cannot be updated and the branch moved meanwhile, the save's commit stays although the save failed.
5. Git by path: the console's own git calls read ``.gitattributes`` from the worktree (to choose filters, never
   the saved content); git run by other code in the worktree (Task 11's run finalize) follows a junction on
   Windows as if it were a folder. Task 11 excludes links.

Note: a unit rebuilt at the same path while the console runs is refused (pinned folder) until a restart.

Globs (``console.yaml`` ``editable``): ``/`` separates folders; ``**`` as a whole segment matches any number of
folders (``tools/**/*.py`` matches ``tools/x.py`` and ``tools/a/b.py``; a trailing ``/**`` matches everything
below); ``*`` and ``?`` never cross a ``/``. An entry that starts with ``!`` is a deny and beats every other entry.

A save is one commit of that one path, made with git plumbing from the written bytes (see
``_commit_bytes``), message on stdin, identity from the repo's own config. Saves to one worktree are serialised
by a lock (``worktree_lock``, also taken by a catch-up reset), so the sha check, the write and the commit cannot
interleave with another save. If a git step fails, the file is put back as it was.
"""

from __future__ import annotations

import atexit
import codecs
import contextlib
import errno
import functools
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Callable, Mapping, Sequence

from console import procs

__all__ = [
    "Busy",
    "Conflict",
    "FileView",
    "Forbidden",
    "Invalid",
    "MAX_BYTES",
    "blob_sha",
    "commit_files",
    "diff",
    "empty_hooks_dir",
    "git_bytes",
    "git_location",
    "is_editable",
    "is_plain_ref",
    "read",
    "resolve",
    "tree",
    "usable_path",
    "worktree_lock",
    "write",
]

MAX_BYTES = 200 * 1024  # largest text a save accepts (UTF-8 bytes, line endings as LF)
_SNIFF_BYTES = 8192  # a NUL in this much of the start marks a file as binary
_MAX_REL = 512

# open() flags; the ones a platform lacks are 0 (NONBLOCK keeps a FIFO from blocking an open).
_O_BINARY = getattr(os, "O_BINARY", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

_NOT_THE_EXERCISE = "not part of the exercise"
# First match wins. Only used for paths that are not editable.
_READ_ONLY_REASONS = (
    (
        "runs on the facilitator's machine",
        (
            "harness/**/*.py",
            "scripts/**",
            ".github/hooks/scripts/**",
            ".github/workflows/**",
            "tools/**/*.py",
            "tools/**/*.sh",
            "tools/**/*.ps1",
            "**/__init__.*",  # R18: Python reads these by their names, wherever they are saved
            "**/*.pth",
            "**/sitecustomize.py",
            "**/usercustomize.py",
        ),
    ),
    (
        "build setup is fixed for the workshop",
        ("CMakeLists.txt", "CMakePresets.json", "cmake/**", "third_party/**", ".devcontainer/**"),
    ),
)

_HIDDEN_ANYWHERE = frozenset({".git", "__pycache__"})  # in a tree listing; ".git" is refused by the guard itself
_HIDDEN_TOP_LEVEL_DIRS = frozenset({".harness", "results"})  # plus every folder named build*

_BAD_NAME_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_DEVICE_NAME = re.compile(r"(con|prn|aux|nul|conin\$|conout\$|(com|lpt)[0-9¹²³])", re.IGNORECASE)
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_COMMIT_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_UNIT_ID = re.compile(r"[A-Za-z0-9._-]+")


class Busy(Exception):
    """The unit is taken just now (a run holds it): nothing was written. The text says by what."""


class Forbidden(Exception):
    """The path is outside what a browser may touch, or read-only. The text says why."""


class Invalid(Exception):
    """The content cannot be saved. ``errors`` are plain sentences for the person who saved."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class Conflict(Exception):
    """The file is not the version the save was based on. ``current`` is the version on disk now."""

    def __init__(self, current: "FileView") -> None:
        super().__init__(f"{current.path} was changed by someone else")
        self.current = current


@dataclass(frozen=True)
class FileView:
    """A file as the browser sees it.

    ``content`` is text with ``\\n`` line ends (``eol`` says which ones are on disk), ``None`` for a binary
    file. ``sha`` is the git blob id of the bytes on disk; for a file that does not exist it is ``""``
    and ``content`` is ``""`` (only a ``Conflict`` carries such a view).
    """

    path: str
    content: str | None
    sha: str
    editable: bool
    reason: str
    binary: bool
    eol: str


def is_plain_ref(ref: object) -> bool:
    """Whether ``ref`` is a plain branch, tag or commit name: nothing git could take for an option or a range."""
    return isinstance(ref, str) and _REF.fullmatch(ref) is not None and ".." not in ref


def usable_path(rel: object) -> bool:
    """Whether ``rel`` is a path ``commit_files`` takes: relative, spelled with ``/`` only, every name one the path
    guard accepts, and not run or build output (a top-level ``.harness``, ``results`` or ``build*`` folder, or
    ``__pycache__``)."""
    try:
        parts = _parts(rel)
    except Forbidden:
        return False
    hidden = any(part.casefold() == "__pycache__" for part in parts) or (len(parts) > 1 and _is_run_folder(parts[0]))
    return bool(parts) and "/".join(parts) == rel and not hidden


def blob_sha(data: bytes) -> str:
    """The id git gives a file with these bytes (``git hash-object``)."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# --- globs -----------------------------------------------------------------------------------


def _segment_regex(segment: str) -> str:
    out, previous_star = [], False
    for ch in segment:
        if ch == "*":
            if not previous_star:
                out.append("[^/]*")
            previous_star = True
            continue
        previous_star = False
        out.append("[^/]" if ch == "?" else re.escape(ch))
    return "".join(out)


@functools.lru_cache(maxsize=256)
def _glob(pattern: str) -> re.Pattern[str]:
    """The regex for a glob, matched against a case-folded path with ``/`` separators."""
    segments = pattern.casefold().split("/")
    last, out = len(segments) - 1, []
    for i, segment in enumerate(segments):
        if segment == "**":
            out.append(".+" if i == last else "(?:[^/]+/)*")
            continue
        out.append(_segment_regex(segment))
        if i != last:
            out.append("/")
    return re.compile("".join(out))


def _split(rel: str) -> list[str]:
    return [part for part in re.split(r"[\\/]+", rel) if part not in ("", ".")]


def _read_only_reason(folded: str) -> str:
    for reason, patterns in _READ_ONLY_REASONS:
        if any(_glob(pattern).fullmatch(folded) for pattern in patterns):
            return reason
    return _NOT_THE_EXERCISE


def is_editable(rel: str, editable: Sequence[str]) -> tuple[bool, str]:
    """Whether a browser may save ``rel``, and for a read-only path the reason (``""`` when editable)."""
    parts = _split(rel)
    if ".." in parts:
        return False, _NOT_THE_EXERCISE
    folded = "/".join(parts).casefold()
    allowed = False
    for entry in editable:
        if entry.startswith("!"):
            if _glob(entry[1:]).fullmatch(folded):
                return False, _read_only_reason(folded)
        elif _glob(entry).fullmatch(folded):
            allowed = True
    return (True, "") if allowed else (False, _read_only_reason(folded))


def _folder_may_hold_editable(parts: list[str], editable: Sequence[str]) -> bool:
    """Whether some path below the folder ``parts`` can match a positive entry (``!`` entries are not looked at)."""
    folded = [part.casefold() for part in parts]
    for entry in editable:
        if entry.startswith("!"):
            continue
        segments = entry.casefold().split("/")
        for i, part in enumerate(folded):
            if i >= len(segments):
                break
            if segments[i] == "**":
                return True
            if not re.fullmatch(_segment_regex(segments[i]), part):
                break
        else:
            if len(segments) > len(folded):
                return True
    return False


# --- the path guard -----------------------------------------------------------------------------


def _check_name(name: str) -> None:
    if name.casefold() == ".git":
        raise Forbidden(".git is not available")
    if _BAD_NAME_CHARS.search(name) or name[-1] in ". " or _DEVICE_NAME.fullmatch(name.split(".")[0].rstrip()):
        raise Forbidden(f"{name!r} is not a usable file name")


def _parts(rel: object) -> list[str]:
    """The folders and file name of ``rel``, or ``Forbidden``."""
    if not isinstance(rel, str) or len(rel) > _MAX_REL or "\0" in rel:
        raise Forbidden("not a usable path")
    if rel.startswith(("/", "\\")) or PureWindowsPath(rel).drive:
        raise Forbidden("only paths inside the unit's files are allowed")
    parts = _split(rel)
    if ".." in parts:
        raise Forbidden("'..' is not allowed in a path")
    for part in parts:
        _check_name(part)
    return parts


def _folded(parts: Sequence[str]) -> list[str]:
    return [part.casefold() for part in parts]


def _is_run_folder(name: str) -> bool:
    """Whether a top-level folder of this name holds run artefacts or build output."""
    folded = name.casefold()
    return folded in _HIDDEN_TOP_LEVEL_DIRS or folded.startswith("build")


def _check_not_hidden(root: Path, parts: list[str]) -> None:
    """``Forbidden`` for ``__pycache__`` anywhere and for what is in (or is) a top-level run or build folder."""
    hidden = any(part.casefold() == "__pycache__" for part in parts)
    if parts and _is_run_folder(parts[0]):  # a top-level file named build.sh or results is not a folder
        hidden = hidden or len(parts) > 1 or (root / parts[0]).is_dir()
    if hidden:
        raise Forbidden(f"{'/'.join(parts)} is not available: {_NOT_THE_EXERCISE}")


def _locate(worktree: Path, rel: str) -> tuple[str, Path]:
    """``(path as it is on disk, with "/", file system path)`` for ``rel``, or ``Forbidden``."""
    parts = _parts(rel)
    root = Path(worktree)
    _check_not_hidden(root, parts)
    root_real = Path(os.path.realpath(root))
    real = Path(os.path.realpath(root.joinpath(*parts)))
    depth = len(root_real.parts)
    if _folded(real.parts[:depth]) != _folded(root_real.parts):
        raise Forbidden("the path leads out of the unit's files")
    on_disk = list(real.parts[depth:])
    if _folded(on_disk) != _folded(parts):
        raise Forbidden("the path goes through a link or a short name")
    return "/".join(on_disk), root.joinpath(*on_disk)


def resolve(worktree: Path, rel: str) -> Path:
    """The path of ``rel`` inside ``worktree``.

    ``Forbidden`` when it is absolute, uses ``..`` or ``.git``, or goes through a link. ``""`` is the worktree itself.
    """
    return _locate(worktree, rel)[1]


# --- tree, read -------------------------------------------------------------------------------


def tree(worktree: Path, rel: str = "", editable: Sequence[str] = ()) -> list[dict]:
    """The entries of folder ``rel`` as ``{name, path, dir, editable, reason}``, folders first, then by name.

    Read from disk by path (see the module docstring), so untracked files show. Hidden: ``.git`` and
    ``__pycache__``; at the top level also ``.harness``, ``results`` and ``build*`` folders; links; names
    ``resolve`` would refuse. A folder is
    ``editable`` when something below it can be (``harness`` holds ``harness.yaml``); a file when ``is_editable``
    says so. With no ``editable`` entries nothing is. ``reason`` is why an entry is read-only (``""`` when it is
    ``editable``; for a folder, the reason of a file in it), so a file browser needs no read to tell.
    ``FileNotFoundError`` when ``rel`` is not a folder.
    """
    path, folder = _locate(worktree, rel)
    if not folder.is_dir():
        raise FileNotFoundError(path or ".")
    entries = []
    with os.scandir(folder) as scan:
        for entry in scan:
            name, folded = entry.name, entry.name.casefold()
            is_dir = entry.is_dir(follow_symlinks=False)
            if folded in _HIDDEN_ANYWHERE or entry.is_symlink() or entry.is_junction():
                continue
            if not path and is_dir and _is_run_folder(name):
                continue
            if not (is_dir or entry.is_file(follow_symlinks=False)):
                continue
            try:
                _check_name(name)
            except Forbidden:
                continue
            child = f"{path}/{name}" if path else name
            if is_dir:
                flag = _folder_may_hold_editable(child.split("/"), editable)
                reason = "" if flag else is_editable(f"{child}/_", editable)[1]  # what a file in it would say
            else:
                flag, reason = is_editable(child, editable)
            entries.append({"name": name, "path": child, "dir": is_dir, "editable": flag, "reason": reason})
    entries.sort(key=lambda e: (not e["dir"], e["name"].casefold(), e["name"]))
    return entries


def _eol(data: bytes) -> str:
    first = data.find(b"\n")
    return "\r\n" if first > 0 and data[first - 1 : first] == b"\r" else "\n"


def _view(path: str, data: bytes | None, ok: bool, reason: str) -> FileView:
    if data is None:  # no such file
        return FileView(path, "", "", ok, reason, False, "\n")
    text = None
    if b"\0" not in data[:_SNIFF_BYTES]:
        body = data[len(codecs.BOM_UTF8) :] if data.startswith(codecs.BOM_UTF8) else data
        with contextlib.suppress(UnicodeDecodeError):
            text = body.decode("utf-8").replace("\r\n", "\n")
    return FileView(path, text, blob_sha(data), ok, reason, text is None, _eol(data) if text is not None else "\n")


# --- opening a file: the open handle is what gets checked -------------------------------------------
#
# An agent runs in the same worktree and may swap a folder for a link at any moment. So the path is never
# looked up a second time to confirm what the first lookup found:
#
# * POSIX: the file is reached from a directory fd of the worktree, one folder at a time, each step opened
#   with O_DIRECTORY | O_NOFOLLOW relative to the fd before it. No link can be followed, nothing to verify.
# * Windows has no openat: the file is opened by path, then the handle itself is asked where it is
#   (GetFinalPathNameByHandleW) and that must be the guarded path. Else it is closed, unread and unwritten.
#
# Either way the open file must be a regular file with exactly one name (a hard link to a file outside the
# worktree has the guarded path as one of its names), and the worktree folder itself must still be the one
# first used for that path. Windows builds the expected final path from the real path pinned then, never from
# a realpath of the worktree taken at check time (a swap right after the check would make that agree).

_HOW = {  # how a file is opened: read it; read and change an existing one; make a new one
    "read": os.O_RDONLY | _O_BINARY,
    "edit": os.O_RDWR | _O_BINARY,
    "create": os.O_RDWR | os.O_CREAT | os.O_EXCL | _O_BINARY,
}

# worktree path -> (real path, device, inode) of the folder first used for it
_pins: dict[str, tuple[str, int, int]] = {}
_pins_guard = threading.Lock()


def _os_open(path: "Path | str", flags: int, mode: int = 0o666, *, dir_fd: int | None = None) -> int:
    """``os.open``; every open in this module goes through here (and so can be watched by a test)."""
    return os.open(path, flags, mode, dir_fd=dir_fd)


def _pin_root(root: Path, info: os.stat_result) -> None:
    """Remember which folder ``root`` is the first time it is used; ``Forbidden`` if it is another one later.

    Pinned: the real path of ``root`` then (links resolved, long names) and the ``(device, inode)`` of ``info``.
    Every check starts from the worktree folder: if that were renamed away and something else put in its
    place, all of them would be right about the wrong folder. A unit rebuilt at the same path while the
    console runs is refused until the console is restarted.
    """
    key = os.path.normcase(os.path.abspath(root))
    with _pins_guard:
        pinned = _pins.get(key)
        if pinned is None:
            pinned = _pins[key] = (os.path.realpath(root), info.st_dev, info.st_ino)
    if pinned[1:] != (info.st_dev, info.st_ino):
        raise Forbidden("the unit's folder was replaced")


def _pinned_root(root: Path) -> str:
    """The real path ``_pin_root`` recorded for ``root`` (expected paths are built from it, never recomputed)."""
    with _pins_guard:
        pinned = _pins.get(os.path.normcase(os.path.abspath(root)))
    if pinned is None:
        raise Forbidden("the unit's folder has not been checked")
    return pinned[0]


def _check_plain_file(fd: int, path: str) -> None:
    """The open file must be a regular file with one name; ``IsADirectoryError`` for a folder."""
    info = os.fstat(fd)
    if stat.S_ISDIR(info.st_mode):
        raise IsADirectoryError(path)
    if not stat.S_ISREG(info.st_mode):
        raise Forbidden("not a regular file")
    if info.st_nlink != 1:
        raise Forbidden("the file has more than one name")


if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    _kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _DELETE, _GENERIC_READ, _GENERIC_WRITE = 0x00010000, 0x80000000, 0x40000000
    _SHARE_ALL, _OPEN_EXISTING, _FILE_DISPOSITION_INFO = 7, 3, 4
    _INVALID_HANDLE = wintypes.HANDLE(-1).value

    def _plain(path: str) -> str:
        """``path`` without the extended-length prefix that marks a raw Windows path."""
        if path.startswith("\\\\?\\UNC\\"):
            return "\\\\" + path[8:]
        return path[4:] if path.startswith("\\\\?\\") else path

    def _final_path(fd: int) -> str:
        """Where Windows says the open file is: links resolved, long names, no prefix."""
        handle = msvcrt.get_osfhandle(fd)
        size = 512
        while True:
            buffer = ctypes.create_unicode_buffer(size)
            length = _kernel32.GetFinalPathNameByHandleW(handle, buffer, size, 0)
            if length == 0:
                raise ctypes.WinError(ctypes.get_last_error())
            if length < size:
                return _plain(buffer.value)
            size = length + 1

    def _verify_handle(fd: int, root: Path, parts: list[str]) -> None:
        """``Forbidden`` unless the open file is a plain file at ``root/parts`` (asks the handle, not the path).

        The expected path is the real path pinned for ``root`` plus ``parts``: a worktree swapped for a junction
        after the pin check leads the open elsewhere, and a realpath taken now would follow the swap too.
        """
        expected = os.path.normcase(_plain(os.path.join(_pinned_root(root), *parts)))
        if os.path.normcase(_final_path(fd)) != expected:
            raise Forbidden("the path leads out of the unit's files")
        _check_plain_file(fd, "/".join(parts))

    def _open_file(root: Path, path: str, how: str) -> int | None:
        """A verified fd for ``path`` (``None`` when there is no file); see the notes above for ``how``.

        ``IsADirectoryError`` for a folder, ``FileExistsError`` for ``create`` of a file that exists,
        ``Forbidden`` when the handle is not at the guarded path or is not a plain file. For ``create`` the
        file is made first and verified after: when that fails nothing is written to it and it is not
        deleted (it may be an empty file where a link has just led).
        """
        parts = path.split("/") if path else []
        if not parts:
            raise IsADirectoryError(path)
        _pin_root(root, os.stat(root))
        full = root.joinpath(*parts)
        if how == "create":
            full.parent.mkdir(parents=True, exist_ok=True)  # by path: see the module docstring
        elif full.is_dir():
            raise IsADirectoryError(path)
        try:
            fd = _os_open(full, _HOW[how])
        except (FileNotFoundError, NotADirectoryError):
            if how == "create":
                raise
            return None
        except PermissionError:
            if full.is_dir():  # Windows says "access denied" when opening a folder
                raise IsADirectoryError(path) from None
            raise
        try:
            _verify_handle(fd, root, parts)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _remove_new_file(root: Path, path: str, created: os.stat_result) -> None:
        """Empty and delete the file this save created, through a verified handle (never by path).

        The file is opened again with DELETE access and every sharing mode (so nothing else is blocked, and
        its folder cannot be renamed while the handle is open), checked like any open, compared with the
        file this save made, emptied, and marked for deletion; it goes when the handle is closed. If any
        check fails the handle is closed and nothing is deleted.
        """
        parts = path.split("/")
        _pin_root(root, os.stat(root))
        access = _DELETE | _GENERIC_READ | _GENERIC_WRITE
        handle = _kernel32.CreateFileW(str(root.joinpath(*parts)), access, _SHARE_ALL, None, _OPEN_EXISTING, 0, None)
        if handle == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDWR | _O_BINARY)
        except BaseException:
            _kernel32.CloseHandle(handle)
            raise
        try:
            _verify_handle(fd, root, parts)
            if not os.path.samestat(created, os.fstat(fd)):
                raise Forbidden("not the file this save made")
            os.ftruncate(fd, 0)
            flag = wintypes.BOOLEAN(1)
            if not _kernel32.SetFileInformationByHandle(handle, _FILE_DISPOSITION_INFO, ctypes.byref(flag), 1):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            os.close(fd)

else:

    def _step_down(fd: int, name: str, create: bool) -> int | None:
        """An fd for the folder ``name`` in the folder ``fd``, never through a link; ``None`` if it is missing."""
        flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
        for _ in range(2):
            try:
                return _os_open(name, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    return None
                with contextlib.suppress(FileExistsError):
                    os.mkdir(name, 0o777, dir_fd=fd)
            except OSError as err:
                if err.errno not in (errno.ELOOP, errno.ENOTDIR):
                    raise
                try:
                    kind = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
                except FileNotFoundError:
                    kind = None
                if kind is not None and not stat.S_ISLNK(kind) and not stat.S_ISDIR(kind):
                    raise NotADirectoryError(name) from None  # a file where a folder should be
                raise Forbidden("the path leads through a link, or a folder changed while in use") from None
        raise Forbidden("a folder changed while it was in use")

    def _folder_fd(root: Path, names: list[str], create: bool) -> int | None:
        """An fd for the folder ``root/names``, walked down from ``root`` without following a link."""
        fd = _os_open(root, os.O_RDONLY | _O_DIRECTORY)  # the worktree itself is trusted, once pinned
        try:
            _pin_root(root, os.fstat(fd))
            for name in names:
                below = _step_down(fd, name, create)
                os.close(fd)
                fd = -1
                if below is None:
                    return None
                fd = below
        except BaseException:
            if fd >= 0:
                os.close(fd)
            raise
        return fd

    def _open_leaf(folder: int, name: str, how: str, path: str) -> int | None:
        """An fd for the file ``name`` in the folder ``folder`` (``None`` if missing), never through a link."""
        try:
            fd = _os_open(name, _HOW[how] | _O_NOFOLLOW | _O_NONBLOCK, dir_fd=folder)
        except FileNotFoundError:
            if how == "create":
                raise
            return None
        except OSError as err:
            if err.errno == errno.ELOOP:
                raise Forbidden("the path leads through a link") from None
            raise
        try:
            _check_plain_file(fd, path)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _open_file(root: Path, path: str, how: str) -> int | None:
        """A fd for ``path`` (``None`` when there is no file); ``how`` as in the notes above.

        ``IsADirectoryError`` for a folder, ``FileExistsError`` for ``create`` of a file that exists,
        ``Forbidden`` when a link is in the way or the file is not a plain file. Nothing is looked up twice,
        so no link can be followed.
        """
        parts = path.split("/") if path else []
        if not parts:
            raise IsADirectoryError(path)
        try:
            folder = _folder_fd(root, parts[:-1], create=(how == "create"))
        except NotADirectoryError:
            if how == "create":
                raise
            return None
        if folder is None:
            return None
        try:
            return _open_leaf(folder, parts[-1], how, path)
        finally:
            os.close(folder)

    def _remove_new_file(root: Path, path: str, created: os.stat_result) -> None:
        """Empty and delete the file this save created, by name inside the folder fd it is reached from."""
        *folders, name = path.split("/")
        folder = _folder_fd(root, folders, create=False)
        if folder is None:
            return
        try:
            fd = _open_leaf(folder, name, "edit", path)
            if fd is None:
                return
            try:
                if not os.path.samestat(created, os.fstat(fd)):
                    raise Forbidden("not the file this save made")
                os.ftruncate(fd, 0)
            finally:
                os.close(fd)
            if os.path.samestat(created, os.stat(name, dir_fd=folder, follow_symlinks=False)):
                os.unlink(name, dir_fd=folder)
        finally:
            os.close(folder)


def _read_all(fd: int, limit: int | None = None, path: str = "") -> bytes:
    """All the bytes of the open file; with ``limit``, ``Invalid`` as soon as it has more (one byte past it is read)."""
    chunks, total = [], 0
    while chunk := os.read(fd, (1 << 20) if limit is None else min(1 << 20, limit + 1 - total)):
        chunks.append(chunk)
        total += len(chunk)
        if limit is not None and total > limit:
            raise Invalid([f"{path}: file too large (more than {limit} bytes)"])
    return b"".join(chunks)


def _read_file(root: Path, path: str, limit: int | None = None) -> bytes | None:
    """The bytes of the file through a verified handle; ``None`` when there is no file."""
    fd = _open_file(root, path, "read")
    if fd is None:
        return None
    try:
        return _read_all(fd, limit, path)
    finally:
        os.close(fd)


def read(worktree: Path, rel: str, editable: Sequence[str], max_bytes: int | None = None) -> FileView:
    """The file at ``rel`` (read-only files too, with their reason). ``FileNotFoundError`` when it is not a file.

    With ``max_bytes`` a file of more bytes is refused with ``Invalid`` ("file too large"); no more than
    ``max_bytes`` plus one byte is read of it.
    """
    path, _ = _locate(worktree, rel)
    try:
        data = _read_file(Path(worktree), path, max_bytes)
    except IsADirectoryError:
        data = None
    if data is None:
        raise FileNotFoundError(path or ".")
    ok, reason = is_editable(path, editable)
    return _view(path, data, ok, reason)


# --- write = commit ---------------------------------------------------------------------------------

_locks: dict[str, threading.Lock] = {}  # one per worktree
_locks_guard = threading.Lock()


def worktree_lock(worktree: Path) -> threading.Lock:
    """The lock that serialises whatever changes a worktree: saves (``write``) and catch-up resets.

    One lock per worktree, however its path is spelled; not reentrant. Whoever holds it can rely on no
    other save or reset touching the files, the index or the branch meanwhile.
    """
    key = os.path.normcase(os.path.realpath(worktree))
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


_no_hooks_dir: str | None = None
_no_hooks_guard = threading.Lock()


def empty_hooks_dir() -> str:
    """An empty folder for ``core.hooksPath``; made once per process, removed at exit."""
    global _no_hooks_dir
    with _no_hooks_guard:
        if _no_hooks_dir is None or not os.path.isdir(_no_hooks_dir):
            _no_hooks_dir = tempfile.mkdtemp(prefix="console-no-hooks-")
            atexit.register(shutil.rmtree, _no_hooks_dir, True)
        return _no_hooks_dir


def _admin_dir(real_root: str) -> str:
    """The admin dir git keeps for the unit worktree at ``real_root``, found from data outside the worktree only.

    A unit worktree is ``<data>/units/<id>/repo`` and the clone it belongs to ``<data>/source`` (the layout of
    ``units.py``). Git keeps one folder per linked worktree in ``<data>/source/.git/worktrees/``; its ``gitdir``
    file holds the path of that worktree's ``.git`` file. Exactly one of them must name ``real_root/.git``, else
    ``RuntimeError``. The worktree's own ``.git`` file is never read: anyone who can write in the worktree can
    rewrite it to point at another repo.
    """
    data = os.path.dirname(os.path.dirname(os.path.dirname(real_root)))
    wanted = os.path.normcase(os.path.join(real_root, ".git"))
    try:
        with os.scandir(os.path.join(data, "source", ".git", "worktrees")) as scan:
            entries = [entry for entry in scan if entry.is_dir(follow_symlinks=False)]
    except OSError:
        entries = []
    found = []
    for entry in entries:
        try:
            with open(os.path.join(entry.path, "gitdir"), encoding="utf-8") as f:
                named = f.read().strip()
            # git wrote the worktree path its own way (slashes, long names): compare real folders, not spellings
            where = os.path.join(os.path.realpath(os.path.dirname(named)), os.path.basename(named))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if os.path.normcase(where) == wanted:
            found.append(entry.path)
    if len(found) != 1:
        raise RuntimeError("not a unit worktree of this console: its git folder cannot be found")
    return found[0]


def _location(real_root: str) -> list[str]:
    return ["-C", real_root, f"--git-dir={_admin_dir(real_root)}", f"--work-tree={real_root}"]


def git_location(worktree: Path) -> list[str]:
    """The git options for a unit worktree: ``-C``, ``--git-dir`` (its admin dir, see ``_admin_dir``), ``--work-tree``.

    For every git call on a unit worktree (this module and ``units.py``), so that git is never pointed at a repo
    by the worktree's ``.git`` file. ``RuntimeError`` when ``worktree`` is not a unit worktree of this console.
    """
    return _location(os.path.realpath(worktree))


def _run_raw(
    worktree: Path,
    *args: str,
    stdin: str | bytes | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """``git args`` on the unit worktree ``worktree``: no shell, paths taken literally, never asks for input.

    Git gets the worktree's admin dir and work tree by name, from the real path pinned for the worktree (whose
    folder is checked to be the pinned one), never from its ``.git`` file. So the config it reads is the clone's;
    on top, every call is made with ``core.hooksPath`` pointing at an empty folder and ``core.fsmonitor`` off, and
    with the environment of ``procs.git_env`` (no tokens, no ``GIT_*`` variables of the console's, replace refs
    ignored) plus ``env``. ``stdin`` is text (UTF-8) or raw bytes. A call that takes longer than
    ``procs.GIT_TIMEOUT_S`` is killed: ``RuntimeError("git timed out: <subcommand>")``.
    """
    _pin_root(worktree, os.stat(worktree))
    location = _location(_pinned_root(worktree))
    data = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    git = ["git", "--literal-pathspecs", "-c", f"core.hooksPath={empty_hooks_dir()}", "-c", "core.fsmonitor=false"]
    return procs.run_git([*git, *location], args, stdin=data, env=env)


def _run(
    worktree: Path,
    *args: str,
    stdin: str | bytes | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """``_run_raw`` with the output as text (UTF-8, what cannot be decoded replaced)."""
    done = _run_raw(worktree, *args, stdin=stdin, env=env)
    return subprocess.CompletedProcess(
        done.args,
        done.returncode,
        done.stdout.decode("utf-8", errors="replace"),
        done.stderr.decode("utf-8", errors="replace"),
    )


def git_bytes(worktree: Path, *args: str, stdin: str | bytes | None = None) -> bytes:
    """Stdout of ``git args`` as it is (bytes), run as every git call of this module (see ``_run_raw``): for what
    git has committed (``ls-tree``, ``cat-file``), which must not be decoded as text. ``RuntimeError`` with git's
    stderr when it fails. The repo is the one of the unit worktree ``worktree``: a branch of another unit is in it too.
    """
    done = _run_raw(worktree, *args, stdin=stdin)
    if done.returncode != 0:
        why = done.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(why or f"git {args[0]} failed (exit {done.returncode})")
    return done.stdout


def _git(
    worktree: Path,
    *args: str,
    stdin: str | bytes | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Stdout of ``git args`` on the unit worktree (see ``_run``); ``RuntimeError`` with git's stderr when it fails."""
    done = _run(worktree, *args, stdin=stdin, env=env)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip() or f"git {args[0]} failed (exit {done.returncode})")
    return done.stdout


def _commit_bytes(root: Path, path: str, data: bytes, unit_id: str) -> None:
    """Commit ``data`` as the content of ``path`` on the current branch, without git ever reading the file.

    Plumbing only, on exactly the bytes that were written: ``hash-object`` takes them on stdin (``--path``
    makes git apply its usual filters for that name, so the blob is what ``git add`` would have made; when the
    HEAD version has CRLF, ``--no-filters`` keeps the bytes as they are, as ``git add`` does for a file whose
    indexed version has CRLF), a scratch index (in a console temp folder, not in the git dir) holds HEAD plus
    this one entry, ``commit-tree`` makes the commit, and ``update-ref`` moves the branch only if it is still
    where this started. Then the real index gets the same entry so that ``git status`` stays clean for the file;
    other staged entries are left alone. If the branch moved, nothing is committed; if the index cannot be
    updated (locked), the commit is taken back. A save that changes nothing makes no commit.
    """
    lines = _git(root, "rev-parse", "HEAD", "HEAD^{tree}").splitlines()
    if len(lines) != 2:
        raise RuntimeError("git rev-parse gave an unexpected answer")
    head, head_tree = lines
    listed = _git(root, "ls-tree", head, "--", path).split("\t", 1)[0].split(" ")  # mode, type, id
    in_head, crlf_in_head = listed[0], False
    if len(listed) == 3 and listed[1] == "blob":
        crlf_in_head = "\r\n" in _git(root, "cat-file", "blob", listed[2])
    filters = "--no-filters" if crlf_in_head else f"--path={path}"
    blob = _git(root, "hash-object", "-w", "--stdin", filters, stdin=data).strip()
    entry = f"{'100755' if in_head == '100755' else '100644'},{blob},{path}"
    with tempfile.TemporaryDirectory(prefix="console-index-") as scratch:
        scratch_index = {"GIT_INDEX_FILE": os.path.join(scratch, "index")}
        _git(root, "read-tree", head, env=scratch_index)
        _git(root, "update-index", "--add", "--cacheinfo", entry, env=scratch_index)
        tree = _git(root, "write-tree", env=scratch_index).strip()
    commit = None
    if tree != head_tree:
        message = f"console: {path} ({unit_id})\n\nConsole-Unit: {unit_id}\n"
        commit =_git(root, "commit-tree", tree, "-p", head, stdin=message).strip()
        _git(root, "update-ref", "-m", f"console: {path}", "HEAD", commit, head)
    try:
        _git(root, "update-index", "--add", "--cacheinfo", entry)
    except BaseException:  # the index is locked: take the commit back, so a failed save leaves nothing behind
        if commit is not None:
            with contextlib.suppress(Exception):
                _git(root, "update-ref", "-m", f"console: undo {path}", "HEAD", head, commit)
        raise


def _text_errors(path: str, content: object) -> list[str]:
    if not isinstance(content, str):
        return [f"{path}: content must be text"]
    try:
        size = len(content.replace("\r\n", "\n").encode("utf-8"))
    except UnicodeEncodeError:
        return [f"{path}: not valid text"]
    if "\0" in content:
        return [f"{path}: text files only (found a NUL character)"]
    if size > MAX_BYTES:
        return [f"{path}: larger than {MAX_BYTES // 1024} KB ({size} bytes)"]
    return []


def _check_parents(target: Path, path: str) -> None:
    """``Invalid`` when the nearest folder above a new file that exists is not a folder."""
    for parent in target.parents:
        if parent.exists():
            if not parent.is_dir():
                raise Invalid([f"{path}: {parent.name} is a file, not a folder"])
            break


def _open_current(
    root: Path, path: str, target: Path, ok: bool, reason: str, base_sha: str
) -> tuple[int | None, bytes | None, FileView]:
    """Open the file for editing (verified), read it, and check it is the version the save is based on.

    ``(fd, bytes, view)``; ``fd`` is ``None`` and the bytes too when there is no file yet.
    """
    try:
        fd = _open_file(root, path, "edit")
    except IsADirectoryError:
        raise Invalid([f"{path}: is a folder"]) from None
    try:
        existing = _read_all(fd) if fd is not None else None
        current = _view(path, existing, ok, reason)
        if current.sha != base_sha:
            raise Conflict(current)
        if current.binary:
            raise Invalid([f"{path}: binary file, cannot be edited here"])
        if fd is None:
            _check_parents(target, path)
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise
    return fd, existing, current


def _create_file(root: Path, path: str, ok: bool, reason: str) -> int:
    """Make the new file (verified fd). ``Conflict`` when it has appeared since the check."""
    try:
        fd = _open_file(root, path, "create")
    except FileExistsError:  # Windows says this too for a folder that has just become a link
        data = _read_file(root, path)
        if data is None:
            raise Forbidden(f"{path} changed while it was in use") from None
        raise Conflict(_view(path, data, ok, reason)) from None
    if fd is None:
        raise FileNotFoundError(path)
    return fd


def _overwrite(fd: int, data: bytes) -> None:
    """Replace the content of the open file, in place (so permissions and the file itself stay)."""
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]
    os.fsync(fd)


def _put_back(root: Path, path: str, before: bytes | None, created: os.stat_result | None) -> None:
    """Undo a save that failed after the file was touched: restore it, or empty and delete a new file.

    Goes through a verified handle like the save. An existing file is rewritten through it; a new file is
    only touched if the handle is the file this save made (handle to handle), emptied, and deleted through
    that handle (Windows) or by name inside the checked folder fd (POSIX); never by path.
    """
    with contextlib.suppress(Exception):
        if before is not None:
            fd = _open_file(root, path, "edit")
            if fd is not None:
                try:
                    _overwrite(fd, before)
                finally:
                    os.close(fd)
        elif created is not None:
            _remove_new_file(root, path, created)


def write(
    worktree: Path,
    rel: str,
    content: str,
    base_sha: str,
    editable: Sequence[str],
    unit_id: str,
    check: Callable[[str, str], list[str]],
    guard: Callable[[], str | None] | None = None,
) -> FileView:
    """Save ``content`` to ``rel`` as one commit on the worktree's branch; returns the saved file.

    Refused in this order: ``Forbidden`` (path guard, then read-only), ``Invalid`` (not text, over 200 KB, or
    ``check(path, content)`` returned errors), ``Conflict`` (the file on disk is not ``base_sha``; use ``""`` for
    a new file). The file keeps its line ends (CRLF when its first newline is CRLF, else LF) and a BOM; a new
    file is LF without BOM. It is written in place, through a handle checked to be the guarded file (see the
    module docstring), and committed from those same bytes, not from what is on disk afterwards. Saving what
    is already there makes no commit. ``Forbidden`` also when a link, a second name (hard link) or a replaced
    worktree folder is found while the save runs: nothing outside has then been read, and no existing file
    outside overwritten. ``RuntimeError`` when git fails or the file cannot be opened or written (``save
    failed``, e.g. another program holds it); the file is then put back as it was.

    ``guard`` is asked with the worktree lock held, before anything is read or written: an answer other than
    ``None`` (what holds the unit) refuses the save with ``Busy``. So a save cannot slip in after a run has taken
    the unit (a run takes the unit lock, then the worktree lock to read where the unit's branch is). The guard
    must not wait on the units store lock (``UnitStore.holder`` does not).
    """
    if not isinstance(unit_id, str) or not _UNIT_ID.fullmatch(unit_id):
        raise ValueError(f"unit id must be a plain name, got {unit_id!r}")
    path, target = _locate(worktree, rel)
    ok, reason = is_editable(path, editable)
    if not ok:
        raise Forbidden(f"{path} is read-only: {reason}")
    errors = _text_errors(path, content)
    text = content.replace("\r\n", "\n") if not errors else ""
    if not errors:
        errors = check(path, text)
    if errors:
        raise Invalid(list(errors))

    root = Path(worktree)
    with worktree_lock(root):
        holder = guard() if guard is not None else None
        if holder is not None:
            raise Busy(f"{unit_id} is in use: {holder}")
        try:
            fd, existing, current = _open_current(root, path, target, ok, reason, base_sha)
            created, touched = None, False
            try:
                try:
                    bom = codecs.BOM_UTF8 if existing is not None and existing.startswith(codecs.BOM_UTF8) else b""
                    data = bom + text.replace("\n", current.eol).encode("utf-8")
                    if fd is None:
                        fd = _create_file(root, path, ok, reason)
                        created = os.fstat(fd)
                    touched = True
                    if data != existing:
                        _overwrite(fd, data)
                finally:
                    if fd is not None:
                        os.close(fd)
                _commit_bytes(root, path, data, unit_id)
            except BaseException:
                if touched:
                    _put_back(root, path, existing, created)
                raise
        except OSError as err:
            raise RuntimeError("save failed") from err
    return _view(path, data, ok, reason)


# --- diff ---------------------------------------------------------------------------------------


def diff(worktree: Path, base_ref: str) -> str:
    """``git diff <base_ref>...HEAD``: what the unit's commits changed since ``base_ref`` (branch, tag or commit id).

    No external diff or textconv program runs. ``Invalid`` for anything that is not a plain ref name (nothing
    that git could take for an option); ``RuntimeError`` when git fails (an unknown ref, for one).
    """
    if not is_plain_ref(base_ref):
        raise Invalid([f"{base_ref!r} is not a usable ref"])
    return _git(Path(worktree), "diff", "--no-color", "--no-ext-diff", "--no-textconv", f"{base_ref}...HEAD", "--")


def unsaved_diff(worktree: Path) -> str:
    """``git diff HEAD``: what is changed in the worktree's tracked files and not saved (not committed). A command run
    from Today's "Commands on the stage" (a planted fault, a patch) leaves its change here; ``diff`` above shows only
    what was saved. No external diff or textconv program runs; ``RuntimeError`` when git fails."""
    return _git(Path(worktree), "diff", "--no-color", "--no-ext-diff", "--no-textconv", "HEAD", "--")


# --- a commit of files that are not in the worktree ----------------------------------------------


def commit_files(worktree: Path, entries: Mapping[str, bytes], message: str, parent: str) -> str:
    """The id of a new commit: the tree of commit ``parent`` plus ``entries`` (path -> exact bytes), with ``parent`` as
    its parent. For a handover run's overlay (``console.handover``).

    Made like a save (``_commit_bytes``) and for a caller that holds ``worktree_lock``: git never reads a file of the
    worktree, the bytes go in on stdin (``hash-object -w --no-filters``: stored as given, no line end change), the tree
    is made in a scratch index in a console temp folder (``read-tree``, ``update-index --index-info``, ``write-tree``)
    and ``commit-tree`` makes the commit (``message`` on stdin, identity from the repo's config). No ref, no index and
    no file of the worktree is touched: the caller points a branch at the commit (``update-ref``) and checks it out,
    and git writes the files itself, never through a link (a file or link where an entry needs a folder is replaced).

    Every path must be a plain relative path of the unit's files, spelled with ``/`` and as ``resolve`` would accept
    it (``Forbidden``: ``..``, ``.git``, a drive, a wildcard, a backslash, ``//``, an empty path, run or build output);
    each is stored as a regular file (mode 100644). ``ValueError`` for a ``parent`` that is not a commit id;
    ``RuntimeError`` when git fails.
    """
    if not isinstance(parent, str) or not _COMMIT_ID.fullmatch(parent):
        raise ValueError(f"parent must be a commit id, got {parent!r}")
    root = Path(worktree)
    for rel in entries:
        parts = _parts(rel)
        if not parts or "/".join(parts) != rel:
            raise Forbidden(f"{rel!r} is not a usable path")
        _check_not_hidden(root, parts)
    index_info = []
    for rel, data in entries.items():
        blob = _git(root, "hash-object", "-w", "--stdin", "--no-filters", stdin=data).strip()
        index_info.append(f"100644 {blob}\t{rel}\n")
    with tempfile.TemporaryDirectory(prefix="console-index-") as scratch:
        scratch_index = {"GIT_INDEX_FILE": os.path.join(scratch, "index")}
        _git(root, "read-tree", parent, env=scratch_index)
        if index_info:
            _git(root, "update-index", "--index-info", stdin="".join(index_info), env=scratch_index)
        tree = _git(root, "write-tree", env=scratch_index).strip()
    return _git(root, "commit-tree", tree, "-p", parent, stdin=message).strip()
