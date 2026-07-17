# QMem promote 机制演进方案归档

> 本目录记录了 QMem MCP 在 2026-07-13 设计"跨项目共识"机制时，讨论和分析过的所有方案。
> 每个方案独立一个 .md 文件，记录核心设计、优缺点、为什么被采纳或否决。
> 这是我们经过多轮深度对比才最终决策的来时路。

## 方案索引

| 文件 | 方案 | 核心思路 | 最终状态 |
|---|---|---|---|
| [01-v2.0-is-global.md](01-v2.0-is-global.md) | V2.0 is_global 标记 | 同表共存 + RRF boost 加权 | ❌ 被否决（隔离不彻底） |
| [02-v2.1-bugfix.md](02-v2.1-bugfix.md) | V2.1 修 bug | V2.0 基础上修 _delete 描述/VACUUM | ✅ 采纳（过渡） |
| [03-v2.2-skill-file.md](03-v2.2-skill-file.md) | V2.2 skill 文件硬删 | 搬到 SKILL.md + 硬删 DB | ❌ 被否决（丢失检索能力） |
| [04-description-optimization.md](04-description-optimization.md) | description 优化轮次 | 动态 description / label 参数 | ❌ 被否决（description 被错用） |
| [05-router-index.md](05-router-index.md) | 正文索引方案 | 静态 description + 正文项目列表 | ❌ 被否决（LLM 做人眼匹配） |
| [06-dual-database.md](06-dual-database.md) | 双库方案 | Q2/Q4 物理分 .db 文件 | ❌ 被否决（搬行复杂、embedding 同步） |
| [07-single-table-tier.md](07-single-table-tier.md) | 单表 tier 字段方案 | 同表加 tier 列 + 检索过滤 | ⚠️ 备选（简单但非物理隔离） |
| [08-three-table-refs.md](08-three-table-refs.md) | ★ 三表引用方案（最终方向） | 项目表/共识表/关联表 多对多引用 | ★ 最终方向 |
| [09-open-source-comparison.md](09-open-source-comparison.md) | 开源工具横向对比 | Mem0/Letta/Zep 如何处理共识 | 参考 |
| [10-single-table-virtual-refs.md](10-single-table-virtual-refs.md) | ★ 单表+虚拟引用（最终 RFC） | 项目表/共识表/关联表 多对多引用 | ★ 最终 RFC |
| [11-v10-iteration-history.md](11-v10-iteration-history.md) | V10 迭代纪事 | 从初版到 RFC 的 9 次修复全过程 | 来时路 |

## 演进时间线

```
V2.0 (is_global 标记)
  │  问题：同表混存，boost 权重不可控
  ▼
V2.1 (修 bug)
  │  过渡版本
  ▼
V2.2 (skill 文件硬删)
  │  问题：搬到文件后检索能力完全丢失
  ▼
description 优化 / 正文索引
  │  尝试让 AI 找到 skill 文件
  │  问题：description 被错用，LLM 做人眼匹配
  ▼
双库方案
  │  尝试物理分库 + 保留检索
  │  问题：跨库搬行/embedding 同步/工具选择负担
  ▼
单表 tier 字段
  │  简化：只加标签不搬数据
  │  问题：非物理隔离，共识和动态记忆同表
  ▼
★ 三表引用方案（最终方向）
     项目表 + 共识表 + 多对多关联表
     一处改变，处处引用
```

## 核心决策逻辑

最终的决策基于对"共识"本质的理解：

> **共识不是"搬过来的副本"，而是"被引用的源头"。**
>
> 多个项目遇到同一个问题 → 提取到共识 → 项目通过引用关联 →
> 共识修改后所有引用方自动看到最新版（一处改变，处处引用）。
