"""
Operator trust tiers: how much the server trusts the code it is asked to run.

The tier is chosen by the operator with ``SKILL_ORCHESTRATOR_TRUST_TIER`` in
the *server's* environment. MCP clients cannot change it through a tool call.
It is read on every run, like the env passthrough.

- ``trusted``: no resource limits (timeout and process-tree kill only).
- ``standard`` (default): resource limits sized for ordinary skills.
- ``restricted``: tight limits, no subprocesses, no env passthrough, and a
  read-only skill library: only verified skills run, and the tools that add
  or change code (``execute_code``, ``create_skill``, ``update_skill``,
  ``import_skill``, ``rollback_skill``, ``delete_skill``) are refused.

An unknown tier value falls back to ``restricted`` (fail closed) and is
reported by ``get_orchestrator_status``.

The tier's resource limits can be adjusted one at a time with
``SKILL_ORCHESTRATOR_MEMORY_LIMIT_MB``, ``SKILL_ORCHESTRATOR_FILE_SIZE_LIMIT_MB``
(``0`` or ``off`` = no limit) and ``SKILL_ORCHESTRATOR_ALLOW_SUBPROCESSES``
(``on``/``off``). The tool policy of a tier cannot be adjusted.

Limits are guard rails against runaway or buggy code, not a security
boundary: skills run as the server's OS user (see README, Security).
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

TRUST_TIER_ENV = "SKILL_ORCHESTRATOR_TRUST_TIER"
MEMORY_LIMIT_ENV = "SKILL_ORCHESTRATOR_MEMORY_LIMIT_MB"
FILE_SIZE_LIMIT_ENV = "SKILL_ORCHESTRATOR_FILE_SIZE_LIMIT_MB"
ALLOW_SUBPROCESSES_ENV = "SKILL_ORCHESTRATOR_ALLOW_SUBPROCESSES"

TIER_TRUSTED = "trusted"
TIER_STANDARD = "standard"
TIER_RESTRICTED = "restricted"
TIERS: Tuple[str, ...] = (TIER_TRUSTED, TIER_STANDARD, TIER_RESTRICTED)
DEFAULT_TIER = TIER_STANDARD

_OFF = ("0", "off", "false", "no", "none", "unlimited")
_ON = ("1", "on", "true", "yes")


@dataclass(frozen=True)
class ResourceLimits:
    """Limits applied to each skill / execute_code process tree.

    ``None`` means no limit. Enforcement differs by platform:

    - ``memory_mb``: POSIX ``RLIMIT_AS`` per process; Windows job object
      limit on the committed memory of the whole process tree.
    - ``file_size_mb``: POSIX ``RLIMIT_FSIZE`` (largest file a process may
      write). Not enforced on Windows.
    - ``allow_subprocesses=False``: POSIX ``RLIMIT_NPROC=0`` (on Linux this
      also blocks new threads; ignored by the kernel for root); Windows job
      object active-process limit of 1.
    """

    memory_mb: Optional[int]
    file_size_mb: Optional[int]
    allow_subprocesses: bool

    @property
    def any(self) -> bool:
        return (
            self.memory_mb is not None
            or self.file_size_mb is not None
            or not self.allow_subprocesses
        )

    def to_payload(self) -> Dict[str, Any]:
        return asdict(self)


TIER_LIMITS: Dict[str, ResourceLimits] = {
    TIER_TRUSTED: ResourceLimits(memory_mb=None, file_size_mb=None, allow_subprocesses=True),
    TIER_STANDARD: ResourceLimits(memory_mb=4096, file_size_mb=1024, allow_subprocesses=True),
    TIER_RESTRICTED: ResourceLimits(memory_mb=1024, file_size_mb=64, allow_subprocesses=False),
}

# Tools refused in the restricted tier: everything that adds or changes code.
RESTRICTED_TOOLS: Tuple[str, ...] = (
    "execute_code",
    "create_skill",
    "update_skill",
    "import_skill",
    "rollback_skill",
    "delete_skill",
)


@dataclass(frozen=True)
class TrustPolicy:
    tier: str
    limits: ResourceLimits
    # The raw tier value when it was not a known tier (policy is restricted).
    invalid_tier: Optional[str] = None
    # Override values that could not be parsed (name -> raw value); ignored.
    invalid_overrides: Tuple[Tuple[str, str], ...] = ()

    @property
    def restricted(self) -> bool:
        return self.tier == TIER_RESTRICTED

    @property
    def allow_env_passthrough(self) -> bool:
        return not self.restricted

    @property
    def allow_inprocess(self) -> bool:
        return not self.restricted

    @property
    def verified_skills_only(self) -> bool:
        return self.restricted

    @property
    def limits_required(self) -> bool:
        """Refuse to run when a requested limit cannot be enforced (fail closed)."""
        return self.restricted

    def allows_tool(self, tool: str) -> bool:
        return not (self.restricted and tool in RESTRICTED_TOOLS)

    def describe_tier(self) -> str:
        if self.invalid_tier is not None:
            return (
                f"{self.tier} ({TRUST_TIER_ENV}={self.invalid_tier!r} is not one of "
                f"{', '.join(TIERS)}; using restricted)"
            )
        return self.tier

    def refusal(self, tool: str) -> str:
        return (
            f"Error: {tool} is disabled: this server runs in the "
            f"{self.describe_tier()} trust tier, where only verified skills already in "
            "the library can run and the library cannot be changed. The server operator "
            f"sets {TRUST_TIER_ENV}."
        )


def _parse_mb(raw: str) -> Optional[Tuple[Optional[int]]]:
    """Parse a size override. Returns (value,) or None if invalid."""
    value = raw.strip().lower()
    if value in _OFF:
        return (None,)
    try:
        n = int(value)
    except ValueError:
        return None
    if n < 0:
        return None
    return (n or None,)


def current_policy(env: Optional[Mapping[str, str]] = None) -> TrustPolicy:
    """The policy for the next run, from the server's environment."""
    if env is None:
        env = os.environ
    raw_tier = env.get(TRUST_TIER_ENV)
    invalid_tier: Optional[str] = None
    if raw_tier is None or not raw_tier.strip():
        tier = DEFAULT_TIER
    else:
        tier = raw_tier.strip().lower()
        if tier not in TIERS:
            invalid_tier = raw_tier
            tier = TIER_RESTRICTED

    base = TIER_LIMITS[tier]
    memory_mb = base.memory_mb
    file_size_mb = base.file_size_mb
    allow_subprocesses = base.allow_subprocesses
    invalid: list[Tuple[str, str]] = []

    raw = env.get(MEMORY_LIMIT_ENV)
    if raw is not None and raw.strip():
        parsed = _parse_mb(raw)
        if parsed is None:
            invalid.append((MEMORY_LIMIT_ENV, raw))
        else:
            memory_mb = parsed[0]

    raw = env.get(FILE_SIZE_LIMIT_ENV)
    if raw is not None and raw.strip():
        parsed = _parse_mb(raw)
        if parsed is None:
            invalid.append((FILE_SIZE_LIMIT_ENV, raw))
        else:
            file_size_mb = parsed[0]

    raw = env.get(ALLOW_SUBPROCESSES_ENV)
    if raw is not None and raw.strip():
        flag = raw.strip().lower()
        if flag in _ON:
            allow_subprocesses = True
        elif flag in _OFF:
            allow_subprocesses = False
        else:
            invalid.append((ALLOW_SUBPROCESSES_ENV, raw))

    return TrustPolicy(
        tier=tier,
        limits=ResourceLimits(
            memory_mb=memory_mb,
            file_size_mb=file_size_mb,
            allow_subprocesses=allow_subprocesses,
        ),
        invalid_tier=invalid_tier,
        invalid_overrides=tuple(invalid),
    )


def describe_limits(limits: ResourceLimits, *, is_windows: Optional[bool] = None) -> Dict[str, str]:
    """Human-readable limit -> enforcement on this platform."""
    if is_windows is None:
        is_windows = os.name == "nt"
    out: Dict[str, str] = {}
    if limits.memory_mb is None:
        out["memory"] = "no limit"
    elif is_windows:
        out["memory"] = f"{limits.memory_mb} MB for the whole process tree (job object)"
    else:
        out["memory"] = f"{limits.memory_mb} MB address space per process (RLIMIT_AS)"
    if limits.file_size_mb is None:
        out["file size"] = "no limit"
    elif is_windows:
        out["file size"] = f"{limits.file_size_mb} MB requested; not enforced on Windows"
    else:
        out["file size"] = f"{limits.file_size_mb} MB per file written (RLIMIT_FSIZE)"
    if limits.allow_subprocesses:
        out["subprocesses"] = "allowed (killed with the run)"
    elif is_windows:
        out["subprocesses"] = "blocked (job object active-process limit; venv launcher counted)"
    else:
        root = hasattr(os, "geteuid") and os.geteuid() == 0
        out["subprocesses"] = "blocked (RLIMIT_NPROC=0; also blocks threads on Linux)" + (
            "; NOT enforceable: the server runs as root" if root else ""
        )
    return out
