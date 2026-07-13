# QMem 架构差距分析 (Gaps Analysis)

本文档记录了当前 QMem 的 Python 落地版（Python Fallback / v2）与原版《PRQ-Four Quadrants 原生规范》之间的 4 大核心架构差距。这是后续进行 Mojo 重构或架构深化的核心待办事项。

## 🔴 差距一：Q2 物理隔离被破坏，发生介质混存
- **原版规范**：绝对禁止 Q2（第二象限：全局公共资产）和 Q4（第四象限：项目动态踩坑本）发生存储介质交叉。Q2 必须是存放在独立 `D:\code\.assets\*.md` 目录下的纯文本 Markdown 文件（Drawer-style 抽屉式），以防止不同技术栈在模糊的向量空间中发生记忆串流。
- **当前现状**：目前的 Python 落地版没有去读取物理的 `.assets` 目录。我们通过 `memory_promote(obs_id)` 接口，给特定的 Q4 记录打上 `is_global=1` 的标签，直接让其在 SQLite（Q4存储介质）的向量空间里提权。这实际上**把 Q2 揉进了 Q4 的向量数据库里**，违背了物理切割介质的红线。
- **优化方向**：`mem_recall` 接口需要新增分支逻辑。除了查询 Q4 数据库，当 query 命中 `.assets` 目录下的某物理文件名时，必须将该 MD 文件的纯文本内容无损读取并拼接到召回结果中。

## 🟢 差距二（已解决）：`mem_delete` 的物理清理与异步空间回收
- **原版规范**：彻底抛弃只增不减（ADD-only），旧决策必须执行硬抹除（Hard Erase），底层触发器同步清理向量空间，并立即执行 `VACUUM` 整理磁盘碎片，保持单体数据库的极致轻量。
- **当前现状（v2.1）**：已完全修复。无论是 Python 还是 Mojo 版，`mem_delete` 接口现已执行真实的 `DELETE FROM memory_facts`，底层触发器（`trg_vector_delete` 和 `trg_fts_delete`）瞬间生效，立即无感抹除向量和全文检索索引，彻底解决向量污染问题。为避免高频删除导致 I/O 阻塞，`VACUUM` 碎片回收动作已从删除接口中剥离，被优化为在 MCP Server 启动生命周期（`_init` / `__init__`）中异步执行。

## 🟡 差距三：第三象限冷启动向导（`/init`）缺位
- **原版规范**：在克隆新仓库后，人类执行 `/init` 指令，AI 会在全局红线潜意识下扫描项目，在当前根目录生成 `CLAUDE.md`（Q3 户口本），并自动在文件末尾追加对第二象限的物理指针（例如：`Ref: ../.assets/vue2-common.md`）。
- **当前现状**：目前通过 `init_project_context.py` 实现的仅是一个只读探针，系统尚未向大模型暴露显式的 `/init` 指令流来强制落盘 Q3。

## 🟡 差距四：提权标签实现的语义差异
- **原版规范**：提权微操通过 `UPDATE memory_facts SET project = 'GLOBAL'` 实现，使其脱离原有私有项目归属，辐射到主账本。
- **当前现状**：Python 版本妥协使用了新增列 `is_global = 1` 来实现。两者在 RRF 排序提权效果上等效，但原方案在实体关系切分上更为纯粹（主键只有 `project` 和 `uuid`）。
