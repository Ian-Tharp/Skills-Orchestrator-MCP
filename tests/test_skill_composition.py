"""Tests for the skill composition system.

These tests verify that skills can invoke other skills with proper:
- Call depth tracking (__call_depth__)
- Parent skill tracking (__parent_skill__)
- Circular call detection
- Max depth limiting
- Parameter passing between skills
- Helper functions (list_available_skills, get_skill_info)
"""
from __future__ import annotations

import os
from typing import Any, Dict
from unittest.mock import patch

import pytest

from skill_orchestrator_mcp.server import _execute_code_internal


def _make_skill_registry(*skills: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Helper to create a mock skill registry from skill definitions."""
    return {s["name"]: s for s in skills}


@pytest.fixture(autouse=True)
def subprocess_mode() -> None:
    """Ensure subprocess execution mode for all tests."""
    os.environ.pop("SKILL_ORCHESTRATOR_EXEC_MODE", None)


class TestCallDepthTracking:
    """Tests for __call_depth__ context variable."""

    @pytest.mark.asyncio
    async def test_top_level_skill_has_depth_one(self) -> None:
        # Arrange
        registry = _make_skill_registry(
            {
                "name": "level_one",
                "code": "__result__ = {'depth': __call_depth__}",
                "description": "Reports its call depth",
            }
        )
        code = "__result__ = {'depth': __call_depth__}"

        # Act
        with patch(
            "skill_orchestrator_mcp.server._build_skill_registry",
            return_value=registry,
        ):
            output = await _execute_code_internal(
                code=code,
                timeout=5,
                capture_output=True,
                enable_composition=True,
                current_skill_name="level_one",
            )

        # Assert - JSON output uses "depth": 1
        assert '"depth": 1' in output

    @pytest.mark.asyncio
    async def test_child_skill_has_depth_two(self) -> None:
        # Arrange
        registry = _make_skill_registry(
            {
                "name": "parent",
                "code": "child_result = invoke_skill('child')\n__result__ = {'child': child_result}",
                "description": "Calls child skill",
            },
            {
                "name": "child",
                "code": "__result__ = {'depth': __call_depth__, 'parent': __parent_skill__}",
                "description": "Reports context",
            },
        )
        parent_code = "child_result = invoke_skill('child')\n__result__ = {'my_depth': __call_depth__, 'child': child_result}"

        # Act
        with patch(
            "skill_orchestrator_mcp.server._build_skill_registry",
            return_value=registry,
        ):
            output = await _execute_code_internal(
                code=parent_code,
                timeout=5,
                capture_output=True,
                enable_composition=True,
                current_skill_name="parent",
            )

        # Assert - JSON output uses double quotes
        assert '"my_depth": 1' in output
        assert '"depth": 2' in output

    @pytest.mark.asyncio
    async def test_three_level_nesting_tracks_depth_correctly(self) -> None:
        # Arrange
        registry = _make_skill_registry(
            {
                "name": "level_one",
                "code": "r = invoke_skill('level_two')\n__result__ = {'level': 1, 'depth': __call_depth__, 'child': r}",
                "description": "Top level",
            },
            {
                "name": "level_two",
                "code": "r = invoke_skill('level_three')\n__result__ = {'level': 2, 'depth': __call_depth__, 'child': r}",
                "description": "Middle level",
            },
            {
                "name": "level_three",
                "code": "__result__ = {'level': 3, 'depth': __call_depth__}",
                "description": "Bottom level",
            },
        )
        top_code = "r = invoke_skill('level_two')\n__result__ = {'level': 1, 'depth': __call_depth__, 'child': r}"

        # Act
        with patch(
            "skill_orchestrator_mcp.server._build_skill_registry",
            return_value=registry,
        ):
            output = await _execute_code_internal(
                code=top_code,
                timeout=10,
                capture_output=True,
                enable_composition=True,
                current_skill_name="level_one",
            )

        # Assert - each level should have incrementing depth (JSON format)
        assert '"level": 1' in output
        assert '"level": 2' in output
        assert '"level": 3' in output
        # Depth should be 1, 2, 3 for each level
        assert output.count('"depth": 1') >= 1
        assert output.count('"depth": 2') >= 1
        assert output.count('"depth": 3') >= 1


class TestParentSkillTracking:
    """Tests for __parent_skill__ context variable."""

    @pytest.mark.asyncio
    async def test_top_level_skill_has_no_parent(self) -> None:
        # Arrange
        code = "__result__ = {'parent': __parent_skill__}"

        # Act
        with patch(
            "skill_orchestrator_mcp.server._build_skill_registry",
            return_value={},
        ):
            output = await _execute_code_internal(
                code=code,
                timeout=5,
                capture_output=True,
                enable_composition=True,
                current_skill_name="top_skill",
            )

        # Assert - JSON null for None
        assert '"parent": null' in output

    @pytest.mark.asyncio
    async def test_child_skill_knows_its_parent(self) -> None:
        # Arrange
        registry = _make_skill_registry(
            {
                "name": "the_parent",
                "code": "__result__ = invoke_skill('the_child')",
                "description": "Parent",
            },
            {
                "name": "the_child",
                "code": "__result__ = {'my_parent': __parent_skill__, 'my_name': __skill_name__}",
                "description": "Child that reports parent",
            },
        )
        parent_code = "__result__ = invoke_skill('the_child')"

        # Act
        with patch(
            "skill_orchestrator_mcp.server._build_skill_registry",
            return_value=registry,
        ):
            output = await _execute_code_internal(
                code=parent_code,
                timeout=5,
                capture_output=True,
                enable_composition=True,
                current_skill_name="the_parent",
            )

        # Assert - JSON format
        assert '"my_parent": "the_parent"' in output
        assert '"my_name": "the_child"' in output


class TestCircularCallDetection:
    """Tests for circular/recursive call prevention."""

    @pytest.mark.asyncio
    async def test_direct_self_recursion_is_blocked(self) -> None:
        # Arrange
        registry = _make_skill_registry(
            {
                "name": "recursive",
                "code": "invoke_skill('recursive')\n__result__ = 'never reached'",
                "description": "Tries to call itself",
            }
        )
        code = "invoke_skill('recursive')\n__result__ = 'never reached'"

        # Act
        with patch(
            "skill_orchestrator_mcp.server._build_skill_registry",
            return_value=registry,
        ):
            output = await _execute_code_internal(
                code=code,
                timeout=5,
                capture_output=True,
                enable_composition=True,
                current_skill_name="recursive",
            )

        # Assert
        assert "Circular skill invocation detected" in output
        assert "recursive" in output

    @pytest.mark.asyncio
    async def test_indirect_circular_call_is_blocked(self) -> None:
        # Arrange: A -> B -> A (indirect recursion)
        registry = _make_skill_registry(
            {
                "name": "skill_a",
                "code": "__result__ = invoke_skill('skill_b')",
                "description": "Calls B",
            },
            {
                "name": "skill_b",
                "code": "__result__ = invoke_skill('skill_a')",
                "description": "Calls A, creating cycle",
            },
        )
        code = "__result__ = invoke_skill('skill_b')"

        # Act
        with patch(
            "skill_orchestrator_mcp.server._build_skill_registry",
            return_value=registry,
        ):
            output = await _execute_code_internal(
                code=code,
                timeout=5,
                capture_output=True,
                enable_composition=True,
                current_skill_name="skill_a",
            )

        # Assert
        assert "Circular skill invocation detected" in output


class TestMaxDepthLimiting:
    """Tests for maximum call depth enforcement."""

    @pytest.mark.asyncio
    async def test_exceeding_max_depth_raises_error(self) -> None:
        # Arrange: Create a chain that exceeds MAX_SKILL_CALL_DEPTH (default 10)
        # skill_0 -> skill_1 -> ... -> skill_10 -> skill_11 (should fail)
        skills = []
        for i in range(15):
            if i < 14:
                skills.append(
                    {
                        "name": f"skill_{i}",
                        "code": f"__result__ = invoke_skill('skill_{i+1}')",
                        "description": f"Calls skill_{i+1}",
                    }
                )
            else:
                skills.append(
                    {
                        "name": f"skill_{i}",
                        "code": "__result__ = 'end'",
                        "description": "End of chain",
                    }
                )
        registry = _make_skill_registry(*skills)
        code = "__result__ = invoke_skill('skill_1')"

        # Act
        with patch(
            "skill_orchestrator_mcp.server._build_skill_registry",
            return_value=registry,
        ):
            output = await _execute_code_internal(
                code=code,
                timeout=10,
                capture_output=True,
                enable_composition=True,
                current_skill_name="skill_0",
            )

        # Assert
        assert "Maximum skill call depth" in output or "exceeded" in output.lower()


class TestSkillNotFound:
    """Tests for handling missing skills."""

    @pytest.mark.asyncio
    async def test_invoking_nonexistent_skill_raises_error(self) -> None:
        # Arrange
        registry = _make_skill_registry(
            {
                "name": "existing",
                "code": "__result__ = 'exists'",
                "description": "An existing skill",
            }
        )
        code = "__result__ = invoke_skill('nonexistent')"

        # Act
        with patch(
            "skill_orchestrator_mcp.server._build_skill_registry",
            return_value=registry,
        ):
            output = await _execute_code_internal(
                code=code,
                timeout=5,
                capture_output=True,
                enable_composition=True,
                current_skill_name="caller",
            )

        # Assert
        assert "not found" in output.lower()
        assert "nonexistent" in output


class TestCompositionHelpers:
    """Tests for list_available_skills() and get_skill_info() helpers."""

    @pytest.mark.asyncio
    async def test_list_available_skills_returns_registry(self) -> None:
        # Arrange
        registry = _make_skill_registry(
            {
                "name": "alpha",
                "code": "__result__ = 'a'",
                "description": "Alpha skill",
            },
            {
                "name": "beta",
                "code": "__result__ = 'b'",
                "description": "Beta skill",
            },
        )
        code = """
skills = list_available_skills()
__result__ = {'count': len(skills), 'names': sorted(skills.keys())}
"""

        # Act
        with patch(
            "skill_orchestrator_mcp.server._build_skill_registry",
            return_value=registry,
        ):
            output = await _execute_code_internal(
                code=code,
                timeout=5,
                capture_output=True,
                enable_composition=True,
                current_skill_name="inspector",
            )

        # Assert - JSON format
        assert '"count": 2' in output
        assert "alpha" in output
        assert "beta" in output

    @pytest.mark.asyncio
    async def test_get_skill_info_returns_metadata(self) -> None:
        # Arrange
        registry = _make_skill_registry(
            {
                "name": "documented_skill",
                "code": "__result__ = 'test'",
                "description": "A well documented skill",
                "parameters_schema": {"type": "object"},
            }
        )
        code = """
info = get_skill_info('documented_skill')
__result__ = info
"""

        # Act
        with patch(
            "skill_orchestrator_mcp.server._build_skill_registry",
            return_value=registry,
        ):
            output = await _execute_code_internal(
                code=code,
                timeout=5,
                capture_output=True,
                enable_composition=True,
                current_skill_name="inspector",
            )

        # Assert
        assert "documented_skill" in output
        assert "A well documented skill" in output

    @pytest.mark.asyncio
    async def test_get_skill_info_returns_none_for_missing(self) -> None:
        # Arrange
        code = """
info = get_skill_info('missing_skill')
__result__ = {'found': info is not None}
"""

        # Act
        with patch(
            "skill_orchestrator_mcp.server._build_skill_registry",
            return_value={},
        ):
            output = await _execute_code_internal(
                code=code,
                timeout=5,
                capture_output=True,
                enable_composition=True,
                current_skill_name="inspector",
            )

        # Assert - JSON false for False
        assert '"found": false' in output


class TestParameterPassing:
    """Tests for parameter passing between skills."""

    @pytest.mark.asyncio
    async def test_parameters_are_passed_to_child_skill(self) -> None:
        # Arrange
        registry = _make_skill_registry(
            {
                "name": "parent",
                "code": "__result__ = invoke_skill('child', {'value': 42, 'name': 'test'})",
                "description": "Parent",
            },
            {
                "name": "child",
                "code": "__result__ = {'received_value': params.get('value'), 'received_name': params.get('name')}",
                "description": "Child that echoes params",
            },
        )
        parent_code = "__result__ = invoke_skill('child', {'value': 42, 'name': 'test'})"

        # Act
        with patch(
            "skill_orchestrator_mcp.server._build_skill_registry",
            return_value=registry,
        ):
            output = await _execute_code_internal(
                code=parent_code,
                timeout=5,
                capture_output=True,
                enable_composition=True,
                current_skill_name="parent",
            )

        # Assert - JSON format
        assert '"received_value": 42' in output
        assert '"received_name": "test"' in output

    @pytest.mark.asyncio
    async def test_context_is_passed_to_child_skill(self) -> None:
        # Arrange
        registry = _make_skill_registry(
            {
                "name": "parent",
                "code": "__result__ = invoke_skill('child', {}, {'env': 'test'})",
                "description": "Parent",
            },
            {
                "name": "child",
                "code": "__result__ = {'env': context.get('env')}",
                "description": "Child that reads context",
            },
        )
        parent_code = "__result__ = invoke_skill('child', {}, {'env': 'production'})"

        # Act
        with patch(
            "skill_orchestrator_mcp.server._build_skill_registry",
            return_value=registry,
        ):
            output = await _execute_code_internal(
                code=parent_code,
                timeout=5,
                capture_output=True,
                enable_composition=True,
                current_skill_name="parent",
            )

        # Assert - JSON format
        assert '"env": "production"' in output


class TestCompositionDisabled:
    """Tests for behavior when composition is disabled."""

    @pytest.mark.asyncio
    async def test_invoke_skill_fails_without_composition_enabled(self) -> None:
        # Arrange
        code = "result = invoke_skill('some_skill')"

        # Act
        output = await _execute_code_internal(
            code=code,
            timeout=5,
            capture_output=True,
            enable_composition=False,  # Disabled
            current_skill_name=None,
        )

        # Assert - should fail because invoke_skill isn't defined without composition
        assert "Error" in output or "not defined" in output.lower() or "not available" in output.lower()
