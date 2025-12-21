from __future__ import annotations

import os

import pytest

from skill_orchestrator_mcp.server import _execute_code_internal


@pytest.mark.asyncio
async def test_execute_code_internal_enforces_timeout() -> None:
    # Arrange
    os.environ.pop("SKILL_ORCHESTRATOR_EXEC_MODE", None)  # ensure subprocess mode
    code = "import time\n\ntime.sleep(2)\nprint('done')\n"

    # Act
    output = await _execute_code_internal(code=code, context=None, timeout=1, capture_output=True)

    # Assert
    assert "Execution timed out" in output
    assert "process kill attempted" in output


@pytest.mark.asyncio
async def test_execute_code_internal_captures_stdout() -> None:
    # Arrange
    os.environ.pop("SKILL_ORCHESTRATOR_EXEC_MODE", None)  # ensure subprocess mode
    code = "print('ok')\n"

    # Act
    output = await _execute_code_internal(code=code, context=None, timeout=5, capture_output=True)

    # Assert
    assert "**Output:**" in output
    assert "ok" in output


