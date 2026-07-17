import os
import subprocess
import json
import time
import sys

class CBMWrapper:
    """
    Subprocess wrapper for the official codebase-memory-mcp binary.
    Acts as an MCP middleware to forward requests to the native C core.

    CBM 子进程是独立的 MCP server，必须先 initialize 握手才能 tools/list。
    本类负责：启动握手、请求转发、崩溃恢复、错误透传。
    """
    def __init__(self, binary_path=None):
        self.binary_path = binary_path or self._detect_binary()
        self.process = None
        self._next_id = 0
        self._spawn()

    def _detect_binary(self):
        """探测 codebase-memory-mcp 可执行文件位置：优先 QMem 目录内，其次 PATH。"""
        ext = ".exe" if os.name == "nt" else ""
        _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        local_bin = os.path.join(_ROOT, f"codebase-memory-mcp{ext}")
        if os.path.exists(local_bin):
            return local_bin
        return f"codebase-memory-mcp{ext}"

    def _spawn(self):
        """启动 CBM 子进程并发送 initialize 握手。"""
        try:
            self.process = subprocess.Popen(
                [self.binary_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=sys.stderr, # 重定向至 sys.stderr 避免缓冲区满死锁
                text=True,
                bufsize=1
            )
        except Exception as e:
            self.process = None
            print(f"⚠️ Failed to spawn codebase-memory-mcp: {e}", file=sys.stderr)
            return

        # MCP 握手：initialize → initialized notification
        init_resp = self._send_raw("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "QMem-cbm-wrapper", "version": "2.0"}
        })
        if "error" in init_resp:
            print(f"⚠️ CBM initialize failed: {init_resp['error']}", file=sys.stderr)
        else:
            # 发送 initialized 通知（notification 无 id，不需要响应）
            try:
                self.process.stdin.write(json.dumps({
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized"
                }) + "\n")
                self.process.stdin.flush()
            except Exception:
                pass
            print("✅ codebase-memory-mcp core spawned and initialized.", file=sys.stderr)

    def _send_raw(self, method: str, params: dict = None) -> dict:
        """底层发送：不检查进程存活（供 _spawn 握手阶段使用）。"""
        if not self.process:
            return {"error": {"code": -32603, "message": "CBM core not running"}}
        self._next_id += 1
        request = {"jsonrpc": "2.0", "id": f"fwd-{self._next_id}", "method": method}
        if params is not None:
            request["params"] = params
        try:
            self.process.stdin.write(json.dumps(request) + "\n")
            self.process.stdin.flush()
            resp_str = self.process.stdout.readline()
            if not resp_str:
                return {"error": {"code": -32603, "message": "No response from CBM core"}}
            return json.loads(resp_str)
        except Exception as e:
            return {"error": {"code": -32603, "message": str(e)}}

    def send_request(self, method: str, params: dict = None) -> dict:
        """
        对外转发入口。崩溃自动恢复，错误原样透传（保留 CBM 的 error code/message 结构）。
        """
        # 崩溃恢复：检查子进程是否还活着
        if not self.process or self.process.poll() is not None:
            print("⚠️ CBM core exited, respawning...", file=sys.stderr)
            self._spawn()
            if not self.process:
                return {"error": {"code": -32603, "message": "CBM core unavailable"}}

        resp = self._send_raw(method, params)

        # 若发送失败且进程已死，重启后重试一次
        if "error" in resp and (not self.process or self.process.poll() is not None):
            print("⚠️ CBM core died during request, respawning and retrying...", file=sys.stderr)
            self._spawn()
            if self.process:
                resp = self._send_raw(method, params)

        return resp

    def close(self):
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                self.process.kill()
            self.process = None
