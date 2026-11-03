"""
Atomic JSON file writes shared by every store.

All writes go through a temp file in the target's directory followed by
``os.replace``, so readers never see a partial file. On Windows,
``os.replace`` fails with ``PermissionError`` while another process (for
example a second MCP server starting up at the same moment) has the target
open, so the replace is retried briefly. A failed write never leaves its
temp file behind.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

_REPLACE_ATTEMPTS = 10


def write_temp_json(path: Path, data: Any) -> Path:
    """Write JSON to a temp file next to `path` (same dir, so a rename is atomic)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
    ) as tmp:
        tmp_path = Path(tmp.name)
        try:
            json.dump(data, tmp, indent=2, default=str)
            tmp.flush()
            os.fsync(tmp.fileno())
        except BaseException:
            tmp.close()
            tmp_path.unlink(missing_ok=True)
            raise
    return tmp_path


def replace_file(tmp_path: Path, path: Path) -> None:
    """``os.replace`` with a short retry for Windows sharing violations.

    On final failure the temp file is removed.
    """
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError:
            if attempt < _REPLACE_ATTEMPTS - 1:
                time.sleep(0.02 * (attempt + 1))
                continue
            tmp_path.unlink(missing_ok=True)
            raise
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically (temp file + rename, retried on Windows)."""
    replace_file(write_temp_json(path, data), path)
