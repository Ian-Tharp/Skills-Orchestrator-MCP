# Skill Orchestrator — Phase 1: Adoption

**Date:** 2026-07-15
**Status:** Approved
**Phase:** 1 of 4 (Adoption → Harden → Capabilities → Ship)

## Problem

The server works (15 MCP tools, composition, subprocess isolation) but is barely used: 24 stored skills, ~13 real invocations ever, most domain skills at zero. Ten of the 24 skills are `test_*` fixtures polluting the live registry. Several others duplicate native Claude Code skills. Nothing prompts connected agents to reach for the orchestrator, so it sits idle.

Enhancing features nobody invokes is waste. Phase 1 makes the server earn organic usage, which then generates the data that prioritizes Phases 2–4.

## Design

### 1. Purge test fixtures from the live registry

Delete the 10 `test_*` skill JSONs from `~/.skill_orchestrator/skills/`. Recreate any that automated tests need as fixtures inside the repo's `tests/` directory (created/destroyed by the tests themselves, pointed at a temp skills dir — never the live registry). Update `.skill_index.json` / `.skill_usage.json` accordingly (or let the server rebuild them).

### 2. Retire prompt-template skills that duplicate native Claude skills

`ian_voice`, `gpw_worldbuilding_fragment`, `gpw_present_day_ai_essay`, `gpw_fork_point_scenario`, and `greeting_generator` are prompt templates, not computation. Native Claude Code skills handle prompts better (they load into model context; the orchestrator runs Python). Archive their JSONs to a `retired/` subfolder rather than hard-deleting, in case any prompt text isn't captured elsewhere.

### 3. Define and document the niche

The orchestrator's unique value — things native Claude skills **cannot** do:

- **Persistent cross-session state** — `set_state`/`get_state` survives conversation resets.
- **Cross-agent shared memory** — Claude Code, Cursor, and OpenClaw all point at the same `~/.skill_orchestrator` store.
- **Heavy compute offload** — `execute_code` processes large data in a subprocess, returning only summaries (context efficiency).
- **Artifacts** — out-of-band storage for large outputs with chunked retrieval.

Rewrite the README's positioning around this niche, and rewrite each surviving skill's `description` field so `search_skills` surfaces them for the right queries.

### 4. Keep and fix the earners

Survivors: `omni_task_manager`, `persistent_session_context`, `domain_context_switcher`, `context_efficient_codebase_mapper`, `session_bridge`, `decision_chronicle`, `knowledge_arsenal`, `idea_capture`, `sme_accelerator`. For each: verify it runs (invoke with representative params), fix breakage, sharpen its description/tags, delete it if it fails and isn't worth fixing. Expected outcome: 5–9 working, discoverable skills.

### 5. Wire triggers so agents reach for it

Add a short "Skill Orchestrator" section to the global `~/.claude/CLAUDE.md` (or per-project CLAUDE.md where relevant) stating when to use it: cross-session/cross-agent state, data-heavy processing, large-output storage. Keep it under ~10 lines — guidance, not a manual. No SessionStart auto-inject hooks (deferred; YAGNI until organic usage proves insufficient).

## Error handling

- Skill deletion is file moves/deletes on the live registry — archive `retired/` copies before removal; test fixtures are recreated by tests, safe to delete outright.
- Server may cache index in memory; restart connected clients after registry surgery.

## Testing

- After purge: `get_orchestrator_status` shows expected skill count; `search_skills` returns no `test_*` results.
- Repo test suite passes against temp-dir fixtures (no dependency on live registry).
- Each surviving skill invoked once successfully with representative parameters.

## Out of scope (later phases)

- Version-string fix, README claim verification, sandbox hardening (Phase 2).
- Sandbox package deps, scheduled runs, cross-agent registry features (Phase 3).
- CI, PyPI, public docs (Phase 4).

## Success criteria

- Live registry contains only working, well-described, niche-aligned skills.
- Tests pass without touching the live registry.
- CLAUDE.md guidance exists; in subsequent sessions, agents invoke orchestrator tools for state/offload tasks without being told to.
