# 方案：单表 + tier 字段

> 状态：⚠️ 备选（简单但非物理隔离）
> 时间：2026-07-13
> 背景：双库方案太复杂，尝试最简化的标记方案

## 核心设计

共识和动态记忆在同一张表，用 `tier` 字段区分（替代 V2.0 的 `is_global`）：

```sql
ALTER TABLE memory_facts ADD COLUMN tier TEXT NOT NULL DEFAULT 'q4';
-- tier='q4'：项目动态记忆（默认）
-- tier='consensus'：跨项目共识（promote 后）
```

### 关键区别：tier 和 type 正交

`type` 描述记忆的**性质**（decision/bugfix/reference），`tier` 描述**层级**（动态/共识）。一条记忆同时是 bugfix 和共识：

| 记忆 | type | tier |
|---|---|---|
| 保供管控的某次 API 改动 | decision | q4 |
| IS_DELETE 中文值（6 个项目都踩了） | bugfix | consensus |
| cloud-frame 架构总览 | reference | consensus |

### promote 极简

```python
def _promote(self, args):
    obs_id = args.get("obs_id")
    conn.execute("UPDATE memory_facts SET tier='consensus' WHERE obs_uuid=?", (obs_id,))
    conn.commit()
    # 完了。一行 UPDATE，数据不动，embedding/FTS 全保留。
```

### 检索按 tier 过滤

```python
# mem_recall 只搜动态记忆
results = searcher.hybrid_search_rrf(query, vec, tier='q4', ...)

# consensus_recall 只搜共识
results = searcher.hybrid_search_rrf(query, vec, tier='consensus', ...)
```

search_rrf.py 每个方法加一个 WHERE 条件：
```python
if tier:
    sql += " AND mf.tier = ?"
```

## 优点

### 1. promote 极简
一行 UPDATE，不搬数据，不重算 embedding，不跨库。和 V2.0 的 is_global 一样简单。

### 2. 检索完整保留
embedding/FTS 全部不动，RRF 照常命中。

### 3. 完全可逆
降级就是 `SET tier='q4'`，一行 UPDATE。

### 4. 代码改动极小
加一列 + promote 改一行 + search 加 WHERE 条件。

### 5. type 信息不丢失
和 V2.0 的 is_global 不同，tier 和 type 正交。一条共识保留它的 type（bugfix/decision/...）。

## 缺点

### 1. ★ 非物理隔离
共识和动态记忆在**同一个 .db 文件同一张表**。隔离靠 WHERE 条件，不是物理分文件。

这意味着：
- 备份：不能单独备份共识
- 损坏：一个库坏了共识也跟着丢
- 审计：混在一张表，想只看共识要 WHERE 过滤

### 2. 无法表达项目与共识的引用关系
和 V2.0 一样的问题——"bfo_zj_yxyd 引用了哪些共识"是隐式的。tier 只能区分"是/否共识"，不能表达"谁引用了谁"。

### 3. 加载项目时不知道该带哪些共识
`mem_context(project='bfo_zj_yxyd')` 只能返回 bfo_zj_yxyd 的动态记忆。要带上相关共识，需要额外机制（AGENTS.md 栈声明 / 手动关联），tier 字段本身不解决这个。

## 与 V2.0 is_global 的区别

| 维度 | V2.0 is_global | 本方案 tier |
|---|---|---|
| 字段语义 | 0/1 布尔（是否全局） | 'q4'/'consensus' 字符串（层级） |
| 检索方式 | RRF boost 加权（混在一起） | WHERE 过滤（分开搜） |
| type 信息 | is_global 和 type 正交但 boost 不感知 type | tier 和 type 正交 |
| 可扩展性 | 只能 0/1 | 可加 'q3' 等新层级 |

**本质**：V2.0 是"标记 + 加权"（共存但 boost），本方案是"标记 + 过滤"（共存但分开搜）。过滤比加权更可控。

## 为什么是备选而非最终

tier 方案解决了 V2.0 的"加权不可控"问题，但没解决"无法表达引用关系"问题。共识和项目之间没有显式的多对多关联——加载项目时不能自动带上相关共识。

这个缺陷直接催生了三表引用方案：**加一张关联表，让"谁引用了谁"变成结构化数据**。
