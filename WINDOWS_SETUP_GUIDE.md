# QMem Windows 内网测试机 MCP 配置指南

在 Windows 测试机（内网环境）上使用 Python 回退版的 QMem 服务，你需要将它配置到支持 MCP 协议的客户端（如 Cursor、Claude Desktop 等）中。

## 1. 核心运行文件

测试机的核心入口已经封装为了批处理脚本，该脚本会自动处理环境变量和 Python 路径，确保无乱码和依赖正确：
- **入口路径**: `C:\QMem\python\start_python_mcp.bat`

## 2. MCP 客户端配置方法

### 如果使用 Cursor
打开 Cursor 的设置（Settings） -> Features -> MCP Servers，点击 `+ Add New MCP Server`，填写以下信息：

- **Name**: `QMem-Memory`
- **Type**: `command`
- **Command**: `cmd.exe /c C:\QMem\python\start_python_mcp.bat`

### 如果使用 Claude Desktop (或修改 JSON 配置)
编辑你的 `claude_desktop_config.json`，在 `mcpServers` 节点下增加以下配置：

```json
{
  "mcpServers": {
    "QMem-Windows-Python": {
      "command": "cmd.exe",
      "args": [
        "/c",
        "C:\\QMem\\python\\start_python_mcp.bat"
      ]
    }
  }
}
```

## 3. 常见问题排查

如果在客户端连接时出现 `JSON-RPC parsing error` 或 `timeout`，可以尝试：
1. 打开 Windows 命令行 (`cmd.exe`)
2. 手动运行 `C:\QMem\python\start_python_mcp.bat`
3. 检查是否有 Python 报错（如找不到模块、找不到 `core_memory.db` 等）。
4. 如果输出一直停留在空白等待输入，代表服务启动成功，正在等待 JSON-RPC 握手数据（按 Ctrl+C 可退出）。
