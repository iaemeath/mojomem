# 方案：description 优化轮次

> 状态：❌ 被否决（description 字段被错用为索引）
> 时间：2026-07-13
> 背景：V2.2 skill 文件方案的子问题——如何让 AI 找到并加载 q2-consensus skill

## 问题起点

V2.2 把共识写到 `~/.agents/skills/q2-consensus/SKILL.md`，但生成的 frontmatter description 是固定泛化文案：

```yaml
---
name: q2-consensus
description: 部分项目共识
---
```

ZCode skill 的自动触发依赖 description 做语义匹配。"部分项目共识"不含任何领域关键词，几乎不可能匹配到任何具体任务场景。

## 尝试的优化路径

### 轮次 1：固定文案优化

改为 `"Use when 新会话开场。跨项目共识知识库..."` + 触发场景枚举。

**问题**：description 不随共识内容变化，AI 不知道当前有哪些 project 的共识可用。

### 轮次 2：动态 description（列 project ID）

每次 promote 重建路由时，把当前所有 project 文件名拼进 description：

```yaml
description: "当前覆盖项目：_java-cloud-common、_weakpwd"
```

**问题**：`_java-cloud-common`、`_weakpwd` 这种内部 ID 扔给 AI，它根本不知道是什么——等于没说。

### 轮次 3：动态 description + label 参数

promote 时加 `label` 参数（人类可读标签），写入文件注释 `<!-- label:... -->`，重建路由时读出拼进 description：

```python
memory_promote(obs_id, label='弱口令改造6系统跨系统教训')
```

```yaml
description: "当前覆盖：SpringBoot+cloud-frame共性陷阱、弱口令改造6系统跨系统教训"
```

**问题**：label 是自由文本，会语义漂移。同一个主题可能被打成不同的标签。

## 核心缺陷：description 字段被错用

通过多轮迭代发现，所有优化都在解决一个**根本性的错用**：

description 的设计意图是**触发条件**（"什么时候该加载我"），不是**内容目录**（"我里面有什么"）：

| | 触发条件 | 内容目录 |
|---|---|---|
| 回答的问题 | 这个 skill 和当前任务相关吗？ | 这个 skill 里有哪些条目？ |
| 长度 | 一句话 | 随条目数线性增长 |
| 匹配方式 | 语义匹配（粗粒度） | 逐条比对（细粒度） |

把内容目录塞进触发条件里，10 个 project 时还能用，30 个时 description 变成一堵墙，信噪比下降，LLM 的触发判断反而变差。

## 学到的教训

1. **description 应该静态**：只管触发条件，不列内容
2. **标签是自由文本，不可控**：同一主题多种说法，LLM 模糊匹配不可靠
3. **HTML 注释存元数据是脆弱的**：`<!-- label:... -->` 不是标准机制，内容含 `-->` 就断

## 与正文索引方案的关系

这轮分析催生了 [05-router-index.md](05-router-index.md)（正文索引方案）——把内容目录从 description 移到正文。但正文索引方案本身也被否决了。
