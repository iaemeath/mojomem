import subprocess
import json

p = subprocess.Popen(
    ["./mcp_server"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    env={"LD_LIBRARY_PATH": "/home/iaemeath/code/mojomem/ort_sdk/linux/lib:."}
)

reqs = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "mem_save", "arguments": {"project_id": "testproj", "content": "Mojo FFI is super fast."}}},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "mem_list_projects", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "mem_search", "arguments": {"query": "fast", "project": "testproj", "limit": 5}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "mem_update", "arguments": {"obs_id": "a1b2c3d4e5f60000", "title": "Updated Title", "content": "Mojo FFI is extremely fast!"}}},
    {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "memory_promote", "arguments": {"obs_id": "a1b2c3d4e5f60000"}}},
    {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "mem_context", "arguments": {"project": "testproj", "limit": 5}}},
    {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "mem_delete", "arguments": {"obs_id": "a1b2c3d4e5f60000"}}},
]

for req in reqs:
    req_str = json.dumps(req) + "\n"
    print("Sending:", req_str.strip())
    p.stdin.write(req_str)
    p.stdin.flush()
    try:
        out = p.stdout.readline()
        print("Received:", out.strip())
    except Exception as e:
        print("Error:", e)

p.kill()
