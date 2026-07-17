# QMem

> 跨会话 AI 记忆系统 · 自托管 · 纯 Python · MCP Server
>
> Cross-session AI memory as an [MCP](https://modelcontextprotocol.io/) server — self-hosted, parasitic (no background daemon, no external API, fully local).

[中文](#中文) | [English](#english)

---

## 中文

### 这是什么

QMem 是一个运行在 **stdio JSON-RPC** 上的 MCP 记忆服务器，给无状态的 LLM 会话（Claude Code / Cursor / ZCode / Claude Desktop）提供**跨会话、跨项目**的持久记忆：决策、踩坑、进度、共识。

它**寄生**在宿主 Agent 进程里 —— 没有独立后台服务、不监听端口（可选 GUI 除外）、不调用任何外部 API。所有数据落在本地 SQLite。

### 核心架构（V3.3）

```
LLM 客户端 (JSON-RPC over stdin/stdout)
        │
   QMemMCP  (mcp/python/mcp_server.py, Python 3.13)
        ├─ BGEEmbedding      bge-small-zh-v1.5 ONNX, 512 维, 本地 CPU
        ├─ HybridSearcher    RRF: BM25(FTS5) + cosine(sqlite-vec), k=60
        ├─ CBMWrapper        子进程转发 → codebase-memory-mcp.exe（代码图谱）
        ├─ core_memory.db    SQLite + sqlite-vec(vec0) + FTS5 + project_refs
        ├─ call_log.db       独立 WAL, 工具调用审计（90 天自动清理）
        └─ gui/server.py     只读可视化 HTTP :8765
```

**单表全家桶（single-table-all-in-one）**：动态记忆与共识记忆共用一张 `memory_facts` 表，用 `tier` 字段做逻辑隔离：

- `tier = q4` —— 当前项目的草稿/踩坑/进度（项目私有）
- `tier = consensus` —— 跨项目共享的稳定知识（共识域）

**`project_refs` 虚拟引用**：consensus 记忆只存一份，各项目通过多对多引用表"看见"它；一处修改，所有引用方自动生效，无需复制。

**两套知识，各司其职**：
- **动态记忆**（决策/踩坑/进度）→ 存在 QMem，由 `mem_recall` 的 RRF 混合检索召回
- **代码事实**（函数/类/调用图）→ **不存 QMem**，透明转发给 CBM（`search_graph` / `trace_path` / `get_architecture` / `detect_changes`）

### V3.3 治理层

- **写入闸门**：`mem_save` 先查项目 q4 最近邻，相似度 >0.85 返回候选，`force=true` 可绕过
- **召回去重 + 质量信号**：`staleness` / `is_superseded` / `deduped`
- **三步法配额**：top-50 候选 → 按 tier 分组 → 保底配额（q4≥2, consensus≥2）→ 剩余按 RRF 竞争
- **摘要式召回**（`mem_context`）：返回预览，按需 `mem_get_full` 拉全文，防爆 token
- **`mem_consolidate_project`**：检测项目内冗余簇

共 **17 个本地工具**（4 写 / 5 读 / 8 治理）+ 透明转发的 CBM 工具。

### 目录结构

```
qmem/
├── .claude-plugin/plugin.json     # Claude Code 插件清单
├── .zcode-plugin/plugin.json      # ZCode 插件清单
├── .mcp.json                      # MCP server 配置
├── package.json
├── agents/acceptance-check.md     # 验收检查 agent
├── skills/qmem-memory/SKILL.md    # 使用手册 skill
└── mcp/
    ├── python/                    # ★ 全部源码
    │   ├── mcp_server.py          # 主服务器 (V3.3, 17 工具)
    │   ├── search_rrf.py          # RRF 混合检索
    │   ├── embedding.py           # BGE ONNX embedding
    │   ├── cbm_wrapper.py         # CBM 子进程转发 (崩溃自愈+重试)
    │   ├── init_project_context.py
    │   ├── qmem_cli.py            # CLI 入口
    │   ├── schema.sql             # DDL: 4 表 + 4 触发器 + 索引
    │   ├── check_db.py
    │   ├── requirements.txt
    │   └── start_python_mcp.bat
    ├── gui/                       # 零依赖只读可视化 (:8765)
    ├── update/                    # V3.0/V3.2/V3.3 架构文档 + V2→V3 演进档案
    ├── install.ps1
    ├── WINDOWS_SETUP_GUIDE.md
    └── mcp_config_example.json
```

> 体积大头是两个**运行时下载物**（已 gitignore，不入库）：
> - `mcp/codebase-memory-mcp.exe` — PyInstaller 打包的 CBM 代码图谱引擎
> - `mcp/bge-small-zh-v1.5-onnx/` — embedding 模型权重

### 安装与运行

**依赖**：Python 3.13，`pip install -r mcp/python/requirements.txt`
（`sqlite-vec`、`onnxruntime`、`numpy`、`tokenizers`、`huggingface-hub`，均可离线安装）

**首次部署**需补齐两个运行时下载物到 `mcp/` 下：
1. BGE 模型：`huggingface_hub.snapshot_download("Xenova/bge-small-zh-v1.5", local_dir="mcp/bge-small-zh-v1.5-onnx")`
2. CBM 引擎：从 [codebase-memory](https://github.com/craws/codebase-memory) 获取对应平台的 `codebase-memory-mcp` 二进制，放入 `mcp/`

**启动**：`mcp/python/start_python_mcp.bat`（Windows）。在 MCP 客户端里把 `command` 指向它即可。详见 `mcp/WINDOWS_SETUP_GUIDE.md`。

**插件形式**：本仓库同时是一个 Claude Code / ZCode 插件，安装后通过 `${CLAUDE_PLUGIN_ROOT}/mcp/python/start_python_mcp.bat` 自动注册。

### 仓库关系

- **`iaemeath/QMem`（本仓库）** —— 活跃的**纯 Python**实现，Windows 主力。
- **`iaemeath/qmem-mojo`** —— 早期 Mojo + C-FFI 实现的**冻结快照**（性能更佳，主要面向 Linux），不再迭代，活跃版本以本仓库为准。

### 致谢

核心记忆表示与协议设计致敬开源项目 [codebase-memory](https://github.com/craws/codebase-memory)。

---

## English

QMem is an MCP memory server (stdio JSON-RPC) giving stateless LLM sessions persistent, cross-session and cross-project memory — decisions, pitfalls, progress, and consensus knowledge. It is **parasitic**: no background daemon, no listened port (except an optional read-only GUI), no external API. All data stays in local SQLite.

### Architecture (V3.3)

Pure-Python server with a **single-table-all-in-one** design: dynamic and consensus memories share one `memory_facts` table, isolated logically by a `tier` field (`q4` = project-private drafts; `consensus` = cross-project shared). Cross-project sharing uses `project_refs` virtual references — a consensus fact lives in one place and is *seen* by every project that references it, so a single edit propagates with no duplication.

Retrieval is **RRF hybrid** (BM25 over FTS5 + cosine over `sqlite-vec`, fused in one rank space, k=60) with a three-step tier quota. The V3.3 governance layer adds a write-similarity gate, recall dedup/quality signals, summarized recall, and in-project consolidation — 17 local tools in total, plus transparent forwarding of code-graph queries to the bundled CBM engine.

See `mcp/update/QMem-V3.3-Architecture.md` for the full reference.

### Repos

- **`iaemeath/QMem`** (this one) — active **pure-Python** implementation, primary on Windows.
- **`iaemeath/qmem-mojo`** — frozen snapshot of the earlier Mojo + C-FFI implementation (faster, Linux-focused); no longer iterated.

### License

MIT — see [LICENSE](LICENSE).
