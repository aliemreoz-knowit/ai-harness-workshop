"""Replace a file in one step, so that a reader (or a crash) sees the old file or the new one, never half of it."""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from pathlib import Path

__all__ = ["atomic_write"]

# Windows refuses os.replace while another process has the target open: try again briefly.
_REPLACE_TRIES = 10
_REPLACE_WAIT_S = 0.05


def atomic_write(path: Path, data: bytes | str) -> None:
    """Write ``data`` (text is UTF-8 with ``\\n`` line ends) to ``path``, replacing what is there; ``OSError`` on failure.

    The folder is made when it is missing. The data goes to a temporary file beside ``path``, is flushed to the disk
    and moved into place. ``tempfile.mkstemp`` creates that file readable by its owner only (where the OS supports
    it), and so the file at ``path`` is too, whatever the file it replaces allowed. A failed write leaves no
    temporary file behind.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = data.encode("utf-8") if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(_REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == _REPLACE_TRIES - 1:
                    raise
                time.sleep(_REPLACE_WAIT_S)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
