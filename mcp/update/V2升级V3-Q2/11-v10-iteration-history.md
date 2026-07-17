# V10 方案迭代纪事 —— 从初版到 RFC 的来时路

> 这份文档记录了方案 10（单表全家桶 + 虚拟外键引用）从诞生到定稿的全过程。
> 每一次迭代都源于一次深度审查发现的缺陷，每一次修复都让架构更坚固。
> 这是辛苦的来时路——最终 RFC 规范里的每一条设计约束，背后都有一个被修复的血泪教训。

## 时间线总览

```
V10 初版（单表+tier+refs 基本骨架）
  │
  ├─ 审查 1：缺 promote 去重 / demote 不知原 project / list 工具缺失
  │  → 补去重合并 + origin_project + list_consensus_projects
  │
  ├─ 审查 2：合并去重不可逆+臃肿 / demote 溯源断裂 / 孤儿引用
  │  → 抛弃自动合并，改 AI 精炼 + origin_project 回溯 + refs 生命周期维护
  │
  ├─ 审查 3：防呆缺失 / 召回倾斜
  │  → V10.1 软装补丁：越权拦截器 + 双通道配额
  │
  ├─ 审查 4（外部 AI）：合并不可逆 / 越权误伤 / 召回倾斜 / 孤儿引用
  │  → 抛弃自动合并改 health_check + confirm 确认 + 自适应配额 + ref_source 保护
  │
  ├─ 审查 5（外部 AI）：🚨 RRF 跨池分数不可比（致命数学错误）
  │  → 单次查询同源 RRF + Python 层配额截取
  │
  ├─ 审查 6（外部 AI）：配额算法 bug（贪心填充导致保底失效）
  │  → 三步法配额：先保底→再竞争→统一排序
  │
  ├─ 审查 7（外部 AI）：🚨 demote 防御补丁导致"全员封杀"
  │  → 从 updated_at 判断改为 origin_project 脐带剪断机制
  │
  ├─ 审查 8：mem_update 忘记传 origin_project='' 绕过 demote 防护
  │  → 脐带剪断强制检查（mem_update 改 content 时强制声明 origin_project 去留）
  │
  └─ 审查 9（外部 AI）：demote 清理 refs 导致"过桥抽板"
     → demote 彻底放弃清理 project_refs，只保留两条绝对安全路径
```

---

## 迭代 1：基本骨架补全

**发现者**：内部审查

**发现的问题**：
1. promote 缺去重逻辑——两个项目各 promote 同一条，consensus 域有两条重复
2. demote 不知道改回哪个 project——promote 时 `UPDATE project=共识域名` 覆盖了原值
3. 缺 `list_consensus_projects` 工具——AI promote 时不知道有哪些共识域可选

**修复**：
- 加 promote 去重（同标题检查）
- 加 `origin_project` 列记录来源
- 加 `list_consensus_projects` 工具

---

## 迭代 2：合并去重的致命缺陷

**发现者**：内部审查（对自己设计的反思）

**发现的问题**：
1. **合并去重不可逆**：promote 去重用 `content || '\n\n---\n\n' || new_content` 字符串拼接 + 删原行。合并后原始 obs_uuid 被删，demote 无法分离——"这段文字属于谁"无从知晓
2. **信息臃肿**：代码层 `||` 拼接不是知识提炼，5 轮合并后变成 1000 字流水账，消耗 token 且降低 AI 理解效率
3. **孤儿引用**：`project_refs` 只建不删，项目完结后僵尸引用堆积

**修复**：
- **抛弃自动合并**——每条记忆独立 promote，保留各自 obs_uuid。AI 认为需要精炼时，由 AI 主动 mem_update 写新版本 + mem_delete 旧版本。内容由 AI 判断，不由代码拼接
- `origin_project` 精确回退——demote 改回 `tier='q4', project=origin_project`
- `project_refs` 生命周期维护——mem_delete 删共识后检查域清空→清 refs；项目全删后清出向 refs

**教训**：用数据库的字符串操作完成知识工程的内容融合——这两者的能力级别不匹配。真正的合并应该是 AI 读两条后写一条精炼的新记忆，而非 `||` 拼接。

---

## 迭代 3：V10.1 软装补丁（防呆与召回）

**发现者**：内部审查

**发现的问题**：
1. **防呆缺失**：单表设计让 AI 分不清"这是我的草稿"还是"这是别人的共识"。AI 看到 consensus 觉得"这句多余"，顺手 mem_update 改了——影响所有引用该共识的项目（蝴蝶效应）
2. **召回倾斜**：mem_recall 把 q4 和 consensus 放同候选池 RRF 排序。共识增长后数据量远大于单项目草稿，局部记忆被全局常识淹没

**修复（V10.1 软装补丁）**：
- 越权拦截器：mem_update/mem_delete 操作 consensus 行时拦截
- 双通道配额检索：q4 和 consensus 各取 top-N

---

## 迭代 4：外部 AI 深度审查（4 个设计层缺陷）

**发现者**：外部 AI（Gemini Antigravity）

这次审查更深入设计层而非代码层，发现了 4 个架构级缺陷：

### 缺陷 1：缝合怪陷阱（合并去重导致不可逆与信息臃肿）

与迭代 2 的发现一致，但更尖锐地指出：即使不做字符串拼接，自动合并本身就有"无法降级撤销"和"上下文垃圾场"的问题。

**修复**：确认"不做自动合并"的方向，新增 `consensus_health_check` 工具作为触发精炼的机制——AI promote 后或定期调用，发现 embedding 相似度>0.85 的记录对，提示 AI 精炼。工具不做任何自动操作，只报告+建议。

### 缺陷 2：越权误伤（共享记忆防呆薄弱）

V10.1 的越权拦截器是**一刀切禁止**（return error），但这样阻止了合法的共识更新。

**修复**：改为 `confirm_consensus=true` 确认机制——第一次调用被拦截并返回影响范围（被 N 个项目引用），AI 确认后第二次调用放行。既防呆又不阻断。

### 缺陷 3：召回倾斜（鸠占鹊巢）

V10.1 的双通道配额是硬编码 `limit=5`，项目只有 1 条动态记忆时浪费 4 个配额位。

**修复**：改为自适应配额——按 q4/consensus 实际候选数分配，保底各 2 个，不足时让出。

### 缺陷 4：空域清理误杀手动引用（连坐效应）

mem_delete 清理空共识域时无差别删除所有 refs，AI 删旧共识准备写新共识的瞬时空窗会误杀手动引用。

**修复**：`project_refs` 加 `ref_source` 列（'promote'/'manual'），所有清理只删 `ref_source='promote'`，绝不删手动建的。

---

## 迭代 5：🚨 致命——RRF 跨池分数不可比

**发现者**：外部 AI（Gemini Antigravity）

**这是整个迭代过程中最致命的发现——一个数学层面的错误。**

### 问题

迭代 4 的"自适应双通道配额"把 mem_recall 拆成了**两次独立的** `hybrid_search_rrf` 调用：

```python
results_q4 = hybrid_search_rrf(query, tier='q4', ...)        # 查询 1
results_consensus = hybrid_search_rrf(query, tier='consensus', ...)  # 查询 2
return sorted(results_q4 + results_consensus, key='score')   # 合并
```

RRF 公式是基于**排名（Rank）**计算的：`score = 1/(k + rank_BM25) + 1/(k + rank_Vector)`。

- 查询 1（q4 池）排第 1 的，RRF 分数 ≈ 0.033
- 查询 2（consensus 池）排第 1 的，RRF 分数也 ≈ 0.033

两个池的 rank 空间是**独立的**——"q4 里的第 1 名"和"consensus 里的第 1 名"拿到相同的 RRF 分数，尽管绝对相关度可能差 10 倍。Python 层 `sorted(key='score')` 合并后，两边分数交错，**RRF 融合彻底失效**。

### 讽刺

这正是我之前在 08 三表方案中警告过的"跨表 RRF 不可比"问题。我修到单表方案时却犯了自己警告过的错误。单表的同源优势只有在**同一次查询**里才成立，拆成两次就丢了。

### 修复

**单次查询同源 RRF + Python 层配额截取**：

```python
# 单次查询：q4 + consensus 在同一候选池做 RRF（rank 空间统一）
ranked = self.searcher.hybrid_search_rrf(query, vec, project_filter="AND mf.project IN (scope)", limit=50)

# Python 层配额截取（从排好序的结果按 tier 分配配额）
```

**教训**：单表的同源 RRF 优势**只有在同一次查询、同一个 rank 空间里才成立**。任何拆分查询的做法都会让 RRF 分数变成两个独立坐标系，不可比较。

---

## 迭代 6：配额算法 bug（贪心填充导致保底失效）

**发现者**：外部 AI（Gemini Antigravity）

### 问题

迭代 5 的 Python 层配额截取用了**顺序遍历即时填充**：

```python
for item in ranked:
    if len(results) >= total_limit:
        break
    if item['tier'] == 'q4':
        results.append(item)  # 即时装填
    elif item['tier'] == 'consensus':
        results.append(item)  # 即时装填
```

**极端场景**：共识库资料丰富，ranked 前 10 名全是 consensus。循环遍历前 10 项全部塞入 results。循环到第 11 项（第一个 q4）时 `len(results) >= 10` 触发 break——**q4 保底配额完全没保住**。

根因：**顺序遍历即时填充**——高分项把坑位填满后，弱势群体没有机会上桌。保底配额必须在竞争配额之前预留。

### 修复（三步法配额截取）

```python
# 第一步：按 tier 分组
q4_items = [x for x in ranked if x['tier'] == 'q4']
cons_items = [x for x in ranked if x['tier'] == 'consensus']

# 第二步：强制吃掉各自的保底配额（即使排在第 49/50 名）
final = q4_items[:2] + cons_items[:2]

# 第三步：剩余候选混合，按统一 RRF 分数竞争剩下的空位
remaining = sorted(q4_items[2:] + cons_items[2:], key='score', reverse=True)
final.extend(remaining[:10 - len(final)])

# 最终按统一 RRF 分数降序返回
return sorted(final, key='score', reverse=True)[:10]
```

**三步法的正确性**：
1. **绝对保底**：哪怕草稿排在第 49/50 名，必定拿到 2 个保底位
2. **绝对公平**：保底之后，剩余坑位严格遵循 RRF 分数竞争
3. **输出有序**：最终返回按 RRF 绝对分数严格降序

**教训**：配额保底不能靠顺序遍历——必须先预留保底位，再让剩余候选竞争。这是调度算法的经典原则（优先级反转防护）。

---

## 迭代 7：🚨 demote 防御补丁导致"全员封杀"

**发现者**：内部审查（自查 + 外部 AI 协助确认）

### 问题

demote 的溯源黑洞防护最初用 **`updated_at > created_at`** 判断——认为"内容被修改过的共识拒绝降级"。

但 promote 的 SQL 本身就包含 `SET updated_at=CURRENT_TIMESTAMP`：

```sql
UPDATE memory_facts SET tier='consensus', project=?, updated_at=CURRENT_TIMESTAMP WHERE obs_uuid=?
```

这意味着 **promote 执行后 `updated_at` 必然大于 `created_at`**。用时间戳判断会导致**所有 promote 过的记录都被拒绝 demote**——全员封杀。demote 工具完全废了。

### 修复

从 `updated_at > created_at` 判断改为 **`origin_project` 为空**判断（脐带剪断机制）：

- promote 时：`origin_project = 原始项目名`（写入脐带）
- AI 合并精炼时：`mem_update` 主动传 `origin_project=''`（剪断脐带）
- demote 时：检查 `origin_project` 是否为空——为空说明已融合多源，拒绝降级

`origin_project` 是**语义化**的脐带——promote 时写入，合并精炼时剪断，判断逻辑精确且无歧义。不像 `updated_at` 会被 promote 本身的副作用污染。

**教训**：用会被业务操作副作用的字段（updated_at）做语义判断，一定会出 bug。必须用语义化的专用字段（origin_project）。

---

## 迭代 8：mem_update 忘记传 origin_project='' 绕过 demote 防护

**发现者**：内部审查

### 问题

迭代 7 的脐带剪断机制依赖 AI 在合并精炼时**主动传 `origin_project=''`**。但 AI 可能只改 content 忘记清空 origin_project：

```
AI 调 mem_update(obs_id=共识A, content=融合后的内容)
→ 忘记传 origin_project=''
→ origin_project 仍然指向项目 X
→ demote(共识A) → 通过检查（origin_project 不为空）→ 降级到项目 X
→ 但内容已融合了项目 Y 的信息 → 项目 Y 的记忆永久丢失
```

防护被绕过了——因为脐带剪断依赖 AI 主动行为，而 AI 可能忘记。

### 修复（脐带剪断强制检查）

在 mem_update 的越权确认逻辑里加**参数级强制检查**：

```python
# 如果 AI 修改了 consensus 的 content，必须同时决定 origin_project 的去留
if row["tier"] == "consensus" and "content" in args and "origin_project" not in args:
    return {"warning": "你正在修改共识内容。请显式声明溯源状态："
            "传 origin_project='' （合并多源共识时剪断脐带），"
            "或传 origin_project=<原值> （仅更新措辞，溯源不变）。"}
```

AI 不可能"忘记"——代码强制它做出选择。修改 consensus content 时如果不传 origin_project，直接被拦截要求显式声明。

**教训**：依赖 AI 主动行为的防护一定会被绕过（AI 会忘记/忽略）。必须在代码层强制——参数级检查比提示级提醒可靠得多。

---

## 迭代 9：demote 清理 refs 导致"过桥抽板"

**发现者**：外部 AI（Gemini Antigravity）

**这是最隐蔽的 edge case——需要四步沙盘推演才能发现。**

### 问题

demote 里曾经有清理 project_refs 的逻辑：

```python
# 检查 origin 是否还有该域的共识
other_refs = SELECT COUNT(*) FROM memory_facts
             WHERE tier='consensus' AND origin_project=origin
if other_refs == 0:
    DELETE FROM project_refs WHERE project=origin AND ref_source='promote'
```

**沙盘推演**：
1. 项目 X promote 共识 A（origin=X），项目 Y promote 共识 B（origin=Y），项目 X promote 共识 C（origin=X）
2. AI 合并 A+B 为融合版，传 `origin_project=''`（脐带剪断），删除 B。现在 A 的 origin 为空，C 的 origin=X
3. AI demote 共识 C 退回项目 X。demote 检查：`origin=X 的 consensus 还有多少？` → A 的 origin 已被清空不计入，C 刚被降级 → **other_refs = 0**
4. 触发清理：`DELETE FROM project_refs WHERE project=X` → **项目 X 与该共识域的引用链接被删**
5. 后果：项目 X 的 `mem_context` 再也拉不到这个共识域——**虽然共识 A 里仍然流淌着项目 X 当初贡献的血液，但项目 X 被剥夺了可见性**

### 根因

当允许 `origin_project=''` 抹去归属标记后，数据库层面就**永远无法准确判断一个项目是否还参与某个域的建设**。COUNT(origin_project=X) 统计不到已融合的共识。

### 修复

**demote 彻底放弃清理 project_refs**。

`project_refs` 的清理只保留两条**绝对安全路径**：
1. **mem_delete 导致域清空**：整个共识域的记忆都没了 → 清理指向该域的 refs。判断条件是"域里 COUNT=0"，不受 origin_project 影响。
2. **项目动态记忆全删**：项目本身消失了 → 清理出向 refs。判断条件是"项目的 q4 COUNT=0"，也不受 origin_project 影响。

demote（把一条记忆退回草稿）这种**局部操作**绝不应该影响 project_refs（项目级引用关系）。

> **宁可多带一点额外的共识上下文，也绝不切断项目与自己参与过的共识的可见性。**

**教训**：当系统允许"抹去归属标记"（origin_project=''）时，所有依赖归属标记做 COUNT 判断的清理逻辑都不可靠。清理条件必须基于**绝对事实**（域空了/项目没了），不能基于**可被抹去的归属**。

---

## 最终 RFC 的 11 条设计约束

以上 9 次迭代的结果，凝结为最终 RFC（`10-single-table-virtual-refs.md`）里的 11 条设计要点备忘。每一条背后都有一个被修复的缺陷：

| # | 设计约束 | 来源迭代 | 防御的缺陷 |
|---|---|---|---|
| 1 | promote 不做自动合并 | 迭代 2、4 | 合并不可逆 + 信息臃肿 |
| 2 | origin_project 列 + 不用 updated_at 判断 | 迭代 2、7 | demote 溯源断裂 + 全员封杀 |
| 3 | project_refs 两条安全清理路径 | 迭代 2、9 | 孤儿引用 + 过桥抽板 |
| 4 | ref_source 区分引用来源 | 迭代 4 | 空域清理误杀手动引用 |
| 5 | mem_save upsert 加 tier 守卫 | 迭代 1 | mem_save 覆盖 consensus |
| 6 | confirm_consensus 确认机制 | 迭代 3、4 | 蝴蝶效应式误伤 |
| 7 | 单次查询同源 RRF + 三步法配额 | 迭代 5、6 | RRF 跨池失效 + 保底失效 |
| 8 | consensus_health_check 工具 | 迭代 4 | 不做自动合并导致堆积 |
| 9 | 脐带剪断强制检查 | 迭代 7、8 | demote 防护被绕过 |
| 10 | 防爆机制（top-N 而非 pinned 硬过滤） | 迭代 3 | context 爆炸 |
| 11 | list_consensus_projects 工具 | 迭代 1 | AI 不知道有哪些共识域 |

---

## 核心教训总结

这 9 次迭代揭示了 4 个深层设计原则：

1. **确定性归代码，模糊性归 AI**：内容合并不该用 `||` 拼接（代码层），该让 AI 判断（语义层）。但触发合并的机制（health_check）由代码提供。

2. **不要用会被副作用的字段做语义判断**：`updated_at` 会被 promote 刷新，所以不能用来判断"是否被修改过"。必须用语义化的专用字段（`origin_project`）。

3. **依赖 AI 主动行为的防护一定会被绕过**：提示 AI"记得传 origin_project=''"不如代码强制"不传就拦截"。参数级检查 > 提示级提醒。

4. **当系统允许抹去归属标记时，所有依赖归属的 COUNT 判断都不可靠**：清理条件必须基于绝对事实（域空了/项目没了），不能基于可被抹去的归属。

---

> 这份文档不仅记录了方案 10 的演进，更记录了一种工程方法论：
> 每一次"找茬"都不是对设计的否定，而是对确定性的追求。
> 最终 RFC 里的每一条约束都是"被血泪验证过的"——这就是为什么它叫 RFC 而不是 draft。
