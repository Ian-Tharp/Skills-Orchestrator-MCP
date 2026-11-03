"""Tests for the persistent state store (set_state / get_state / list_state)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from skill_orchestrator_mcp import server
from skill_orchestrator_mcp.models import (
    ListStateInput,
    ResponseFormat,
    ReturnMode,
    StateInput,
)

from .conftest import Storage


async def _set(key: str, value: Any, namespace: str = "default") -> str:
    return await server.set_state(StateInput(key=key, value=value, namespace=namespace))


# ----------------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------------


def test_nested_value_helpers_round_trip() -> None:
    data: dict[str, Any] = {}
    server._set_nested_value(data, "a.b.c", 1)
    server._set_nested_value(data, "a.b.d", [1, 2])

    assert data == {"a": {"b": {"c": 1, "d": [1, 2]}}}
    assert server._get_nested_value(data, "a.b.c") == 1
    assert server._get_nested_value(data, "a.missing") is None
    # Descending through a non-dict returns None instead of raising.
    assert server._get_nested_value(data, "a.b.d.x") is None


# ----------------------------------------------------------------------------
# set_state / get_state
# ----------------------------------------------------------------------------


async def test_set_then_get_scalar_and_nested(storage: Storage) -> None:
    await _set("user.prefs.theme", "dark")
    await _set("user.count", 3)

    assert await server.get_state("user.prefs.theme") == "dark"
    assert await server.get_state("user.count") == "3"
    assert json.loads(await server.get_state("user.prefs")) == {"theme": "dark"}
    assert json.loads((storage.state_dir / "default.json").read_text()) == {
        "user": {"prefs": {"theme": "dark"}, "count": 3}
    }


async def test_namespaces_are_isolated(storage: Storage) -> None:
    await _set("k", "one", namespace="alpha")
    await _set("k", "two", namespace="beta")

    assert await server.get_state("k", namespace="alpha") == "one"
    assert await server.get_state("k", namespace="beta") == "two"
    assert "not found" in await server.get_state("k")


async def test_get_state_missing_key_and_namespace(storage: Storage) -> None:
    assert "not found" in await server.get_state("nope", namespace="never_written")
    assert not (storage.state_dir / "never_written.json").exists()


async def test_falsy_values_are_returned_not_reported_missing(storage: Storage) -> None:
    await _set("zero", 0)
    await _set("flag", False)
    await _set("empty", "")

    assert await server.get_state("zero") == "0"
    assert await server.get_state("flag") == "False"
    assert await server.get_state("empty") == ""


async def test_set_state_overwrites_existing_value(storage: Storage) -> None:
    await _set("a.b", 1)
    await _set("a.b", {"c": 2})
    assert json.loads(await server.get_state("a.b")) == {"c": 2}


async def test_set_state_through_non_dict_intermediate_returns_error(storage: Storage) -> None:
    # Bug: this used to raise TypeError ('int' object does not support item assignment).
    await _set("a", 1)

    msg = await _set("a.b", 2)

    assert msg.startswith("Error")
    assert await server.get_state("a") == "1"


def test_state_input_rejects_empty_key_and_long_namespace() -> None:
    with pytest.raises(ValidationError):
        StateInput(key="", value=1)
    with pytest.raises(ValidationError):
        StateInput(key="k", value=1, namespace="x" * 51)


# ----------------------------------------------------------------------------
# Path traversal (bug: namespaces were joined into file paths unchecked)
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("namespace", ["../skills/evil", "../../escaped", "a/b", "..", ".hidden"])
async def test_set_state_rejects_namespace_traversal(storage: Storage, namespace: str) -> None:
    msg = await _set("k", "v", namespace=namespace)

    assert msg.startswith("Error")
    written = [p for p in storage.root.parent.rglob("*.json") if p.parent != storage.state_dir]
    assert written == []


async def test_get_state_rejects_namespace_traversal(storage: Storage) -> None:
    (storage.skills_dir / "secret.json").write_text(json.dumps({"code": "TOP SECRET"}))

    text = await server.get_state("code", namespace="../skills/secret")

    assert text.startswith("Error")
    assert "TOP SECRET" not in text


async def test_list_state_and_resource_reject_namespace_traversal(storage: Storage) -> None:
    (storage.skills_dir / "secret.json").write_text(json.dumps({"code": "TOP SECRET"}))

    listed = await server.list_state(ListStateInput(namespace="../skills/secret"))
    resource = server.get_state_resource("../skills/secret")

    assert listed.startswith("Error")
    assert "TOP SECRET" not in resource
    assert "error" in json.loads(resource)


# ----------------------------------------------------------------------------
# list_state
# ----------------------------------------------------------------------------


async def test_list_state_empty_namespace(storage: Storage) -> None:
    assert "No state found" in await server.list_state(ListStateInput(namespace="empty"))


async def test_list_state_json_full_with_values(storage: Storage) -> None:
    await _set("a.b", 1)
    await _set("c", "hello")

    text = await server.list_state(
        ListStateInput(response_format=ResponseFormat.JSON, return_mode=ReturnMode.FULL, include_values=True)
    )
    payload = json.loads(text.split("\n\n---\n")[0])

    assert payload["total_keys"] == 2
    assert {i["key"]: i["value"] for i in payload["items"]} == {"a.b": 1, "c": "hello"}


async def test_list_state_pagination_and_previews(storage: Storage) -> None:
    for i in range(5):
        await _set(f"k{i}", "x" * 100)

    text = await server.list_state(
        ListStateInput(response_format=ResponseFormat.JSON, return_mode=ReturnMode.FULL, limit=2, cursor=4, value_preview_length=10)
    )
    payload = json.loads(text.split("\n\n---\n")[0])

    assert payload["next_cursor"] is None
    assert len(payload["items"]) == 1
    # FULL without include_values falls back to previews.
    assert payload["items"][0]["value_preview"] == "x" * 10 + "..."


async def test_list_state_handle_mode_stores_artifact(storage: Storage) -> None:
    await _set("k", "v")

    ref = json.loads(await server.list_state(ListStateInput(return_mode=ReturnMode.HANDLE)))

    chunk = server.ARTIFACT_STORE.get_text_chunk(ref["artifact_id"])
    assert chunk is not None
    assert "**k** (str)" in chunk["chunk"]
    assert chunk["meta"]["expires_at"] is not None  # listings get a short TTL


async def test_state_resource_returns_namespace(storage: Storage) -> None:
    await _set("a", 1, namespace="ns1")
    assert json.loads(server.get_state_resource("ns1")) == {"a": 1}


def test_state_files_live_only_in_tmp(storage: Storage) -> None:
    # Guard: the fixture really redirected storage away from the user's home.
    assert storage.state_dir == server.DEFAULT_STATE_DIR
    assert Path.home() / ".skill_orchestrator" != storage.root
