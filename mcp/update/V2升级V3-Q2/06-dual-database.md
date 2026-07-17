# 方案：双库（Q2 共识库 + Q4 动态记忆库）

> 状态：❌ 被否决（搬行复杂、embedding 跨库同步、工具选择负担）
> 时间：2026-07-13

## 核心设计

两个 .db 文件，**结构相同**（都用 schema.sql 建表），职责不同：

| | q4_memory.db | q2_consensus.db |
|---|---|---|
| 角色 | 日常动态记忆 | promote 搬来的共识 |
| 写入 | mem_save 正常写入 | promote 时从 Q4 搬行 |
| 检索 | mem_recall（现有） | consensus_recall（新增） |
| 结构 | memory_facts + vectors + fts5 | 相同三表（独立实例） |

### 为什么不拆表而拆库

`memory_vectors JOIN memory_facts` 和 FTS5 external-content 都依赖同库。拆表会破坏 JOIN 和触发器。拆库 = 两套完整的表，各管各的 JOIN/触发器，零破坏。

### promote 操作

```
1. SELECT * FROM q4 WHERE obs_uuid=?        ← 读 Q4 完整行
2. INSERT INTO q2 (全字段)                   ← 写入 Q2
3. 手动搬 embedding 到 Q2 的 vectors 表      ← 跨库触发器不生效
4. DELETE FROM q4 WHERE obs_uuid=?          ← 从 Q4 删除
```

### 检索模式

- `mem_recall` → 搜 Q4（动态记忆）
- `consensus_recall` → 搜 Q2（共识）
- 两库独立检索，不跨库联合

## 优点

### 1. 物理隔离最强
共识和动态记忆在**不同的 .db 文件**里。备份/迁移/损坏都互不影响。

### 2. 检索能力保留
两库都有 embedding + FTS5，RRF 检索完整保留。

### 3. schema 独立
Q2 库的向量空间只含共识，语义检索更精准（不被动态记忆的向量干扰）。

## 缺点

### 1. ★ 跨库搬行 + embedding 同步复杂（致命）

`memory_vectors` 的触发器只在同库生效。Q4→Q2 搬行时，Q2 的 vectors 不会被触发器填充，必须手动同步：

```python
# 从 Q4 读 embedding bytes
vec = q4_conn.execute("SELECT embedding FROM memory_vectors WHERE rowid=?", ...)
# 写入 Q2
q2_conn.execute("INSERT INTO memory_vectors(rowid, embedding) VALUES(?,?)", ...)
```

**id 冲突问题**：两个库的 AUTOINCREMENT 独立计数，id 会冲突。需要用 obs_uuid 做关联，重新分配 Q2 的 id。

### 2. AI 的工具选择负担

两个检索工具并存：
- `mem_recall` → 搜 Q4 日常记忆
- `consensus_recall` → 搜 Q2 共识

AI 每次要判断"这个问题该搜哪个库"。如果两个都搜，就是跨库联合检索（回到之前的问题）。如果只搜一个，AI 可能漏掉另一边。

### 3. 共识是动态的，搬行是单向的

如果共识过时了想"降级"回 Q4？或者共识需要更新内容？跨库 UPDATE 很别扭。

### 4. 两套 HybridSearcher 实例

search_rrf.py 要实例化两次，分别连不同库。

## 与开源工具的对比

开源记忆工具（Mem0、Letta、Zep）**没有一个**把记忆按"共识/非共识"物理分库。它们的做法是统一存储 + metadata 标签过滤。因为"是否共识"是一个会变的属性，物理分库意味着每次升级/降级都要搬数据。

详见 [09-open-source-comparison.md](09-open-source-comparison.md)。

## 为什么被否决

双库方案在隔离性上最强，但工程复杂度和使用体验最重。搬行的 embedding 同步、id 冲突、工具选择负担——每个都是真实的工程成本。对当前 31 条记忆的规模来说，物理分库是过度设计。

> **核心矛盾**：物理搬家要打包（同步 embedding）、要对齐地址（id 冲突）、搬家后原处没了（检索丢失风险）。标记只是贴个便签——东西还在原处，随时能找到。
