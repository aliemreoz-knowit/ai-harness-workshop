"""Download the Monaco editor into ``web/vendor/monaco`` (the console has no build step and no npm at runtime).

    python scripts/vendor_monaco.py            download, verify and install (does nothing if it is installed already)
    python scripts/vendor_monaco.py --check    only look: exit 0 if the pinned version is installed, else 1; no network

What it does, in this order, and why:

1. Asks the npm registry for the metadata of the pinned version and reads ``dist.integrity`` (a sha512).
2. Downloads the tarball (about 5 MB) into memory and compares its sha512 with that integrity value. A mismatch
   stops everything: nothing from the tarball is written to disk.
3. Checks every member name (no absolute path, no ``..``, no backslash, no colon, no link) and extracts
   only ``package/min/vs/**``, into a staging folder next to the target. Files are written from bytes this script
   reads; the tarball never extracts itself.
4. Writes ``VERSION`` (the version, then the integrity) last, then swaps the staging folder in for the old install.

``web/vendor/`` is git-ignored: every machine runs this once. Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import io
import json
import shutil
import sys
import tarfile
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Callable, Sequence

VERSION = "0.52.2"
META_URL = f"https://registry.npmjs.org/monaco-editor/{VERSION}"
TARBALL_URL = f"https://registry.npmjs.org/monaco-editor/-/monaco-editor-{VERSION}.tgz"
DEST = Path(__file__).resolve().parents[1] / "web" / "vendor" / "monaco"

_PREFIX = "package/min/"  # what is installed is _PREFIX + "vs/**", as "vs/**" below the destination
_KEEP = _PREFIX + "vs/"
REQUIRED = (  # what the console needs to start the editor and its workers
    "vs/loader.js",
    "vs/editor/editor.main.js",
    "vs/editor/editor.main.css",
    "vs/base/worker/workerMain.js",
)
_MAX_DOWNLOAD = 64 * 1024 * 1024  # bytes: the tarball is about 5 MB
_MAX_UNPACKED = 256 * 1024 * 1024  # bytes: what min/vs unpacks to is about 30 MB
_TIMEOUT_S = 60


class VendorError(Exception):
    """Something that stops the install, in words for the person who ran the script."""


# --- looking ---------------------------------------------------------------------------------------


def check(dest: Path) -> list[str]:
    """What is wrong with the install in ``dest`` (empty list: the pinned version is installed and complete)."""
    problems = []
    try:
        recorded = (dest / "VERSION").read_text(encoding="utf-8").split()
    except OSError:
        return [f"{dest} has no VERSION file: the editor is not installed"]
    if not recorded or recorded[0] != VERSION:
        problems.append(f"installed version is {recorded[0] if recorded else 'unknown'}, this console wants {VERSION}")
    for rel in REQUIRED:
        target = dest / rel
        if not target.is_file() or target.stat().st_size == 0:
            problems.append(f"{rel} is missing or empty")
    return problems


# --- downloading -------------------------------------------------------------------------------------


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to another https address (urllib would also follow one to http or ftp)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise VendorError(f"refusing a redirect to a non-https address: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HttpsOnly)


def _fetch(url: str) -> bytes:
    """The body of ``url`` (https only, redirects too; at most ``_MAX_DOWNLOAD`` bytes)."""
    if not url.startswith("https://"):
        raise VendorError(f"refusing a non-https address: {url}")
    try:
        with _OPENER.open(url, timeout=_TIMEOUT_S) as response:
            data = response.read(_MAX_DOWNLOAD + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise VendorError(f"could not download {url}: {err}") from None
    if len(data) > _MAX_DOWNLOAD:
        raise VendorError(f"{url} is larger than {_MAX_DOWNLOAD // 2**20} MB: not downloading it")
    return data


def verify_integrity(data: bytes, integrity: str) -> None:
    """``VendorError`` unless ``data`` has the sha512 named in ``integrity`` (``sha512-<base64>``, entries split by spaces)."""
    expected = next((entry[len("sha512-") :] for entry in integrity.split() if entry.startswith("sha512-")), "")
    if not expected:
        raise VendorError(f"the registry gave no sha512 integrity value (got {integrity!r})")
    actual = base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
    if not hmac.compare_digest(actual.encode("ascii"), expected.encode("ascii", "replace")):
        raise VendorError("the downloaded file does not match the sha512 the registry published: not installing it")


def _integrity_from(meta: bytes) -> str:
    try:
        info = json.loads(meta)
        integrity = info["dist"]["integrity"]
        version = info["version"]
    except (ValueError, KeyError, TypeError):
        raise VendorError(f"the registry answer for monaco-editor {VERSION} is not what was expected") from None
    if version != VERSION or not isinstance(integrity, str):
        raise VendorError(f"the registry answer for monaco-editor {VERSION} is not what was expected")
    return integrity


# --- unpacking ---------------------------------------------------------------------------------------


def _check_member(member: tarfile.TarInfo) -> None:
    name = member.name
    parts = name.split("/")
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or "\0" in name
        or ":" in name  # a drive letter, or an NTFS stream
        or any(part == ".." for part in parts)
    ):
        raise VendorError(f"the tarball has a member that could write outside the target: {name!r}")
    if name.startswith(_KEEP) and not (member.isfile() or member.isdir()):
        raise VendorError(f"the tarball has a link or special file in the editor tree: {name!r}")


def extract(data: bytes, dest: Path) -> int:
    """Write ``package/min/vs/**`` of the .tgz ``data`` as ``vs/**`` below ``dest``; the number of files written.

    Every member is checked before the first file is written (``VendorError``, nothing written).
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            members = tar.getmembers()
            for member in members:
                _check_member(member)
            wanted = [m for m in members if m.isfile() and m.name.startswith(_KEEP)]
            if sum(m.size for m in wanted) > _MAX_UNPACKED:
                raise VendorError("the tarball unpacks to more than expected: not installing it")
            root = dest.resolve()
            written = 0
            for member in wanted:
                target = (root / member.name[len(_PREFIX) :]).resolve()
                if not target.is_relative_to(root):
                    raise VendorError(f"the tarball has a member that could write outside the target: {member.name!r}")
                source = tar.extractfile(member)
                if source is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as out:
                    shutil.copyfileobj(source, out)
                written += 1
    except (tarfile.TarError, EOFError, zlib.error, OSError) as err:  # a damaged download, or the disk
        raise VendorError(f"could not unpack the downloaded file: {err}") from None
    return written


# --- installing ----------------------------------------------------------------------------------------


def install(dest: Path, fetch: Callable[[str], bytes] = _fetch) -> None:
    """Download, verify and install the pinned version into ``dest`` (replacing what is there)."""
    integrity = _integrity_from(fetch(META_URL))
    data = fetch(TARBALL_URL)
    verify_integrity(data, integrity)

    staging = dest.with_name(dest.name + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        if extract(data, staging) == 0:
            raise VendorError("the tarball holds no package/min/vs files")
        (staging / "VERSION").write_text(f"{VERSION}\n{integrity}\n", encoding="utf-8")
        problems = check(staging)
        if problems:
            raise VendorError("the tarball is incomplete: " + "; ".join(problems))
        old = dest.with_name(dest.name + ".old")
        shutil.rmtree(old, ignore_errors=True)
        had_old = dest.exists()
        if had_old:
            dest.rename(old)
        try:
            staging.rename(dest)
        except OSError:
            if had_old:  # put the working install back
                try:
                    old.rename(dest)
                except OSError:
                    raise VendorError(f"could not install into {dest}; the previous install is in {old}") from None
            raise
        shutil.rmtree(old, ignore_errors=True)
    except OSError as err:
        raise VendorError(f"could not install into {dest}: {err}") from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Install the Monaco editor {VERSION} for the console's web pages.")
    parser.add_argument("--check", action="store_true", help="only check that it is installed (no network); exit 0 or 1")
    parser.add_argument("--dest", type=Path, default=DEST, metavar="DIR", help=f"install folder (default: {DEST})")
    args = parser.parse_args(argv)

    problems = check(args.dest)
    if args.check:
        if problems:
            print("Monaco is not ready: run  python scripts/vendor_monaco.py", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        print(f"monaco-editor {VERSION} is installed in {args.dest}")
        return 0
    if not problems:
        print(f"monaco-editor {VERSION} is installed already in {args.dest}")
        return 0
    try:
        print(f"Downloading monaco-editor {VERSION} from registry.npmjs.org ...", flush=True)
        install(args.dest)
    except VendorError as err:
        print(f"vendor_monaco: {err}", file=sys.stderr)
        return 1
    print(f"Installed monaco-editor {VERSION} into {args.dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
