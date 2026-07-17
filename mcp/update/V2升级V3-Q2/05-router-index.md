# 方案：正文索引（静态 description + 正文项目列表）

> 状态：❌ 被否决（让 LLM 做检索引擎该做的事）
> 时间：2026-07-13
> 背景：description 优化轮次失败后，尝试分离"触发"和"检索"

## 核心设计

把 V2.2 的问题拆成两层：

- **description**：静态触发条件，一句话，不随共识数量增长
- **正文**：项目列表 + Read 指令，AI 加载 skill 后读正文找到对应文件

```markdown
---
name: q2-consensus
description: "Use when 需要查阅跨项目共识、踩坑经验、架构决策。"
---

# Q2 跨项目共识路由

按当前 project 读取对应文件：

$SkillRoot = "...\q2-consensus"

# 弱口令改造6系统跨系统教训
IS_DELETE 中文值、跨系统导出陷阱、完成进度
Read "$SkillRoot\_weakpwd.md"

# SpringBoot+cloud-frame 共性陷阱
CLOB 列长度、@Transactional 失效、autocreatetable
Read "$SkillRoot\_java-cloud-common.md"
```

### 设计灵感

来自已有的 `intranet-dev` 和 `cly-workflows` skill——它们都验证过这个模式：
- description 是静态的 `"Use when 涉及任何内网开发基础设施操作"`
- 正文里列了 8 个子 skill 各自的描述

## 优点

- **description 保持静态**：不膨胀，触发判断稳定
- **内容目录在正文**：AI 加载后可以逐条比对 project 名/关键词
- **与现有 skill 模式一致**：有成功先例

## 缺点

### 1. ★ 本质是让 LLM 做检索引擎该做的事（致命）

AI 读正文逐条匹配 project 名/label，这本质上是**人眼扫描**。30 条以内勉强，上百条时：
- context 膨胀（正文越来越长）
- 匹配不可靠（LLM 注意力分散，可能漏看）

### 2. 关键词摘要需要维护

正文里每个 project 条目除了文件名，还带"关键词摘要"（从该文件所有 `## 标题` 提取）。这些摘要需要 promote 时自动生成，额外复杂度。

### 3. 没有语义检索能力

AI 只能靠 project 名精确匹配或关键词模糊匹配。如果用户问"达梦数据库有什么坑"，但共识标题是"DM7 schema 隔离"，关键词不重叠就找不到。

## 与 QMem 自身能力的矛盾

> 讽刺的事实——QMem 本身就有 RRF 混合检索。

正文索引方案把共识从 DB 搬到文件（物理隔离，这是 V2.2 的方向），但搬运后却丢了检索能力，退化成"AI 读列表人眼匹配"。这比 QMem 原生的 RRF 检索差了一个量级。

```
共识在 DB → embedding + FTS5 → RRF 向量+词法融合检索（强）
共识在文件 → 正文关键词列表 → LLM 人眼扫描（弱）
```

## 为什么被否决

正文索引是 V2.2 skill 文件方案的"修补"——试图让 AI 能找到文件里的共识。但根因是 V2.2 把共识搬离了 DB 导致检索丢失。修补不如解决根因：**共识应该留在 DB 里，保留检索能力**。

这直接催生了后续的"双库方案"和"单表 tier 字段方案"——两者都试图在保留检索的同时实现隔离。
