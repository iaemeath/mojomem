# QMem V3.2 完整架构文档

> **版本**：V3.2（在 V3.0 单表全家桶 + 虚拟外键引用基础上，新增调用审计 / 摘要化召回 / 跨项目健康检查 / 可视化 GUI / 共识域写守卫）
> **日期**：2026-07-17
> **位置**：`D:\cly-marketplace\qmem\mcp\python\mcp_server.py`（MCP server）、`D:\cly-marketplace\qmem\mcp\core_memory.db`（记忆数据）、`D:\cly-marketplace\qmem\mcp\call_log.db`（调用审计）、`D:\cly-marketplace\qmem\mcp\gui\`（可视化）
> **定位**：跨会话记忆唯一来源。替代了原 Engram + mem_search_vector + codebase-memory 三套系统。
>
> ⚠️ 本文档以**源码实际行为**为准。V3.3 治理改动（写入门禁/检索去重/review_queue/consolidate）见 `QMem-V3.3-Architecture.md`，`serverInfo.version` 现为 `3.3`。
> ⚠️ 早期文档中的 `C:\QMem\` 是已废弃占位路径，该目录已删除、资源已清空，真实路径以本文为准。

---

## 〇、版本演进速览（V3.0 → V3.2）

| 版本 | 关键变化 | 源码落点 |
|---|---|---|
| V3.0 | 单表全家桶 + tier 层级 + project_refs 引用图谱 + 14 个工具 + RRF 混检 + 三步法配额 | `mcp_server.py` 基础框架 |
| V3.1 | **mem_context 摘要化**：返回 content 前 100 字预览 + has_more，配合新增的 `mem_get_full` 按需拉全文（防爆 token） | `_context()` / `_get_full()` |
| V3.1 | **mem_save 共识域写守卫**：向已存在的共识域名直接写 q4 时拦截，提示走 promote 流程，防孤儿记忆 | `_save()` 开头 |
| V3.1 | **工具数 14 → 16**：新增 `mem_get_full`、`cross_project_health_check` | `local_tools` / `_tools_list()` |
| V3.2 | **调用审计日志**：独立 `call_log.db`（WAL），记录每次工具调用的耗时/成功/参数摘要/响应大小/会话标签，90 天自动清理，写入失败绝不影响工具返回 | `_init_log_db()` / `_log_call()` / `_tools_call()` finally |
| V3.2 | **记忆可视化 GUI**：零依赖只读 HTTP 服务（端口 8765），记忆列表浏览 + project_refs 引用图谱可视化 | `gui/server.py` + `gui/index.html` + `gui/graph.html` |
| V3.2 | **版本号/字段名同步修正**：serverInfo→3.2、check_db→3.2、CLI 注释→3.2、CLI context 分支字段名（`observations`→`own_memories`/`consensus_memories`）对齐、删除 uint8 死代码回退逻辑（目录无该文件）、SETUP 指南补 16 工具+GUI+审计 | 多文件 |
| V3.2 | **mem_delete 默认软删除**：默认 `UPDATE SET deleted_at`（可恢复，防误删），`hard=true` 物理删除；移除预留的 `session_id` 列 | `mcp_server.py:_delete()` / `schema.sql` / `_REQUIRED_COLUMNS` |

> 以下各章按"当前实际行为"完整描述；与 V3.0 一致的部分只点出差异，不重复铺陈。

---

## 一、系统定位

QMem 是一套自建的轻量级 AI 跨会话记忆系统，以 MCP（Model Context Protocol）server 形式供 LLM 客户端调用。

它解决的核心问题：**LLM 每次会话是无状态的**，需要把"上次会话学到的项目知识、踩过的坑、做的决策"持久化下来，并在新会话开场按需召回。

它管理两类知识：

| 类别 | 存储方式 | 检索方式 |
|---|---|---|
| **动态记忆**（决策理由/踩坑根因/任务进度） | SQLite `memory_facts` 表 + 向量 + FTS5 | `mem_recall`（RRF 混合检索） |
| **代码事实**（函数/类/调用图） | 转发给原生 `codebase-memory-mcp.exe` | `search_graph` / `trace_path` 等（CBM 转发） |

---

## 二、整体架构（V3.2）

```
┌──────────────────────────────────────────────────────────────┐
│  LLM 客户端 (ZCode / Cursor / Claude Desktop / Claude Code)   │
│  通过 JSON-RPC over stdin/stdout 调用 MCP 工具                │
└──────────────────────────┬───────────────────────────────────┘
                           │
        ┌──────────────────▼───────────────────────┐
        │  QMemMCP (mcp_server.py v3.2)            │  ← Python 主进程
        │  - 16 个本地工具                          │
        │  - 转发 CBM 工具（代码查询）              │
        │  - 调用审计埋点（每次 tools/call）        │
        └───┬─────────┬───────────┬──────────┬─────┘
            │         │           │          │
   ┌────────▼──┐  ┌───▼────┐  ┌───▼──────┐  │ stdin/stdout
   │BGEEmbedding│  │Hybrid  │  │CBMWrapper│  │ JSON-RPC
   │(ONNX 本地) │  │Searcher│  │(subprocess)│ │
   │512维向量   │  │(RRF)   │  │           │ │
   │uint8 优先  │  └───┬────┘  └────┬──────┘ │
   └────┬──────┘      │            │        │
        │      ┌───────▼────────┐   │        │
        │      │core_memory.db  │   │        │  ┌─────────────────────┐
        │      │+ vec0 + FTS5   │   │        │  │codebase-memory-mcp  │
        │      │+ project_refs  │   │        │  │.exe (257MB 原生 C)  │
        │      └────────────────┘   │        │  │- graph.db.zst 图谱  │
        │                           │        │  │- LSP 调用/数据流    │
        │      ┌────────────────────▼──┐     │  └─────────────────────┘
        │      │call_log.db (WAL)      │     │
        │      │调用审计：耗时/成功/   │     │
        │      │参数摘要/响应大小/     │◄────┘ 每次 tools/call 后异步落库
        │      │session_tag，90天清理  │
        │      └───────────────────────┘
        │
        │      ┌───────────────────────────┐
        └─────►│gui/server.py (只读 HTTP)  │  端口 8765
               │记忆列表 + 引用图谱可视化   │  零依赖标准库
               └───────────────────────────┘
```

**四层分工**：

| 层 | 组件 | 职责 |
|---|---|---|
| 接入层 | `mcp_server.py`（QMemMCP） | JSON-RPC 协议处理、工具分发、本地工具实现、调用审计埋点 |
| 记忆层 | `core_memory.db`（SQLite + sqlite-vec + FTS5） | 记忆事实存储、向量索引、全文索引、引用图谱 |
| 审计层 | `call_log.db`（SQLite WAL，独立文件） | 工具调用流水记录，供后续优化分析（不加载 vec 扩展） |
| 代码层 | `codebase-memory-mcp.exe`（CBM） | 函数/类/调用图等代码结构查询 |
| 可视化层 | `gui/server.py`（HTTP 8765） | 只读浏览记忆列表与引用图谱 |

---

## 三、数据库设计

### 3.1 核心表：`memory_facts`（core_memory.db）

所有记忆（动态 + 共识）在同一张表，通过字段区分层级和生命周期：

```sql
CREATE TABLE memory_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    obs_uuid TEXT UNIQUE NOT NULL,          -- 全局唯一 ID（12 位 hex）
    project TEXT NOT NULL DEFAULT '',       -- 项目名（动态记忆）或共识域名（consensus，tier 字段区分）
    topic_key TEXT DEFAULT '',              -- 主题分类（arch/workflow/m1.4 等，纯主题不混生命周期）
    title TEXT DEFAULT '',
    content TEXT NOT NULL,                  -- 正文
    type TEXT DEFAULT 'manual',             -- ★ 必填。语义类型+生命周期：reference(稳定)/progress(易过期)/decision/bugfix/learning/manual
    tier TEXT NOT NULL DEFAULT 'q4',        -- ★ 层级：q4(动态草稿) / consensus(跨项目共识)
    origin_project TEXT DEFAULT '',         -- ★ promote 前的原始 project（脐带机制）
    content_hash TEXT DEFAULT '',           -- sha256(title+content)[:16]，变更检测
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    review_after TIMESTAMP,                 -- 易过期信息复审时间
    deleted_at TIMESTAMP                    -- ★ 软删除时间戳（NULL=存活；mem_delete 默认软删，可恢复）
);
```

> 说明：V3.2 已移除预留的 `session_id` 列（schema.sql + `_REQUIRED_COLUMNS` 均已删除；已存在库中该空列因 SQLite 无法 DROP COLUMN 而无害保留，代码不读写）。`deleted_at` 已启用软删除路径：`mem_delete` 默认 `UPDATE SET deleted_at`（可恢复），传 `hard=true` 走物理删除。所有检索路径均带 `deleted_at IS NULL` 守卫，软删行不会被命中。

### 3.2 向量虚表：`memory_vectors`

```sql
CREATE VIRTUAL TABLE memory_vectors USING vec0(
    embedding float[512] distance_metric=cosine
);
```

- 用 `bge-small-zh-v1.5` ONNX 模型生成 512 维向量
- **rowid 与 memory_facts.id 对齐**，JOIN 即可拿到元数据
- vec0 不支持 UPDATE embedding，更新时需"删后重建"（`_save`/`_update` 已处理）

### 3.3 全文索引：`memory_facts_fts`

```sql
CREATE VIRTUAL TABLE memory_facts_fts USING fts5(
    title, content, topic_key, type, project,
    content='memory_facts', content_rowid='id',
    tokenize='unicode61'
);
```

- external-content 模式，避免数据冗余
- BM25 排序，英文标识符精确匹配强（中文盲区由向量路补）

### 3.4 引用图谱：`project_refs`

```sql
CREATE TABLE project_refs (
    project TEXT NOT NULL,                  -- 真实项目（如 bfo_zj_yxyd）
    ref_project TEXT NOT NULL,              -- 共识域（如 java-cloud-common）
    ref_source TEXT NOT NULL DEFAULT 'promote',  -- promote(自动建) / manual(add_consensus_ref 手动建)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (project, ref_project)
);
```

记录"哪个项目引用了哪个共识域"。`mem_context` / `mem_recall` 通过此表自动加载项目引用的共识。

### 3.5 ★ 调用审计表：`tool_call_log`（call_log.db，V3.2 新增）

```sql
CREATE TABLE tool_call_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    tool_name   TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'local',   -- local / cbm
    duration_ms INTEGER DEFAULT 0,               -- 单次调用耗时（毫秒）
    success     INTEGER NOT NULL DEFAULT 1,      -- 1=成功 0=失败
    error_msg   TEXT DEFAULT '',                 -- 失败时截断至 500 字符
    arg_summary TEXT DEFAULT '',                 -- args JSON 序列化后截断至 500 字符
    resp_size   INTEGER DEFAULT 0,               -- 返回结果 JSON 字节数
    session_tag TEXT DEFAULT ''                  -- 进程级会话标签（时间戳 YYYYMMDD_HHMMSS）
);
```

**关键设计**：

- **独立文件**：`call_log.db` 与 `core_memory.db` 物理分离，审计库**不加载 vec 扩展**，互不干扰。
- **WAL 模式**：`PRAGMA journal_mode=WAL`，高并发写入不阻塞。
- **自动清理**：每次 `_init_log_db` 执行 `DELETE FROM tool_call_log WHERE ts < datetime('now','-90 days')`。
- **绝不影响业务**：`_log_call` 全程 try/except 包裹，写入失败只打 stderr，不抛异常、不影响工具返回值。
- **延迟初始化**：`_init_log_db` 在 `initialize` 握手时调用一次，`_log_db_ready` 标志位避免重复建表。

### 3.6 触发器与索引

core_memory.db：3 个 FTS 同步触发器（insert/update/delete）+ 1 个向量清理触发器（delete）。8 个索引覆盖 project/topic_key/type/tier/origin_project/deleted_at/created_at。project_refs 2 个索引。
call_log.db：2 个索引（`idx_log_ts`、`idx_log_tool`）。

---

## 四、核心概念

### 4.1 tier 字段 —— 层级隔离

| tier 值 | 含义 | 检索方式 |
|---|---|---|
| `q4` | 项目动态记忆（草稿） | `mem_recall` / `mem_search`（默认只搜 q4） |
| `consensus` | 跨项目共识 | `consensus_recall`（专搜）/ `mem_context`（通过 ref 自动加载） |

tier 是**逻辑隔离**——共识和动态记忆同表共存，靠 WHERE 条件区分。共识域名是普通名称（如 `java-cloud-common`），不需要特殊前缀——`tier='consensus'` 是权威标识。mem_save 的 upsert 有 `AND tier='q4'` 守卫，保证写入永远不碰 consensus 行。

### 4.2 project_refs 引用图谱 —— 一处改变，处处引用

```
bfo_zj_yxyd ──引用──┐
bfo_cndz     ──引用──┼──→ java-cloud-common（共识，只存一份）
dispatch-app ──引用──┘      └─ IS_DELETE 中文值
                           └─ CLOB 列长度陷阱
```

- 项目通过 `project_refs` 引用共识域
- `mem_context` / `mem_recall` 自动加载引用的共识
- 共识修改后所有引用方下次加载自动看到新内容——因为读的是同一份，没有副本

### 4.3 origin_project 脐带机制 —— 溯源黑洞防护

| 时机 | origin_project 值 | 含义 |
|---|---|---|
| promote 时 | 原始 project 名 | 脐带存在，demote 可精确回退 |
| AI 合并精炼时 | AI 传 `origin_project=''` | 脐带剪断，demote 拒绝降级 |
| demote 时 | 检查是否为空 | 空 = 已融合多源，拒绝降级（溯源黑洞防护） |

mem_update 改 consensus content 时**强制要求显式声明** origin_project 去留——防止 AI 忘记剪断脐带导致 demote 防护被绕过。

### 4.4 lifecycle 维度

**type 字段（★ 必填）**：决定生命周期。每次 `mem_save` 必须标对，不传 type 会报错。

| type 值 | 生命周期 | 含义 |
|---|---|---|
| `reference` | 稳定 | 项目骨架/数据模型/踩坑根因/决策理由 |
| `progress` | 易过期 | 当前进度/未推送/待修复/完成度（超 30 天需复审） |
| `decision` | 稳定 | 架构决策带理由 |
| `bugfix` | 稳定 | 已修复的 bug 根因 |
| `learning` | 稳定 | 经验教训 |
| `manual` | 稳定 | 手动记录 |

- **topic_key（可选）**：纯主题分类。记忆少（≤2 条）时留空，记忆多（>3 条）时按主题命名（如 `arch`、`workflow`、`m1.4`）。
- **upsert 锚点**：同 `project + scope + topic_key + tier='q4'` 命中则更新。

---

## 五、16 个本地工具（V3.1 起 14 → 16）

| 工具 | 方向 | 用途 | 版本 |
|---|---|---|---|
| `mem_save` | Push | 写动态记忆（tier=q4）。type 必填。**★ 共识域写守卫：向已有共识域名写 q4 时拦截提示走 promote** | V3.1 增强守卫 |
| `mem_recall` | Pull | 单次同源 RRF：搜项目动态记忆 + 引用的共识域（三步法配额截取） | V3.0 |
| `consensus_recall` | Pull | 专搜共识库（tier=consensus） | V3.0 |
| `mem_search` | Pull | 精确过滤查找（FTS5 MATCH，仅 tier=q4） | V3.0 |
| `mem_update` | — | 更新记忆。consensus 需 confirm_consensus=true；改 content 需声明 origin_project | V3.0 |
| `mem_context` | Pull | 开场召回：**★ 摘要索引（title+content前100字+obs_id+has_more），q4 全量（硬上限100），共识防爆 top-N** | V3.1 摘要化 |
| **`mem_get_full`** | Pull | **★ V3.1 新增**：按 obs_id 拉完整内容，支持逗号分隔批量（上限 20），返回 not_found 列表 | V3.1 新增 |
| `mem_delete` | — | **★ 默认软删除**（标记 deleted_at 可恢复，防误删）；`hard=true` 物理删除（清 FTS/向量）。consensus 需 confirm_consensus=true；空域 refs 清理按 `deleted_at IS NULL` 计存活 | V3.2 软删除 |
| `memory_promote` | — | 提取共识：UPDATE tier+project+origin_project + 建 ref | V3.0 |
| `memory_demote` | — | 降级回动态。origin_project 为空则拒绝（溯源黑洞防护）。不碰 project_refs | V3.0 |
| `consensus_health_check` | Pull | 找共识域内 embedding>0.85 的相似记录对，提示 AI 精炼 | V3.0 |
| **`cross_project_health_check`** | Pull | **★ V3.1 新增**：检测**跨项目** q4 记忆的语义重复（默认阈值 0.85），发现可提取为共识的候选 | V3.1 新增 |
| `add_consensus_ref` | — | 手动建立项目→共识域引用（ref_source='manual'） | V3.0 |
| `list_consensus_projects` | Pull | 列出共识域（供 promote 选择目标） | V3.0 |
| `mem_list_projects` | Pull | 列出动态记忆的 project | V3.0 |
| `init_project_context` | — | 探测目录身份（git remote/pom/package.json） | V3.0 |

另有 CBM 转发工具（search_graph/trace_path/get_architecture/detect_changes 等），不在 `local_tools` 集合内，通过 `_dispatch_tool` 自动转发到 codebase-memory-mcp。

> **源码判定本地工具 vs 转发的依据**：`source = "local" if name in self.local_tools else "cbm"`。`local_tools` 集合是 16 个名字的硬编码 set。

---

## 六、核心算法

### 6.1 RRF 混合检索（`search_rrf.py`）

**单次查询同源 RRF**：q4 和 consensus 在同一个候选池里做 BM25 + cosine → RRF 融合。rank 空间统一，分数天然可比。

```
score = 1/(k + rank_fts + 1) + 1/(k + rank_vec + 1)    # k=60
```

- 词法路：FTS5 BM25，limit=20，MATCH 失败时降级 LIKE
- 向量路：vec0 cosine，limit=limit*3，返回 distance 换算 similarity = 1 - distance
- 融合：按 obs_id 聚合两路 RRF 分数，min_similarity 仅过滤语义路

**⚠️ 绝对不能拆成两次查询**：拆开后两个独立 rank 空间的 RRF 分数不可比——q4 池的 rank-0 和 consensus 池的 rank-0 拿到相同分数，合并后交错，融合失效。`hybrid_search_rrf` 通过 `projects`（复数 IN）+ `tiers`（复数 IN）参数在同一次 SQL 里同时过滤两路。

### 6.2 三步法配额截取（`_recall` 内部）

防止共识库数据量增长后淹没项目动态记忆：

```
第 1 步：hybrid_search_rrf 取 top-50 候选，按 tier 分组（q4_items / cons_items）
第 2 步：各自取保底配额（q4 保 2，consensus 保 2）——即使草稿排在第 49 名也必定上桌
第 3 步：剩余候选混合按 RRF 分数竞争空位（total_limit - 已占坑位）
最终：统一按 RRF 分数降序返回
```

**⚠️ 不能用顺序遍历即时填充**：高分 consensus 会提前填满坑位，q4 保底失效。

### 6.3 ★ 摘要化召回（`_context`，V3.1 改进）

```
1. 自身 q4 动态记忆：全量返回（硬上限 MAX_OWN=100，按 created_at DESC）
2. 查引用的共识域：project_refs → ref_list
3. 加载引用的共识：tier='consensus' AND project IN (ref_list)，防爆 top-N（默认 5）
4. 摘要化：content → content_preview（前 100 字）+ has_more（是否被截断）
   返回 own_memories / consensus_memories（均含 obs_id，不含全文）
5. AI 看完索引后用 mem_get_full(obs_id=) 按需拉指定记忆全文
```

**为什么改**：旧版 `mem_context` 直接返回全文，记忆数增长后开场召回 token 爆炸。摘要化把"列目录"和"读全文"拆成两步，AI 只为真正需要的记忆付全文 token。

### 6.4 嵌入模型（`embedding.py`）

- 模型：`bge-small-zh-v1.5`，ONNX 版，本地 CPU 推理
- **加载路径**：`onnx/model.onnx`（权重在 `model.onnx_data`，约 94MB）
- 维度：512
- 流程：tokenizer 截断 510 token（留 2 给 [CLS][SEP]）→ ONNX 推理 → mean pooling（带 attention_mask）→ L2 归一化
- 降级：onnxruntime/tokenizers 未装或模型缺失时返回零向量（保证不崩）

> ⚠️ 实测本机 `bge-small-zh-v1.5-onnx/onnx/` 下只有 `model.onnx` + `model.onnx_data`（94MB），无 `model_uint8.onnx`，故实际走回退路径。若要启用 uint8 量化加速，需补放量化模型文件。

### 6.5 CBM 转发（`cbm_wrapper.py`）

subprocess 管道通信（stdin/stdout 文本行协议），stderr 重定向避免缓冲区满死锁。启动时 `initialize` + `notifications/initialized` 握手。`send_request` 检测 `process.poll()` 崩溃后自动 `_spawn` 重启并重试一次。`tools/list` 时合并本地 16 工具 + CBM 的全部工具，对客户端透明。错误原样透传（保留 CBM 的 error code/message 结构）。

### 6.6 ★ 调用审计埋点（`_tools_call`，V3.2 新增）

```python
def _tools_call(self, params):
    source = "local" if name in self.local_tools else "cbm"
    t0 = time.time()
    try:
        result = self._dispatch_tool(name, args, params)
        resp_size = len(json.dumps(result, ensure_ascii=False))
        return result
    except Exception as e:
        success = False; error_msg = str(e)[:500]
        raise
    finally:
        duration_ms = int((time.time() - t0) * 1000)
        self._log_call(name, source, duration_ms, success, error_msg, args, resp_size)
```

- **finally 落库**：无论成功/失败都记录，保证流水完整。
- **resp_size**：返回结果 JSON 字节数，用于发现"返回过大需要摘要化"的工具。
- **arg_summary**：args 序列化后截断 500 字符，平衡可读性与体积。
- **session_tag**：进程级时间戳，同一 MCP 进程的所有调用同 tag，便于区分不同会话批次。

---

## 七、共识机制（方案 10 RFC 核心）

### 7.1 promote：提取共识

```python
memory_promote(obs_id="<obs_id>", consensus_domain="java-cloud-common")
```

一行 UPDATE：`SET tier='consensus', project='java-cloud-common', origin_project='<原项目>'`。不搬数据不重算向量。自动 `INSERT OR IGNORE INTO project_refs (原项目, 共识域, 'promote')`。

**不做自动合并**：多条同主题共识各自独立存在。AI 发现冗余时：
1. `consensus_health_check` 检测域内 embedding>0.85 的相似对
2. `cross_project_health_check` 检测**跨项目** q4 语义重复（V3.1 新增，提示提取为共识）
3. `mem_update` 写精炼新版本（传 `origin_project=''` 剪断脐带，`confirm_consensus=true`）
4. `mem_delete` 删除旧版本（传 `confirm_consensus=true`）

### 7.2 越权确认

mem_update / mem_delete 操作 tier='consensus' 的行时：
- 不带 `confirm_consensus=true` → 拦截，返回影响范围（被 N 个项目引用）
- 带 `confirm_consensus=true` → 放行
- 改 consensus content 但未声明 `origin_project` → 拦截，要求显式声明（剪断或保留）

### 7.3 ★ mem_save 共识域写守卫（V3.1 新增）

```python
# _save() 开头
is_consensus_domain = conn.execute(
    "SELECT 1 FROM memory_facts WHERE project=? AND tier='consensus' AND deleted_at IS NULL LIMIT 1",
    (project_id,)
).fetchone()
if is_consensus_domain:
    return {"warning": "'{project_id}' 是共识域...直接写入会产生 tier=q4 的孤儿记忆..."}
```

**防的是什么**：AI 误把新知识直接 `mem_save(project_id="java-cloud-common", ...)`，会生成一条 `tier=q4` 的孤儿——它挂在共识域名下但 tier 不对，既不被任何项目召回，也不在共识检索范围内。守卫拦截并提示正确路径：① 先写到来源项目再 promote；② 或用 mem_update 更新已有共识条目。

### 7.4 demote：降级回动态

```python
memory_demote(obs_id="<obs_id>")
```

- origin_project 为空 → **拒绝降级**（溯源黑洞防护）
- origin_project 有值 → `SET tier='q4', project=origin_project, origin_project=''`
- **不清理 project_refs**（防过桥抽板——demote 只是退回一条记忆，不该切断项目与共识域的引用关系）

### 7.5 project_refs 生命周期

只保留两条**绝对安全**的清理路径（均按 `deleted_at IS NULL` 计存活，软删行视为已不存在）：
1. **mem_delete 导致域清空**：共识域的记忆全删了（含软删）→ 清理指向该域的 promote refs（不删 manual refs）
2. **项目动态记忆全删**：项目的 q4 记忆全删了（含软删）→ 清理该项目的出向 promote refs

demote 绝不清理 project_refs。

---

## 八、★ 记忆可视化 GUI（V3.2 新增）

零依赖（仅 Python 标准库 `http.server`），只读连接 `core_memory.db`（`mode=ro`，绝不写库）。

```bash
cd D:\cly-marketplace\qmem\mcp\gui && python server.py
# 访问 http://localhost:8765
```

| 路由 | 功能 |
|---|---|
| `GET /` → `index.html` | 记忆列表浏览器：左栏 Project/共识域过滤，工具栏 Tier/Type/搜索/重置，卡片列表滚动 |
| `GET /graph` → `graph.html` | project_refs 引用图谱可视化（节点=项目/共识域，边=引用关系） |
| `GET /api/memories` | 记忆列表，支持 project/tier/type/q 过滤，默认 LIMIT 200 |
| `GET /api/stats` | 统计汇总：总数、type/tier 维度分布、各 project 记忆数 |
| `GET /api/graph` | 引用图：nodes（id/type/count/types/degree）+ edges（source/target/ref_source），含孤立节点 |

**安全设计**：静态文件路由做了目录穿越防护（`abs_path.startswith(GUI_DIR)` 校验）。

---

## 九、防呆与安全约束（V3.2 共 13 条）

V3.0 的 11 条 + V3.1/V3.2 新增 3 条：

1. **promote 不做自动合并** —— 避免字符串拼接不可逆 + 信息臃肿
2. **origin_project 脐带机制** —— promote 写入，合并精炼剪断，demote 检测为空则拒绝
3. **project_refs 两条安全清理路径** —— 只在域清空/项目消失时清理，demote 绝不清理（防过桥抽板）
4. **ref_source 区分引用来源** —— 清理只删 ref_source='promote'，绝不删 manual
5. **mem_save upsert 加 tier='q4' 守卫** —— 保证写入永远不碰 consensus 行
6. **confirm_consensus 越权确认** —— 不一刀切禁止，而是要求显式确认
7. **单次查询同源 RRF + 三步法配额** —— 绝不拆两次查询；不能用顺序遍历即时填充
8. **consensus_health_check 工具** —— 不做自动合并但提供触发机制
9. **脐带剪断强制检查** —— mem_update 改 consensus content 时强制要求声明 origin_project 去留
10. **防爆机制** —— mem_context 摘要化 + q4 硬上限 100 + 共识 consensus_limit（默认 5）
11. **list_consensus_projects 工具** —— AI promote 前查看现有共识域
12. **★ mem_save 共识域写守卫（V3.1）** —— 向已有共识域名写 q4 时拦截，防孤儿记忆
13. **★ 调用审计永不阻断业务（V3.2）** —— `_log_call` 全程 try/except，日志库写失败只打 stderr，绝不影响工具返回值；独立文件 + 不加载 vec 扩展，审计与业务物理隔离
14. **★ mem_delete 默认软删除可恢复（V3.2）** —— 默认 `UPDATE SET deleted_at`（可恢复，防误删），`hard=true` 才物理删除；所有检索路径带 `deleted_at IS NULL` 守卫保证软删行不被命中；空域 refs 清理按存活计数（软删视为已不存在）

---

## 十、嵌入模型

- **模型**：`bge-small-zh-v1.5`，ONNX 版
- **位置**：`D:\cly-marketplace\qmem\mcp\bge-small-zh-v1.5-onnx\`
- **维度**：512
- **加载路径**：`onnx/model.onnx`（权重 `model.onnx_data` 约 94MB）
- **推理**：本地 CPU（onnxruntime，`CPUExecutionProvider`），无外部 API 依赖
- **流程**：tokenizer 截断 510 token → ONNX 推理 → mean pooling（带 attention_mask）→ L2 归一化
- **降级**：onnxruntime/tokenizers 未装或模型文件缺失时返回零向量（保证不崩）

---

## 十一、运行环境

| 项目 | 值 |
|---|---|
| Python | `C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe`（3.13） |
| 记忆库 | `D:\cly-marketplace\qmem\mcp\core_memory.db`（SQLite 3.50.4 + sqlite-vec + FTS5） |
| 审计库 | `D:\cly-marketplace\qmem\mcp\call_log.db`（SQLite WAL，V3.2 新增） |
| CBM 二进制 | `D:\cly-marketplace\qmem\mcp\codebase-memory-mcp.exe`（257MB，原生 C 编译） |
| GUI 服务 | `D:\cly-marketplace\qmem\mcp\gui\server.py`（端口 8765，只读） |
| 启动脚本 | `D:\cly-marketplace\qmem\mcp\python\start_python_mcp.bat`（便携，Python 路径可参数化） |
| 依赖 | sqlite-vec>=0.1.9, onnxruntime>=1.24.4, numpy, tokenizers, huggingface-hub |

---

## 十二、文件清单（V3.2）

```
D:\cly-marketplace\qmem\mcp\       ← QMem 根（早期文档写作 C:\QMem\，已废弃）
├── core_memory.db                  ← 记忆库（memory_facts + memory_vectors + memory_facts_fts + project_refs）
├── call_log.db                     ← ★ V3.2 调用审计库（tool_call_log，WAL）
├── codebase-memory-mcp.exe         ← CBM 原生二进制（代码查询）
├── bge-small-zh-v1.5-onnx/         ← 嵌入模型（ONNX + tokenizer）
│   ├── config.json
│   ├── tokenizer.json
│   └── onnx/
│       ├── model.onnx              ← 嵌入模型入口
│       └── model.onnx_data         ← 94MB 权重
├── python/                         ← QMem MCP server 源码
│   ├── mcp_server.py               ← 主服务（V3.2，16 个工具 + 调用审计）
│   ├── search_rrf.py               ← RRF 混合检索（projects/tiers 复数 IN）
│   ├── embedding.py                ← BGE ONNX 嵌入（uint8 优先）
│   ├── cbm_wrapper.py              ← CBM 子进程转发（崩溃恢复）
│   ├── init_project_context.py     ← 目录身份探测（git/pom/package.json）
│   ├── qmem_cli.py                 ← CLI 入口（save/search/context/projects/init/detect-changes）
│   ├── schema.sql                  ← v3 DDL（4 表 + 4 触发器 + 索引）
│   ├── test_call_log.py            ← ★ V3.2 调用日志功能测试（7 场景）
│   ├── requirements.txt            ← 依赖清单
│   └── start_python_mcp.bat        ← Windows 便携启动脚本
├── gui/                            ← ★ V3.2 记忆可视化（零依赖只读 HTTP）
│   ├── server.py                   ← HTTP 服务（端口 8765，3 个 API + 静态文件）
│   ├── index.html                  ← 记忆列表浏览器
│   ├── graph.html                  ← 引用图谱可视化
│   └── assets/                     ← common.js / markdown.js / style.css
├── check_db.py                     ← DB 状态检查脚本（tier/project 分布 + 列完整性）
├── install.ps1                     ← CBM 安装脚本
├── mcp_config_example.json         ← MCP 客户端配置示例（Windows Python / Linux Mojo）
├── WINDOWS_SETUP_GUIDE.md          ← Windows 配置指南（⚠️ 仍写 v3.0/14 工具，未同步 V3.2）
└── update/                         ← 方案演进文档归档
    ├── QMem-V3.0-Architecture.md   ← V3.0 完整架构（本文前序）
    ├── QMem-V3.2-Architecture.md   ← ★ 本文（V3.2 完整架构）
    └── V2升级V3-Q2/                ← 方案 01~11 演进归档 + README 索引
```

---

## 十三、Skill 与 CLAUDE.md / AGENTS.md 分工

| 层 | 文件 | 承载内容 | 加载方式 |
|---|---|---|---|
| **硬约束** | `~/.claude/CLAUDE.md`（全局）+ `D:\code\CLAUDE.md`（工作区）+ 项目子目录 CLAUDE.md | 内网环境、编码约束、AI 自查、克隆项目身份红线；强制"开局加载 qmem-memory skill 并执行 mem_context" | 每次会话自动注入 |
| **完整手册** | `qmem-memory` skill（SKILL.md） | 工具速查 + 卫生规则 + 共识管理 + 共识域导航 + 已迁移 project | 开局强制加载 |
| **元规则** | QMem `memory-hygiene` project（scope=personal） | 记忆卫生规则正文 | mem_context / mem_recall 按需召回 |

---

## 十四、与开源工具的对比

| 维度 | QMem V3.2 | Mem0 | Letta (MemGPT) | Zep |
|---|---|---|---|---|
| 存储 | SQLite + vec0 + FTS5 | 向量 DB + graph | 分层 memory | 时序知识图谱 |
| 嵌入 | 本地 BGE ONNX (512d) | API 依赖 | API 依赖 | API 依赖 |
| 共识机制 | tier + project_refs 引用图谱 | metadata 标签 | 不区分 | 节点类型标签 |
| 引用关系 | ★ 显式多对多 project_refs 表 | 无 | 无 | 图边 |
| 检索 | RRF（FTS5+向量融合）+ 三步法配额 | 纯向量 | 向量 | 图查询+向量 |
| 防呆 | confirm_consensus + 脐带剪断 + 共识域写守卫 | 无 | 无 | 无 |
| 可观测 | ★ call_log 调用审计 + GUI 可视化 | 弱 | 弱 | 弱 |
| 离线 | ✅ 全本地 | ❌ | ❌ | ❌ |

QMem 的独特之处：**引用关系显式化**（project_refs 表）+ **离线全本地**（ONNX 嵌入 + SQLite）+ **工程级防呆**（13 条设计约束）+ **可观测性**（调用审计 + 可视化 GUI）。

---

## 十五、典型工作流

### 15.1 新会话开场（拉模型召回）

```
1. 加载 qmem-memory skill
2. mem_context(project="<文件夹名>")
   → 返回 own_memories（摘要索引）+ consensus_memories（摘要索引）+ consensus_domains
3. 对需要的记忆：mem_get_full(obs_id="id1,id2,...")  ← 批量拉全文
4. 若要查代码事实：search_graph / trace_path（CBM 转发）
```

### 15.2 沉淀新知识（推模型）

```
# 项目级知识
mem_save(project_id="bfo_zj_yxyd", type="bugfix",
         title="IS_DELETE 必须用中文'否'",
         content="cloud-frame 系列 AbstractCloudPlatformUtil.YES='是'/NO='否'...")

# 发现跨项目通用 → 提取共识
memory_promote(obs_id="<obs_id>", consensus_domain="java-cloud-common")
# 自动建 project_refs(bfo_zj_yxyd → java-cloud-common, promote)
```

### 15.3 共识维护

```
# 检查域内冗余
consensus_health_check(consensus_domain="java-cloud-common")
# 检查跨项目可提取候选
cross_project_health_check(threshold=0.85)
# 精炼合并（剪断脐带）
mem_update(obs_id="<旧版>", content="...", origin_project="", confirm_consensus=true)
mem_delete(obs_id="<冗余旧版>", confirm_consensus=true)
```

### 15.4 调用审计分析（V3.2）

```bash
# 直接查 call_log.db（任何 SQLite 客户端）
SELECT tool_name, COUNT(*) calls, AVG(duration_ms) avg_ms,
       SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) failures
FROM tool_call_log WHERE ts > datetime('now','-7 days')
GROUP BY tool_name ORDER BY calls DESC;

# 或跑测试套件验证审计功能
python D:\cly-marketplace\qmem\mcp\python\test_call_log.py
```

---

## 十六、遗留问题处理记录

### 16.1 已修正（2026-07-17）

| 项 | 修正前 | 修正后 | 落点 |
|---|---|---|---|
| serverInfo.version | `"3.0"` | `"3.2"` | `mcp_server.py:148` |
| check_db.py version | `'3.0'` | `'3.2'` | `check_db.py:19` |
| CLI 版本号 | 注释/parser 写 v3.0 | 对齐 v3.2 | `qmem_cli.py:3,14` |
| CLI context 字段不一致 | 读 `observations` + `pinned`（server 不再返回） | 读 `own_memories` + `consensus_memories` + `content_preview`，并输出引用共识 | `qmem_cli.py` context 分支 |
| uint8 死代码 | 优先 `model_uint8.onnx` 但目录无此文件，永远走回退 | 删除优先逻辑，直接加载 `model.onnx`（与实际文件一致） | `embedding.py:_load_model()` |
| WINDOWS_SETUP_GUIDE.md | "v3.0，14 个工具" | V3.2、16 工具 + GUI 启动 + 审计库说明 | `WINDOWS_SETUP_GUIDE.md` |
| `deleted_at` 软删除 | 列存在但 `_delete` 走硬删（`DELETE FROM`），`deleted_at` 全程为 NULL | `_delete` 改两段式：默认 `UPDATE SET deleted_at`（软删可恢复），`hard=true` 走物理删除；所有检索路径已有 `deleted_at IS NULL` 守卫，软删行不被命中；空域 refs 清理按存活计数 | `mcp_server.py:_delete()` + 工具描述 |
| `session_id` 列 | 已 ADD COLUMN 但写入路径未填值（预留空列） | 从 `schema.sql` 和 `_REQUIRED_COLUMNS` 移除；已存在库中的空列因 SQLite 无法 DROP COLUMN 而无害保留（代码不读写） | `schema.sql` + `mcp_server.py:_REQUIRED_COLUMNS` |

### 16.2 验证记录（2026-07-17）

软删除/物理删除流程已端到端验证：
- 保存 → 软删前 search 命中 1 条 → `mem_delete`（默认）返回 `soft_deleted` → 软删后 search 命中 0 条（守卫生效）→ `mem_delete(hard=true)` 物理清理 → `mem_list_projects` 无残留。
- 语法检查：`mcp_server.py` / `embedding.py` / `qmem_cli.py` / `check_db.py` 全部通过。

---

> 本文档是 QMem V3.2 的完整架构说明，以 `D:\cly-marketplace\qmem\mcp\` 源码实际行为为准。
> V3.0 基线详见同目录 `QMem-V3.0-Architecture.md`；方案演进详见 `update/V2升级V3-Q2/`（方案 01~11 + README 索引 + V10 迭代纪事）。
> V3.3（Token 爆炸/记忆污染治理）详见同目录 `QMem-V3.3-Architecture.md`。
