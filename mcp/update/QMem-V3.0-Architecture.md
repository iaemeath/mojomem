# QMem V3.0 完整架构文档

> **版本**：V3.0（方案 10 RFC：单表全家桶 + 虚拟外键引用）
> **日期**：2026-07-14
> **位置**：`D:\cly-marketplace\qmem\mcp\python\mcp_server.py`（MCP server）、`D:\cly-marketplace\qmem\mcp\core_memory.db`（数据）
> **定位**：跨会话记忆唯一来源。替代了原 Engram + mem_search_vector + codebase-memory 三套系统。
>
> ⚠️ 本文为 V3.0 历史基线快照。文中后续出现的 `C:\QMem\` 是早期部署占位路径，**该目录已删除、资源已清空**，真实路径为 `D:\cly-marketplace\qmem\mcp\`。最新架构见 `QMem-V3.2-Architecture.md`，治理改动见 `QMem-V3.3-Architecture.md`。

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

## 二、整体架构

```
┌─────────────────────────────────────────────────────────┐
│  LLM 客户端 (ZCode / Cursor / Claude Desktop)           │
│  通过 JSON-RPC over stdin/stdout 调用 MCP 工具           │
└───────────────────────┬─────────────────────────────────┘
                        │
        ┌───────────────▼────────────────┐
        │  QMemMCP (mcp_server.py v3.0)  │  ← Python 主进程
        │  - 14 个本地工具               │
        │  - 转发 CBM 工具（代码查询）    │
        └───┬──────────┬────────────┬────┘
            │          │            │
   ┌────────▼──┐  ┌───▼────┐  ┌───▼────────────┐
   │BGEEmbedding│  │Hybrid  │  │CBMWrapper       │
   │(ONNX 本地) │  │Searcher│  │(subprocess 转发)│
   │512维向量   │  │(RRF)   │  │                │
   └────┬──────┘  └───┬────┘  └────┬───────────┘
        │             │            │ stdin/stdout
        │     ┌───────▼────────┐   │ JSON-RPC
        │     │SQLite + vec0   │   │
        │     │+ FTS5 虚表     │   │
        │     └────────────────┘   │
        └─────────────────────────►│
                          ┌────────▼─────────────────┐
                          │codebase-memory-mcp.exe   │
                          │(257MB 原生 C 二进制)      │
                          │- graph.db.zst 知识图谱   │
                          │- LSP 调用/数据流分析     │
                          └──────────────────────────┘
```

**三层分工**：

| 层 | 组件 | 职责 |
|---|---|---|
| 接入层 | `mcp_server.py`（QMemMCP） | JSON-RPC 协议处理、工具分发、本地工具实现 |
| 记忆层 | SQLite + sqlite-vec + FTS5 | 记忆事实存储、向量索引、全文索引 |
| 代码层 | `codebase-memory-mcp.exe`（CBM） | 函数/类/调用图等代码结构查询 |

---

## 三、数据库设计

### 3.1 核心表：`memory_facts`

所有记忆（动态 + 共识）在同一张表，通过字段区分层级和生命周期：

```sql
CREATE TABLE memory_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    obs_uuid TEXT UNIQUE NOT NULL,          -- 全局唯一 ID（12 位 hex）
    project TEXT NOT NULL DEFAULT '',       -- 项目名（动态记忆）或共识域名（consensus，普通名称，tier 字段区分）
    topic_key TEXT DEFAULT '',              -- 主题分类（arch/workflow/traps/m1.1 等，纯主题不混生命周期）
    title TEXT DEFAULT '',                  -- 标题
    content TEXT NOT NULL,                  -- 正文
    type TEXT DEFAULT 'manual',             -- ★ 必填。语义类型+生命周期：reference(稳定)/project(易过期)/decision/bugfix/learning/manual
    scope TEXT NOT NULL DEFAULT 'project',  -- 可见范围：project/personal
    tier TEXT NOT NULL DEFAULT 'q4',        -- ★ 层级：q4(动态草稿) / consensus(跨项目共识)
    origin_project TEXT DEFAULT '',         -- ★ promote 前的原始 project（脐带机制）
    content_hash TEXT DEFAULT '',           -- sha256(title+content)[:16]，变更检测
    pinned INTEGER NOT NULL DEFAULT 0,      -- 置顶（开场优先加载）
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    review_after TIMESTAMP,                 -- 易过期信息复审时间
    deleted_at TIMESTAMP                    -- 软删除（NULL=存活）
);
```

### 3.2 向量虚表：`memory_vectors`

```sql
CREATE VIRTUAL TABLE memory_vectors USING vec0(
    embedding float[512] distance_metric=cosine
);
```

- 用 `bge-small-zh-v1.5` ONNX 模型生成 512 维向量
- **rowid 与 memory_facts.id 对齐**，JOIN 即可拿到元数据
- vec0 不支持 UPDATE embedding，更新时需"删后重建"

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

记录"哪个项目引用了哪个共识域"。`mem_context` 通过此表自动加载项目引用的共识。

### 3.5 触发器与索引

3 个 FTS 同步触发器（insert/update/delete）+ 1 个向量清理触发器（delete）。8 个索引覆盖 project/topic_key/type/scope/tier/origin_project/deleted_at/created_at/pinned。project_refs 2 个索引（project/ref_project）。

---

## 四、核心概念

### 4.1 tier 字段 —— 层级隔离

| tier 值 | 含义 | 检索方式 |
|---|---|---|
| `q4` | 项目动态记忆（草稿） | `mem_recall` / `mem_search`（默认只搜 q4） |
| `consensus` | 跨项目共识 | `consensus_recall`（专搜）/ `mem_context`（通过 ref 自动加载） |

tier 是**逻辑隔离**——共识和动态记忆同表共存，靠 WHERE 条件区分。共识域名是普通名称（如 `java-cloud-common`），不需要特殊前缀——tier='consensus' 是权威标识。mem_save 的 upsert 有 `AND tier='q4'` 守卫，保证写入永远不碰 consensus 行。

### 4.2 project_refs 引用图谱 —— 一处改变，处处引用

```
bfo_zj_yxyd ──引用──┐
bfo_cndz     ──引用──┼──→ java-cloud-common（共识，只存一份）
dispatch-app ──引用──┘      └─ IS_DELETE 中文值
                           └─ CLOB 列长度陷阱
```

- 项目通过 `project_refs` 引用共识域
- `mem_context` 自动加载引用的共识
- 共识修改后所有引用方下次加载自动看到新内容——因为读的是同一份，没有副本

### 4.3 origin_project 脐带机制 —— 溯源黑洞防护

| 时机 | origin_project 值 | 含义 |
|---|---|---|
| promote 时 | 原始 project 名 | 脐带存在，demote 可精确回退 |
| AI 合并精炼时 | AI 传 `origin_project=''` | 脐带剪断，demote 拒绝降级 |
| demote 时 | 检查是否为空 | 空 = 已融合多源，拒绝降级（溯源黑洞防护） |

mem_update 改 consensus content 时**强制要求显式声明** origin_project 去留——防止 AI 忘记剪断脐带导致 demote 防护被绕过。

### 4.4 lifecycle 维度（取代旧 C+ 结构的 -kb/-status 后缀）

**旧 C+ 结构**用 topic_key 后缀（`-kb`/`-status`）混了两个职责：生命周期分类 + 主题分类。

**V3.0 改进**：

- **type 字段（★ 必填）**：决定生命周期。每次 `mem_save` 必须标对，mem_save 不传 type 会报错。

| type 值 | 生命周期 | 含义 | 审查策略 |
|---|---|---|---|
| `reference` | 稳定 | 项目骨架/数据模型/踩坑根因/决策理由 | 代码大改时才更新 |
| `project` | 易过期 | 当前进度/未推送/待修复/完成度 | 每次推进时 upsert，超 30 天需验证 |
| `decision` | 稳定 | 架构决策带理由 | 决策变更时更新 |
| `bugfix` | 稳定 | 已修复的 bug 根因 | 不需审查 |
| `learning` | 稳定 | 经验教训 | 不需审查 |
| `manual` | 稳定 | 手动记录 | 不需审查 |

- **topic_key（可选）**：纯主题分类。记忆少（≤2 条）时留空，记忆多（>3 条）时按主题命名（如 `arch`、`workflow`、`m1.4`、`d5000`）。不带生命周期后缀。

**upsert 锚点**：同 `project + scope + topic_key + tier='q4'` 命中则更新。topic_key 留空时按 `project + scope + 空 topic_key` upsert。

**易过期审查**：`WHERE type='project' AND created_at < datetime('now', '-30 days')` 查出需复审的进度类记忆。

---

## 五、14 个本地工具

| 工具 | 方向 | 用途 |
|---|---|---|
| `mem_save` | Push | 写动态记忆（tier=q4）。type 必填，topic_key 可选。upsert 加 tier='q4' 守卫 |
| `mem_recall` | Pull | 单次同源 RRF：搜项目动态记忆 + 引用的共识域（三步法配额截取） |
| `consensus_recall` | Pull | 专搜共识库（tier=consensus） |
| `mem_search` | Pull | 精确过滤查找（FTS5 MATCH，仅 tier=q4） |
| `mem_update` | — | 更新记忆。consensus 需 confirm_consensus=true；改 content 需声明 origin_project |
| `mem_context` | Pull | 开场召回：项目自身记忆 + 引用的共识（防爆 top-N） |
| `mem_delete` | — | 硬删除。consensus 需 confirm_consensus=true；自动清理空域 refs |
| `memory_promote` | — | 提取共识：UPDATE tier+project+origin_project + 建 ref |
| `memory_demote` | — | 降级回动态。origin_project 为空则拒绝（溯源黑洞防护）。不碰 project_refs |
| `consensus_health_check` | Pull | 找共识域内 embedding>0.85 的相似记录对，提示 AI 精炼 |
| `add_consensus_ref` | — | 手动建立项目→共识域引用（ref_source='manual'） |
| `list_consensus_projects` | Pull | 列出共识域（供 promote 选择目标） |
| `mem_list_projects` | Pull | 列出动态记忆的 project |
| `init_project_context` | — | 探测目录身份（git remote/pom/package.json） |

另有 CBM 转发工具（search_graph/trace_path/get_architecture 等），通过 QMem 自动转发到 codebase-memory-mcp。

---

## 六、核心算法

### 6.1 RRF 混合检索（`search_rrf.py`）

**单次查询同源 RRF**：q4 和 consensus 在同一个候选池里做 BM25 + cosine → RRF 融合。rank 空间统一，分数天然可比。

```
score = 1/(k + rank_fts + 1) + 1/(k + rank_vec + 1)    # k=60
```

**⚠️ 绝对不能拆成两次查询**：拆开后两个独立 rank 空间的 RRF 分数不可比——q4 池的 rank-0 和 consensus 池的 rank-0 拿到相同分数，合并后交错，融合失效。

### 6.2 三步法配额截取（mem_recall 内部）

防止共识库数据量增长后淹没项目动态记忆：

```
第 1 步：按 tier 分组（q4_items / cons_items）
第 2 步：各自取保底配额（q4 保 2，consensus 保 2）——即使草稿排在第 49 名也必定上桌
第 3 步：剩余候选混合按 RRF 分数竞争空位
最终：统一按 RRF 分数降序返回
```

**⚠️ 不能用顺序遍历即时填充**：高分 consensus 会提前填满坑位，q4 保底失效。

### 6.3 嵌入模型（`embedding.py`）

- 模型：`bge-small-zh-v1.5`，ONNX uint8 量化版，本地 CPU 推理
- 维度：512
- 流程：tokenizer 截断 510 token → ONNX 推理 → mean pooling → L2 归一化

### 6.4 CBM 转发（`cbm_wrapper.py`）

subprocess 管道通信，崩溃自动恢复。`tools/list` 时合并本地 14 工具 + CBM 的全部工具，对客户端透明。

---

## 七、共识机制（方案 10 RFC 核心）

### 7.1 promote：提取共识

```python
memory_promote(obs_id="<obs_id>", consensus_domain="java-cloud-common")
```

一行 UPDATE：`SET tier='consensus', project='java-cloud-common', origin_project='<原项目>'`。不搬数据不重算向量。自动 INSERT project_refs (原项目, 共识域, 'promote')。

**不做自动合并**：多条同主题共识各自独立存在。AI 发现冗余时：
1. `consensus_health_check` 检测 embedding>0.85 的相似对
2. `mem_update` 写精炼新版本（传 `origin_project=''` 剪断脐带，`confirm_consensus=true`）
3. `mem_delete` 删除旧版本（传 `confirm_consensus=true`）

### 7.2 越权确认

mem_update / mem_delete 操作 tier='consensus' 的行时：
- 不带 `confirm_consensus=true` → 拦截，返回影响范围（被 N 个项目引用）
- 带 `confirm_consensus=true` → 放行
- 改 consensus content 但未声明 `origin_project` → 拦截，要求显式声明（剪断或保留）

### 7.3 demote：降级回动态

```python
memory_demote(obs_id="<obs_id>")
```

- origin_project 为空 → **拒绝降级**（溯源黑洞防护）
- origin_project 有值 → `SET tier='q4', project=origin_project, origin_project=''`
- **不清理 project_refs**（防过桥抽板——demote 只是退回一条记忆，不该切断项目与共识域的引用关系）

### 7.4 project_refs 生命周期

只保留两条**绝对安全**的清理路径：
1. **mem_delete 导致域清空**：共识域的记忆全删了 → 清理指向该域的 promote refs（不删 manual refs）
2. **项目动态记忆全删**：项目的 q4 记忆全删了 → 清理该项目的出向 promote refs

demote 绝不清理 project_refs。

---

## 八、防呆与安全约束（11 条设计约束）

这些约束每一条都来自迭代过程中发现的缺陷（详见 `C:\QMem\update\11-v10-iteration-history.md`）：

1. **promote 不做自动合并** —— 避免字符串拼接不可逆 + 信息臃肿
2. **origin_project 脐带机制** —— promote 时写入，合并精炼时剪断，demote 检测为空则拒绝。不用 updated_at 判断（promote 本身会刷新 updated_at 导致全员封杀）
3. **project_refs 两条安全清理路径** —— 只在域清空/项目消失时清理，demote 绝不清理（防过桥抽板）
4. **ref_source 区分引用来源** —— 清理只删 ref_source='promote'，绝不删 manual
5. **mem_save upsert 加 tier='q4' 守卫** —— 保证写入永远不碰 consensus 行
6. **confirm_consensus 越权确认** —— 不一刀切禁止，而是要求显式确认
7. **单次查询同源 RRF + 三步法配额** —— 绝不拆两次查询（RRF 跨池不可比）；不能用顺序遍历即时填充（保底失效）
8. **consensus_health_check 工具** —— 不做自动合并但提供触发机制
9. **脐带剪断强制检查** —— mem_update 改 consensus content 时强制要求声明 origin_project 去留
10. **防爆机制** —— mem_context 用 LIMIT consensus_limit（默认 5）取 top-N
11. **list_consensus_projects 工具** —— AI promote 前查看现有共识域

---

## 九、共识域导航

| 共识域 | 类型 | 共识范围 | 应建立引用的项目 |
|---|---|---|---|
| `weakpwd` | 任务级 | 弱口令改造 6 系统总览+跨系统教训+完成进度 | bfo_cndz / bfo_tz_dispatch_report / binfo-tz-message-manage / taizhou-digital-platform / dispatch-app-zj / meeting_jj / meeting_tz / front-end-old-metting |
| `vue2-common` | 技术栈级 | Vue2+ElementUI+webpack 共性陷阱（待沉淀） | dispatch-all-new / zj-sjhgk / front-end-old-metting / meeting_jj 前端 |
| `java-cloud-common` | 技术栈级 | SpringBoot+cloud-frame+MyBatis 共性（IS_DELETE中文值/CLOB/@Transactional） | bfo_zj_yxyd / dispatch-event-zj / dispatch-app-zj / changzhou-balance-plan / 3 父工程 |
| `dameng-common` | 技术栈级 | 达梦 SQL/disql/DM6DM7 驱动差异（待沉淀） | 所有 Java 后端项目 |

---

## 十、已迁移的 project

| project | 记忆数 | project | 记忆数 |
|---|---|---|---|
| bfo_zj_yxyd | 2 (reference+project) | bfo_cndz | 2 (reference+project) |
| dispatch-event-zj | 2 (reference+project) | bfo_tz_dispatch_report | 2 (reference+project) |
| dispatch-app-zj | 1 (reference) | binfo-tz-message-manage | 2 (reference+project) |
| dispatch-all-new | 1 (reference) | taizhou-digital-platform | 2 (reference+project) |
| zj-sjhgk | 1 (reference) | changzhou-balance-plan | 8 (reference+project+bugfix) |
| schedule-shifts | 1 (reference) | cloud-frame-parent | 1 (reference 实体锚点) |
| cloud-balance-parent | 1 (reference 实体锚点) | cloud-msg-parent | 1 (reference 实体锚点) |
| memory-hygiene | 4 (decision+project, scope=personal) | weakpwd | 1 (consensus) |
| java-cloud-common | 1 (consensus) | | |

共计 33 条记忆，17 个 project（15 个 q4 + 2 个 consensus）。

---

## 十一、嵌入模型

- **模型**：`bge-small-zh-v1.5`，ONNX uint8 量化版
- **位置**：`C:\QMem\bge-small-zh-v1.5-onnx\`
- **维度**：512
- **推理**：本地 CPU（onnxruntime），无外部 API 依赖
- **流程**：tokenizer 截断 510 token → ONNX 推理 → mean pooling（带 attention_mask）→ L2 归一化
- **降级**：onnxruntime/tokenizers 未装时返回零向量（保证不崩）

---

## 十二、运行环境

| 项目 | 值 |
|---|---|
| Python | `C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe`（3.13） |
| 数据库 | `C:\QMem\core_memory.db`（SQLite 3.50.4 + sqlite-vec + FTS5） |
| CBM 二进制 | `C:\QMem\codebase-memory-mcp.exe`（257MB，原生 C 编译） |
| 启动脚本 | `C:\QMem\python\start_python_mcp.bat` |
| 重启脚本 | `C:\QMem\restart_mcp.ps1` |
| 依赖 | sqlite-vec, onnxruntime, numpy, tokenizers, huggingface-hub（见 `requirements.txt`） |

---

## 十三、文件清单

```
C:\QMem\
├── core_memory.db                  ← SQLite 数据库（33 条记忆，2 条 consensus + 31 条 q4）
├── codebase-memory-mcp.exe         ← CBM 原生二进制（代码查询）
├── bge-small-zh-v1.5-onnx/         ← 嵌入模型（ONNX + tokenizer）
│   ├── config.json
│   ├── tokenizer.json
│   └── onnx/
│       ├── model.onnx
│       └── model.onnx_data
├── python/                         ← QMem MCP server 源码
│   ├── mcp_server.py               ← 主服务（v3.0，14 个工具）
│   ├── search_rrf.py               ← RRF 混合检索（支持 projects/tiers 复数）
│   ├── embedding.py                ← BGE ONNX 嵌入
│   ├── cbm_wrapper.py              ← CBM 子进程转发
│   ├── init_project_context.py     ← 目录身份探测
│   ├── qmem_cli.py                 ← CLI 入口
│   ├── schema.sql                  ← v3 DDL（memory_facts + memory_vectors + memory_facts_fts + project_refs）
│   ├── requirements.txt            ← 依赖清单
│   └── start_python_mcp.bat        ← Windows 启动脚本
├── check_db.py                     ← DB 状态检查脚本
├── restart_mcp.ps1                 ← MCP 重启脚本（杀进程）
├── install.ps1                     ← CBM 安装脚本
├── mcp_config_example.json         ← MCP 客户端配置示例
├── WINDOWS_SETUP_GUIDE.md          ← Windows 配置指南
└── update/                         ← 方案演进文档归档（来时路）
    ├── README.md                   ← 索引 + 演进时间线
    ├── 01~09-*.md                  ← V2.0~V2.2/双库/单表tier/三表/开源对比
    ├── 10-single-table-virtual-refs.md  ← ★ 方案 10 RFC 规范
    └── 11-v10-iteration-history.md ← 9 次迭代修复纪事
```

---

## 十四、Skill 与 AGENTS.md 分工

| 层 | 文件 | 承载内容 | 加载方式 |
|---|---|---|---|
| **硬约束** | `~/.zcode/AGENTS.md` + `D:\code\AGENTS.md` | 一句话："开局必须加载 qmem-memory skill 并执行 mem_context" | 每次会话自动注入 |
| **完整手册** | `~/.agents/skills/qmem-memory/SKILL.md` | 工具速查 + 卫生规则 + 共识管理 + 共识域导航 + 已迁移 project | AGENTS.md 强制开局加载 |
| **元规则** | QMem `memory-hygiene` project（scope=personal） | 记忆卫生规则正文（规则 1-12） | mem_context / mem_recall 按需召回 |

---

## 十五、与开源工具的对比

| 维度 | QMem V3.0 | Mem0 | Letta (MemGPT) | Zep |
|---|---|---|---|---|
| 存储 | SQLite + vec0 + FTS5 | 向量 DB + graph | 分层 memory | 时序知识图谱 |
| 嵌入 | 本地 BGE ONNX (512d) | API 依赖 | API 依赖 | API 依赖 |
| 共识机制 | tier + project_refs 引用图谱 | metadata 标签 | 不区分 | 节点类型标签 |
| 引用关系 | ★ 显式多对多 project_refs 表 | 无 | 无 | 图边 |
| 检索 | RRF（FTS5+向量融合） | 纯向量 | 向量 | 图查询+向量 |
| 防呆 | confirm_consensus + 脐带剪断 | 无 | 无 | 无 |
| 离线 | ✅ 全本地 | ❌ | ❌ | ❌ |

QMem 的独特之处：**引用关系显式化**（project_refs 表）+ **离线全本地**（ONNX 嵌入 + SQLite）+ **工程级防呆**（11 条设计约束）。

---

> 本文档是 QMem V3.0 的完整架构说明。方案演进过程详见 `C:\QMem\update\` 目录。RFC 规范详见 `10-single-table-virtual-refs.md`。迭代修复纪事详见 `11-v10-iteration-history.md`。
