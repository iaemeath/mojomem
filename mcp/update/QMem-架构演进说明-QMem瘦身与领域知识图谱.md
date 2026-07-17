# QMem 架构演进说明：现状（双 tier）与 Y 方案（瘦身 + 独立领域知识图谱）

> **日期**：2026-07-17
> **文档性质**：供第三方评判的架构说明文档。客观陈述当前 QMem 架构与拟议的 Y 方案架构，列出对比维度，**不替评判者下结论**。
> **评判请求**：请阅读者基于自身工程经验，判断"保持现状（双 tier 共识域）"与"瘦身 QMem + 新建独立领域知识图谱 MCP"两种走向哪种更合理。文末列有作者倾向，但明确标注为倾向而非定论。
> **关联文档**：系统体检见 `QMem-FullAnalysis-2026-07-17.md`，机制细节见 `QMem-V3.2-Architecture.md`，V3.3 治理见 `QMem-V3.3-Architecture.md`，概念总纲见 `QMem-两种记忆方式-项目记忆与领域知识图谱.md`。

---

## 〇、背景：这套系统是干什么的

QMem 是一个**自托管的 AI 跨会话记忆系统**，以 MCP（Model Context Protocol）server 形态运行，通过 JSON-RPC over stdin/stdout 与宿主 AI（Claude Code / Cursor）通信。

- **定位**：寄生系统——无后台进程、无外部 API、全本地触发。所有记忆操作由 AI 在会话中显式调用工具驱动。
- **根目录**：`D:\cly-marketplace\qmem\mcp\`
- **记忆库**：`core_memory.db`（SQLite，2.6MB）
- **审计库**：`call_log.db`（独立 WAL 文件）
- **MCP 入口**：`python\mcp_server.py`

它要解决的核心问题：**AI 会话之间没有记忆**。第二次打开同一个项目时，AI 不记得上次做到哪、踩过什么坑、这个项目的业务概念是什么。QMem 给 AI 一个持久化的记忆层。

---

## 一、当前 QMem 架构（双 tier 共识域）

### 1.1 一句话概括

**单库单表 + tier 字段分层**。动态记忆（q4）和跨项目共识（consensus）存在同一张表里，靠 `tier` 字段区分；项目通过虚拟外键引用共识域。同时嵌套转发 codebase-memory-mcp（CBM）的代码图谱查询。

### 1.2 数据模型（schema.sql 实读）

**核心是"单表全家桶"架构**：

```
memory_facts (主表) — 13 列
├── id, obs_uuid(12位hex), project, topic_key, title, content
├── type(reference/progress/decision/bugfix/learning/manual)
├── tier(q4 | consensus)              ← 分层关键字段
├── origin_project                    ← promote 前来源（demote 回溯依据）
├── content_hash                      ← sha256(title+content)[:16]
└── created_at, updated_at, deleted_at

memory_vectors (vec0 虚表)     — rowid 对齐 memory_facts.id，512 维 cosine
memory_facts_fts (FTS5)        — external-content，指向 memory_facts
project_refs                   — project↔consensus_domain 多对多引用（promote/manual 两种来源）
```

**4 个触发器**保证 facts 与 vectors/fts 一致（FTS insert/update/delete + vector delete）。
**⚠️ 没有 update 触发器同步向量**——改 content/title 时必须手动 DELETE+INSERT 向量（`mcp_server.py` 的 `_update` 显式这么做），这是隐式约束。

### 1.3 tier 分层语义

| tier | 含义 | project 字段填什么 | 生命周期 |
|---|---|---|---|
| `q4` | 项目动态记忆 | 具体项目名（如 `changzhou-balance-plan`） | 高频读写，随项目演进 |
| `consensus` | 跨项目共识 | 共识域名（如 `power-grid-domain`、`java-cloud-common`） | 低频写入，沉淀后基本不动 |

**promote/demote 是 O(1) 操作**：只 UPDATE tier + project + origin_project，不搬数据不重算向量。这是单表架构的核心巧思。

### 1.4 技术栈

| 层 | 技术 | 说明 |
|---|---|---|
| 嵌入模型 | `bge-small-zh-v1.5`（ONNX） | 512 维，本地 CPU 推理 |
| 向量存储 | `sqlite-vec`（vec0 虚表） | cosine 距离 |
| 全文检索 | SQLite FTS5 | external-content，unicode61 分词 |
| 数据库 | SQLite | 单库多表 + 4 触发器 |
| 运行时 | Python 3.13 | 依赖仅 5 个包 |
| 外部协作 | `codebase-memory-mcp.exe`（257MB 原生 C 二进制） | subprocess 包装转发代码图谱查询 |

### 1.5 工具清单（17 个本地工具 + CBM 转发）

**写入类（4 个）**

| 工具 | 机制要点 |
|---|---|
| `mem_save` | 共识域守卫（project_id 是域时拦 q4）+ topic_key upsert + 写入门禁（`_nearest_neighbor` 查近邻，sim>0.85 拦截，force 放行） |
| `mem_update` | 越权守卫（consensus 需 confirm_consensus）+ 脐带剪断强制（改 consensus content 需声明 origin_project 去留）+ 手动重算向量 |
| `mem_delete` | 默认软删除；hard=true 物理删；删 consensus 需 confirm_consensus；空域/空项目自动清理 project_refs |
| `memory_promote` | 原地飞升：UPDATE tier+project+origin_project + 建 ref |

**读取类（5 个）**

| 工具 | 机制要点 |
|---|---|
| `mem_recall` | RRF 混合检索（BM25+cosine，k=60）+ 三步法配额（q4 min2 + consensus min2） |
| `mem_context` | 开场召回：q4 全量 + 引用共识 top-N + review_queue（超 30 天的 progress） |
| `mem_get_full` | 按 obs_id 拉全文，支持批量 |
| `consensus_recall` | 专搜 consensus tier |
| `mem_search` | FTS5 MATCH 精确过滤（仅 q4） |

**治理类（8 个）**

| 工具 | 机制要点 |
|---|---|
| `memory_demote` | 降级回 q4；origin_project 为空则拒绝（溯源黑洞防护） |
| `consensus_health_check` | 域内两两 embedding 点积 >0.85 报重复 |
| `cross_project_health_check` | 跨项目 q4 语义重复检测（promote 候选） |
| `mem_consolidate_project` | 单项目内相似簇检测（消化存量堆积） |
| `add_consensus_ref` / `list_consensus_projects` / `mem_list_projects` / `init_project_context` | 引用管理、列举、目录身份探测 |

**CBM 转发**：`search_graph` / `trace_path` / `detect_changes` / `get_architecture` 等代码图谱查询由 `cbm_wrapper.py` 转发到 `codebase-memory-mcp` 子进程，崩溃自动 respawn + 重试。

### 1.6 检索引擎（search_rrf.py）

**RRF（Reciprocal Rank Fusion）融合**：
- 词法路（FTS5 BM25）+ 语义路（vec_distance_cosine）
- 同一 rank 空间，分数 = `Σ 1/(k + rank + 1)`，k=60
- 向量查询用函数式 API `vec_distance_cosine(mv.embedding, ?)`，而非 vec0 的 MATCH 语法（后者不能与普通列 WHERE 混用）

### 1.7 V3.3 治理层

| 治理点 | 对抗问题 |
|---|---|
| 写入门禁 | 写入盲目性（无脑新建） |
| 检索去重+质量信号 | 检索出口污染（staleness / is_superseded / deduped） |
| rowcount 精确化 | 误导调用方 |
| review_queue | progress 永不过期（拉模型复审） |
| 单项目 consolidate | 存量堆积 |

### 1.8 三层知识承载

当前 QMem 实际承载三类知识，全部塞在一个系统里：

| 知识类型 | tier | 例子 |
|---|---|---|
| **项目工程状态** | q4 | changzhou 架构/进度/踩坑 |
| **跨项目技术陷阱** | consensus（`java-cloud-common`/`weakpwd`） | IS_DELETE 中文值、CLOB 陷阱 |
| **业务领域知识** | consensus（`power-grid-domain`） | 发电计划 96 点、D5000 表结构、负荷电量 |

> **注**：跨项目技术陷阱目前**同时**也硬编码在全局 `~/.claude/CLAUDE.md`（每次会话自动注入），与 consensus 域存在职责重叠。

---

## 二、Y 方案架构（瘦身 QMem + 独立领域知识图谱 MCP）

### 2.1 一句话概括

**把共识域从 QMem 中剥离，QMem 回归纯项目记忆；业务领域知识独立成一个专门的 MCP 知识服务器，为 AI 提供语义理解能力。** 三套系统正交分工。

### 2.2 整体架构图

```
┌─────────────────────────────────────────────────────────────┐
│                      宿主 AI（Claude Code）                  │
│                                                              │
│   会话中按需调用三类 MCP server（各自独立进程/库/工具空间）  │
└──────────┬──────────────────┬──────────────────┬────────────┘
           │                  │                  │
           ▼                  ▼                  ▼
  ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
  │  QMem（瘦身）   │ │ 领域知识图谱 MCP │ │  CBM（不变）    │
  │  纯项目记忆     │ │ 电力调度语义理解 │ │  代码事实图谱   │
  │  q4 only        │ │  （新建）        │ │                 │
  │                 │ │                 │ │                 │
  │ · 架构/端口     │ │ · 发电计划概念  │ │ · 函数/类/方法  │
  │ · 任务进度      │ │ · D5000表结构   │ │ · 调用关系      │
  │ · 踩坑根因      │ │ · 负荷电量      │ │ · 表字段        │
  │ · 架构决策      │ │ · 新能源出力    │ │ · 架构社区检测  │
  │ · 个人偏好      │ │ · 设备重载      │ │                 │
  │                 │ │                 │ │                 │
  │ tier=q4 单层    │ │ 独立 SQLite 库  │ │ 闭源 exe        │
  │ 按项目隔离      │ │ RAG 语义召回    │ │ Cypher 查询     │
  └─────────────────┘ └─────────────────┘ └─────────────────┘
           ▲
           │ 另有兜底层
           ▼
  ┌─────────────────────────────────────────────────────────┐
  │  全局 ~/.claude/CLAUDE.md（永久规范，每次会话自动注入）  │
  │  · IS_DELETE 中文值、CLOB 陷阱（跨项目技术陷阱硬规范）  │
  │  · 通用编码约束、内网环境说明                            │
  └─────────────────────────────────────────────────────────┘
```

### 2.3 三个系统的职责边界（正交三维）

这是 Y 方案的核心设计——**三个系统沿三个正交的维度分工，互不重叠**：

| 系统 | 维度 | 回答的问题 | 召回方式 | 触发时机 |
|---|---|---|---|---|
| **QMem（瘦身）** | **时间**（项目演进） | "这个项目现在做到哪了？架构是什么？" | 按项目隔离，拉模型 | 编码开场 + 收尾 |
| **领域知识图谱 MCP** | **认知**（业务理解） | "这个业务概念到底是什么？AI 别理解错" | 跨项目共享，拉模型 | 需求分析 / 语义理解 |
| **CBM** | **结构**（代码实体） | "这个函数被谁调用？表字段是什么？" | 代码图谱，拉模型 | 编码查代码事实 |
| **全局 CLAUDE.md** | **规范**（硬约束） | "必须遵守的规则是什么？" | 推模型（全量注入） | 每次会话自动 |

### 2.4 瘦身后的 QMem

**砍掉所有 consensus 相关**：

| 对象 | 处置 |
|---|---|
| `tier` 字段 | 删除（恒为 q4） |
| `origin_project` 字段 | 删除 |
| `project_refs` 表 | DROP |
| `memory_promote` / `memory_demote` / `consensus_recall` / `consensus_health_check` / `add_consensus_ref` / `list_consensus_projects` | 删除（6 个工具） |
| `mem_save` 共识域守卫 | 删除 |
| `mem_context` 引用共识加载 | 删除第二阶段 |
| `mem_recall` 三步法配额 | 简化为单 tier RRF |
| `mem_update` / `mem_delete` confirm_consensus + 脐带剪断 | 删除 |
| `_nearest_neighbor` tier 参数 | 简化为单 tier |

**瘦身后 QMem 剩余**：11 个工具（4 写 + 5 读 + 2 治理），schema 从 13 列降到 11 列，无 project_refs 表。

**跨项目技术陷阱处置（预案）**：每个项目 q4 各存一份副本。理由是项目切换频率低、主要深耕单项目；副本召回在深耕项目内命中率高，未存副本的项目由全局 CLAUDE.md 兜底（双层兜底）。

### 2.5 领域知识图谱 MCP（新建）

**定位**：电力调度领域的语义理解服务，只收 AI 容易理解错的专业概念（"纠偏字典"，非百科全书）。

**技术栈**：复刻 QMem 已验证的三件套——SQLite + sqlite_vec(BGE-small-zh 512维) + FTS5 + RRF 混合检索。

**核心工具（拟）**：

| 工具 | 用途 |
|---|---|
| `domain_lookup(query)` | 语义召回业务概念（主用，需求分析阶段调用） |
| `domain_get(concept_id)` | 拉取单个概念的完整定义 |
| `domain_add` / `domain_update` / `domain_delete` | 概念 CRUD（低频，专家确认后沉淀） |
| `domain_list` | 列出所有概念（看板） |

**内容结构（拟，DCK 领域概念卡）**：每条概念按"定义 → 业务含义 → 对开发的影响 → ⚠️ AI 易误解点（纠偏声明）→ 关联概念"组织。核心是"⚠️ AI 易误解点"。

**与 CBM 的边界**（已明确，不重叠）：
- CBM 管代码实体（函数/调用/表字段），有完整 Cypher + Leiden 社区检测
- 领域 MCP 管业务概念，是"图状语义"但不做图数据库；概念关联写在 content 文本里，靠 RAG 召回 + AI 读文本建立关联
- 多跳路径查询归 CBM，业务概念关联归领域 MCP

**为什么不用 CBM 直接承载业务概念**：CBM 是闭源 exe，唯一写入入口是 `index_repository(repo_path)`，绑定代码仓库；节点 label/ranking 全是代码实体（Function/Method/Route），无业务概念导入入口。CBM 是"代码知识图谱"，不是"通用图数据库"。

---

## 三、两种架构的对比维度

> 以下维度客观列出，供评判者自行权衡。每个维度的"差异说明"尽量不夹带倾向。

### 维度 1：系统复杂度

| | 现状（双 tier） | Y 方案 |
|---|---|---|
| MCP server 数量 | 1 个（QMem，含 CBM 嵌套） | 2 个（瘦身 QMem + 领域 MCP），CBM 不变 |
| QMem 工具数 | 17 个 | 11 个 |
| QMem schema 列数 | 13 列 + project_refs 表 | 11 列，无 project_refs |
| 治理逻辑耦合度 | 高（consensus 逻辑耦合在每个保留工具里） | 低（瘦身后的 QMem 只面对单 tier） |
| 总代码资产 | 一套复杂系统 | 两套简单系统 |

**差异说明**：现状是"一个职责混合的复杂系统"，Y 是"两个职责单一的简单系统"。软件工程的一般经验是后者更易维护，但 Y 多了一个要维护的新系统。

### 维度 2：AI 语义理解的获取方式

| | 现状 | Y 方案 |
|---|---|---|
| 业务知识获取 | `mem_recall` 混合检索时"顺便"带出 consensus | AI 显式调用 `domain_lookup` 专门获取 |
| 主动性 | 被动（检索 q4 时捎带） | 主动（专门调用工具） |
| 注意力集中度 | 业务知识与项目草稿混在同一检索结果 | 业务知识独立召回，无草稿干扰 |

**差异说明**：现状的业务知识获取是"被动捎带"，Y 是"主动调用"。"赋予 AI 语义理解能力"这一诉求，在主动调用形态下更显著。

### 维度 3：检索纯净度（业务知识 vs 项目草稿）

| | 现状 | Y 方案 |
|---|---|---|
| 隔离方式 | 靠 tier 字段 + 纪律保证（需求分析时用 consensus_recall） | 靠物理隔离保证（领域 MCP 里根本没有项目草稿） |
| 纪律依赖 | 高（AI 必须记住用哪个工具） | 低（机制本身保证纯净） |

**差异说明**：现状要求 AI 遵守"需求分析阶段只用 consensus_recall"的纪律；Y 通过物理隔离让机制本身保证纯净，不依赖纪律。

### 维度 4：跨项目技术陷阱的承载

| | 现状 | Y 方案 |
|---|---|---|
| 承载方式 | consensus 域（如 `java-cloud-common`） | 每个项目 q4 存副本 + 全局 CLAUDE.md 兜底 |
| 共享机制 | 一处存、处处引用（promote + ref） | 多份副本，无引用关系 |
| 更新一致性 | 一处更新，处处生效 | 要改 N 个副本，漏改则不一致 |
| 盲区风险 | 无（共识统一） | 有（未踩坑的项目无副本），但被全局 CLAUDE.md 兜底 |

**差异说明**：现状的"一处存处处引用"在一致性上更优；Y 的"存多份"有一致性代价，但因项目切换低频 + 全局 CLAUDE.md 兜底，盲区被消解。这一维度现状略占优。

### 维度 5：扩展性（多领域、多项目共享）

| | 现状 | Y 方案 |
|---|---|---|
| 加新领域 | 新建 consensus 域，挤在 QMem 的 tier 里 | 新建领域 MCP，或往现有 MCP 加概念 |
| 多项目共享业务知识 | consensus 域（受 QMem 检索机制限制） | 一个领域 MCP 服务所有项目（真正横向共享） |
| 系统膨胀压力 | 共识域多了 QMem 复杂度线性增长 | 领域 MCP 独立，不影响 QMem |

**差异说明**：Y 的"一个领域服务多项目"是物理横向共享；现状的 consensus 域是受限于 QMem 检索机制的横向共享。Y 在扩展性上更灵活。

### 维度 6：初始成本

| | 现状 | Y 方案 |
|---|---|---|
| 改造工作量 | 零（现状） | 新建领域 MCP（复用 QMem 技术栈，骨架现成）+ QMem 瘦身 |
| 一次性 vs 持续 | — | 一次性成本 |

**差异说明**：现状零成本，Y 有一次性新建成本。这是现状唯一明显占优的维度。

---

## 四、对比汇总表

| 维度 | 现状（双 tier） | Y 方案（瘦身+独立图谱） |
|---|---|---|
| 系统复杂度 | 一个复杂系统 | 两个简单系统 |
| AI 语义理解获取 | 被动捎带 | 主动调用 |
| 检索纯净度 | 靠纪律保证 | 靠物理隔离保证 |
| 跨项目技术陷阱承载 | 一处存处处引用（一致性好） | 存多份+CLAUDE.md兜底（有冗余代价） |
| 扩展性 | consensus 域挤一起 | 物理隔离，真正共享 |
| 初始成本 | 零 | 新建一个 MCP |

---

## 五、作者倾向（明确标注：倾向，非定论）

> **这一节是作者个人判断，供评判者参考，但不应作为结论本身。请评判者独立得出自己的判断。**

作者倾向 **Y 方案**，理由：

1. **共识域的根本错误是位置错放**：把横向共享的业务知识塞进了纵向的项目记忆系统。Y 把它拆出来独立成系统，是纠正位置错放；现状是继续在错位上修补。

2. **"存多份"预案反向证明了共识域对技术陷阱也不是必需**：连技术陷阱这种最典型的"跨项目共识"都能用"项目副本 + 全局 CLAUDE.md 兜底"解决，说明 consensus 机制在你的使用模式下是过度工程。那么 consensus 存在的唯一理由只剩业务领域知识——而业务领域知识恰恰是最需要独立系统、最需要主动召回、最需要物理隔离的那一类。

3. **正交三维比线性三层清晰**：QMem（时间）/ 领域 MCP（认知）/ CBM（结构）各管一个正交维度，每个系统能一句话说清职责。现状的"q4 + consensus + CBM"是线性三层，业务知识和项目记忆混在一个系统里。

4. **"赋予 AI 能力"的本质是主动调用工具**：Y 的 `domain_lookup` 让 AI 在需求分析阶段主动获取语义纠偏；现状的混合检索是被动捎带，注意力分散。

**作者认可的反方观点**（诚实列出）：
- Y 的初始成本是真实的一次性工作量
- 维度 4（技术陷阱一致性）现状略占优，"存多份"有冗余和漏改风险
- 多一个系统就多一份维护责任
- 9 条种子数据建一个独立 MCP，初期可能显得"空"

这些代价作者认为可控（被 QMem 技术栈复用摊薄 + 业务知识低频写入维护成本低 + 领域知识贵精不贵多），但评判者可据此得出不同结论。

---

## 六、待评判者重点回应的问题

请评判者特别就以下几点给出意见：

1. **"两个简单系统 vs 一个复杂系统"**：在你的实际使用场景（内网、多项目但深耕单项目、AI 编码助手）下，哪种更合理？
2. **"存多份技术陷阱"预案**：一致性代价 vs 维护简洁性，能否接受？全局 CLAUDE.md 兜底是否足够？
3. **领域知识图谱是否必须独立成 MCP**：能否接受在 QMem 库内加一个 `tier=domain` 层（B' 变体，省去新建 MCP）？还是必须物理隔离才有意义？
4. **9 条种子数据建独立 MCP 是否过度**：领域知识贵精不贵多的论断是否成立？
5. **CBM 边界**：业务概念关联靠"content 文本内嵌 + RAG 召回"（伪图方案）是否够用，还是必须上真正的图数据库？

---

> 本文为架构说明文档，2026-07-17 生成，供第三方评判。系统现状以 `D:\cly-marketplace\qmem\mcp\` 源码与真实库实测为准。
