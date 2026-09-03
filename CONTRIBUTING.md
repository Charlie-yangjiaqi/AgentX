# Contributing to AgentX

Thanks for your interest! AgentX is a young project with a strong internal
design culture (evidence over claims), so please read this before opening a
PR. Issues and discussions in Chinese or English are both welcome.

## Development setup

Requirements: Python 3.13+, [uv](https://docs.astral.sh/uv/).

```bash
uv sync                 # install dependencies + dev group from uv.lock
uv run pytest           # run the full suite
uv run ruff check .     # lint
uv run mypy             # type check (strict)
```

All three must pass before a PR is merged. The suite is hermetic — tests never
touch a real API key, real config, or the network. A provider with no key
configured falls back to a mock.

## Architecture notes (read first)

- **Entry points**: `agentx` (Typer CLI, `src/agentx/cli/app.py`) and
  `agentx-mcp` (MCP server, `src/agentx/mcp/server.py`) both compose the same
  `Application` root in `src/agentx/app/application.py`.
- **Domain models live in `src/agentx/state/models.py`** — most cross-module
  data shapes are defined there, not in local dataclasses.
- **Config & secrets**: `~/.agentx/config.json` never stores API keys. Keys
  live in `~/.agentx/.env`; config only holds an `api_key_env` reference.
- **No claim without evidence** is the core design rule. New features that
  produce facts for the planner must make their evidence traceable (see
  `src/agentx/validation/`).
- **Degradation over failure**: CodeGraph unavailable? Degrade to file scan
  and say so in `errors`. Tree-sitter crashes in the worker? Poison one file,
  not the server. New features should follow the same bias.

## Code style

- `ruff` config in `pyproject.toml`: line length 100, `py313` target,
  `E/F/W/I/UP/B/SIM/ASYNC` rule sets.
- `mypy --strict` is enforced. New code must be fully typed.
- Docstrings may be written in Chinese — both languages are used across the
  codebase and understood by maintainers.

## Testing

- Tests live in `tests/`, pytest with `asyncio_mode = "auto"`.
- Add tests for new behavior; keep them hermetic (`tmp_path`, `monkeypatch`,
  no real keys, no network).
- The shared `conftest.py` `gate_bypass` fixture short-circuits the human
  decision gate for legacy-flow tests — only use it when the gate is not the
  thing under test.

## Pull request process

1. Fork the repo and create a branch from `main`.
2. Make your change, add tests, run `uv run pytest && uv run ruff check . && uv run mypy`.
3. Keep PRs focused: one logical change per PR. If a refactor is bundled with
   a feature, split them.
4. In the PR description, state what evidence backs the change (a failing
   test before / passing after counts).

## Before your first commit to this repo (maintainers)

AgentX ships to the public. Before publishing:

- `git status` must not show `.agentx/`, `tmp/`, or any `*.db` (see
  `.gitignore`).
- No developer-machine paths or personal identifiers in `src/` or docs
  (`agentx doctor` and `agentx codegraph status` paths come from runtime
  resolution, never hardcoded).
- Version bumps touch both `pyproject.toml` and `src/agentx/__init__.py`
  (`__version__`).
