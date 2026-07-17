# QMem V3.3 架构文档（增量）

> **本文是 `QMem-V3.2-Architecture.md` 的增量补充**，只记录 V3.2 → V3.3 的变化。
> 完整架构（表结构、RRF、共识机制、GUI、审计等）见 V3.2 文档，本文不重复。
> V3.0 基线见 `QMem-V3.0-Architecture.md`。

## 版本定位

V3.2 解决的是"软删除落地、移除 session_id 残留、文档版本对齐"等遗留工程问题。
V3.3 解决的是**另一个维度的问题**：面对 Token 爆炸与记忆污染的宏观治理。

V3.3 的全部改动集中在 `mcp_server.py`，无 schema 变更、无新依赖、无后台进程。本地工具数 16 → 17。

---

## 一、问题诊断（基于真实数据，非理论推断）

针对"QMem 缺乏宏观管理导致向量臃肿/污染 Prompt"的外部分析，先以真实库数据校准。

### 1.1 事实纠正

1. **不存在"向量臃肿"**：真实库（`D:\cly-marketplace\qmem\mcp\core_memory.db`）71 条存活记忆，平均 1592 字节/条，总计 113KB（≈3.7 万中文字）。一次 `mem_context` 摘要召回撑死几千 token，谈不上臃肿。"向量碎片臃肿"是大几千条以上才显现的问题，当前规模不存在。

2. **参照系纠错**：外部分析称"Claude Code 用 Auto Dream（后台空闲子模型压缩 200 行 Markdown）"——**事实错误**。Claude Code 实际机制是 **auto-compact**：上下文窗口接近 **95%** 时触发，把早期对话**原地摘要**后重建上下文（[platform.claude.com](https://platform.claude.com/docs/en/build-with-claude/compaction)、[decodeclaude.com](https://decodeclaude.com/compaction-deep-dive/)）。它是**单会话内**机制、有明确 95% 阈值，非"后台空闲/子模型/200 行笔记"。不应基于错误前提设计。

3. **已有对抗基础**：QMem 并非"完全缺乏宏观管理"——`consensus_health_check`（域内去重）、`cross_project_health_check`（跨项目重复检测）、`topic_key` upsert 锚点已存在。实测 `changzhou-balance-plan` 有 8 条记忆被 upsert 覆盖过（updated_at > created_at），说明锚点在阻止重复堆积。

### 1.2 真实存在的三个病症

| 病症 | 证据 | 严重度 |
|---|---|---|
| A. 写入盲目性：AI 不知该 upsert 还是新建，倾向无脑新建 | changzhou reference 堆到 21 条；topic_key 多数为空（锚点形同虚设） | 🔴 高（污染源头） |
| B. 进度类永不过期：progress 标"超30天需复审"但无机制执行复审 | 14 条 progress，部分已推进但旧版仍占位 | 🟡 中（噪声源） |
| C. 检索无去重/无质量分：RRF 只管相关度，不管"是否过时/冗余/低质" | `mem_recall` 返回 top-N 不带质量信号 | 🟡 中（污染出口） |

---

## 二、治理方案（两阶段，治本优先）

**核心原则**：QMem 是寄生系统（无后台进程、无外部 API、全本地触发）。所有治理走"**显式、本地、AI 主导**"路径，不引入后台守护或自动合并——自动合并是宿主 Agent 的职责。

### 2.1 第一阶段（治本：堵住"进"和"出"两口）

**① 写入门禁（对抗病症 A，`_save` + `_nearest_neighbor`）**
- `mem_save` 纯新增路径写入前，用 `title+content` 向量查本项目 q4 近邻（threshold=0.85）。
- 命中已有高相似记忆时**返回 candidates 拦截**（含候选 obs_id/title/topic_key/相似度），提示 AI 决策：是同主题更新→改用 `mem_update`，还是确为新主题→`mem_save(force=true)` 放行。
- 复用 `BGEEmbedding` + `memory_vectors`，零新依赖。用 `vec_distance_cosine` 函数式 API（与 `search_rrf.semantic_search` 同款），**不用 vec0 MATCH/k 语法**（后者不能与普通列 WHERE 混用）。
- upsert 路径（topic_key 命中）不经门禁——那是更新不是重复。

**② 检索去重 + 质量信号（对抗病症 C，`_enrich_results`）**
- `_recall` / `_consensus_recall` 返回前后处理：结果集内两两相似度 > 0.9 的，只留 score 最高的，其余标记 `deduped`。
- 每条结果附带质量信号：
  - `staleness`：progress 类距今天数 + `overdue`（是否超 30 天复审线）；其他类为 `null`。
  - `is_superseded`：本项目内是否有同 topic_key 且 updated_at 更大的存活记忆覆盖它。
- AI 看到信号自然会降权/跳过低质条目。

**③ `_delete` 返回值精确化（`rowcount` 替代 `total_changes`）**
- `conn.total_changes` 是累计值，会把 FTS/向量同步触发器（trg_fts_*）引发的额外变更一起算（软删 1 行实际返回 5），误导调用方。
- 改用 `cursor.rowcount` 精确计数目标表受影响行数。`_add_consensus_ref` 同步修正（INSERT OR IGNORE 的"是否新建"语义）。

### 2.2 第二阶段（治标清理 + 主动遗忘）

**④ 过期复审拉模型提示（对抗病症 B，`_context` 附带 `review_queue`）**
- `mem_context` 开场顺手用 `WHERE type='progress' AND updated_at < datetime('now','-30 days')` 查出本项目过期 progress，挂到返回的 `review_queue` 字段。
- AI 开场看到提示，自然用 `mem_get_full` 拉来核实、已推进的 upsert 更新、已作废的软删。**零定时任务、零后台进程**——开场召回是 AI 必经之路，搭便车提示。
- 拉模型触发优于推模型：寄生系统不该有后台守护。

**⑤ 单项目 consolidate 工具（消化存量堆积，`_consolidate_project`）**
- 新增 `mem_consolidate_project`：检测同一 project 内互相相似的 q4 记忆对（`cross_project_health_check` 的单项目版）。
- 返回相似对，标注 `keep`（较新，合并后保留）与 `redundant`（软删候选），提示 AI 用 `mem_update` 融合 + `mem_delete` 软删冗余。
- 实测 changzhou（threshold=0.80）检出 10 对：如 `模块二全部4Phase完成总结` ↔ `模块二Phase3+4完成总结` sim=0.92、`模块2.3发电计划上报` ↔ `模块2.7可调能力上报` sim=0.915——正是分阶段同构进度的真实冗余。

---

## 三、工具清单变化（V3.2 16 → V3.3 17 个本地工具）

| 工具 | 变化 | 用途 |
|---|---|---|
| `mem_save` | 参数 +`force`，行为 +门禁拦截 | 写入前近邻预检，命中返回 candidates |
| `mem_recall` | 返回 +质量信号 +去重 | staleness / is_superseded / deduped |
| `consensus_recall` | 返回 +质量信号 +去重 | 同 mem_recall |
| `mem_context` | 返回 +`review_queue` / `review_count` | 过期 progress 拉模型复审提示 |
| `mem_delete` | 返回 rowcount 精确计数 | soft_deleted/hard_deleted 精确到目标行 |
| `mem_consolidate_project` | ★ 新增 | 单项目内相似簇检测（存量治理） |

---

## 四、关键技术细节

1. **vec0 的 `MATCH/k` 语法不能与普通列 WHERE 混用**（报 `no such column: memory_vectors`），近邻查询必须用 `vec_distance_cosine` 函数式 API（与 `search_rrf.semantic_search` 同款）。

2. **`conn.total_changes` 是累计值含触发器变更**，精确计数用 `cursor.rowcount`。软删 1 行时 total_changes=5（含 trg_fts_update/trg_vector_delete 等），rowcount=1。

3. **写入门禁测试用例陷阱**：短 query（如"常州项目架构"）与库内条目相似度才 0.48，无法触发 0.85 门禁；必须用完整原文当 query 才能触发。这不是 bug，是短文本语义太泛。

4. **`_enrich_results` 的 `is_superseded` 查询**：用 meta 里已有的 `updated_at` 做参数化比较，避免相关子查询（`COALESCE((SELECT ...))`）的低效。

---

## 五、验证记录（2026-07-17）

端到端全部断言通过（真实库 `D:\cly-marketplace\qmem\mcp\core_memory.db`）：
- **门禁**：复制 changzhou 现有条目完整内容重写 → sim=0.993 命中拦截，返回 2 个 candidates；`force=true` 放行 created。
- **rowcount**：软删返回 `soft_deleted: 1`（修复前是 5）。
- **质量信号**：`_recall` 每条结果带 `staleness` + `is_superseded` 字段；reference 类 staleness=null 正确。
- **review_queue**：`mem_context` 返回含 `review_queue`/`review_count`（changzhou 当前 progress 均在 30 天内，count=0，机制正确）。
- **consolidate**：changzhou threshold=0.80 检出 10 对真实冗余簇。
- **工具注册**：`mem_consolidate_project` 已在 tools/list，`mem_save.force` 参数存在，`mem_context` 描述含 review_queue。

---

## 附：路径澄清

> ⚠️ `WINDOWS_SETUP_GUIDE.md` 中的 `C:\QMem\` 是**未部署的占位路径**，源自部署示例。该目录已被删除，当前不存在。真实运行路径：

| 实体 | 真实路径 |
|---|---|
| 核心记忆库 | `D:\cly-marketplace\qmem\mcp\core_memory.db` |
| 调用审计库 | `D:\cly-marketplace\qmem\mcp\call_log.db` |
| MCP 服务入口 | `D:\cly-marketplace\qmem\mcp\python\mcp_server.py` |

---

> 本文以 `D:\cly-marketplace\qmem\mcp\` 源码实际行为为准。
