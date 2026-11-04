"""
Sandbox environment for skill / execute_code child processes.

The child must not inherit the MCP server's environment: MCP clients commonly
start servers with API keys and tokens in the environment, and any skill (or
any `execute_code` call) could read them from ``os.environ``. Instead the child
gets a small allowlist of variables that Python and common libraries need to
start and find temp/home directories, plus a few fixed settings.

Operators can add more names with ``SKILL_ORCHESTRATOR_ENV_PASSTHROUGH``
(comma-separated). That variable is read from the *server's* environment, so
an MCP client cannot set it through a tool call. Values are copied from the
server's environment; nothing is ever passed through by pattern.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Mapping, Optional, Tuple

PASSTHROUGH_ENV_VAR = "SKILL_ORCHESTRATOR_ENV_PASSTHROUGH"

# Needed on every platform.
_BASE_ALLOWLIST: Tuple[str, ...] = (
    "PATH",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
)

# POSIX: home/temp lookups (Path.home(), tempfile, user site-packages).
_POSIX_ALLOWLIST: Tuple[str, ...] = (
    "HOME",
    "TMPDIR",
)

# Windows: without SYSTEMROOT, Python cannot initialise sockets or random;
# TEMP/TMP feed tempfile; USERPROFILE/HOMEDRIVE/HOMEPATH feed Path.home();
# APPDATA/LOCALAPPDATA locate user site-packages.
_WINDOWS_ALLOWLIST: Tuple[str, ...] = (
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS",
)

# Always set in the child (override anything inherited or passed through).
FIXED_CHILD_ENV: Dict[str, str] = {
    # Child stdout/stderr are decoded as UTF-8 by the server on every platform.
    "PYTHONIOENCODING": "utf-8",
    # Skill runs live in a throwaway temp dir; don't litter it with .pyc files.
    "PYTHONDONTWRITEBYTECODE": "1",
}

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def default_allowlist(is_windows: Optional[bool] = None) -> Tuple[str, ...]:
    """Names copied from the server environment by default on this platform."""
    if is_windows is None:
        is_windows = os.name == "nt"
    return _BASE_ALLOWLIST + (_WINDOWS_ALLOWLIST if is_windows else _POSIX_ALLOWLIST)


def parse_passthrough(raw: Optional[str]) -> Tuple[List[str], List[str]]:
    """Split a passthrough setting into (valid names, rejected entries)."""
    names: List[str] = []
    rejected: List[str] = []
    for part in (raw or "").split(","):
        name = part.strip()
        if not name:
            continue
        if _ENV_NAME_RE.match(name):
            if name not in names:
                names.append(name)
        else:
            rejected.append(name)
    return names, rejected


def _lookup(env: Mapping[str, str], name: str, case_insensitive: bool) -> Optional[str]:
    if name in env:
        return env[name]
    if case_insensitive:
        upper = name.upper()
        for key, value in env.items():
            if key.upper() == upper:
                return value
    return None


def build_child_env(
    parent_env: Optional[Mapping[str, str]] = None,
    *,
    is_windows: Optional[bool] = None,
    allow_passthrough: bool = True,
) -> Dict[str, str]:
    """Return the complete environment for a sandboxed child process.

    Only allowlisted names (platform defaults plus the operator's
    ``SKILL_ORCHESTRATOR_ENV_PASSTHROUGH``) are copied from ``parent_env``
    (default: ``os.environ``). Everything else, including every secret, is
    dropped. ``allow_passthrough=False`` (restricted trust tier) ignores the
    operator passthrough and copies only the platform defaults.
    """
    if parent_env is None:
        parent_env = os.environ
    if is_windows is None:
        is_windows = os.name == "nt"

    passthrough, _ = (
        parse_passthrough(parent_env.get(PASSTHROUGH_ENV_VAR)) if allow_passthrough else ([], [])
    )
    child: Dict[str, str] = {}
    for name in (*default_allowlist(is_windows), *passthrough):
        value = _lookup(parent_env, name, case_insensitive=is_windows)
        if value is not None:
            child[name] = value
    child.update(FIXED_CHILD_ENV)
    return child


def describe() -> Dict[str, List[str]]:
    """What the child currently receives (names only, never values)."""
    passthrough, rejected = parse_passthrough(os.environ.get(PASSTHROUGH_ENV_VAR))
    return {
        "default": list(default_allowlist()),
        "passthrough": passthrough,
        "rejected": rejected,
        "fixed": sorted(FIXED_CHILD_ENV),
    }
