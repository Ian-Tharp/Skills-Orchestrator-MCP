"""
Cross-process file locks.

Each MCP client (Claude Code, Cursor, ...) starts its own server process, and
they all share one ``~/.skill_orchestrator`` store. A ``threading.Lock`` only
serializes one process, so read-modify-write sequences (state commits, skill
updates, telemetry compaction) also take an OS-level lock on a lock file.

The lock is advisory (``fcntl.flock`` on POSIX, ``msvcrt.locking`` on
Windows) and not re-entrant: never nest two acquisitions of the same path.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator

DEFAULT_TIMEOUT_SECONDS = 30.0
_POLL_SECONDS = 0.01

_thread_locks: Dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


def _thread_lock_for(key: str) -> threading.Lock:
    with _thread_locks_guard:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = _thread_locks[key] = threading.Lock()
        return lock


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def file_lock(path: Path, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Iterator[None]:
    """Hold an exclusive lock on `path` (created if missing) for the block.

    Raises TimeoutError if the lock can't be taken within `timeout` seconds.
    """
    key = os.path.abspath(str(path))
    thread_lock = _thread_lock_for(key)
    if not thread_lock.acquire(timeout=timeout):
        raise TimeoutError(f"Timed out waiting for lock {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(key, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            deadline = time.monotonic() + timeout
            while not _try_lock(fd):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for lock {path}")
                time.sleep(_POLL_SECONDS)
            try:
                yield
            finally:
                _unlock(fd)
        finally:
            os.close(fd)
    finally:
        thread_lock.release()
