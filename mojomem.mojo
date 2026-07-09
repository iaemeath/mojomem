from python import Python

fn main() raises:
    # 动态将当前目录加入 Python 模块搜索路径
    var sys = Python.import_module("sys")
    var os = Python.import_module("os")
    sys.path.insert(0, os.getcwd())
    
    # 引入我们用 Python 编写的核心 MCP Server 引擎
    # Mojo 具有零成本互操作特性，它会将 Python 解释器连同我们刚才写的核心代码
    # 一并打包在未来的可执行二进制中。
    var mcp_server = Python.import_module("mcp_server")
    
    print("🚀 Mojomem MCP Server 启动 (Powered by Mojo & Python Interop)")
    
    # 阻塞式监听 StdIO 的 JSON-RPC 请求
    mcp_server.serve()
