# Changelog

English · [简体中文](CHANGELOG.zh-CN.md)

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

AgentX 的架构按阶段演进（Phase 1 → 8）。每个 Phase 解决一个
"AI 直接操作代码"的失效模式，最终收敛为
**工程认知系统 + 安全决策系统 + 可验证修改系统**。

## [0.2.0] — 2026-09-04

### Phase 8.2 — Index Freshness / 增量更新 / 重建决策模型

让 AgentX 成为"会自己维护的工程知识库"，而不是"每次变化都全量重扫的工具"：

- **三类可独立归因指纹**：`scope_fingerprint`（已有）/ `source_fingerprint`
  （scope-agnostic 代码内容）/ `build_scope_fingerprint`（active target 编译边界）
  随 enrich / reindex 落库；scope 配置变化不再伪装成源码变化。
- **Freshness 状态机**：`VALID / STALE_RECOMMENDED / AUTO_UPDATED /
  REINDEX_REQUIRED`；阈值（数量 / 比例 / 变化类型）联合判定并全部可配置
  （`config.json#freshness` + `AGENTX_FRESHNESS_*` env）。
- **真文件级增量**：改 1 个 `.c` → 只对该文件重跑 semantic/type，按
  `(file,name,type)` diff-merge 符号；删除文件 → 其 file/symbol/type facts 全部
  移除，无残留；call/include 取自 CodeGraph 引擎增量同步后的权威快照。
- **安全升级网**：无法可靠局部修复（如公共 `.h` 大范围改动）→ 升级
  `REINDEX_REQUIRED`，绝不静默用不完整增量污染 Index。
- **重建决策模型**：`reindex` 是唯一完整重建入口；`sync`/`status`/`query` 自动
  维护封顶 Level 2；CODE_WRITE 永不隐式触发 full reindex；plan/auto 遇
  `REINDEX_REQUIRED` 硬停。
- MCP 所有响应顶层带 `index_freshness`。

### Phase 8.3 — Human Project Knowledge / Human Index

在 Machine Index 之上新增工程师可读的知识表达层：

- 新增 `src/agentx/human/`：`HumanKnowledgeService`（generate / refresh / status）、
  `HumanKnowledgeBundle`（零 LLM 证据收集）、确定性文档渲染、`manifest.json`
  （document → modules / knowledge_sources / knowledge_dependencies）。
- 生成 `PROJECT_OVERVIEW.md` / `ARCHITECTURE.md` / `MODULES.md`，写入
  `<project>_codebase_index/human/`（与 index.json 同目录，非项目根 docs）。
- **增量刷新**：manifest `knowledge_dependencies` 决定哪些文档受影响
  （改函数 → 只刷 MODULES/受影响文档；改架构关系 → 刷 ARCHITECTURE）。
- **Fact / Inference / Unknown 分离**：confidence 由 scorer 决定，LLM 不参与；
  non_build / third_party 明确标注"不在当前 build target"，绝不当当前固件描述。
- **LLM 幻觉防护**：散文生成后经 allowlist 校验，引用 bundle 外的大写标识符即
  丢弃（LLM 只做语言组织，不新增事实）。
- project_understanding 缺失 → 自动补齐；补不出 → 文档标 Needs verification，
  不阻塞。
- MCP 新增 `human_index` action（task=generate|refresh|status），
  `operation_class=INDEX_WRITE / changes_code=false / requires_decision_gate=false`。

## [0.1.1] — 2026-09-03

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

### Phase 8.1 — Index Control Plane / 索引写权限与 scope 一致性

让宿主 AI 能安全管理索引：区分权限面，杜绝 "AI 改 scope → sync 只刷 hash →
旧 Index 残留" 的失效模式。

- **scope 一致性修复**：`ProjectIndex.scope_fingerprint`（ignore + third_party +
  project_include + build_target 的归一化 hash）随 enrich 落库；`sync_index` 检测
  fingerprint 变化 → 强制 `scope_rebuild`（reclassify + enrich），不再依赖源码
  增删启发式判断 —— 修复"改 scope 后 L0 incremental 只刷 hash、旧分类残留"的
  真实一致性 bug。
- **ignore matcher 修复**：`*.py` / `*.py/**` 等无斜杠 glob 此前只匹配顶层；
  现按任意深度 basename 匹配（gitignore 语义），`*.py` 正确命中 `User/tool.py`。
  `is_ignored` 统一复用同一 matcher。
- **MCP 新 action**：
  - `scope_update`（INDEX_WRITE）：写 `.agentxscope.yaml` + 校验 + 影响 preview，
    返回 `scope_changed`。只写 AgentX 自身配置，绝不触碰用户源码。
  - `reindex`（INDEX_WRITE）：强制重建 Index（最新 scope），返回
    `index_status` / `fingerprint` / `scope_fingerprint` / `scope_summary`；
    大项目同样后台化。
- **权限元数据**：每次返回顶层带 `operation_class` / `changes_code` /
  `requires_decision_gate`；新增 `agentx.capabilities` tool 返回全 action 分类表。
  - `READ`：query / search_feature / build_status / status / review / verify
  - `INDEX_WRITE`（changes_code=false，不需审批）：understand / sync /
    scope_update / reindex
  - `CODE_WRITE_PREVIEW`：plan（走 Decision Gate）
  - `CODE_WRITE`：auto（修改用户源码，需人工决策）
- **两阶段拆分**：宿主先跑 INDEX_WRITE（scope_update → reindex）再跑 plan，
  索引维护不再整单进入代码修改审批。

量化效果（350A）：

```
scope_update(ignore += *.py,*.pyc) → scope_changed=true
reindex → action=scope_rebuild, scope_summary: project=285 third_party=693 non_build=178
python 文件进 Index: 0   operation_class=INDEX_WRITE changes_code=false
```

### 后续方向（规划中，未实现）

- `INDEX_READ` / `INDEX_WRITE` / `CODE_WRITE` 的三面拆分已就绪（见上），
  进一步将 `CODE_WRITE`（改源码）与索引维护在宿主侧策略上彻底分离。

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

[Unreleased]: https://github.com/Charlie-yangjiaqi/AgentX/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/Charlie-yangjiaqi/AgentX/releases/tag/v0.1.1
[0.1.0]: https://github.com/Charlie-yangjiaqi/AgentX/releases/tag/v0.1.0
