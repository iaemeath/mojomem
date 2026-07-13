# ★ 方案：三表引用（项目表 + 共识表 + 多对多关联表）

> 状态：★ 最终方向
> 时间：2026-07-13

## 核心理念

> **共识不是"搬过来的副本"，而是"被引用的源头"。**
>
> 多个项目遇到同一个问题 → 提取到共识 → 项目通过引用关联 →
> 共识修改后所有引用方自动看到最新版（一处改变，处处引用）。

```
bfo_zj_yxyd ──引用──┐
bfo_cndz     ──引用──┼──→ _java-cloud-common（共识，只存一份）
dispatch-app ──引用──┘      └─ IS_DELETE 中文值
                           └─ CLOB 列长度陷阱
                           └─ @Transactional 失效
```

## 数据结构

### 表 1：项目动态记忆（project_memory）

```sql
CREATE TABLE project_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    obs_uuid TEXT UNIQUE NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    topic_key TEXT DEFAULT '',
    title TEXT DEFAULT '',
    content TEXT NOT NULL,
    type TEXT DEFAULT 'manual',
    scope TEXT NOT NULL DEFAULT 'project',
    content_hash TEXT DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP
);
-- 配套：project_vectors (vec0) + project_memory_fts (fts5) + 触发器
```

### 表 2：共识记忆（consensus_memory）

```sql
CREATE TABLE consensus_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    obs_uuid TEXT UNIQUE NOT NULL,
    consensus_project TEXT NOT NULL DEFAULT '',  -- _java-cloud-common / _weakpwd
    topic_key TEXT DEFAULT '',
    title TEXT DEFAULT '',
    content TEXT NOT NULL,
    type TEXT DEFAULT 'manual',
    content_hash TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP
);
-- 配套：consensus_vectors (vec0) + consensus_memory_fts (fts5) + 触发器
```

### 表 3：多对多关联（project_consensus）

```sql
CREATE TABLE project_consensus (
    project TEXT NOT NULL,              -- bfo_zj_yxyd
    consensus_project TEXT NOT NULL,    -- _java-cloud-common
    PRIMARY KEY (project, consensus_project)
);
```

## 三个核心操作

### 1. promote：提取为共识

**场景**：多个项目遇到同一个问题，提取为共识。

```python
def _promote(self, obs_id, consensus_project):
    # 1. 从 project_memory 读完整行 + embedding bytes
    row = SELECT * FROM project_memory WHERE obs_uuid=?
    vec_bytes = SELECT embedding FROM project_vectors WHERE rowid=row.id

    # 2. 【去重检查】consensus_memory 是否已有同主题记忆
    existing = SELECT id FROM consensus_memory
               WHERE consensus_project=? AND title=? AND deleted_at IS NULL
    if existing:
        # 已有同标题共识 → 合并内容（追加而非重复插入）
        UPDATE consensus_memory SET content = content || '\n\n---\n\n' || new_content
        重算 embedding 写入 consensus_vectors
        DELETE FROM project_memory WHERE obs_uuid=?
        INSERT OR IGNORE INTO project_consensus VALUES (原project, consensus_project)
        return {"action": "merged"}

    # 3. 无重复 → 正常搬行
    INSERT INTO consensus_memory (obs_uuid, consensus_project, ...) VALUES (...)
    new_id = last_insert_rowid()

    # 4. 搬 embedding bytes（不重算，直接搬）
    INSERT INTO consensus_vectors (rowid, embedding) VALUES (new_id, vec_bytes)

    # 5. 从 project_memory 删除（触发器清 project_vectors + project_memory_fts）
    DELETE FROM project_memory WHERE obs_uuid=?

    # 6. 记录引用关系
    INSERT OR IGNORE INTO project_consensus VALUES (原project, consensus_project)

    # 全程一个事务，失败 ROLLBACK
```

**去重逻辑**：两个项目分别存了同一条记忆（如 bfo_zj_yxyd 和 bfo_cndz 各有一条 IS_DELETE bugfix），promote 第二条时发现 consensus_memory 已有同标题 → 追加合并内容而非重复插入。最终 consensus_memory 只有一条更完整的共识。

### 2. 加载项目：自动带上引用的共识

```python
def _context(self, project):
    # 1. 自身记忆
    own = SELECT * FROM project_memory WHERE project=? AND deleted_at IS NULL

    # 2. 查引用的共识
    refs = SELECT consensus_project FROM project_consensus WHERE project=?
    # → ['_java-cloud-common', '_weakpwd']

    # 3. 加载共识
    consensus = SELECT * FROM consensus_memory
                WHERE consensus_project IN (refs) AND deleted_at IS NULL

    # 4. 返回，标注来源
    return {
        "project": project,
        "own_memories": own,              # 项目自身
        "consensus_memories": consensus   # 引用的共识
    }
```

### 3. 修改共识：一处改变，处处引用

```python
def _update(self, obs_id, content):
    # 【双表判断】先查 consensus_memory，再查 project_memory
    row = SELECT id FROM consensus_memory WHERE obs_uuid=?
    if row:
        # 共识 → 改 consensus_memory + 重算 consensus_vectors
        UPDATE consensus_memory SET content=? WHERE obs_uuid=?
        重算 embedding：
          DELETE FROM consensus_vectors WHERE rowid=?
          INSERT INTO consensus_vectors (rowid, embedding) VALUES (?, 新vec)
        return  # → 所有引用方下次加载自动看到新内容

    row = SELECT id FROM project_memory WHERE obs_uuid=?
    if row:
        # 动态记忆 → 改 project_memory + 重算 project_vectors
        UPDATE project_memory SET content=? WHERE obs_uuid=?
        重算 embedding：
          DELETE FROM project_vectors WHERE rowid=?
          INSERT INTO project_vectors (rowid, embedding) VALUES (?, 新vec)
```

**双表 embedding 同步**：更新内容时，必须判断 obs_id 在哪张表，然后重算对应表的 vectors（consensus_vectors 或 project_vectors）。两套表各自独立，不能混。

## 检索模式

### mem_recall：搜当前项目 + 引用的共识

```python
def _recall(self, query, current_project):
    refs = SELECT consensus_project FROM project_consensus WHERE project=?

    # 搜 project_memory（WHERE project=?）
    own_results = project_searcher.hybrid_search_rrf(query, vec, project=current_project)

    # 搜 consensus_memory（WHERE consensus_project IN refs）
    consensus_results = consensus_searcher.hybrid_search_rrf(query, vec, projects=refs)

    # 【跨表排序问题】两表的 RRF 分数独立计算，分布不同，直接合并不公平
    # 解决：各自取 top-N 后按相对 rank 重新归一化 RRF
    # own_results 取 top-5, consensus_results 取 top-5
    # 合并后按 1/(k+rank) 统一重排（rank 从 0 重新计）
    return merge_and_rerank(own_results, consensus_results)
```

**跨表 RRF 排序**：project_memory 和 consensus_memory 的 BM25/cosine 分布不同，各自的 RRF 分数不可直接比较。合并时不能简单按分数降序——要**各自取 top-N 后按相对 rank 做第二轮 RRF 归一化**（每路结果从 rank=0 重新计数，融合排序）。这样共识和动态记忆在同一 rank 空间内公平竞争。

### consensus_recall：专搜共识

```python
def _consensus_recall(self, query):
    # 只搜 consensus_memory，不限定 project（跨所有共识）
    return consensus_searcher.hybrid_search_rrf(query, vec)
```

## 引用关系怎么建立

| 途径 | 场景 |
|---|---|
| **promote 时自动** | 提取共识时自动 INSERT ref（来源 project → 共识 project） |
| **手动 add_consensus_ref** | 项目本身没踩过坑，但用了这个框架，需要知道相关共识 |
| **AGENTS.md 栈声明** | AI 读 CLAUDE.md 的"栈声明"行，手动调 add_consensus_ref |

## promote 时怎么确定归入哪个共识 project

promote 需要 `consensus_project` 参数（如 `_java-cloud-common`）。AI 调用时需要知道有哪些共识 project 可选。

**新增工具 `list_consensus_projects`**：

```python
{"name": "list_consensus_projects",
 "description": "列出所有共识 project（_前缀）及其记忆数，供 promote 时选择目标。"}
```

```python
def _list_consensus_projects(self):
    return SELECT consensus_project, COUNT(*) FROM consensus_memory
           WHERE deleted_at IS NULL GROUP BY consensus_project
```

AI promote 前先调 `list_consensus_projects` 看现有共识列表，决定归入哪个或创建新的。

## 独特优势

### 1. 引用是显式可查询的关系

```sql
-- bfo_zj_yxyd 引用了哪些共识？
SELECT consensus_project FROM project_consensus WHERE project='bfo_zj_yxyd';

-- _java-cloud-common 被哪些项目引用？
SELECT project FROM project_consensus WHERE consensus_project='_java-cloud-common';

-- 哪些共识没有被任何项目引用？（孤儿共识，可清理）
```

其他方案里"项目和共识的关系"是隐式的。本方案变成数据库里的显式关系，精确可控。

### 2. 一处改变，处处引用

共识只有一份，引用方每次加载都是查源头，天然看到最新版。不需要通知、同步、推送。

### 3. 物理隔离 + 检索保留

两套独立的表 + 向量 + FTS：
- 共识和动态记忆物理分表，不混在一起
- 各有自己的向量空间，检索不互相干扰
- RRF 检索完整保留

### 4. embedding 不重算

promote 时直接从 project_vectors 读出原始 bytes 写入 consensus_vectors。embedding 是确定性的（同样文本同样向量），不需要重新过模型。

## 与所有方案的对比

| 维度 | V2.0 | V2.2 | 双库 | tier字段 | **本方案** |
|---|---|---|---|---|---|
| 共识存哪 | 同表 | 文件 | 独立.db | 同表 | **独立表** |
| 共识几份 | 1 | 1 | 1 | 1 | **1** |
| 项目关联共识 | 无 | 无 | 无 | 无 | **★ refs表** |
| 加载时看到共识 | 靠boost | skill触发 | 手动调工具 | 手动调工具 | **★ 自动带** |
| 修改共识 | 原地改 | 手动改文件 | 跨库改 | 原地改 | **★ 原地改，自动更新** |
| 检索能力 | RRF | 丢失 | RRF | RRF | **RRF** |
| promote 操作 | UPDATE 1字段 | 删DB+写文件 | 跨库搬行 | UPDATE 1字段 | **搬行+建ref** |
| 降级 | 改标记 | 手动搬回 | 跨库回搬 | 改字段 | **反向搬行** |
| promote 去重 | 无 | 无 | 无 | 无 | **★ 标题去重+合并** |

## 工具列表（最终）

| 工具 | 用途 |
|---|---|
| mem_save | 写 project_memory（动态记忆） |
| mem_recall | 搜 project_memory + 引用的 consensus_memory（跨表 RRF 归一化排序） |
| mem_search | 精确过滤 project_memory |
| mem_update | 改 project_memory 或 consensus_memory（自动判断 + 对应 vectors 重算） |
| mem_context | 加载项目自身 + 引用的共识 |
| mem_delete | 删 project_memory 或 consensus_memory（自动判断） |
| memory_promote | project_memory → consensus_memory 搬行 + 建 ref + 去重合并 |
| memory_demote | 反向搬行（consensus → project） |
| consensus_recall | 专搜 consensus_memory |
| add_consensus_ref | 手动建立项目→共识引用 |
| list_consensus_projects | 列出共识 project（供 promote 选择目标） |
| mem_list_projects | 列 project_memory 的 project |
| init_project_context | 目录身份探测（不变） |

## 设计要点备忘

以下是在审查中发现并已补充到上述设计中的关键细节：

1. **promote 去重/合并**：两个项目分别存了同一条记忆，promote 第二条时发现 consensus_memory 已有同标题 → 追加合并内容（`content || '\n\n---\n\n' || new_content`）而非重复插入。
2. **_update 双表 embedding 同步**：更新内容时先判断 obs_id 在哪张表，重算对应表的 vectors（consensus_vectors 或 project_vectors），不能混。
3. **mem_delete 双表判断**：同 _update，先查 consensus_memory 再查 project_memory。
4. **promote 时 consensus_project 选择**：新增 `list_consensus_projects` 工具，AI promote 前先查看现有共识列表，决定归入哪个或创建新的。
5. **mem_recall 跨表 RRF 排序**：两表分数不可直接比较，各自取 top-N 后按相对 rank 做第二轮 RRF 归一化。

> 数据迁移是一次性操作，不纳入本方案，单独处理。
