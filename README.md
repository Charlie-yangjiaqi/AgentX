# AgentX

**Evidence-based multi-agent engineering engine for embedded firmware** —
plan / review / verify with a persistent project index.

> AgentX brings engineering judgment to AI coding agents: it plans, reviews,
> and verifies changes against a long-lived, fingerprint-guarded understanding
> of your embedded C/C++ project. Any AI coding host can drive it over MCP.

English · [简体中文](README.zh-CN.md)

---

## What AgentX does

AgentX is a human-in-the-loop collaboration engine that turns "an AI agent
changed my code" into "an AI agent changed my code, with a reviewed plan,
machine-verified results, and evidence you can trace."

```
PROJECT → FINGERPRINT → INDEX → PLAN → EXECUTION (AI host) → REVIEW → VERIFY
```

**Core loop**

1. **Project Index** — a long-lived digital twin of your project, stored in
   `<project>_codebase_index/`.
2. **Project Fingerprint** — `hash(file paths + contents + config)`. Hard rule:
   AgentX never reuses an index it cannot prove matches the current project
   state. Index states: `VALID` / `STALE` / `MISSING` / `CORRUPTED`.
3. **Understanding layer** — fuses **CodeGraph** (project-level symbols, call
   graph, build relations) + **Tree-sitter** (file-level function / struct /
   enum / macro semantics) + **Build Reality** (Keil / IAR compile status) into
   one index. CodeGraph is a source of truth, not the only truth — when it is
   unavailable AgentX degrades to file scanning instead of failing.
4. **Plan → Review → Verify** — multi-agent workflow (Planner / Reviewer /
   Verifier) orchestrated by a state machine, guarded by deterministic rule
   layers: Module Knowledge, Scope Control, Human Decision Boundary, and
   Evidence Validation. **No claim without evidence**: every plan step must
   cite index facts before it is accepted.

## Models

- **Host mode** — use the AI host's own model over the MCP sampling channel
  (e.g. when AgentX runs as an MCP server for a coding agent).
- **Native mode** (default) — AgentX calls model APIs directly
  (DeepSeek / OpenAI / any OpenAI-compatible endpoint).
- Each role (plan / review / verify) can be assigned its own
  provider + model via `config.agents.<role>`. Verify is deterministic
  (zero-LLM) by default.
- Failure policy: retry → fallback provider → structured error. The MCP
  server never crashes.

## Install

Requires Python **3.13+** (and [uv](https://docs.astral.sh/uv/)).

```bash
uv sync                       # in the repo, for development
uv tool install agentx        # once published: global install (adds agentx-mcp)
```

Or run from the repo:

```bash
uv run agentx --help
uv run agentx-mcp            # MCP server over stdio
```

## Configuration

```bash
agentx setup          # interactive setup
```

Or hand-write `~/.agentx/config.json`:

```json
{
  "model_source": "agentx",
  "agents": {
    "plan":   {"model": "deepseek-v4-pro"},
    "review": {"model": "deepseek-v4-flash"},
    "verify": {"provider": "none"}
  }
}
```

- **API keys are never written to `config.json`** — they live in
  `~/.agentx/.env`, referenced as `api_key_env`. Resolution order:
  environment variable → `~/.agentx/.env` → config file → provider preset.
- `agentx config api` walks you through provider / key / base URL / model,
  and `agentx config api test` checks connectivity.

## CodeGraph: bundled auto-distribution

No separate CodeGraph install needed. On first `plan` / `index` AgentX
downloads the pinned version (currently `1.6.0`) to
`~/.agentx/vendor/codegraph/<version>/` with SHA512 double-checking,
resume support, and refuse-to-run on checksum failure. Availability priority:
`CODEGRAPH_BIN` / `CODEGRAPH_NODE` env vars (user-managed) → bundled vendor →
auto-download → file-scan fallback (or hard fail with
`AGENTX_CODEGRAPH_REQUIRED=1`).

```bash
agentx codegraph status     # version / platform / install location
agentx codegraph install    # install / reinstall pinned version
agentx codegraph upgrade    # reinstall to the pinned version
```

Environment variables: `AGENTX_VENDOR_DIR` overrides the vendor root,
`CODEGRAPH_MIRROR` supplies a mirror download template (`{target}` /
`{version}` placeholders) for air-gapped networks. Built-in telemetry is off
by default (`CODEGRAPH_TELEMETRY=0`); explicit user settings are respected.

> **License note**: the bundled `@colbymchenry/codegraph` is MIT-licensed
> (Copyright (c) 2026 Colby Mchenry); its full license text ships with the
> install under `~/.agentx/vendor/codegraph/<version>/LICENSE`.

## Features

### Three-layer semantic index (C / C++)

| Layer | What it provides |
|---|---|
| CodeGraph 1.6 | project-level facts: symbol locations, call edges, includes, build links |
| Tree-sitter | file-level syntax facts: function signatures, struct members, enum values, macro definitions |
| AgentX | fused engineering knowledge: Index → Query → Evidence |

`agentx query` answers questions with zero LLM involvement:

```bash
agentx query --symbol key_scan      # definition + signature + callers + build status
agentx query --symbol KEY0_PRES     # macro location + value
agentx query --symbol LCD_TypeDef   # struct members + types + line numbers
```

### Module knowledge layer

Deterministic, zero-LLM induction from index evidence: file → module, with
role typing (`bsp | app | middleware | hal | driver | lib`), dependencies,
consumers, and build status. Third-party libraries (LVGL, FreeRTOS, …) are
detected automatically and frozen — they never pollute business dependency
analysis. Keil/IAR groups are treated as ground truth for file ownership.

### Three-tier input scope

- **project** — full understanding
- **third_party** — interface-level understanding (not deleted, degraded:
  files / build / includes / public symbols kept, library-internal call edges
  dropped, business↔library edges marked `external: true`)
- **ignore** — fully filtered out of fingerprint / scan / index

`agentx init` auto-discovers scope and writes `.agentxscope.yaml` for you.
Legacy `.agentxignore` files remain supported.

### Stability under native-crash conditions

Bulk tree-sitter parsing of thousands of files runs in an isolated worker
process (`python -m agentx.semantic.worker --serve`, subprocess, Windows-
compatible). A native crash or timeout poisons only one file, records
`semantic_worker_crash` / `semantic_timeout`, auto-restarts the worker, and
the MCP server keeps running. Files over `semantic.max_file_size_mb`
(default 5 MB) skip AST extraction gracefully.

> tree-sitter is pinned `>=0.25.2,<0.26` — the 0.26.0 Python binding has a
> native memory bug (accumulated heap corruption while walking ASTs → SIGSEGV).

### Human decision boundary

A deterministic gate forces user confirmation on risky changes: multiple
close candidates, near-tie scores, public-API impact, or high blast radius.
Every plan change must cite index evidence (PASS / WARNING / BLOCK with
propagation chains).

## MCP: single `agentx` tool

Register the stdio server in your AI host:

```
mcp:
  servers:
    agentx:
      command: agentx-mcp
```

| action | responsibility | output |
|---|---|---|
| `auto` (default) | full loop: Plan → Review → Verify | phase + plan + review + verify |
| `plan` | Index / Fingerprint / understanding → change plan | index status + plan (steps/files/risks/verify commands) |
| `review` | review with minimal context: Index + Plan + Diff | verdict + findings |
| `verify` | deterministic machine verification (runs the plan's verify commands) | verdict + build/tests/evidence |
| `status` | Index state / fingerprint / recent plans | overview |

Example call:

```json
{
  "project_path": "/path/to/firmware",
  "task": "implement parameter transaction support, keep API compatible",
  "action": "auto",
  "options": {"review": true, "verify": true}
}
```

**Recommended workflow**

```
1. agentx(action=plan)    → build project understanding + change plan
2. AI host edits code following the plan
3. agentx(action=review)  → FAIL? fix and go back to step 2
4. agentx(action=verify)  → PASS/FAIL, judged by machine evidence
```

## CLI overview

```bash
agentx init          # initialize project scope (auto-detection + .agentxscope.yaml)
agentx plan          # build understanding and produce a plan
agentx review        # review current changes
agentx verify        # run deterministic verification
agentx status        # index / fingerprint / task state
agentx query --symbol <name>    # index queries, no LLM
agentx doctor        # health check (parser / worker / LLM provider)
agentx codegraph status
```

## Development

```bash
uv sync
uv run pytest        # ~550 tests
uv run ruff check
uv run mypy
```

The test suite is hermetic: no real API keys, no network, no real config —
providers fall back to a mock when no key is configured.

## Project layout

```
src/agentx/
├── app/          application composition root (CLI / TUI / MCP share it)
├── agents/       agent definitions + prompts + runtime (role ≠ model)
├── providers/    LLM providers: OpenAI-compatible, host-sampling, fallback, mock
├── tools/        permission-gated agent tools (fs / shell / git / test)
├── index/        Project Index + fingerprint state machine + diff sync
├── understanding/ CodeGraph bootstrap & analysis, filescan degradation
├── semantic/     tree-sitter C semantics in worker-process isolation
├── build/        Keil (.uvprojx/.ewp) / IAR project parsing — "Build Reality"
├── module/       deterministic module discovery & responsibility scoring
├── query/        zero-LLM index queries (symbol / feature / architecture)
├── decision/     human decision boundary + change analyzer
├── validation/   evidence validation (PASS / WARNING / BLOCK)
├── scope/        three-tier input scope (.agentxscope.yaml)
├── state/        domain models + SQLite persistence
├── config/       ~/.agentx/config.json model + LLM presets
├── vendor/       CodeGraph pinned distribution (SHA512-verified download)
├── mcp/          MCP server (stdio) + background job manager
├── tui/          Textual chat workspace
└── runtime/      workflow context + structured events
```

## Why this exists

AI coding agents are excellent at *executing*, but they have no memory of the
project and no way to prove their claims. AgentX is the engineering-judgment
layer that sits next to an executor: it maintains what the project *is*, plans
only against evidence, reviews like a senior engineer, and verifies with
machine facts instead of vibes. It is not another code editor or agent — it is
the **verification and planning substrate** those agents are missing,
especially for the embedded world of Keil projects, GBK encodings, and
thousands-of-files firmware trees.

## License

[MIT](LICENSE) © 2026 AgentX Contributors
