# QMem MCP 全面分析（源码 + 真实库实测）

> **日期**：2026-07-17
> **方法**：不依靠任何会话记忆，从 `D:\cly-marketplace\qmem\mcp\` 全部源码出发，结合真实库 `core_memory.db` 实测验证。
> **定位**：本文是 QMem 的**横切面体检报告**——系统是什么、怎么实现、真实跑起来什么状态、发现哪些问题、本次修了什么。完整机制细节见 `QMem-V3.2-Architecture.md`，V3.3 治理改动见 `QMem-V3.3-Architecture.md`。

---

## 一、QMem 是什么

**QMem 是一个自托管的 AI 跨会话记忆系统**，以 MCP（Model Context Protocol）server 形态运行，通过 JSON-RPC over stdin/stdout 与宿主 Agent（Claude Code / Cursor）通信。

- **定位**：寄生系统——无后台进程、无外部 API、全本地触发。所有记忆操作由 AI 在会话中显式调用工具驱动，自动合并/后台守护是宿主 Agent 的职责，不是 QMem 的。
- **真实运行路径**（实测确认）：
  - 根目录：`D:\cly-marketplace\qmem\mcp\`
  - 记忆库：`core_memory.db`（2.8MB）
  - 审计库：`call_log.db`（独立 WAL 文件）
  - MCP 入口：`python\mcp_server.py`
- **占位路径说明**：早期文档/示例中的 `C:\QMem\` 是已废弃的部署占位路径，该目录已删除、资源已清空。本次已将所有功能性文件与文档中的占位路径更新为真实路径（详见第八节）。

---

## 二、技术栈与依赖

| 层 | 技术 | 说明 |
|---|---|---|
| 嵌入模型 | `bge-small-zh-v1.5`（ONNX） | 512 维，本地 CPU 推理，mean pooling + L2 归一化，tokenizer 截断 510 token |
| 向量存储 | `sqlite-vec`（vec0 虚表） | cosine 距离，512 维 |
| 全文检索 | SQLite FTS5 | external-content 模式，指向 `memory_facts`，unicode61 分词 |
| 数据库 | SQLite | 单库多表 + 4 个同步触发器 |
| 运行时 | Python 3.13（实测） | 仅依赖 `sqlite-vec / onnxruntime / numpy / tokenizers / huggingface-hub` |
| 外部协作 | `codebase-memory-mcp.exe`（257MB 原生 C 二进制） | 通过 subprocess 包装转发代码图谱查询 |

`requirements.txt` 极简（5 个包），全内网可装。模型缺失/onnxruntime 未装时降级返回零向量，保证不崩。

---

## 三、数据模型（schema.sql 实读）

**核心是"单表全家桶"架构**——动态记忆（q4）和共识（consensus）共用一张表，靠 `tier` 字段区分：

```
memory_facts (主表) — 13 列（已清理旧 Engram 遗留列）
├── id, obs_uuid(12位hex), project, topic_key, title, content
├── type(reference/progress/decision/bugfix/learning/manual)
├── tier(q4 | consensus)
├── origin_project  ← promote 前来源（demote 回溯依据）
├── content_hash    ← sha256(title+content)[:16]
└── created_at, updated_at, deleted_at

（2026-07-17 已 DROP：scope / session_id / pinned / review_after —— 旧 Engram 时代遗留或从未使用）

memory_vectors (vec0 虚表) — rowid 对齐 memory_facts.id，512 维 cosine
memory_facts_fts (FTS5)   — external-content，指向 memory_facts，content_rowid=id
project_refs              — project↔consensus_domain 多对多引用（promote/manual 两种来源）
```

**4 个触发器**保证 facts 与 vectors/fts 一致：
- `trg_fts_insert` / `trg_fts_update` / `trg_fts_delete`：FTS 同步
- `trg_vector_delete`：删除 facts 时清向量

⚠️ **没有 update 触发器同步向量**——所以 `_update` 改 content/title 时必须**手动 DELETE+INSERT 向量**（`mcp_server.py:432-433` 确实这么做）。这是一个隐式约束，改 `_update` 时必须记得。

**实测一致性**（清理后基线）：存活 72 条 = 向量 72 条 = FTS 72 条，三表完全对齐。

---

## 四、17 个本地工具的完整机制

### 写入类（4 个）

| 工具 | 机制要点 |
|---|---|
| **`mem_save`** | ① **共识域守卫**：project_id 是已有 consensus 域时拦截 q4 写入（防孤儿）→ ② **topic_key upsert**：同 project+topic_key+tier=q4 命中则更新（不经门禁）→ ③ **写入门禁**（V3.3）：纯新增路径用 `_nearest_neighbor` 查本项目 q4 近邻，相似度 >0.85 返回 candidates 拦截，`force=true` 放行 |
| `mem_update` | 越权守卫（consensus 需 `confirm_consensus`）+ 脐带剪断强制（改 consensus content 必须声明 origin_project 去留）；content/title 变更**手动重算向量** |
| `mem_delete` | 默认**软删除**（标记 deleted_at，可恢复）；`hard=true` 物理删除；空域/空项目自动清理 project_refs；返回值用 `cursor.rowcount`（V3.3 修正，非 `total_changes`） |
| `memory_promote` | 原地飞升：UPDATE tier+project+origin_project + 建 ref，不挪数据不重算向量 |

### 读取类（5 个）

| 工具 | 机制要点 |
|---|---|
| **`mem_recall`** | RRF 混合检索（BM25+cosine，k=60）+ **三步法配额**（q4 min 2 + consensus min 2）+ `_enrich_results` 去重与质量信号（V3.3） |
| `mem_context` | 开场召回：q4 全量（硬上限 100，摘要 100 字预览）+ 引用共识 top-N + **review_queue**（超 30 天的 progress，V3.3 拉模型复审提示） |
| `mem_get_full` | 按 obs_id 拉全文，支持逗号分隔批量（上限 20） |
| `consensus_recall` | 专搜 consensus tier，同样过 `_enrich_results` |
| `mem_search` | FTS5 MATCH 精确过滤（仅 q4），失败降级 LIKE |

### 治理类（8 个）

| 工具 | 机制要点 |
|---|---|
| `memory_demote` | 降级回 q4；**origin_project 为空则拒绝**（溯源黑洞防护）；不清理 refs（防过桥抽板） |
| `consensus_health_check` | 域内两两 embedding 点积 >0.85 报重复 |
| `cross_project_health_check` | **跨项目** q4 语义重复检测（promote 候选） |
| `mem_consolidate_project`（V3.3） | **单项目内**相似簇检测（消化存量堆积），返回 keep/redundant 对 |
| `add_consensus_ref` / `list_consensus_projects` / `mem_list_projects` / `init_project_context` | 引用管理、列举、目录身份探测 |

### CBM 转发

非本地工具（`search_graph` / `trace_path` / `detect_changes` / `get_architecture` 等代码图谱查询）由 `cbm_wrapper.py` 转发到 `codebase-memory-mcp` 子进程，**崩溃自动 respawn + 重试一次**，错误原样透传保留 CBM 的 error code/message 结构。CBM 子进程是独立 MCP server，启动时需 initialize 握手。

---

## 五、检索引擎细节（search_rrf.py）

**RRF（Reciprocal Rank Fusion）融合**：
- 词法路（FTS5 BM25，limit 20）+ 语义路（vec_distance_cosine，limit × 3）
- 同一 rank 空间，分数 = `Σ 1/(k + rank + 1)`，k=60
- `min_similarity` 只过滤语义路（词法路不过滤）
- `projects` / `tiers` 复数 IN 过滤，**词法路和向量路同源**（保证候选池一致）

**关键 API 选择**：向量查询用 **`vec_distance_cosine(mv.embedding, ?)` 函数式 API**，而非 vec0 的 `MATCH/k` 语法——后者不能与普通列 WHERE 混用（报 `no such column: memory_vectors`）。`distance ∈ [0,2]`，`similarity = 1 - distance`。这个选择贯穿 `search_rrf.semantic_search` 和 `mcp_server._nearest_neighbor`。

---

## 六、V3.3 治理层

| 治理点 | 对抗问题 | 实现 |
|---|---|---|
| ① 写入门禁 | 写入盲目性（无脑新建） | `_nearest_neighbor` + candidates 拦截 + force 放行 |
| ② 检索去重+质量信号 | 检索出口污染 | `_enrich_results`：staleness / is_superseded / deduped(>0.9) |
| ③ rowcount 精确化 | 误导调用方 | `cursor.rowcount` 替代 `conn.total_changes`（软删 1 行 total_changes=5） |
| ④ review_queue | progress 永不过期 | `mem_context` 拉模型复审提示（零后台） |
| ⑤ 单项目 consolidate | 存量堆积 | `mem_consolidate_project` 检测+提示 AI 合并 |

---

## 七、实测验证（真实库，本会话亲跑）

| 测试项 | 结果 |
|---|---|
| 三表一致性 | 清理后 facts=72 = vectors=72 = fts=72 ✅ |
| `mem_context` | changzhou own=40, consensus=5, review_count 机制正确 ✅ |
| `mem_recall` 质量信号 | 每条带 staleness / is_superseded ✅ |
| **写入门禁** | 复制 changzhou q4 原文 → sim=**1.0000** 拦截，3 个 candidates ✅；新颖内容放行 ✅ |
| `mem_consolidate_project` | changzhou threshold=0.85 → 检出多对真实冗余（如"模块二全部4Phase总结"↔"Phase3+4总结" sim=0.92）|
| `mem_delete` rowcount | 软删返回 `soft_deleted:1`（非 5）✅，deleted_at 正确置位 |
| 工具注册 | local_tools=17，schema 声明含 CBM 转发共 31 ✅ |
| py_compile | 全 9 个 Python 文件 OK ✅ |

> ⚠️ **门禁测试用例陷阱**：若用 `_recall` 返回的第一条（常是 consensus 记忆）的内容去测 q4 门禁，会因为跨 tier 查不到近邻而误判"门禁失效"。必须用**真实的 changzhou q4 记忆原文**才能触发 sim>0.85。这是测试取材问题，非门禁 bug。

---

## 八、实测发现的真实问题与本次修复

> 本节是本次会话的**问题清单 + 修复记录**，全部基于源码与真实库实测，非臆测。

### 8.1 已修复（代码/配置/文档类）

| # | 问题 | 修复 |
|---|---|---|
| A | **serverInfo version 滞后**：`_init()` 返回 `version="3.2"`，但已实现 V3.3 全部治理 | `mcp_server.py:147` 改为 `"3.3"`；`check_db.py` 的显示 version 同步改 3.3 |
| B | **`check_db.py` 硬编码 `C:\QMem\core_memory.db`**，直接运行 Failed | 改为基于脚本目录定位真实库（与 mcp_server.py 的 DBPATH 同源逻辑） |
| C | **`test_call_log.py` LOG_DB 硬编码 `C:\QMem\call_log.db`** | 改为 `os.path.join(_DIR, "..", "call_log.db")` 相对定位 |
| D | **`gui/server.py` 注释写 `C:\QMem\`**（代码逻辑用相对路径，本就正确） | 注释更新为真实路径说明 |
| E | **`mcp_config_example.json` 示例用 `C:\QMem\`** | 改为 `D:\cly-marketplace\qmem\mcp\python\start_python_mcp.bat` |
| F | **`WINDOWS_SETUP_GUIDE.md` 9 处 `C:\QMem\`** | 全量更新为真实路径 + 头部加占位路径废弃说明 |
| G | **V3.2 架构文档**：头部位置行 + 正文 6 处 `C:\QMem\` + version 提及 | 全量更新 + 加 V3.3/废弃路径说明 |
| H | **V3.0 架构文档**：头部位置行 | 更新 + 加历史快照脚注（正文历史叙事保留，最小修改） |

### 8.2 已修复（脏数据类）

| # | 问题 | 修复 |
|---|---|---|
| I | **`type` 非法值 `'project'`（2 条）**：id=197/198（模块二进度总结），不在合法 enum 内 | 用 `mem_update` 改为 `progress`（语义正确：阶段性进度）。修复后 type 全部归位 reference/progress/bugfix/decision |
| J | **project_refs 自引用** `(power-grid-domain → power-grid-domain, promote)`：一次错误 self-promote 产物，功能无害但属脏行 | 删除该行（project_refs 18→17） |
| K | **测试污染**：本会话功能验证时，`_save` 复制 consensus 内容到 changzhou q4 产生 1 条重复（id=216），另 1 条新颖度测试（id=218）已软删 | 两条均硬删清除（id=216/218），三表重新一致 |
| L | **遗留列 `scope/session_id/pinned` + 冗余字段 `review_after`**：scope/pinned 是旧 Engram 时代残留（72 条全非空但代码零引用），session_id 已全空，review_after 从未使用 | **`ALTER TABLE DROP COLUMN` 删除 4 列**（SQLite 3.50.4 支持；删前先 DROP `idx_facts_scope`/`idx_facts_pinned` 索引，且**必须加载 sqlite_vec 扩展**否则触发器重建失败）。同步清理 schema.sql 定义、`_REQUIRED_COLUMNS` 迁移字典、gui SELECT。库 VACUUM 后 2.8MB→2.6MB |

### 8.3 已知但未改（记录备查，遵循最小修改原则）

| # | 问题 | 为何不改 |
|---|---|---|
| M | **`_save` 的 type 校验形同虚设**：`if not obs_type or obs_type == "manual" and not args.get("type")` —— `obs_type` 默认 `"manual"` 永远非空，校验永不触发，任何字符串能落库（这正是问题 I 的 `project` 能写入的根因） | 改校验逻辑会影响现有写入行为，需用户确认范围；本次只清数据。**建议后续加 enum 白名单校验** |
| N | **`add_consensus_ref` / `memory_promote` 无自引用防护**（project==consensus_domain 时不拦截，问题 J 的根因） | 同上，加防护属逻辑增强非缺陷修复，留待确认 |
| O | **changzhou 单项目 40 条（占 q4 63%）**，consolidate 检出大量相似对 | 属业务记忆治理（逐对核实+融合+软删），用户明确指示**稍后处理**，本次不动 |

---

## 九、架构评价

### 优点

1. **寄生设计纯粹**——零后台进程，所有治理靠 AI 显式工具调用 + 拉模型提示（review_queue 搭开场召回便车），符合宿主 Agent 主导原则。
2. **单表全家桶巧妙**——tier 字段区分层级，promote/demote 只 UPDATE 不搬数据不重算向量，O(1) 操作。
3. **三层防呆到位**——共识域守卫、越权 confirm、溯源黑洞防护、软删除可恢复。
4. **门禁+去重+复审**形成"进-存-出"完整治理闭环（V3.3）。
5. **CBM 转发**让记忆系统与代码图谱系统职责分离又协同——记忆存"代码读不出的决策/背景/踩坑"，代码事实（函数签名/调用关系/表字段）查 CBM。

### 风险点

1. **嵌入模型固定 512 维**——换模型需全量重算，向量表无 model_version 字段。
2. **两两组合 O(n²)**——`consolidate` / `health_check` 在记忆破千后会慢（当前 72 条无感）。
3. **type 校验缺失**（问题 L）——非法 type 值能落库，需靠纪律而非代码保证。
4. **自引用无防护**（问题 M）——promote/ref 时 project==domain 不拦截。
5. **`_update` 向量重算靠手动**——无触发器兜底，改代码时易遗漏（当前实现正确，但属隐式约束）。

---

## 十、修复后基线状态（2026-07-17）

```
alive=72  softdel=0  |  vectors=72  fts=72  |  refs=17(无自引用)  |  db=2.6MB
三表一致 ✅   type 全合法 enum ✅   version=3.3 ✅   占位路径已清除 ✅
memory_facts 13 列（已 DROP scope/session_id/pinned/review_after）✅

tier:  q4=63  consensus=9
type:  reference=44  progress=17  bugfix=8  decision=3
q4 集中项目:  changzhou-balance-plan=40  frame-cz-plan=3  其余各 1-2
consensus 域: power-grid-domain=7  weakpwd=1  java-cloud-common=1
```

---

## 附：文件清单与职责

| 文件 | 行数 | 职责 |
|---|---|---|
| `python/mcp_server.py` | 1123 | 主服务：17 工具 dispatch + 调用审计 + 治理层 |
| `python/search_rrf.py` | 179 | RRF 混合检索引擎（BM25+cosine） |
| `python/embedding.py` | 62 | BGE ONNX 嵌入（512 维，CPU） |
| `python/cbm_wrapper.py` | 113 | CBM 子进程转发（崩溃恢复） |
| `python/init_project_context.py` | 101 | 目录身份探测（git/pom/package.json） |
| `python/qmem_cli.py` | 127 | CLI 入口 |
| `python/schema.sql` | 90 | DDL（4 表 + 4 触发器 + 索引） |
| `python/test_call_log.py` | 254 | 调用日志 7 场景测试 |
| `check_db.py` | 36 | DB 状态检查 |
| `gui/server.py` | 247 | 只读可视化 HTTP（端口 8765） |

---

> 本文以 `D:\cly-marketplace\qmem\mcp\` 源码与真实库实测为准，2026-07-17 生成。
