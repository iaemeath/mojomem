# QMem Windows 内网测试机 MCP 配置指南

> QMem **V4.1** — 三 MCP 正交架构（Y 方案落地）
>
> 架构：三个独立 MCP server，各管一个正交维度，各自独立进程/库/工具空间：
> | MCP | 维度 | 职责 | 入口 | 库 |
> |---|---|---|---|---|
> | **QMem** | 时间（项目演进） | 项目架构/进度/踩坑/决策 | `mcp/qmem/server.py` | `core_memory.db` |
> | **DomainKG** | 认知（业务理解） | 电力调度业务概念（"纠偏字典"） | `mcp/domain-kg/server.py` | `domain_knowledge.db` |
> | **codebase-memory** | 结构（代码实体） | 函数/调用关系/表字段 | `mcp/codebase-memory/codebase-memory-mcp.exe` | 独立索引库 |
>
> V3.3 的 consensus 共识机制（project_refs 表）已移除；V4.1 进一步彻底删除 `tier`/`origin_project` 列（V4.0 仅弃用保留）。memory_facts 现为纯单层项目记忆。跨项目技术规范由全局 `~/.claude/CLAUDE.md` 硬编码承载。

> ⚠️ **真实部署路径**：`D:\cly-marketplace\qmem\mcp\`（下文记作 `<QMem根>`）。
> 早期文档/示例中的 `C:\QMem\` 是已废弃的占位部署路径，该目录已删除、资源已清空，不再使用。

## 1. 核心运行文件

三个 MCP 各自独立，启动器在自己的子目录：

| MCP | 入口批处理 | Python 入口 |
|---|---|---|
| QMem | `<QMem根>\qmem\start.bat` | `<QMem根>\qmem\server.py` |
| DomainKG | `<QMem根>\domain-kg\start.bat` | `<QMem根>\domain-kg\server.py` |
| codebase-memory | `<QMem根>\codebase-memory\codebase-memory-mcp.exe` | （独立 exe，无需 Python） |

批处理会自动处理环境变量和 Python 路径（`PYTHON` 环境变量或参数传入），确保无乱码和依赖正确。

## 2. MCP 客户端配置方法

### 如果使用 Cursor
打开 Cursor 的设置（Settings） -> Features -> MCP Servers，点击 `+ Add New MCP Server`，三个 MCP 各填一条：

- **QMem**: `cmd.exe /c D:\cly-marketplace\qmem\mcp\qmem\start.bat`
- **DomainKG**: `cmd.exe /c D:\cly-marketplace\qmem\mcp\domain-kg\start.bat`
- **codebase-memory**: `D:\cly-marketplace\qmem\mcp\codebase-memory\codebase-memory-mcp.exe`

### 如果使用 Claude Desktop / Claude Code (修改 JSON 配置)
编辑 `claude_desktop_config.json`（或工作区 `.mcp.json`），在 `mcpServers` 节点下增加三个配置：

```json
{
  "mcpServers": {
    "QMem": {
      "command": "cmd.exe",
      "args": ["/c", "D:\\cly-marketplace\\qmem\\mcp\\qmem\\start.bat"],
      "env": { "PYTHONUTF8": "1",
               "PYTHON": "C:\\Users\\Administrator\\AppData\\Local\\Programs\\Python\\Python313\\python.exe" }
    },
    "DomainKG": {
      "command": "cmd.exe",
      "args": ["/c", "D:\\cly-marketplace\\qmem\\mcp\\domain-kg\\start.bat"],
      "env": { "PYTHONUTF8": "1",
               "PYTHON": "C:\\Users\\Administrator\\AppData\\Local\\Programs\\Python\\Python313\\python.exe" }
    },
    "codebase-memory": {
      "command": "D:\\cly-marketplace\\qmem\\mcp\\codebase-memory\\codebase-memory-mcp.exe",
      "args": []
    }
  }
}
```

> 注：本仓库不附带预制 `.mcp.json` / `mcp_config_example.json`——三 server 的注册路径即上方第 1 节表格所列入口（`qmem\start.bat` / `domain-kg\start.bat` / `codebase-memory\codebase-memory-mcp.exe`）。按所用客户端把上面 JSON 片段写进 `claude_desktop_config.json` 或工作区 `.mcp.json` 即可。

## 3. 工具列表（V4.0）

### QMem（项目记忆，11 个工具）

| 工具 | 用途 |
|---|---|
| `mem_save` | 写项目记忆。★ 写入门禁：纯新增时若本项目已有相似度>0.85 的记忆返回 candidates 拦截，传 force=true 放行。业务概念/技术规范不存这里 |
| `mem_recall` | RRF 混合检索项目记忆（FTS5+向量） |
| `mem_search` | 精确/过滤查找（project/type） |
| `mem_update` | 按 obs_id 局部更新，自动重算向量 |
| `mem_context` | 开场召回：返回摘要索引（title+content前100字+obs_id）+ review_queue（超 30 天 progress 复审） |
| `mem_get_full` | 按 obs_id 拉完整内容，支持逗号分隔批量（上限 20） |
| `mem_delete` | 默认软删除；hard=true 物理删除 |
| `mem_list_projects` | 列所有 project 的记忆数 |
| `init_project_context` | 目录身份探测（git remote/pom/package.json） |
| `cross_project_health_check` | 检测跨项目语义重复 |
| `mem_consolidate_project` | 单项目内高相似簇检测 |

### DomainKG（业务概念，10 个工具）

| 工具 | 用途 |
|---|---|
| `concept_save` | 写/更新概念卡。★ 强熔断：相似度>0.75 拦截（防"发电计划/出力计划"写成两条） |
| `concept_update` / `concept_delete` / `concept_get` / `concept_list` | 概念卡管理 |
| `edge_save` / `edge_delete` / `list_relations` | 关系边管理（6 种关系） |
| `concept_recall` | 业务概念 RRF 检索（需求分析阶段核对业务名词语义） |
| `concept_neighbors` | 递归 CTE 图遍历（以某概念为中心扩展子图） |

### codebase-memory（代码事实，独立 MCP）
`search_graph` / `trace_path` / `get_architecture` / `get_code_snippet` / `search_code` / `query_graph` / `index_repository` / `detect_changes` 等。详见该 MCP 自带说明。

## 3a. 可选：启动可视化 GUI

```bash
cd D:\cly-marketplace\qmem\mcp\gui && python server.py
# 访问 http://localhost:8765
#   /        → 记忆列表浏览器（Project/Type/搜索过滤，读 qmem/core_memory.db）
#   /graph   → 项目记忆体量分布图
#   /kg      → 领域概念图谱（力导向图，读 domain-kg/domain_knowledge.db）
# 只读连接两个库，绝不写库，零额外依赖。
```

GUI 属于 QMem（DomainKG/CBM 暂无 GUI），同时只读 QMem 和 DomainKG 两个库做可视化。

## 3b. 调用审计（QMem 自动启用）

每次 **QMem** 工具调用自动记录到 `<QMem根>\qmem\call_log.db`（独立 WAL 文件，与记忆库隔离）：
记录字段 `tool_name / source / duration_ms / success / error_msg / arg_summary(≤500字) / resp_size / session_tag`，90 天自动清理，写入失败绝不影响工具返回。
DomainKG / CBM 不写调用日志。

## 4. 常见问题排查

如果在客户端连接时出现 `JSON-RPC parsing error` 或 `timeout`，可以尝试：
1. 打开 Windows 命令行 (`cmd.exe`)
2. 手动运行对应 MCP 的启动器：
   - QMem: `D:\cly-marketplace\qmem\mcp\qmem\start.bat`
   - DomainKG: `D:\cly-marketplace\qmem\mcp\domain-kg\start.bat`
3. 检查是否有 Python 报错（如找不到模块、找不到 db 等）
4. 如果输出一直停留在空白等待输入，代表服务启动成功，正在等待 JSON-RPC 握手数据（按 Ctrl+C 可退出）。

## 5. DB 状态检查

```bash
python D:\cly-marketplace\qmem\mcp\tools\check_db.py
```

输出 total 记忆数、project 分布、`project_refs` 表存在性核对（应为 absent）、`tier`/`origin_project` 列存在性核对（V4.1 应均为 absent）。

## 6. 目录结构（V4.1 拆分后）

```
mcp/
├── qmem/                      ← QMem MCP（项目记忆），自包含
│   ├── server.py  start.bat  schema.sql  search_rrf.py  init_project_context.py
│   ├── core_memory.db  call_log.db
│   ├── embedding.py  bge-small-zh-v1.5-onnx/   ← 独立一份
│   └── requirements.txt
├── domain-kg/                 ← DomainKG MCP（业务概念），自包含
│   ├── server.py  start.bat  kg_schema.sql  kg/
│   ├── domain_knowledge.db
│   ├── embedding.py  bge-small-zh-v1.5-onnx/   ← 独立一份
│   └── requirements.txt
├── codebase-memory/           ← CBM MCP（代码事实）
│   ├── codebase-memory-mcp.exe
│   └── install.ps1            ← CBM exe 安装器（独立工具，从 mcp/ 根移入）
├── gui/                       ← 可视化（属 QMem，只读两库）
├── tools/                     ← 诊断脚本（check_db / test_call_log）
└── update/                    ← 架构演进文档
```

> MCP 客户端配置方法见本文档第 2 节（Cursor / Claude Desktop JSON 两种），不再单设示例文件。

**移除某个 MCP**：删对应子目录 + 删 `.mcp.json` 一条 + 删 `restart_mcp.ps1` 匹配规则，不影响另外两个。
