# QMem Windows 内网测试机 MCP 配置指南

> QMem v3.3 — 单表全家桶 + 虚拟外键引用（方案 10 RFC）+ 调用审计 + 可视化 GUI
>
> 架构：所有记忆（动态草稿 q4 + 跨项目共识 consensus）共享同一张 `memory_facts` 表和同一套向量/FTS 索引。
> 通过 `tier` 字段区分层级，`project_refs` 表记录项目→共识域的多对多引用关系。
> V3.2 新增：`call_log.db` 调用审计（独立 WAL 文件）、`gui/` 只读可视化（端口 8765）。
> V3.3 新增：写入门禁、检索去重+质量信号、review_queue 过期复审、单项目 consolidate。

> ⚠️ **真实部署路径**：`D:\cly-marketplace\qmem\mcp\`（下文记作 `<QMem根>`）。
> 早期文档/示例中的 `C:\QMem\` 是已废弃的占位部署路径，该目录已删除、资源已清空，不再使用。

## 1. 核心运行文件

测试机的核心入口已经封装为了批处理脚本，该脚本会自动处理环境变量和 Python 路径，确保无乱码和依赖正确：
- **入口路径**: `<QMem根>\python\start_python_mcp.bat`
  （即 `D:\cly-marketplace\qmem\mcp\python\start_python_mcp.bat`）

## 2. MCP 客户端配置方法

### 如果使用 Cursor
打开 Cursor 的设置（Settings） -> Features -> MCP Servers，点击 `+ Add New MCP Server`，填写以下信息：

- **Name**: `QMem-Memory`
- **Type**: `command`
- **Command**: `cmd.exe /c D:\cly-marketplace\qmem\mcp\python\start_python_mcp.bat`

### 如果使用 Claude Desktop (或修改 JSON 配置)
编辑你的 `claude_desktop_config.json`，在 `mcpServers` 节点下增加以下配置：

```json
{
  "mcpServers": {
    "QMem-Windows-Python": {
      "command": "cmd.exe",
      "args": [
        "/c",
        "D:\\cly-marketplace\\qmem\\mcp\\python\\start_python_mcp.bat"
      ]
    }
  }
}
```

## 3. 工具列表（v3.3，17 个本地工具）

| 工具 | 用途 |
|---|---|
| `mem_save` | 写动态记忆（tier=q4）。★ 共识域写守卫：向已有共识域名写 q4 时拦截。★ 写入门禁：纯新增时若本项目已有相似度>0.85 的 q4 记忆返回 candidates 拦截，传 force=true 放行 |
| `mem_recall` | 搜项目动态记忆 + 引用的共识（单次同源 RRF + 三步法配额）。★ 返回带质量信号（staleness/is_superseded/deduped） |
| `mem_search` | 精确过滤动态记忆 |
| `mem_update` | 更新记忆（consensus 需 confirm_consensus=true；改 content 需声明 origin_project） |
| `mem_context` | 开场召回：★ 返回摘要索引（title+content前100字+obs_id），需配 mem_get_full 拉全文。★ 附带 review_queue（超 30 天 progress 复审提示） |
| `mem_get_full` | 按 obs_id 拉完整内容，支持逗号分隔批量（上限 20） |
| `mem_delete` | 默认软删除（可恢复）；hard=true 物理删除（consensus 需 confirm_consensus=true；自动清理空域 refs） |
| `memory_promote` | 提取为共识（UPDATE tier+project+origin_project + 建 ref） |
| `memory_demote` | 降级回动态（origin_project 为空则拒绝——溯源黑洞防护） |
| `consensus_recall` | 专搜共识库 |
| `consensus_health_check` | 检查共识域是否有高度相似记录，提示 AI 精炼 |
| `cross_project_health_check` | 检测跨项目 q4 语义重复，发现可提取为共识的候选 |
| `add_consensus_ref` | 手动建立项目→共识域引用 |
| `list_consensus_projects` | 列出共识域（供 promote 选择目标） |
| `mem_list_projects` | 列动态记忆的 project |
| `mem_consolidate_project` | ★ V3.3 新增：单项目内高相似簇检测，消化存量堆积 |
| `init_project_context` | 目录身份探测 |
| CBM 转发工具 | 代码查询（search_graph/trace_path 等），自动转发到 codebase-memory-mcp |

## 3a. 可选：启动可视化 GUI

```bash
cd D:\cly-marketplace\qmem\mcp\gui && python server.py
# 访问 http://localhost:8765
#   /        → 记忆列表浏览器（Project/Tier/Type/搜索过滤）
#   /graph   → project_refs 引用图谱可视化
# 只读连接 core_memory.db，绝不写库，零额外依赖。
```

## 3b. 调用审计（自动启用，无需配置）

每次 MCP 工具调用自动记录到 `<QMem根>\call_log.db`（独立 WAL 文件，与记忆库隔离）：
记录字段 `tool_name / source(local\|cbm) / duration_ms / success / error_msg / arg_summary(≤500字) / resp_size / session_tag`，90 天自动清理，写入失败绝不影响工具返回。

## 4. 常见问题排查

如果在客户端连接时出现 `JSON-RPC parsing error` 或 `timeout`，可以尝试：
1. 打开 Windows 命令行 (`cmd.exe`)
2. 手动运行 `D:\cly-marketplace\qmem\mcp\python\start_python_mcp.bat`
3. 检查是否有 Python 报错（如找不到模块、找不到 `core_memory.db` 等）
4. 如果输出一直停留在空白等待输入，代表服务启动成功，正在等待 JSON-RPC 握手数据（按 Ctrl+C 可退出）。

## 5. DB 状态检查

```bash
python D:\cly-marketplace\qmem\mcp\check_db.py
```

输出 tier 分布（q4/consensus）、project 分布、project_refs 引用关系、列完整性检查。
