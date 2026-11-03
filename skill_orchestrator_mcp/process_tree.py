"""
Process-tree control for skill / execute_code runs.

Every run is started as the root of its own process tree, and the whole tree
is killed when the run ends, whether it finished, failed or timed out:

- POSIX: the child starts a new session (``start_new_session=True``), so it
  and everything it starts share one process group, killed with
  ``killpg(SIGKILL)``. A process that leaves the group on purpose
  (``setsid()``/``setpgid()``) escapes; the restricted tier blocks new
  processes altogether.
- Windows: the child is put in a job object, which every process it starts
  joins automatically (breakaway is not allowed). ``TerminateJobObject``
  kills them all, and the job's kill-on-close flag kills them if the server
  itself dies. The job also enforces the memory and process-count limits.
  If a job cannot be created, ``taskkill /T /F`` is the fallback.

On Windows the child cannot be created already inside the job, so the
runner waits for a start-gate file that is written only after the child has
been assigned to the job. No skill code runs outside the job.

POSIX resource limits are applied by the runner itself (``setrlimit``)
before any skill code runs; see ``_RUNNER_SOURCE`` in ``server.py``.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .trust_tiers import ResourceLimits

START_GATE_NAME = "start.gate"


class ProcessTree:
    """One run's process tree. Create, spawn with ``popen_kwargs()``, ``attach()``, ``kill()``."""

    def __init__(self, limits: ResourceLimits, work_dir: Path) -> None:
        self.limits = limits
        self.work_dir = work_dir
        self.warnings: List[str] = []
        self._pid: Optional[int] = None
        self._job: Optional[int] = None  # Windows job handle
        self._attached = False

    # ------------------------------------------------------------------ spawn

    @property
    def start_gate(self) -> Optional[str]:
        """Path the runner must wait for before running code (Windows only)."""
        if sys.platform == "win32":
            return str(self.work_dir / START_GATE_NAME)
        else:
            return None

    def popen_kwargs(self) -> Dict[str, Any]:
        if sys.platform == "win32":
            flags = 0
            flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            return {"creationflags": flags}
        else:
            return {"start_new_session": True}

    def attach(self, pid: int) -> Optional[str]:
        """Bind the started child to this tree. Returns an error, or None.

        On Windows this creates the job, applies limits, assigns the child,
        then opens the start gate. On failure the gate is still opened (the
        caller decides whether to kill the child or run with a warning) and
        the error says which limits are not enforced.
        """
        self._pid = pid
        error: Optional[str] = None
        if sys.platform == "win32":
            try:
                self._job = _win_create_job(self.limits)
                _win_assign(self._job, pid)
                self._attached = True
            except OSError as e:
                if self._job is not None:
                    _win_close(self._job)
                    self._job = None
                unenforced = []
                if self.limits.memory_mb is not None:
                    unenforced.append("memory")
                if not self.limits.allow_subprocesses:
                    unenforced.append("subprocesses")
                error = (
                    f"could not create a job object for the run ({e}); "
                    "child processes are killed with taskkill instead"
                    + (f", and these limits are not enforced: {', '.join(unenforced)}"
                       if unenforced else "")
                )
                self.warnings.append(error)
            gate = self.start_gate
            if gate is not None:
                Path(gate).write_text("go", encoding="utf-8")
        else:
            self._attached = True
        return error

    # ------------------------------------------------------------------- kill

    async def kill(self) -> None:
        """Kill every process in the tree that is still running. Idempotent."""
        if self._pid is None:
            return
        if sys.platform == "win32":
            if self._job is not None:
                _win_terminate(self._job)
            else:
                await _taskkill(self._pid)
        else:
            try:
                os.killpg(self._pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # Group already gone (every member exited).
                pass

    def close(self) -> None:
        """Kill anything left in the tree and release OS handles. Synchronous,
        so it also runs when the calling task is cancelled.
        """
        if sys.platform == "win32":
            if self._job is not None:
                # KILL_ON_JOB_CLOSE ends every process still in the job.
                _win_close(self._job)
                self._job = None
        elif self._pid is not None:
            try:
                os.killpg(self._pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


async def wait_for_exit(proc: "asyncio.subprocess.Process", timeout: float) -> bool:
    """Wait until the root process exits; True if it did within ``timeout``.

    Polls ``returncode`` rather than awaiting ``proc.wait()``: on Python 3.11
    and 3.12, ``wait()`` does not return while a grandchild still holds the
    child's stdout/stderr pipes, and those grandchildren are exactly what the
    tree kill is for.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    waiter = asyncio.ensure_future(proc.wait())
    try:
        while proc.returncode is None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.wait({waiter}, timeout=min(0.05, remaining))
        return True
    finally:
        if not waiter.done():
            waiter.cancel()


async def _taskkill(pid: int) -> None:
    def _run() -> None:
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    await asyncio.to_thread(_run)


# ----------------------------------------------------------------- Windows

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
    _JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
    _JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def _last_error(what: str) -> OSError:
        code = ctypes.get_last_error()
        return OSError(code, f"{what} failed: {ctypes.FormatError(code).strip()}")

    def _win_create_job(limits: ResourceLimits) -> int:
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            raise _last_error("CreateJobObject")
        info = _ExtendedLimits()
        flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        if limits.memory_mb is not None:
            flags |= _JOB_OBJECT_LIMIT_JOB_MEMORY
            info.JobMemoryLimit = limits.memory_mb * 1024 * 1024
        if not limits.allow_subprocesses:
            flags |= _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            info.BasicLimitInformation.ActiveProcessLimit = 1
        info.BasicLimitInformation.LimitFlags = flags
        if not _kernel32.SetInformationJobObject(
            job, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
        ):
            err = _last_error("SetInformationJobObject")
            _kernel32.CloseHandle(job)
            raise err
        return int(job)

    def _win_assign(job: int, pid: int) -> None:
        handle = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not handle:
            raise _last_error("OpenProcess")
        try:
            if not _kernel32.AssignProcessToJobObject(job, handle):
                raise _last_error("AssignProcessToJobObject")
        finally:
            _kernel32.CloseHandle(handle)

    def _win_terminate(job: int) -> None:
        _kernel32.TerminateJobObject(job, 1)

    def _win_close(job: int) -> None:
        _kernel32.CloseHandle(job)
