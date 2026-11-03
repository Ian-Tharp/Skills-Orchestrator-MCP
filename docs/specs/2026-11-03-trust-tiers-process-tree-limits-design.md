# Skill Orchestrator — Operator Trust Tiers, Process-Tree Kill, Resource Limits

**Date:** 2026-11-03
**Status:** Implemented
**Builds on:** 2026-11-03-sandbox-env-verified-save-portability-design.md

## Problem

The previous spec listed these as not covered:

- Processes started by a skill were not killed on timeout. Only the runner
  was killed (`proc.kill()`); its children kept running, and because they
  inherit the output pipes, `communicate()` could not finish either, so a
  skill that started a background process and returned normally was
  reported as a timeout.
- No memory, file-size or process limits beyond the wall-clock timeout.
- One fixed policy for every deployment: an operator serving a vetted skill
  library had no way to stop the agent running ad-hoc code.

## Design

### 1. Trust tiers (`trust_tiers.py`)

`SKILL_ORCHESTRATOR_TRUST_TIER` = `trusted` | `standard` (default) |
`restricted`, read from the server's environment on every run (tests and
operators can change it without a restart; MCP clients cannot change it).
An unknown value is treated as `restricted` (fail closed) and reported.

| | trusted | standard | restricted |
|---|---|---|---|
| memory_mb | none | 4096 | 1024 |
| file_size_mb | none | 1024 | 64 |
| allow_subprocesses | yes | yes | no |
| code-changing tools | allowed | allowed | refused |
| skills that run | all | all | verified only |
| env passthrough / in-process mode | honored | honored | ignored |
| unenforceable limit | warning | warning | run refused |

Refused tools in `restricted`: `execute_code`, `create_skill`,
`update_skill`, `import_skill`, `rollback_skill`, `delete_skill`.
`invoke_skill` rejects non-verified skills (recorded in telemetry as a
rejection), and the composition registry sent to the child holds only
verified skills, so nested calls cannot reach others. `verify_skill` stays
available.

`SKILL_ORCHESTRATOR_MEMORY_LIMIT_MB`, `SKILL_ORCHESTRATOR_FILE_SIZE_LIMIT_MB`
(`0`/`off` = none) and `SKILL_ORCHESTRATOR_ALLOW_SUBPROCESSES` override one
limit each. They never change a tier's tool policy. Invalid values are
ignored and listed by `get_orchestrator_status`.

Defaults were picked to not break existing users on `standard`: 4 GB of
address space leaves room for numpy/pandas thread pools and malloc arenas
(RLIMIT_AS counts reserved, not used, memory).

### 2. Process-tree kill (`process_tree.py`)

- POSIX: `start_new_session=True`; kill with `killpg(pgid, SIGKILL)`.
- Windows: a job object per run with `KILL_ON_JOB_CLOSE` and
  `DIE_ON_UNHANDLED_EXCEPTION`, no breakaway; kill with
  `TerminateJobObject`. If the job cannot be created, `taskkill /T /F` is
  the fallback and the run carries a warning (or is refused in
  `restricted`).
- The tree is killed when every run ends, not only on timeout, so leftover
  background processes cannot outlive the run or hold its pipes.
- Exit detection polls `proc.returncode` instead of awaiting `proc.wait()`
  or `communicate()`: on CPython 3.11 and 3.12, `wait()` does not return
  while a grandchild holds the child's pipes (3.10 and 3.13 return).
  Output is read by separate tasks, collected after the kill with the
  termination grace period.
- Windows start gate: a process cannot be created inside a job with
  asyncio, so the runner waits for `start.gate` in its temp dir, written
  only after the child is assigned. No skill code runs outside the job.

### 3. Resource limits

- POSIX: the runner calls `setrlimit` (soft and hard, never above an
  existing hard limit) before common imports or skill code:
  `RLIMIT_AS`, `RLIMIT_FSIZE`, and `RLIMIT_NPROC=0` for no subprocesses.
  CPython ignores `SIGXFSZ`, so an oversized write raises `OSError(EFBIG)`.
  `RLIMIT_NPROC` is per user and ignored for root; the runner reports it as
  not applied when running as root.
- Windows: the job object sets `JobMemoryLimit` (whole tree) and
  `ActiveProcessLimit=1`, or 2 when `python.exe` is a venv launcher
  (stdlib venv redirector, uv trampoline), which starts the base interpreter
  as a child before any skill code runs. File size is not enforced.
- Limits that could not be applied come back in the result payload
  (`limit_failures`) and are shown as a sandbox warning, or, when the tier
  requires limits, the runner refuses before running any code
  (`SandboxLimitError`).

### 4. Status

`get_orchestrator_status` gains a **Trust Tier** section: tier (and any
invalid value), each limit with how it is enforced on this platform, the
refused tools, and ignored settings.

## Not covered

- Limits are guard rails against runaway code, not a security boundary:
  same-user code can leave its process group with `setsid()` on POSIX
  (unless `restricted` blocks new processes) or reopen its job on Windows.
- No CPU-time limit beyond the wall-clock timeout; no disk quota.
- Filesystem and network isolation still need a container, VM or OS
  sandbox.
- macOS uses the POSIX path but is not run in CI.
