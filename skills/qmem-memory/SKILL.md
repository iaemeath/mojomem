---
name: qmem-memory
description: "Use when 需要保存/检索跨会话记忆、使用 QMem 工具（mem_save/mem_recall/consensus_recall/memory_promote 等）、查阅项目踩坑根因/决策理由/任务进度、新会话开场召回。QMem v3.0 跨会话记忆唯一来源（单表+tier+project_refs 引用图谱）。"
version: 3.1
---

# QMem 跨会话记忆系统

> QMem v3.1：MCP server 由 qmem 插件自动注册（plugin mcpServers）
> 动态记忆(tier=q4)和共识(tier=consensus)同表共存，promote 只改 tier+project（一行 UPDATE），不搬数据不重算向量。
> 项目通过 project_refs 引用共识域，mem_context 自动加载引用的共识。

## 开场召回（必做）

```
mem_context(project="<文件夹名>")
```

v3.1 两阶段加载：mem_context 返回**摘要索引**（title + content 前100字 + obs_id），AI 全局扫描后用 `mem_get_full(obs_id=)` 拉相关记忆的完整全文。不再有 top-N 截断——全部记忆的摘要都可见。

## 增量验收闭环（整批改完后必做）

「改完一整批代码 → 比对原始需求文档 vs 代码实现」是核心动作。关键：**不要每个文件都重建图谱**，等一个方案/计划执行完整批改完后跑一次。

```
方案执行完成（整批改完）
  → index_repository(repo_path="<项目绝对路径>", mode="fast")  # 真增量重建图谱
  → detect_changes(project="<文件夹名>")                        # 看本次改动影响范围（只读，不更新图谱）
  → qmem:acceptance-check 对照 docs/需求文档出验收报告
```

- **为什么 per-batch 而非 per-write**：index_repository 即便 fast 也要对变更文件跑 LSP 解析（单文件几秒），per-write 会拖垮编码节奏；per-batch 只在方案完成时跑一次
- **mode="fast"**：最轻量重建（只索引过滤后文件、跳过相似度/语义边）
- **detect_changes 不更新图谱**：它只读现有图谱算影响范围，所以必须先 index_repository 让图谱跟上
- **配套意图**：与全局 CLAUDE.md 强制的「开场 mem_context」配对——开场拉历史记忆，收尾增量重建 + 影响分析 + 验收，两端共用同一套图谱
- **前置条件**：项目必须是 git 仓库
- 示例：`changzhou-balance-plan` 一轮改完 → `index_repository(repo_path="D:\\code\\changzhou-balance-plan", mode="fast")` → `detect_changes(project="changzhou-balance-plan")` → acceptance-check 对照 `docs/需求分析-x.y-*.md`

## 工具速查

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `mem_context(project=)` | **开场召回**：返回摘要索引（全量 q4 + 引用的共识），不再返回全文 | project, consensus_limit=5 |
| `mem_get_full(obs_id=)` | **按需拉全文**：从摘要索引拿到 obs_id 后拉完整 content。支持批量（逗号分隔，上限20） | obs_id |
| `mem_recall(query=)` | **RRF 混合检索**：搜项目动态记忆 + 引用的共识域（单次同源 RRF + 三步法配额） | query, current_project, min_similarity=0.5, limit=10 |
| `consensus_recall(query=)` | **专搜共识库**：查阅通用经验/架构陷阱/踩坑根因 | query, min_similarity, limit |
| `mem_search(query=)` | **精确/过滤查找**：FTS5 MATCH + project/type 过滤（仅 tier=q4） | query, project, type, limit |
| `mem_save(project_id=, content=)` | **写入动态记忆**：topic_key 命中自动 upsert（仅 tier=q4 范围） | project_id, content, title, type, topic_key |
| `mem_update(obs_id=)` | 更新记忆。改 consensus 需 `confirm_consensus=true`；改 consensus content 需声明 `origin_project` 去留 | obs_id, content, title, type, confirm_consensus, origin_project |
| `mem_delete(obs_id=)` | 硬删除。删 consensus 需 `confirm_consensus=true`；自动清理空域 refs | obs_id, confirm_consensus |
| `memory_promote(obs_id=, consensus_domain=)` | **提取为共识**：UPDATE tier+project+origin_project + 建 ref。不挪数据 | obs_id, consensus_domain（必填，如 `java-cloud-common`） |
| `memory_demote(obs_id=)` | 降级回动态。origin_project 为空（已融合多源）则拒绝降级 | obs_id |
| `consensus_health_check()` | 检查共识域内部是否有高度相似记录（embedding>0.85），提示精炼 | consensus_domain（可选） |
| `cross_project_health_check()` | **检测跨项目 q4 记忆的语义重复**（embedding>阈值），发现可 promote 的候选 | threshold=0.85, limit=20 |
| `add_consensus_ref(project=, consensus_project=)` | 手动建立项目→共识域引用 | project, consensus_project |
| `list_consensus_projects()` | 列出共识域（供 promote 选择目标） | — |
| `mem_list_projects()` | 列出动态记忆的 project 及记忆数 | — |
| `init_project_context(directory=)` | 探测目录身份（git remote/pom/package.json） | directory |
| **CBM 转发工具** | 代码查询（search_graph/trace_path/get_architecture 等） | 通过 QMem 自动转发到 codebase-memory-mcp |

## 检索技巧

- **中文查询**（保供/达梦/弱口令/断面）：用 `mem_recall`（向量路覆盖中文双字词）
- **英文标识符**（ResponseMsg/IS_DELETE/FeignClient）：用 `mem_recall` 或 `mem_search`（FTS5 精确匹配）
- **查通用经验/陷阱**：用 `consensus_recall`（专搜共识库）
- **阈值策略**：`min_similarity` 默认 0.5；结果为空降到 0.4；噪声多升到 0.6

## 记忆生命周期与主题分类

### type 字段（★ 必填，决定生命周期）

每次 `mem_save` **必须标对 type**，它决定记忆的生命周期和审查策略：

| type 值 | 生命周期 | 含义 | 审查策略 |
|---|---|---|---|
| `reference` | 稳定 | 项目骨架/数据模型/踩坑根因/决策理由 | 代码大改时才更新 |
| `progress` | ★ 易过期 | 当前进度/未推送/待修复/完成度 | 每次推进时 upsert，超 30 天需验证 |
| `decision` | 稳定 | 架构决策带理由 | 决策变更时更新 |
| `bugfix` | 稳定 | 已修复的 bug 根因 | 不需审查 |
| `learning` | 稳定 | 经验教训 | 不需审查 |
| `manual` | 稳定 | 手动记录 | 不需审查 |

### topic_key 字段（可选，仅记忆多时用）

topic_key 是**可选的**主题分类。规则：
- 一个 project 只有 1-2 条记忆时 → **topic_key 留空**（project + type 已足够 upsert）
- 一个 project 记忆多（>3 条）时 → 用 topic_key 按主题/里程碑区分（如 `arch`、`workflow`、`m1.4`、`d5000`）
- topic_key 不带生命周期后缀（旧 `-kb`/`-status` 已废弃），纯按主题命名
- 单项目 topic_key 总数 ≤ 5

### upsert 锚点

`同 project + topic_key + tier='q4'` 命中则更新。topic_key 留空时按 `project + 空 topic_key` upsert。

### 易过期审查

```sql
WHERE type='progress' AND created_at < datetime('now', '-30 days')
```

查出需复审的进度类记忆，引用前用 git/代码验证。

### 卫生规则

1. **type 必须标对** — 稳定知识用 `reference`/`decision`/`bugfix`，易过期进度用 `progress`
2. **topic_key 可选** — 记忆少时留空，多时按主题命名
3. **写可复用结论，不写流水账** — 一条记忆一个主题，合并同主题碎片
4. **易过期信息标时间锚点** — 进度类 title 带日期，超 30 天引用前用 git/代码验证
5. **代码事实不进 QMem 记忆表** — 函数签名/调用关系/表字段用 CBM 转发工具查（search_graph/trace_path/get_code_snippet），记忆表只存代码里读不出的

## 共识管理

### promote（提取共识）

```
memory_promote(obs_id="<obs_id>", consensus_domain="java-cloud-common")
```

- UPDATE tier='consensus' + project=共识域名 + origin_project=来源 + 建 ref
- 共识域名就是普通名称（如 `java-cloud-common`、`weakpwd`），不需要 `_` 前缀——tier='consensus' 是权威标识
- 不做自动合并——多条同主题共识各自独立存在
- AI 发现冗余时主动精炼：consensus_health_check 检测 → mem_update 写新版本（传 origin_project='' 剪断脐带）→ mem_delete 旧版本
- 跨项目重复发现：用 cross_project_health_check 检测不同 project 间的高相似 q4 记忆，人工判断是真重复还是同构模板后再 promote

### 越权确认

- mem_update/mem_delete 操作 consensus 行需 `confirm_consensus=true`
- 改 consensus content 时必须声明 `origin_project` 去留（传 '' 剪断或传原值保留）

### demote（降级回动态）

```
memory_demote(obs_id="<obs_id>")
```

- origin_project 为空（已融合多源）则拒绝降级——溯源黑洞防护
- demote 不清理 project_refs（防过桥抽板）

## 共识域导航

| 共识域 | 类型 | 共识范围 | 应建立引用的项目 |
|---|---|---|---|
| `weakpwd` | 任务级 | 弱口令改造 6 系统总览+跨系统教训+完成进度 | bfo_cndz / bfo_tz_dispatch_report / binfo-tz-message-manage / taizhou-digital-platform / dispatch-app-zj / meeting_jj / meeting_tz / front-end-old-metting |
| `vue2-common` | 技术栈级 | Vue2+ElementUI+webpack 共性陷阱（待沉淀） | dispatch-all-new / zj-sjhgk / front-end-old-metting / meeting_jj 前端 |
| `java-cloud-common` | 技术栈级 | SpringBoot+cloud-frame+MyBatis 共性（IS_DELETE中文值/CLOB/@Transactional） | bfo_zj_yxyd / dispatch-event-zj / dispatch-app-zj / changzhou-balance-plan / 3 父工程 / bfo_cndz / bfo_tz_dispatch_report / binfo-tz-message-manage |
| `dameng-common` | 技术栈级 | 达梦 SQL/disql/DM6DM7 驱动差异（待沉淀） | 所有 Java 后端项目 |
| `power-grid-domain` | 领域级 | 电网调度领域知识（发电计划/D5000/负荷电量/新能源/重载倒送/检修事故） | changzhou-balance-plan / dispatch-app-zj / dispatch-event-zj |

**建立引用**：`add_consensus_ref(project='<真实项目>', consensus_project='<共识域>')`，建立后 mem_context 自动加载。
**内容沉淀原则**：共识域只存跨 ≥2 项目验证过的硬共识，踩坑时发现跨项目用 memory_promote 提取；单项目特有留本项目 tier=q4。

### 领域知识共识域存储规范

**适用场景**：需要将业务领域知识（非技术陷阱，而是业务概念/数据结构/业务规则）存入共识域供多个项目共享。

**存储原则**：

1. **按主题分条**：每个核心概念一条记忆，不要一条塞所有内容。例如"发电计划""负荷电量""新能源""D5000表结构"各一条，而不是全部塞在一条里。这样 RAG 召回精度更高——搜"发电计划"只命中发电计划那条，不会把检修事故也拉进来。

2. **topic_key 规范**：用纯主题标识（如 `concept-generation-plan`、`concept-d5000-ems-tables`），不带项目前缀。共识域知识脱离项目归属。

3. **type 用 reference**：领域知识是稳定的参考资料。

4. **内容结构**：按"概念定义→业务含义→对开发的影响"组织。重点写影响开发决策的结论，不要面面俱到。

5. **project_id 填共识域名**：存入时 `project_id="power-grid-domain"`（共识域名），不是某个具体项目。

6. **拆分粒度参考**：单条记忆控制在 500-800 字。如果一个主题超过 1000 字，考虑拆成两条（如"D5000表结构"和"D5000采样算法"分开）。

**与技术陷阱共识域的区别**：

| | 技术陷阱共识域（如 java-cloud-common） | 领域知识共识域（如 power-grid-domain） |
|---|---|---|
| 内容 | IS_DELETE中文值、CLOB陷阱等踩坑经验 | 发电计划、负荷、新能源等业务概念 |
| 来源 | 从 bugfix/踩坑中提取 | 从业务文档/用户口述中整理 |
| topic_key | 技术主题（如 `java-cloud-common`） | 概念主题（如 `concept-generation-plan`） |
| 验证方式 | 跨项目复现验证 | 领域专家（用户）确认 |
| 更新频率 | 低（陷阱不会变） | 中（业务规则可能调整） |

## 扩张管理

- 每项目固定 2 条线性增长，30 项目内可控
- project 数到 30+ 时考虑分层命名（zj-/tz-/cz- 前缀）
- 同类共性用 memory_promote 提取到共识域（普通名称，无前缀）

## 克隆项目身份红线

同构/克隆项目（meeting_jj↔meeting_tz、front-end-old-metting 13 城、guaranteedSupplyControlPlatform 多城）的 CLAUDE.md 必须有「⚠️ 地区身份红线」段：绝对身份+血缘关系+共享资源警告（弱口令验证禁止写非原密码）+文案地区名。
