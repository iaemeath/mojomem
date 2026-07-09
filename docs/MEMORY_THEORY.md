# 📚 AI 长期记忆机制全景透视与 Mojomem 架构映射

由 Mojomem (PRO v2) 开发者与 AI 协同整理，系统化探究大模型长期记忆的存储、写入与召回本质。

---

## 🏗️ 第一部分：记忆存储（Memory Storage）—— 空间拓扑与分层

大模型的原生上下文（Context Window）属于“瞬时内存（RAM）”，随会话结束而清空。构建长期记忆的核心，在于将外部持久化介质与模型的认知空间进行层次化映射。

### 1. 层次化金字塔存储（Hierarchical Storage）
* **主流技术思想**：模拟计算机系统的 L1/L2/L3/Disk 缓存结构。根据信息的时效性、高频度、全局性对存储介质进行动态分层，越高的层级空间越小、离模型越近。
* **Mojomem 架构映射**：
  * **L1/L2（超高速缓存/动态内存）** → **Q1: Working Memory**：捕获当前会话的最核心上下文。
  * **L3（高频闪存/规约）** → **Q2: Consensus Memory & CLAUDE.md**：常驻或高频推入的全局开发规范与置顶共识。
  * **Disk（冷数据硬盘）** → **Q3 & Q4 数据库**：底层基于 SQLite3，存储结构上下文与海量的情境流水账。

### 2. 知识图谱化结构存储（Graph-based Storage）
* **主流技术思想**：人类记忆以网状概念（Concepts）和实体（Entities）相互关联，而非孤立的文本切片。使用图数据库或关联三元组（Subject-Predicate-Object）来表达逻辑链路（例如：“组件A —[重构]→ Mojo —[解决]→ GIL锁延迟”）。
* **Mojomem 演进方向**：可在 Q3: Structural Context 静态分析（如 `ast_parser.py`）中，提取类、函数、文件的依赖树，将其转化为结构化的实体关系关联。

---

## 💾 第二部分：记忆写入与固化（Memory Write & Consolidation）

如何防止外部数据库沦为“数据垃圾场”？高效的长期记忆系统必须具备提炼、压缩、更新与遗忘的能力。

### 1. 异步反射与记忆固化（Asynchronous Reflection）
* **主流技术思想**：模拟人类的睡眠记忆固化机制。系统在“清醒”（用户处于交互状态）时不做高延迟的深度处理，仅做事件日志（Event Log）的 Append-Only 写入；在系统“静默”（交互间歇、Git Commit 时）启动轻量模型进行后台异步反思，将零碎、冗长的交互历史提炼为高浓度的知识条目（Observations）。
* **Mojomem 架构映射**：
  * **推送模式 (Push)**：LLM 发现重要决策或 Bug 修复时，通过 `mem_save` 或 `mem_update` 触发 Upsert 写入。
  * **未来优化**：利用 Mojo 的高效多线程处理能力，可在后台挂载一个低成本的异步守护进程，对历史数据进行定期的合并与整理（Consolidation）。

### 2. 语义覆盖与遗忘机制（Semantic Overwrite & Eviction）
* **主流技术思想**：
  * **LRU（最近最少使用）**：根据记忆的激活频率动态调整权重，长久不被提及的记忆自动退化或降级。
  * **冲突覆盖（Semantic Overwrite）**：当新决策（如“切换为 SQLite”）写入时，系统自动识别并修改与其冲突的旧决策（如“使用 Redis”）为 `[Deprecated]` 状态。
* **Mojomem 架构映射**：
  * 目前基于 Topic Key 实现了针对指定主题的自动覆盖更新 (Upsert)。

---

## 🔍 第三部分：记忆检索与召回（Search & Retrieval）

普通的向量 RAG（检索增强生成）由于缺乏时序感知且容易断章取义，无法直接胜任“记忆召回”场景。前沿的记忆检索技术核心聚焦于主动权转换、时序融合与混合检索。

### 1. 认知中断与 AI 双向主动召回（Agent-Driven Proactive Retrieval）
* **主流技术思想**：打破外围系统被动塞入记忆（被动 RAG）的死结。参考操作系统虚拟内存管理（MMU）的“缺页中断”机制。大模型在思考（Thinking Process）中意识到上下文缺失时，会主动暂停回答并自我触发系统调用（如调用 `query_memory`），由外部系统（如 Mojo 侧）换入（Swap-in）新的记忆 Page 到上下文（RAM）中。
* **Mojomem 架构映射**：
  * 完全契合 Pull & Query 模式。在大模型需要时，利用 MCP 协议赋予其主动拉取语义与探测结构的权力，把召回的主动权交还给 AI。

### 2. 时序感知检索（Time-Aware / Temporal Retrieval）
* **主流技术思想**：让时间成为检索的第一等公民。普通 RAG 仅计算语义相似度（Cos-Similarity），导致两年前的旧决策与昨天的新决策得分一致。前沿算法引入“时序衰减权重”，优先捞出最新、离当前时机最近的记忆。
* **Mojomem 架构映射**：
  * Mojomem 独立设立了时间线概念，对后续多模态数据的时序对齐提供了天然底座。

### 3. 多模态/双通道混合召回（Hybrid Multi-Route Retrieval）
* **主流技术思想**：单纯向量检索（Dense Search）容易丢失精确的变量、函数、Bug ID 等符号；单纯关键词检索（Sparse Search）缺乏泛化和语义理解。主流方案采用 RRF（Reciprocal Rank Fusion，倒数排名融合） 算法，将两者的排名进行交叉重排。
* **Mojomem 架构映射**：
  * **Mojomem 的核心护城河**：底层完美利用了 SQLite3 的 FTS5 全文检索 与 `sqlite-vec` 向量余弦相似度 计算，通过内置的 RRF 混合检索算法进行融合，并对 Q2（全局共识）进行加权提分。配合 ONNX Runtime 搭载的 BGE-Small-ZH-v1.5，在 Mojo 原生 C 兼容编译的加加持下，完成了对“推、拉、查”三模极速召回的完美闭环。
