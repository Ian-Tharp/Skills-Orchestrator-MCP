"""
Cross-process file locks.

Each MCP client (Claude Code, Cursor, ...) starts its own server process, and
they all share one ``~/.skill_orchestrator`` store. A ``threading.Lock`` only
serializes one process, so read-modify-write sequences (state commits, skill
updates, telemetry compaction) also take an OS-level lock on a lock file.

The lock is advisory (``fcntl.flock`` on POSIX, ``msvcrt.locking`` on
Windows) and not re-entrant: never nest two acquisitions of the same path.

Waiting blocks the calling thread, so async code should do its locked work
in a worker thread (``asyncio.to_thread``) rather than on the event loop.

If the file system doesn't support locking at all (some network or WSL
mounts), the lock degrades to the in-process thread lock and says so once on
stderr, instead of making every write wait for the timeout and fail.
"""

from __future__ import annotations

import errno
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, Optional

DEFAULT_TIMEOUT_SECONDS = 30.0
_POLL_SECONDS = 0.01

# errno values that mean "someone else holds the lock" (retry); anything else
# means locking isn't available here.
_BUSY_ERRNOS = {errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES}
if hasattr(errno, "EDEADLOCK"):
    _BUSY_ERRNOS.add(errno.EDEADLOCK)  # what msvcrt.locking reports when busy

_thread_locks: Dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()
_warned_unsupported = False


class LockUnsupported(OSError):
    """The file system refused to lock for a reason other than contention."""


def _thread_lock_for(key: str) -> threading.Lock:
    with _thread_locks_guard:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = _thread_locks[key] = threading.Lock()
        return lock


def _classify(e: OSError) -> bool:
    """True if `e` means busy; raise LockUnsupported if locking can't work here."""
    if e.errno in _BUSY_ERRNOS:
        return False
    raise LockUnsupported(e.errno, f"file locking not supported: {e}") from e


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as e:
            return _classify(e)
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            return _classify(e)
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _warn_unsupported(path: Path, e: OSError) -> None:
    global _warned_unsupported
    if not _warned_unsupported:
        _warned_unsupported = True
        print(
            f"[skill_orchestrator] warning: cannot lock {path} ({e}); "
            "falling back to in-process locking only",
            file=sys.stderr,
        )


class FileLock:
    """An exclusive lock on one lock file, shared by threads and processes."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._key = os.path.abspath(str(path))
        self._thread_lock = _thread_lock_for(self._key)
        self._fd: Optional[int] = None
        self._file_locked = False

    def acquire(self, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        """Block until held. One deadline covers both the thread and file waits."""
        deadline = time.monotonic() + timeout
        if not self._thread_lock.acquire(timeout=max(0.0, timeout)):
            raise TimeoutError(f"Timed out waiting for lock {self._path}")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._key, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                while True:
                    try:
                        if _try_lock(fd):
                            self._file_locked = True
                            break
                    except LockUnsupported as e:
                        _warn_unsupported(self._path, e)
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for lock {self._path}")
                    time.sleep(_POLL_SECONDS)
            except BaseException:
                os.close(fd)
                raise
            self._fd = fd
        except BaseException:
            self._thread_lock.release()
            raise

    def release(self) -> None:
        fd, self._fd = self._fd, None
        try:
            if fd is not None:
                try:
                    if self._file_locked:
                        _unlock(fd)
                finally:
                    self._file_locked = False
                    os.close(fd)
        finally:
            self._thread_lock.release()


@contextmanager
def file_lock(path: Path, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Iterator[None]:
    """Hold an exclusive lock on `path` (created if missing) for the block.

    Raises TimeoutError if the lock can't be taken within `timeout` seconds.
    """
    lock = FileLock(path)
    lock.acquire(timeout)
    try:
        yield
    finally:
        lock.release()
