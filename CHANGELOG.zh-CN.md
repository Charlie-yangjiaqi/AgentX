# Changelog（变更日志）

[English](CHANGELOG.md) · 简体中文

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

AgentX 的架构按阶段演进（Phase 1 → 8）。每个 Phase 解决一个
"AI 直接操作代码"的失效模式，最终收敛为
**工程认知系统 + 安全决策系统 + 可验证修改系统**。

## [Unreleased]（未发布）

### Phase 8 — Build Scope / 编译边界认知层

解决 "工程里很多文件存在，但不是实际产品代码" 的问题（重点针对 Keil 工程）：

- **Build Scope 边界**：以 Keil Active Target 的 source list 为主 Index 工程边界，
  而非 "文件在这个仓库目录里"。读取 `.uvprojx` 的 FilePath / Target / compiled files，
  形成 Build Boundary。
- **确定性分类**（优先级从高到低）：`ignored > third_party > build-project > non_build`。
  自有但未参与当前 Target 编译的代码标记为 `non_build`，不进主 project 边界，
  由 demo / 未编译源码 / 第三方带来的误分析被排除。
- **Keil FilePath 修复**：此前只读 `<FileName>`（裸名），只能按 basename 匹配 build；
  现在读取 `<FilePath>`（相对 `.uvprojx` 的真实路径）并归一化为工程相对路径，
  可精确判断文件是否在当前 Target。
- **Scope glob 修复**：`Middlewares/**` 形式的 third_party / ignore 写法此前因
  字面前缀匹配永不生效；现归一化 `/**` 后缀，与裸目录写法语义一致。
- **多 Target 门禁**：`build_target_required` —— 多 Target 且未确认分析目标时不自动猜，
  由用户显式选择并持久化到 `.agentxscope.yaml` 的 `build.target`（CLI / MCP 均有引导）。
- **semantic 只跑 project 边界**：禁止"先全量 semantic 再过滤"；
  third_party 与 non_build 保留 CodeGraph 事实（symbols / call_graph），
  不做文件级语义补充。
- **non_build 模块冻结**：module discovery 将 non_build 文件冻结为独立非主模块，
  不污染主 project 模块主链。
- **质量报告**：Scope Report 增加 non_build 统计。

量化效果（350A 单 Target 工程）：

```
project: 285   |   third_party: 695   |   non_build: 184
build files: 418（excluded 0）→ Build boundary（含传递 include 头）: 673
Index: symbols 16327 · call_graph 9255 · modules 102
```

### Phase 8.x — Index Control Plane（规划中，未实现）

当前方向：让宿主 AI 能安全管理索引。

- 区分权限面：`INDEX_READ` / `INDEX_WRITE` / `CODE_WRITE`。
- `INDEX_WRITE`（改 scope / rebuild index / refresh semantic / rebuild codegraph）
  与 `CODE_WRITE`（改源码）分离，杜绝
  "AI 改 scope → sync 只刷 hash → 旧 Index 残留" 的失效模式。

## [0.1.0] — 2026-09-03

开源首发。架构演进覆盖 Phase 1 → 7.9：
从"AI 直接读文件猜代码"演进为
"确定性代码事实 + 工程认知索引 + 人机决策边界 + 证据校验 + 多角色协作"。

### Phase 1 — 基础工程理解层

- 工程索引（Index）与文件级扫描、代码结构解析、工程上下文持久化。
- 早期链路 `文件 → LLM 理解 → 输出` 暴露问题（上下文过大 / 遗漏 / 无法证明依据 /
  幻觉），确立 `Index → Knowledge → Reasoning` 方向。

### Phase 2 — CodeGraph / 工程事实图

- 引入 CodeGraph 确定性代码事实层：symbols / calls / dependencies / include / 类型关系。
- AI 通过查询 `LCD API → display port → feature → caller` 定位，而非猜测。

### Phase 3 — Semantic Knowledge Layer

- 从"代码事实"提升到"工程理解"：module discovery / responsibility / architecture。
- CodeGraph + Tree-sitter 语义 + AgentX 融合成 Engineering Knowledge
  （Index → Query → Evidence）。

### Phase 4 — Plan System

- AI 不直接改代码，先生成工程级修改计划
  （修改文件 / 原因 / 影响范围 / 风险）。

### Phase 5 — Impact Analysis

- 修改前风险判断：Direct Impact（call graph）、Indirect Impact（callback / 注册）、
  Data Dependency（struct 字段消费方）。

### Phase 6 — Multi-Agent 协作层

- 角色分离：Planner / Coder / Reviewer / Tester（AgentX 侧为
  Planner / Reviewer / Verifier），拒绝"一个模型完成全部"。

### Phase 7 — Index Reliability

- **7.1 Index Bootstrap 修复**：修复 `index.json` 有骨架无知识的空索引问题
  （`ensure_index` 未接 `enrich_index`），增加 skeleton detection / 自动 enrich / degraded 状态。
- **7.6 代码语义细节索引**：三层分工 CodeGraph（项目级事实）+ Tree-sitter
  （文件级语法事实）+ AgentX（融合工程知识），`index_version=1.4`。
- **7.7 模块知识层**：确定性、零 LLM 的模块归纳（文件 → 模块），
  第三方库自动识别冻结。
- **7.8 三层 Scope**：project（完整理解）/ third_party（接口级）/ ignore（完全过滤），
  `index_version=1.6`；Quality Report + Scope Report。
- **7.8.1/7.8.2 稳定性加固**：tree-sitter native 崩溃隔离 —— worker 子进程隔离、
  单文件失败只毒化一个文件、大文件保护、`tree-sitter <0.26` 版本锁定。
- **7.8 Human Decision Boundary**：AI 分析但不替用户选择 —— Decision Analyzer
  （候选 / evidence / confidence）+ Decision Gate（多候选 / 分数接近 / 公共接口 /
  大影响 / 跨模块时要求用户确认），11 项测试。
- **7.9 Evidence Validation**：不限制 AI 思考，只限制无证据输出 ——
  Direct / Weak 证据分类，PASS / WARNING / BLOCK 三级判定。
- **7.9.1 序列化防护**：非法 surrogate / 非 UTF-8 路径不破坏 MCP response。
- **7.9.2 长任务后台化**：大项目首次 Index 构建后台化 + `job_id` 轮询 +
  scope 确认续跑，规避 MCP RPC 超时。

### 工程能力（随 0.1.0 一并发布）

- CLI（Typer）：`agentx` init / plan / review / verify / query / doctor / codegraph 等。
- MCP Server（stdio）：统一 `agentx` tool，action = auto / plan / review / verify / status。
- LLM Provider 抽象：宿主 sampling / OpenAI 兼容 / fallback / mock；API Key 存
  `~/.agentx/.env`，config.json 只存引用。
- CodeGraph 内置自动分发（SHA512 双重校验 + 断点续传 + 版本锁定）。
- 许可：MIT；CI（ruff / mypy / pytest）与中英双语 README。

[Unreleased]: https://github.com/Charlie-yangjiaqi/AgentX/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Charlie-yangjiaqi/AgentX/releases/tag/v0.1.0
