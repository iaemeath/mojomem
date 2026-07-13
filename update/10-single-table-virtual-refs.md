# ★ 方案：单表全家桶 + 虚拟外键引用（Virtual Refs）

> 状态：★ 终极融合方案（The Ultimate Hybrid）
> 时间：2026-07-13
> 背景：07 方案（单表 Tier）缺乏关系追踪能力，08 方案（三表引用）工程复杂度过高且存在性能隐患。本方案融合两者长处。

## 核心理念

> **用 07 方案的"极简物理底座"，承载 08 方案的"精妙关联图谱"。**
>
> 物理层面，所有记忆（草稿与共识）共享同一张表和同一套向量索引，杜绝跨表搬运开销。
> 逻辑层面，引入轻量级多对多关系表（project_refs），实现"一处修改处处更新"以及"开场精准推送"。

## 数据结构

### 表 1：全局记忆全家桶（memory_facts）

保持现有单表结构，复用 `tier` 字段和 `fts5`/`vec0`：

```sql
CREATE TABLE memory_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    obs_uuid TEXT UNIQUE NOT NULL,
    project TEXT NOT NULL DEFAULT '',     -- 动态记忆填自身项目名，共识记忆填共识域（如 _java-cloud-common）
    topic_key TEXT DEFAULT '',
    title TEXT DEFAULT '',
    content TEXT NOT NULL,
    type TEXT DEFAULT 'manual',
    scope TEXT NOT NULL DEFAULT 'project',
    tier TEXT NOT NULL DEFAULT 'q4',      -- ★ 核心隔离字段：q4(动态草稿) / consensus(底层共识)
    origin_project TEXT DEFAULT '',       -- ★ promote 前的原始 project（供 demote 回溯）
    content_hash TEXT DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP
);
-- 配套：memory_vectors (vec0) + memory_facts_fts (fts5)
```

### 表 2：依赖引用图谱（project_refs）

新增极轻量的多对多关联表：

```sql
CREATE TABLE project_refs (
    project TEXT NOT NULL,         -- 当前项目 (如 bfo_zj_yxyd)
    ref_project TEXT NOT NULL,     -- 依赖的共识域 (如 _java-cloud-common)
    ref_source TEXT NOT NULL DEFAULT 'promote',  -- ★ promote(自动建立) / manual(add_consensus_ref 手动建)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (project, ref_project)
);
```

`ref_source` 列区分引用来源：promote 时自动建的标 `'promote'`，`add_consensus_ref` 手动建的标 `'manual'`。清理逻辑只清理 promote 来源的引用，不误删手动建的。

## 三个核心操作

### 1. promote：提取共识并建立血缘

**核心原则：不做字符串拼接，不做物理删除，保留每条记忆的独立 obs_uuid。**

当多个项目分别存了同主题记忆，promote 不合并它们——每条记忆独立 `UPDATE tier='consensus'`，各自成为共识域下的一条独立记录。它们靠 `project_refs` 关联到来源项目。AI 认为需要精炼时，由 AI 主动 `mem_update` 写一条提炼后的新共识，再删除旧的——**内容由 AI 判断，不由代码拼接**。

```python
def _promote(self, obs_id, consensus_domain):
    row = SELECT project, title FROM memory_facts WHERE obs_uuid=?
    origin_project = row["project"]

    # 原地飞升：改 tier + project + 记录来源，不挪窝，不重算 Vector
    UPDATE memory_facts
    SET tier='consensus',
        project=?,               -- 改为共识域名
        origin_project=?,        -- ★ 保留原始 project 供 demote 回溯
        updated_at=CURRENT_TIMESTAMP
    WHERE obs_uuid=?

    # 建立血缘图谱
    INSERT OR IGNORE INTO project_refs (project, ref_project)
    VALUES (origin_project, consensus_domain)
```

**为什么不做自动合并**：自动字符串拼接（`content || '\n\n---\n\n' || new_content`）有两个致命问题：
1. **不可逆**：合并后原始 obs_uuid 被删除，demote 时无法分离——"这段文字属于谁"无从知晓
2. **信息臃肿**：代码层 `||` 拼接不是知识提炼，5 轮合并后变成 1000 字流水账，消耗 token 且降低 AI 理解效率

正确做法是**保留独立记录 + 让 AI 决定何时合并**：
- 多条同主题共识各自独立存在，各自有 obs_uuid
- AI 检索时能看到多条相关共识，自己做信息综合
- AI 认为冗余时，主动 `mem_update` 一条记录写成精炼版本，再 `mem_delete` 旧的——这个判断由 AI 做，不由代码做
- demote 可以精确回退：`origin_project` 列记录了每条记忆的来源，改回 `tier='q4', project=origin_project` 即可

### 2. mem_context：开场精准推流 + 防爆机制

```python
def _context(self, current_project, consensus_limit=5):
    # 1. 加载本项目的动态草稿
    own = SELECT * FROM memory_facts
          WHERE project=? AND tier='q4' AND deleted_at IS NULL
          ORDER BY pinned DESC, created_at DESC

    # 2. 查出依赖了哪些共识域
    refs = SELECT ref_project FROM project_refs WHERE project=?

    # 3. 加载引用的共识（★防爆：按 pinned+updated 取 top-N）
    consensus = SELECT * FROM memory_facts
                WHERE tier='consensus' AND project IN (refs)
                AND deleted_at IS NULL
                ORDER BY pinned DESC, updated_at DESC
                LIMIT consensus_limit

    return {
        "project": current_project,
        "own_memories": own,
        "consensus_memories": consensus
    }
```

**防爆机制**：用 `LIMIT consensus_limit`（默认 5）按 `pinned DESC, updated_at DESC` 取 top-N。pinned 优先但未 pinned 的最新共识也能加载。AI 可通过参数控制加载深度。

### 3. mem_recall：全域混合检索

单表设计的天然优势——FTS5 和向量索引都在同一张表上，RRF 分数天然可比，不需要跨表归一化：

```python
def _recall(self, query, current_project):
    refs = SELECT ref_project FROM project_refs WHERE project=?
    search_scope = [current_project] + refs

    # SQL 层面一次查出所有相关内容，交由 RRF 融合排序
    # 词法路（FTS5）+ 向量路（vec0）都带 project IN (scope) 条件
    # 同表同索引，BM25/cosine 分布一致，RRF 分数可直接比较
    return rrf_merge(lex_results, sem_results)
```

## 引用关系怎么建立

| 途径 | 场景 |
|---|---|
| **promote 时自动** | 提取共识时自动 INSERT ref（来源 project → 共识域名） |
| **手动 add_consensus_ref** | 项目本身没踩过坑，但用了这个框架，需要知道相关共识 |
| **AGENTS.md 栈声明** | AI 读 CLAUDE.md 的"栈声明"行，手动调 add_consensus_ref |

## promote 时怎么确定归入哪个共识域

promote 需要 `consensus_domain` 参数（如 `_java-cloud-common`）。AI 调用时需要知道有哪些共识域可选。

**新增工具 `list_consensus_projects`**：

```python
{"name": "list_consensus_projects",
 "description": "列出所有共识域（tier=consensus 的 project）及其记忆数，供 promote 时选择目标。"}
```

AI promote 前先调 `list_consensus_projects` 看现有共识域列表，决定归入哪个或创建新的。

## project_refs 的生命周期维护

`project_refs` 不是只建不删的——它在以下时机自动维护，避免孤儿引用：

### mem_delete 时：清理空共识域的引用

```python
def _delete(self, obs_id):
    row = SELECT project, tier FROM memory_facts WHERE obs_uuid=?

    # 删除记忆（硬删，触发器清 vectors + fts）
    DELETE FROM memory_facts WHERE obs_uuid=?

    # 如果删的是共识，检查该共识域是否还有存活记忆
    if row["tier"] == "consensus":
        consensus_domain = row["project"]
        remaining = SELECT COUNT(*) FROM memory_facts
                    WHERE tier='consensus' AND project=? AND deleted_at IS NULL
        if remaining == 0:
            # 共识域空了 → 清理指向它的 promote 引用
            # ★ 只清理 ref_source='promote'，绝不删 ref_source='manual'
            # （AI 可能正在删旧共识准备写新共识，手动引用不应被瞬时空窗误杀）
            DELETE FROM project_refs WHERE ref_project=? AND ref_source='promote'
```

### 项目记忆全删时：清理该项目的出向引用

```python
# 在 mem_delete 或批量清理后检查
remaining_q4 = SELECT COUNT(*) FROM memory_facts
               WHERE tier='q4' AND project=? AND deleted_at IS NULL
if remaining_q4 == 0:
    # 项目没有动态记忆了 → 清理 promote 来源的引用
    # ★ 只清理 ref_source='promote'，保留 ref_source='manual'（手动建的引用可能仍然需要）
    DELETE FROM project_refs WHERE project=? AND ref_source='promote'
```

### memory_demote 时：可选清理引用

```python
def _demote(self, obs_id):
    row = SELECT origin_project, project FROM memory_facts WHERE obs_uuid=?

    # ★ 溯源黑洞防护（脐带剪断机制）：
    # 当 AI 将多条记录合并精炼时，需在 update 时将 origin_project 清空。
    # 如果 origin_project 为空，说明它已是融合后的终极共识，不再专属任何单一项目，拒绝降级。
    if not row["origin_project"]:
        return {"error": "⚠️ 溯源黑洞：该记录没有 origin_project（可能已在合并精炼中被清空）。"
                "说明它已是融合后的多源共识，无法降级私有化。请直接使用 mem_delete(confirm_consensus=true) 删除。"}

    consensus_domain = row["project"]       # 当前共识域
    origin = row["origin_project"]          # promote 前的原始 project

    # 回退：tier 改回 q4，project 改回来源
    UPDATE memory_facts
    SET tier='q4', project=?, origin_project='', updated_at=CURRENT_TIMESTAMP
    WHERE obs_uuid=?

    # ★ 不清理 project_refs！
    # 曾经有清理逻辑：检查 origin 是否还有该域的共识，没有则删 ref。
    # 但这在脐带剪断机制下会误杀——origin_project 被清空后，
    # other_refs COUNT 无法统计到已融合的共识，导致项目 X 被误判为
    # "在这个域没有共识了"，引用链接被删，项目 X 从此看不到自己参与过的融合共识。
    #
    # project_refs 的清理只保留两个绝对安全的路径：
    #   1. mem_delete 删共识 → 域空了 → 清理指向该域的 refs（_delete 逻辑）
    #   2. 项目动态记忆全删 → 项目没了 → 清理出向 refs（全删清理逻辑）
    # demote 只是"把一条记忆退回草稿"，不该影响项目与共识域的引用关系。
    # 宁可多带一点额外的共识上下文，也绝不切断项目与自己参与过的共识的可见性。
```

**demote 可精确回退（仅限 origin_project 存在的共识）**：
- promote 后未被合并精炼的共识：`origin_project` 有值（记录了来源项目），demote 精确回退到来源项目
- 被 AI 合并精炼过的共识：AI 在 mem_update 时主动清空 `origin_project=''`（脐带剪断），demote 检测到 origin_project 为空则拒绝降级——多源融合后的共识不再专属任何单一项目，引导用 mem_delete 删除

**demote 不清理 project_refs**：曾经有清理逻辑（检查 origin 是否还有该域共识→删 ref），但在脐带剪断机制下会误杀——origin_project 被清空后 COUNT 无法统计融合共识，导致项目被误判为"在这个域没有共识了"，引用链接被意外删除，项目从此看不到自己参与过的融合共识。因此 demote 只做记忆回退（tier+project），**绝不碰 project_refs**。refs 的清理只保留两个绝对安全路径：① mem_delete 导致域清空 ② 项目动态记忆全删导致项目消失。

**为什么不用 `updated_at > created_at` 判断**：promote 的 SQL 本身就会 `SET updated_at=CURRENT_TIMESTAMP`，导致 promote 后 `updated_at` 必然大于 `created_at`。用时间戳判断会导致所有 promote 过的记录都被拒绝 demote（全员封杀）。`origin_project` 是语义化的"脐带"——promote 时写入，合并精炼时剪断，判断逻辑精确且无歧义。

## 单表设计消除的复杂度

相比 08 三表方案，单表设计消除了**所有因分表带来的复杂度**：

| 复杂度来源 | 08 三表方案 | 本方案 |
|---|---|---|
| promote 搬行 | 跨表 SELECT→INSERT→DELETE + 搬 vec bytes | **一行 UPDATE**（tier + project + origin_project） |
| promote embedding 同步 | 跨表搬 vec bytes + 对齐 rowid | **不需要**（同表，embedding 不动） |
| _update 判断在哪张表 | 双表查询 + 双 vectors 重算 | **不需要**（同表，直接改） |
| mem_delete 判断在哪张表 | 双表查询 | **不需要**（同表） |
| mem_recall 跨表分数不可比 | 需要 RRF 归一化 | **不需要**（同表分数可比） |
| 两套触发器 / schema 维护 | 翻倍 | **一套** |
| demote 回退来源 | 需要单独的映射表记录来源 | **origin_project 列**（promote 时记录） |

## 共识健康检查（解决同主题堆积）

不做自动合并的副作用是：同一坑在 5 个项目各 promote 一次，共识域下可能堆积 5 条高度相似的独立记录。需要一个机制触发 AI 去精炼。

**新增工具 `consensus_health_check`**：

```python
{"name": "consensus_health_check",
 "description": "检查共识域健康度：找出内容高度相似的共识记录（embedding 相似度>0.85），"
                "提示 AI 进行精炼合并。建议在 promote 后或定期调用。"}
```

```python
def _consensus_health_check(self, consensus_domain=None):
    # 查出所有共识记录
    where = "WHERE tier='consensus' AND deleted_at IS NULL"
    if consensus_domain:
        where += " AND project=?"
    rows = SELECT obs_uuid, project, title, content FROM memory_facts {where}

    # 按 consensus_domain 分组
    # 每组内做 embedding 两两比对，找出相似度>0.85 的对
    duplicates = []
    for domain, items in group_by(rows, key='project'):
        for i, j in combinations(items, 2):
            sim = cosine_similarity(get_embedding(i), get_embedding(j))
            if sim > 0.85:
                duplicates.append({
                    "domain": domain,
                    "obs_a": i.obs_uuid, "title_a": i.title,
                    "obs_b": j.obs_uuid, "title_b": j.title,
                    "similarity": round(sim, 3),
                    "suggestion": "内容高度相似，建议精炼合并：用 mem_update 融合内容并传 origin_project=''（剪断溯源脐带），随后 mem_delete 删除冗余旧版。"
                })

    return {
        "duplicates": duplicates,
        "total_checked": len(rows),
        "needs_action": len(duplicates)
    }
```

**触发时机**：
- AI 每次 promote 后**主动调用** `consensus_health_check`，检查目标域是否有相似记录
- AI 定期或在新会话开场时调用，做共识域卫生维护
- 发现重复后由 AI 决定如何精炼（mem_update 新版本 + mem_delete 旧版本），不由代码自动拼接

**这个工具不做任何自动操作**——它只报告"发现 N 对相似记录 + 建议"，是否合并、怎么合并完全由 AI 判断。

## 工具列表

| 工具 | 用途 |
|---|---|
| mem_save | 写 memory_facts（tier='q4'，动态记忆）。upsert 加 `AND tier='q4'` 守卫，不碰 consensus 行 |
| mem_recall | 搜自身 project + 引用的共识域（单表 RRF，分数可比） |
| mem_search | 精确过滤 memory_facts |
| mem_update | 改 memory_facts（同表）。操作 consensus 需 confirm_consensus=true。合并共识时需传 origin_project='' 切断溯源 |
| mem_context | 加载项目自身 + 引用的共识（防爆 top-N） |
| mem_delete | 删 memory_facts + 自动清理空共识域 refs。删 consensus 行需 confirm_consensus=true |
| memory_promote | UPDATE tier='consensus' + project 改域名 + origin_project 记录来源 + 建 ref |
| memory_demote | UPDATE tier='q4' + project 改回 origin_project（不碰 project_refs，防过桥抽板） |
| consensus_recall | 专搜 tier='consensus' 的记忆 |
| consensus_health_check | 检查共识域是否有高度相似记录（embedding>0.85），提示 AI 精炼 |
| add_consensus_ref | 手动建立项目→共识域引用（ref_source='manual'） |
| list_consensus_projects | 列出共识域（供 promote 选择目标） |
| mem_list_projects | 列动态记忆的 project |
| init_project_context | 目录身份探测（不变） |

## 与 07 / 08 的对比

| 维度 | 07 单表 Tier | 08 三表引用 | ✨ 本方案（单表+Refs） |
|---|---|---|---|
| 物理架构 | 极简 (1表) | 复杂 (3表+3套索引) | **极简** (1主表+1映射表+1套索引) |
| promote 操作 | 极速 UPDATE | 极重 (搬数据+搬Vector) | **极速 UPDATE** + 加一条关系 |
| promote 合并去重 | 无 | 字符串拼接（不可逆+臃肿） | **不做自动合并**（保留独立记录，AI 决定何时精炼） |
| demote 回退 | 改标记即可 | 搬回原表（需找来源） | **origin_project 精确回退** |
| 显式依赖关系 | 无 | 有 (`project_consensus`) | **有** (`project_refs`) |
| refs 生命周期 | 无 refs | 只建不维护（孤儿引用） | **自动维护**（delete/demote 时清理） |
| 开场自动加载 | 无法加载共识 | 自动打包所有引用共识 | **自动打包（防爆 top-N）** |
| 修改共识后 | 原地改 | 原地改（双表判断） | **原地改（单表，无判断）** |
| 检索排序 | 同表可比 | 需跨表 RRF 归一化 | **同表可比** |

## V10.1 软装补丁 (Soft Patch) —— 大模型防呆与召回防淹没

基于单表无物理边界的特性，在 Python 业务逻辑层（`mcp_server.py`）增加以下两道强力拦截器：

### 1. 越权确认机制（解决大模型误伤共享共识）
在 `mem_update` 和 `mem_delete` 工具内部注入前置校验——**不是一刀切禁止，而是要求显式确认**：
```python
# 修改/删除前，查询该记忆的 tier
row = SELECT tier, origin_project FROM memory_facts WHERE obs_uuid=?
if row["tier"] == "consensus" and not args.get("confirm_consensus"):
    # 查询被多少项目引用，告知影响面
    refs = SELECT COUNT(*) FROM project_refs WHERE ref_project=?
    return {"warning": f"⚠️ 这是一条全局共识，被 {refs} 个项目引用。"
            f"确认修改/删除请传 confirm_consensus=true。"
            f"如需降级为动态记忆请用 memory_demote。"}
# confirm_consensus=true → 正常执行（AI 知道自己在改共识）

# ★ 脐带剪断强制检查（mem_update 专属）：
# 如果 AI 修改了 consensus 的 content，必须同时决定 origin_project 的去留：
#   - 纯更新措辞（内容仍只属 origin_project）→ 保留 origin_project 不动
#   - 合并多条共识（内容已融合多源）→ 必须传 origin_project='' 剪断脐带
# 如果 content 变了但 AI 没传 origin_project 参数 → 警告，要求显式声明
if row["tier"] == "consensus" and "content" in args and "origin_project" not in args:
    return {"warning": "⚠️ 你正在修改共识内容。请显式声明溯源状态："
            "传 origin_project='' （合并多源共识时剪断脐带），"
            "或传 origin_project=<原值> （仅更新措辞，溯源不变）。"}
```
**价值**：既达到了防呆效果（AI 被迫意识到自己在操作共享共识），又不阻断合法的共识更新。AI 第一次调用会被拦截并收到影响范围提示；确认后第二次调用放行。修改共识内容时还强制要求显式声明 origin_project 去留——防止 AI 合并精炼后忘记剪断脐带，导致 demote 防护被绕过。

### 2. 单次查询同源 RRF + Python 层配额截取（解决共识库淹没本地草稿）

**⚠️ 关键约束：绝对不能拆成两次 hybrid_search_rrf 查询！**

RRF 分数基于 rank 计算（`1/(k+rank)`），拆成两次查询后，q4 池的第 1 名和 consensus 池的第 1 名拿到相同的 RRF 分数（都是 rank=0），合并时两边分数交错，RRF 融合彻底失效。单表的同源优势只有在**同一次查询、同一个 rank 空间**里才成立。

**正确做法**：单次查询走完整 RRF，在 Python 层按 tier 做配额截取：

```python
def _recall(self, query, current_project):
    refs = SELECT ref_project FROM project_refs WHERE project=?
    search_scope = [current_project] + refs

    # ★ 单次查询：q4 + consensus 在同一个候选池里做 BM25+cosine→RRF
    # rank 空间统一，分数天然可比
    ranked = self.searcher.hybrid_search_rrf(
        query, vec,
        project_filter=f"AND (mf.project IN ({placeholders}))",  # 同时覆盖 q4 和 consensus
        limit=50  # 取足够多的候选，Python 层再截取
    )

    # ★ 配额截取（三步法）：先保底，再竞争，最后统一排序
    # 不能用顺序遍历即时填充——高分项会提前把坑位填满，弱势群体的保底失效
    q4_min, cons_min, total_limit = 2, 2, 10

    # 第一步：按 tier 分组
    q4_items = [x for x in ranked if x['tier'] == 'q4']
    cons_items = [x for x in ranked if x['tier'] == 'consensus']

    # 第二步：强制吃掉各自的保底配额（按各自 RRF 分数从高到低拿）
    # 即使草稿排在第 49/50 名，只要池子里有，必定拿到 2 个保底位
    final = []
    final.extend(q4_items[:q4_min])        # q4 保底
    final.extend(cons_items[:cons_min])    # consensus 保底

    # 第三步：剩余候选混合，按统一 RRF 分数竞争剩下的空位
    remaining_q4 = q4_items[q4_min:]
    remaining_cons = cons_items[cons_min:]
    remaining_pool = remaining_q4 + remaining_cons
    slots_left = total_limit - len(final)
    final.extend(sorted(remaining_pool, key=lambda x: x['score'], reverse=True)[:slots_left])

    # 最终按统一 RRF 分数降序返回（保底项和竞争项公平排序）
    return sorted(final, key=lambda x: x['score'], reverse=True)[:total_limit]
```

**机制**：
1. **单次 SQL 查询**：q4 和 consensus 在同一个 `WHERE project IN (scope)` 候选池里，BM25 和 cosine 的 rank 空间统一，RRF 融合分数天然可比
2. **保底配额（三步法第一步）**：按 tier 分组后各自取 top-N（q4 保 2、consensus 保 2）。即使 q4 草稿排在候选池第 49/50 名，只要池子里有就必定拿到保底位——不能用顺序遍历即时填充，否则高分 consensus 会提前把坑位填满，弱势群体的保底失效
3. **竞争配额（三步法第二步）**：保底之后剩余的 q4+consensus 候选混合，按统一 RRF 分数竞争剩下的空位——共识比草稿更相关时，共识优先
4. **统一排序（三步法第三步）**：保底项和竞争项混合后按 RRF 分数统一降序，输出给 AI 的是严格按相关度排好的列表

## 设计要点备忘

以下是在审查中确认的关键设计决策：

1. **promote 不做自动合并**：多项目同主题记忆各自独立 promote，各自保留 obs_uuid。避免字符串拼接导致的不可逆 + 信息臃肿。AI 认为需要精炼时，由 AI 主动 mem_update 写新版本 + mem_delete 旧版本——内容由 AI 判断，不由代码拼接。
2. **origin_project 列**：promote 时记录原始 project，使 demote 可以精确回退到来源项目。合并精炼后 AI 主动清空 origin_project（脐带剪断），demote 检测到为空则拒绝降级——溯源黑洞防护。不用 updated_at 判断（promote 本身会刷新 updated_at，导致全员封杀）。
3. **project_refs 生命周期维护（两条安全路径）**：① mem_delete 删共识后检查域是否清空→清理 promote refs（不删 manual refs）② 项目动态记忆全删后清理出向 promote refs。**demote 绝不清理 project_refs**——脐带剪断后 COUNT 无法统计融合共识，demote 清理会误杀项目与自己参与过的共识的可见性（过桥抽板）。
4. **ref_source 列区分引用来源**：project_refs 加 `ref_source`（'promote'/'manual'），所有清理逻辑只删 `ref_source='promote'`，绝不误删 add_consensus_ref 手动建的引用网络。
5. **mem_save upsert 加 tier 守卫**：`WHERE ... AND tier='q4'`，保证 mem_save 只在动态记忆范围内 upsert，不碰 consensus 行。
6. **越权确认（非一刀切禁止）**：mem_update/mem_delete 操作 consensus 行时不直接拒绝，而是要求 `confirm_consensus=true`。AI 第一次被拦截并收到影响范围提示，确认后第二次调用放行——既防呆又不阻断合法更新。
7. **★ 单次查询同源 RRF + 三步法配额截取**：mem_recall 绝不拆成两次 hybrid_search_rrf（拆开后两个独立 rank 空间的 RRF 分数不可比，融合失效）。必须单次查询让 q4+consensus 在同一候选池做 RRF，然后 Python 层用三步法配额截取：①按 tier 分组各自取保底（q4 保 2、consensus 保 2）②剩余候选混合按 RRF 分数竞争空位 ③统一按 RRF 分数降序返回。不能用顺序遍历即时填充（高分项会提前填满坑位，弱势群体保底失效）。
8. **consensus_health_check 工具**：不做自动合并但提供触发机制——AI promote 后或定期调用此工具，发现相似记录（embedding>0.85）后由 AI 决定是否精炼。解决"不做自动合并导致堆积"的副作用。
9. **demote 溯源黑洞防护（脐带剪断）**：AI 在合并精炼共识时，通过 mem_update 显式清空 origin_project=''。demote 时若 origin_project 为空则拒绝降级——多源融合后已不再专属任何项目。mem_update 修改 consensus content 时强制要求显式声明 origin_project 去留（传 '' 剪断或传原值保留），防止 AI 忘记剪断导致 demote 防护被绕过。不用 updated_at 判断（promote 本身会刷新 updated_at，导致全员封杀）。
10. **防爆机制**：`_context` 用 `LIMIT consensus_limit`（默认 5）按 `pinned DESC, updated_at DESC` 取 top-N，不用 pinned 硬过滤。
11. **list_consensus_projects 工具**：AI promote 前查看现有共识域列表。

> 数据迁移是一次性操作，不纳入本方案，单独处理。
