"""
Verified-save gate and skill lifecycle status.

Before `create_skill` / `update_skill` save code, the skill's test cases are
run in the same sandbox as `invoke_skill`. A skill whose tests fail is not
saved, unless the caller explicitly asks to keep it as a ``draft``.

Test cases come from two places:
- ``tests``: structured cases (parameters, context, optional expected result);
- ``examples`` that are JSON objects, run as parameters. Prose examples are
  only used for discovery.

Statuses stored on the skill:
- ``verified``: every test case passed when this version was saved/checked;
- ``unverified``: nothing could be run (no runnable cases, or unmet
  dependencies), so nothing is known;
- ``draft``: at least one case failed and the author saved it anyway.

``degraded`` is not stored. It is computed from telemetry when skills are
listed: the current version failed more than a threshold share of its recent
runs. Nothing is ever deleted automatically.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional

STATUS_VERIFIED = "verified"
STATUS_UNVERIFIED = "unverified"
STATUS_DRAFT = "draft"
STATUSES = (STATUS_VERIFIED, STATUS_UNVERIFIED, STATUS_DRAFT)

MAX_TEST_CASES = 10
MAX_FAILURE_CHARS = 2000

# Fields whose change can change a skill's behavior, so they trigger re-verification.
BEHAVIOR_FIELDS = ("code", "parameters_schema", "dependencies", "tests", "examples")


def skill_status(skill: Mapping[str, Any]) -> str:
    """Stored status, treating skills saved before the gate existed as unverified."""
    status = skill.get("status")
    return status if status in STATUSES else STATUS_UNVERIFIED


def cases_from_examples(examples: Optional[Iterable[Any]]) -> List[Dict[str, Any]]:
    """JSON-object examples become test cases (their object is the parameters)."""
    cases: List[Dict[str, Any]] = []
    for i, ex in enumerate(examples or []):
        if not isinstance(ex, str):
            continue
        text = ex.strip()
        if not text.startswith("{"):
            continue
        try:
            obj = json.loads(text)
        except ValueError:
            continue
        if isinstance(obj, dict):
            cases.append({"name": f"example {i + 1}", "parameters": obj})
    return cases


def collect_cases(skill: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """All runnable cases for a skill: its ``tests`` first, then JSON examples."""
    cases: List[Dict[str, Any]] = []
    for i, t in enumerate(skill.get("tests") or []):
        if isinstance(t, dict):
            case = dict(t)
            case.setdefault("name", f"test {i + 1}")
            cases.append(case)
    cases.extend(cases_from_examples(skill.get("examples")))
    return cases[:MAX_TEST_CASES]


def json_normalize(value: Any) -> Any:
    """Round-trip through JSON so results compare the way they are reported."""
    return json.loads(json.dumps(value, default=str))


def results_match(expected: Any, actual: Any) -> bool:
    try:
        return bool(json_normalize(expected) == json_normalize(actual))
    except (TypeError, ValueError):
        return False


@dataclass
class CaseResult:
    name: str
    passed: bool
    error: Optional[str] = None
    duration_ms: Optional[float] = None


@dataclass
class VerificationReport:
    """Outcome of running a skill's test cases (or why none ran)."""

    status: str
    cases: List[CaseResult] = field(default_factory=list)
    reason: Optional[str] = None
    checked_at: Optional[str] = None

    @property
    def failed(self) -> bool:
        return any(not c.passed for c in self.cases)

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    def summary_line(self) -> str:
        if not self.cases:
            return f"{self.status} ({self.reason or 'no runnable test cases'})"
        return f"{self.status} ({self.passed_count}/{len(self.cases)} test case(s) passed)"

    def failure_block(self) -> str:
        lines = []
        for c in self.cases:
            if not c.passed:
                err = (c.error or "failed")[:MAX_FAILURE_CHARS]
                lines.append(f"- **{c.name}:** {err}")
        return "\n".join(lines)

    def to_record(self) -> Dict[str, Any]:
        """Compact form stored on the skill (no long tracebacks)."""
        rec: Dict[str, Any] = {
            "status": self.status,
            "checked_at": self.checked_at,
            "cases": len(self.cases),
            "passed": self.passed_count,
        }
        if self.reason:
            rec["reason"] = self.reason
        failures = [
            {"case": c.name, "error": " ".join((c.error or "failed").split())[:300]}
            for c in self.cases
            if not c.passed
        ]
        if failures:
            rec["failures"] = failures
        return rec


def is_degraded(
    failed: int, executed: int, *, threshold: float, min_runs: int
) -> bool:
    """True when enough recent runs exist and more than `threshold` of them failed."""
    return executed >= max(1, min_runs) and executed > 0 and failed / executed > threshold
