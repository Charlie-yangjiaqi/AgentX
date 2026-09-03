# AgentX

[![CI](https://github.com/Charlie-yangjiaqi/AgentX/actions/workflows/ci.yml/badge.svg)](https://github.com/Charlie-yangjiaqi/AgentX/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/downloads/)

**面向嵌入式固件的、基于证据的多智能体工程引擎** —— 通过长期项目索引完成
plan / review / verify。

> 让 AI 编码代理拥有工程判断力：AgentX 基于"指纹守护的长期项目认知"来规划、
> 审查和验证对嵌入式 C/C++ 工程的修改。任何 AI 编码宿主都可以通过 MCP 驱动它。

[English](README.md) · 简体中文

文档：[参与贡献](CONTRIBUTING.zh-CN.md) · [Contributing](CONTRIBUTING.md) ·
[变更日志](CHANGELOG.zh-CN.md) · [Changelog](CHANGELOG.md) ·
[许可证](LICENSE) · [许可证中文参考](LICENSE.zh-CN.md)

---

## AgentX 是什么

AgentX 是一个人在环路的协作引擎，把"AI 代理改了我的代码"变成
"AI 代理改了我的代码，并且有被审查过的方案、机器验证的结果、可追溯的证据"。

```
PROJECT → FINGERPRINT → INDEX → PLAN → EXECUTION (AI 宿主) → REVIEW → VERIFY
```

**核心闭环**

1. **Project Index** —— 项目的长期数字化身，存放于 `<项目名>_codebase_index/`。
2. **Project Fingerprint** —— `hash(文件路径 + 内容 + 配置)`。硬规则：
   AgentX 绝不复用无法证明对应当前项目状态的索引。索引状态：
   `VALID`（复用）/ `STALE`（更新）/ `MISSING`（创建）/ `CORRUPTED`（重建）。
3. **理解层** —— 融合 **CodeGraph**（项目级符号/调用图/构建关联）+
   **Tree-sitter**（文件级函数/结构体/枚举/宏语义）+
   **Build Reality**（Keil / IAR 真实编译状态）进同一个索引。
   CodeGraph 是事实来源之一，不是唯一真相；不可用时降级为文件扫描，AgentX 不失败。
4. **Plan → Review → Verify** —— 多智能体工作流（Planner / Reviewer / Verifier）
   由状态机编排，并由确定性规则层守护：模块知识、Scope 控制、
   人类决策边界、证据校验。**无证据不断言**：每个方案步骤必须引用索引事实才能通过。

## 模型

- **宿主模式** —— 使用 AI 宿主的模型（通过 MCP sampling 通道，
  例如 AgentX 作为编码代理的 MCP server 运行时）。
- **原生模式**（默认）—— AgentX 自己调用模型 API
  （DeepSeek / OpenAI / 任意 OpenAI 兼容端点）。
- 每个角色（plan / review / verify）可独立配置 Provider / Model
  （`config.agents.<role>`）。verify 默认确定性（零 LLM）。
- 失败策略：重试 → Fallback Provider → 结构化错误，MCP Server 永不崩溃。

## 安装

需要 Python **3.13+**（以及 [uv](https://docs.astral.sh/uv/)）。

```bash
uv sync                       # 仓库内开发
uv tool install agentx        # 发布后全局安装（含 agentx-mcp 入口）
```

或直接在仓库运行：

```bash
uv run agentx --help
uv run agentx-mcp            # stdio 上的 MCP server
```

## 配置

```bash
agentx setup          # 交互式配置
```

或手写 `~/.agentx/config.json`：

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

- **API Key 绝不写入 config.json** —— 存于 `~/.agentx/.env`，配置中只存
  `api_key_env` 引用。解析优先级：环境变量 → `~/.agentx/.env` → 配置文件 →
  provider 预设。
- `agentx config api` 交互配置 Provider / Key / Base URL / Model，
  `agentx config api test` 测试连通性。

## CodeGraph：内置自动分发

无需单独安装 CodeGraph。首次 `plan` / `index` 时自动下载锁定版本
（当前 `1.6.0`）至 `~/.agentx/vendor/codegraph/<version>/`，全程 SHA512
双重校验、断点续传、校验失败拒绝执行。可用性优先级：
`CODEGRAPH_BIN` / `CODEGRAPH_NODE` 环境变量（用户接管）→ 内置 vendored →
自动下载 → 文件扫描降级（`AGENTX_CODEGRAPH_REQUIRED=1` 时改为失败退出）。

```bash
agentx codegraph status     # 版本 / 平台 / 安装位置
agentx codegraph install    # 安装/重装锁定版本
agentx codegraph upgrade    # 重新安装到锁定版本
```

环境变量：`AGENTX_VENDOR_DIR` 覆盖 vendor 根目录；`CODEGRAPH_MIRROR`
提供内网镜像模板（占位符 `{target}`/`{version}`）。内置 CodeGraph 默认关闭遥测
（`CODEGRAPH_TELEMETRY=0`），用户显式设置时尊重用户。

> **许可证说明**：内置的 `@colbymchenry/codegraph` 为 MIT 许可
> （Copyright (c) 2026 Colby Mchenry），安装时随附完整许可证文本
> （`~/.agentx/vendor/codegraph/<version>/LICENSE`）。

## 特性

### 三层代码语义索引（C / C++）

| 层 | 提供的事实 |
|---|---|
| CodeGraph 1.6 | 项目级事实：symbol 定位 / 调用边 / include / build 关联 |
| Tree-sitter | 文件级语法事实：函数签名 / struct 成员 / enum 值 / 宏定义 |
| AgentX | 融合成工程知识：Index → Query → Evidence |

`agentx query` 零 LLM 回答索引问题：

```bash
agentx query --symbol key_scan      # 定义 + 签名 + 调用方 + Build 状态
agentx query --symbol KEY0_PRES     # 宏定义位置 + 值
agentx query --symbol LCD_TypeDef   # 结构体成员 + 类型 + 行号
```

### 模块知识层

基于索引证据的确定性归纳（零 LLM、零新解析器）：文件 → 模块，含角色类型
（`bsp | app | middleware | hal | driver | lib`）、依赖、消费方、构建状态。
第三方库（LVGL、FreeRTOS 等）自动识别并冻结，绝不污染业务依赖分析。
Keil/IAR Groups 作为文件归属的"人工真值"。

### 三层输入 Scope

- **project** —— 完整理解
- **third_party** —— 接口级理解（不删除、降级分析：保留文件 / build /
  include / public symbol / 业务调用关系；库内部调用边删除，
  业务↔库边界边标记 `external: true`）
- **ignore** —— 完全过滤：fingerprint / scan / semantic / module /
  call graph / index 全不进

`agentx init` 自动检测并生成 `.agentxscope.yaml`。旧 `.agentxignore` 兼容。

### 原生崩溃下的稳定性

批量解析上千文件时，tree-sitter 解析运行在隔离的 worker 子进程
（`python -m agentx.semantic.worker --serve`，subprocess，Windows 兼容）。
native 崩溃/超时只毒化单文件：记录 `semantic_worker_crash` /
`semantic_timeout` → worker 自动重启 → 其余文件继续，MCP server 永不被杀。
超过 `semantic.max_file_size_mb`（默认 5 MB）的文件优雅跳过 AST 提取。

> tree-sitter 锁定 `>=0.25.2,<0.26` —— 0.26.0 Python binding 存在 native
> 内存 bug（连续遍历 AST 累积堆损坏 → SIGSEGV）。

### 人类决策边界

确定性闸门对高风险变更强制用户确认：多候选、分数接近、公共 API、
高波及面。每个方案变更必须引用索引证据（PASS / WARNING / BLOCK 带传播链）。

## MCP：统一入口 `agentx`

在 AI 宿主中注册 stdio server：

```
mcp:
  servers:
    agentx:
      command: agentx-mcp
```

| action | 职责 | 输出 |
|---|---|---|
| `auto`（默认） | 完整闭环：Plan → Review → Verify | phase + plan + review + verify |
| `plan` | Index / Fingerprint / 理解层 → 实施方案 | index 状态 + plan（步骤/文件/风险/验证命令） |
| `review` | 最小上下文审查：Index + Plan + Diff | verdict + findings |
| `verify` | 确定性机器验证（执行 Plan 的验证命令） | verdict + build/tests/evidence |
| `status` | Index 状态 / 指纹 / 最近 Plan | 概览 |

调用示例：

```json
{
  "project_path": "/path/to/firmware",
  "task": "实现参数事务功能，保持 API 兼容",
  "action": "auto",
  "options": {"review": true, "verify": true}
}
```

**推荐工作流**

```
1. agentx(action=plan)    → 建立项目认知 + 实施方案
2. AI 宿主按 Plan 修改代码
3. agentx(action=review)  → FAIL 则修复，回到 2
4. agentx(action=verify)  → PASS/FAIL（机器证据裁决）
```

## CLI 一览

```bash
agentx init          # 初始化项目 scope（自动检测 + .agentxscope.yaml）
agentx plan          # 建立理解并产出方案
agentx review        # 审查当前改动
agentx verify        # 确定性机器验证
agentx status        # index / fingerprint / 任务状态
agentx query --symbol <name>    # 索引查询，零 LLM
agentx doctor        # 健康检查（parser / worker / LLM provider）
agentx codegraph status
```

## 开发

```bash
uv sync
uv run pytest        # ~550 个测试
uv run ruff check
uv run mypy
```

测试套件完全封闭：无真实 API Key、无网络、不触碰真实配置——未配置 key 时
provider 自动回落 MockProvider。

## 目录结构

```
src/agentx/
├── app/          application 组合根（CLI / TUI / MCP 共享）
├── agents/       agent 定义 + prompts + runtime（角色 ≠ 模型）
├── providers/    LLM providers：OpenAI 兼容 / 宿主 sampling / fallback / mock
├── tools/        权限门控的 agent 工具（fs / shell / git / test）
├── index/        Project Index + 指纹状态机 + diff 同步
├── understanding/ CodeGraph bootstrap & 分析，filescan 降级
├── semantic/     tree-sitter C 语义，worker 进程隔离
├── build/        Keil (.uvprojx/.ewp) / IAR 工程解析 —— "Build Reality"
├── module/       确定性模块发现与职责评分
├── query/        零 LLM 索引查询（symbol / feature / architecture）
├── decision/     人类决策边界 + 变更分析器
├── validation/   证据校验（PASS / WARNING / BLOCK）
├── scope/        三层输入 scope（.agentxscope.yaml）
├── state/        领域模型 + SQLite 持久化
├── config/       ~/.agentx/config.json 模型 + LLM 预设
├── vendor/       CodeGraph 锁定版本分发（SHA512 校验下载）
├── mcp/          MCP server（stdio）+ 后台任务管理
├── tui/          Textual 对话工作区
└── runtime/      工作流上下文 + 结构化事件
```

## 为什么会有这个项目

AI 编码代理擅长*执行*，但它们对项目没有记忆，也无法证明自己的断言。
AgentX 是站在执行者旁边的"工程判断层"：它维护项目*是什么*、只基于证据做规划、
像资深工程师一样审查、用机器事实而非感觉来验证。它不是又一个代码编辑器或代理，
而是这些代理缺失的**验证与规划基座**——尤其面向 Keil 工程、GBK 编码、
上千文件固件树的嵌入式世界。

## 许可证

[MIT](LICENSE) © 2026 AgentX Contributors
