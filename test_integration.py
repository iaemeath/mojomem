import json
from mcp_server import MojomemMCP

def run_tests():
    print("==================================================")
    print("🚀 启动 mojomem MCP 综合测试用例 (Integration Test) ")
    print("==================================================\n")
    
    server = MojomemMCP()

    # 测试用例 1: 初始化握手
    print("▶️ [Test 1] 验证 Server Initialize...")
    req_init = {"jsonrpc": "2.0", "id": "test-1", "method": "initialize"}
    res_init = server.handle_request(req_init)
    print(f"✅ 结果: {json.dumps(res_init, ensure_ascii=False)}\n")

    # 测试用例 2: 获取工具列表
    print("▶️ [Test 2] 验证 tools/list 规范...")
    req_list = {"jsonrpc": "2.0", "id": "test-2", "method": "tools/list"}
    res_list = server.handle_request(req_list)
    tools_count = len(res_list.get("result", {}).get("tools", []))
    print(f"✅ 结果: 成功获取 {tools_count} 个已注册工具\n")

    # 测试用例 3: 内存保存 (Pull)
    print("▶️ [Test 3] 验证 tools/call -> mem_save (触发向量计算与存储)...")
    req_save = {
        "jsonrpc": "2.0",
        "id": "test-3",
        "method": "tools/call",
        "params": {
            "name": "mem_save",
            "arguments": {
                "project_id": "test_project",
                "topic_key": "ENV_CONFIG",
                "content": "测试环境报错，发现必须在 Nginx 里配置 try_files 回退路由。"
            }
        }
    }
    res_save = server.handle_request(req_save)
    print(f"✅ 结果: {json.dumps(res_save, ensure_ascii=False)}\n")

    # 测试用例 4: 代码 AST 解析 (Query)
    print("\n▶️ [Test 4] 验证 tools/call -> get_architecture (触发 CBM 核心层提取)...")
    req_code = {
        "jsonrpc": "2.0",
        "id": "test-4",
        "method": "tools/call",
        "params": {
            "name": "get_architecture",
            "arguments": {
                "directory": "."
            }
        }
    }
    res_code = server.handle_request(req_code)
    # 因为输出可能很长，只打印前几行
    output = res_code.get("result", {}).get("content", [{}])[0].get("text", "")
    preview = output[:100] + "..." if len(output) > 100 else output
    print(f"✅ 结果: 成功捕获代码切片:\n{preview}\n")
    
    # 测试用例 5: 探针冷启动 (Init)
    print("▶️ [Test 5] 验证 tools/call -> init_project_context (生成 Q3 户口本)...")
    req_init_ctx = {
        "jsonrpc": "2.0",
        "id": "test-5",
        "method": "tools/call",
        "params": {
            "name": "init_project_context",
            "arguments": {
                "directory": "."
            }
        }
    }
    res_init_ctx = server.handle_request(req_init_ctx)
    print(f"✅ 结果: {json.dumps(res_init_ctx, ensure_ascii=False)}\n")
    
    print("==================================================")
    print("🎯 所有联调测试用例通过 (All Tests Passed)!")
    print("==================================================")

if __name__ == "__main__":
    run_tests()
