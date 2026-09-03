# 参与 AgentX 开发

[English](CONTRIBUTING.md) · 简体中文

感谢你的关注！AgentX 是一个年轻的项目，有着强烈的内部设计文化
（**无证据不断言**），所以在开 PR 之前请先阅读本指南。
Issue 与讨论中英文皆可。

## 开发环境

要求：Python 3.13+、[uv](https://docs.astral.sh/uv/)。

```bash
uv sync                 # 按 uv.lock 安装依赖 + dev 组
uv run pytest           # 运行完整测试套件
uv run ruff check .     # lint
uv run mypy             # 类型检查（strict）
```

PR 合并前以上三项必须全部通过。测试套件完全封闭（hermetic）——测试永不接触
真实 API Key、真实配置或网络；未配置 key 时 provider 自动回落 Mock。

## 架构笔记（请先读）

- **入口**：`agentx`（Typer CLI，`src/agentx/cli/app.py`）与
  `agentx-mcp`（MCP server，`src/agentx/mcp/server.py`）组合自同一个
  `Application` 根（`src/agentx/app/application.py`）。
- **领域模型集中在 `src/agentx/state/models.py`**——大多数跨模块数据形状定义在
  那里，而不是局部 dataclass。
- **配置与密钥**：`~/.agentx/config.json` 绝不存储 API Key。密钥存于
  `~/.agentx/.env`，配置只持有 `api_key_env` 引用。
- **无证据不断言**是核心设计规则。为 Planner 产出事实的新功能，必须让证据可
  回溯（参见 `src/agentx/validation/`）。
- **优先降级而非失败**：CodeGraph 不可用？降级为文件扫描并在 `errors` 说明。
  Tree-sitter 在 worker 崩溃？只毒化一个文件，不拖垮 server。新功能应遵循
  同样的取向。

## 代码风格

- `ruff` 配置在 `pyproject.toml`：行长 100、`py313` 目标、
  `E/F/W/I/UP/B/SIM/ASYNC` 规则集。
- `mypy --strict` 强制启用，新代码必须完整类型标注。
- Docstring 可用中文书写——代码库两种语言都在用，维护者均能理解。

## 测试

- 测试位于 `tests/`，pytest 且 `asyncio_mode = "auto"`。
- 新行为必须补测试；保持封闭（`tmp_path`、`monkeypatch`、无真实 key、无网络）。
- 共享的 `conftest.py` `gate_bypass` fixture 会短路人类决策门——仅供 legacy
  流程测试使用，且仅当被测对象不是该门本身时。

## Pull Request 流程

1. Fork 本仓库，从 `main` 切分支。
2. 完成改动、补测试，运行 `uv run pytest && uv run ruff check . && uv run mypy`。
3. 保持 PR 聚焦：一个 PR 一个逻辑改动。若重构与功能捆绑，请拆开。
4. 在 PR 描述中说明支撑该改动的证据（改动前失败的测试 / 改动后通过，同样算数）。

## 维护者：本仓库首次提交前

AgentX 面向公众发布。发布前：

- `git status` 不得出现 `.agentx/`、`tmp/` 或任何 `*.db`（见 `.gitignore`）。
- `src/` 与文档中不得有开发者机器路径或个人标识（`agentx doctor` 与
  `agentx codegraph status` 的路径来自运行时解析，绝不硬编码）。
- 版本号升级需同时改动 `pyproject.toml` 与 `src/agentx/__init__.py`
  （`__version__`）。
