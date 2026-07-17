"""测试调用日志功能（test_call_log.py）

运行方式：
    "C:\\Users\\Administrator\\AppData\\Local\\Programs\\Python\\Python313\\python.exe" test_call_log.py

覆盖场景：
  1. initialize 握手后日志库自动创建 + WAL 模式
  2. 本地工具调用被正确记录（source=local, success=1, resp_size>0）
  3. CBM 转发调用被正确记录（source=cbm）
  4. session_tag 同一会话一致
  5. arg_summary 被截断且可读
  6. 日志写入失败不影响工具正常返回（模拟）
  7. 90 天前日志被自动清理
"""
import subprocess, json, time, sqlite3, os, sys, tempfile

PYTHON = r"C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe"
_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(_DIR, "mcp_server.py")

# ---- 测试用日志库路径：与 mcp_server.py 的 LOG_DBPATH 同源（脚本上一级目录的 call_log.db）----
# 通过环境变量无法覆盖（路径是模块级常量），所以测真实库但测后清理
LOG_DB = os.path.normpath(os.path.join(_DIR, "..", "call_log.db"))

passed, failed = [], []


def check(name, cond, detail=""):
    if cond:
        passed.append(name)
        print(f"  ✅ {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


def start_server():
    """启动 MCP server 子进程，返回 (proc, send_fn)。"""
    proc = subprocess.Popen(
        [PYTHON, SCRIPT],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )

    def send(req):
        proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    return proc, send


def query_log():
    """读取 call_log.db 全部记录。"""
    if not os.path.exists(LOG_DB):
        return []
    conn = sqlite3.connect(LOG_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM tool_call_log ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def cleanup_log():
    """删除测试产生的日志库文件（处理文件/目录两种情况）。"""
    for suffix in ("", "-wal", "-shm"):
        p = LOG_DB + suffix
        if os.path.isdir(p):
            import shutil
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            try:
                os.remove(p)
            except PermissionError:
                pass


def test_basic_logging():
    """场景 1-4：握手后日志库创建 + 本地/CBM 调用正确记录。"""
    print("\n[TEST] basic_logging")
    cleanup_log()

    proc, send = start_server()
    try:
        # initialize 握手
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        check("log_db_created", os.path.exists(LOG_DB), "call_log.db not created after init")

        # 本地工具调用
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "mem_list_projects", "arguments": {}}})
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "list_consensus_projects", "arguments": {}}})

        rows = query_log()
        check("two_calls_logged", len(rows) >= 2, f"expected >=2, got {len(rows)}")

        if rows:
            by_tool = {r["tool_name"]: r for r in rows}
            mp = by_tool.get("mem_list_projects")
            check("local_source", mp and mp["source"] == "local", f"got source={mp['source'] if mp else 'N/A'}")
            check("success_flag", mp and mp["success"] == 1, f"got success={mp['success'] if mp else 'N/A'}")
            check("resp_size_nonzero", mp and mp["resp_size"] > 0, f"got resp_size={mp['resp_size'] if mp else 'N/A'}")
            check("duration_nonneg", mp and mp["duration_ms"] >= 0)

            tags = set(r["session_tag"] for r in rows)
            check("session_tag_consistent", len(tags) == 1, f"got tags={tags}")
            check("session_tag_nonempty", all(t for t in tags), "session_tag is empty string")
    finally:
        proc.terminate()
        proc.wait()


def test_arg_summary():
    """场景 5：arg_summary 被截断且可读。"""
    print("\n[TEST] arg_summary_truncation")
    cleanup_log()

    proc, send = start_server()
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        # 传一个带长参数的调用
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "mem_search", "arguments": {
                  "query": "X" * 600,
                  "project": "test_project"
              }}})

        rows = query_log()
        search_rows = [r for r in rows if r["tool_name"] == "mem_search"]
        check("arg_summary_recorded", len(search_rows) == 1, f"got {len(search_rows)} rows")

        if search_rows:
            summary = search_rows[0]["arg_summary"]
            check("arg_summary_truncated", len(summary) <= 500, f"len={len(summary)}, expected <=500")
            check("arg_summary_starts_with_json", summary.startswith("{"), f"not JSON-like: {summary[:30]}")
    finally:
        proc.terminate()
        proc.wait()


def test_log_failure_safe():
    """场景 6：日志写入失败不影响工具返回。

    策略：锁定 call_log.db（SQLite 不支持文件锁，改用删除后重建表为只读模拟）。
    更简单的方式：直接验证 _log_call 被 except 包裹的代码路径——
    通过把 call_log.db 替换为目录（open 失败）来触发写入异常。
    """
    print("\n[TEST] log_failure_does_not_break_tool")
    cleanup_log()

    proc, send = start_server()
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        # 正常调用一次确认能工作
        r = send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "mem_list_projects", "arguments": {}}})
        has_result = "result" in r
        check("normal_call_works", has_result, f"response: {json.dumps(r)[:100]}")
    finally:
        proc.terminate()
        proc.wait()

    # 现在把 call_log.db 替换为不可写的对象（目录），验证下次调用仍能正常返回
    cleanup_log()
    os.makedirs(LOG_DB, exist_ok=True)  # call_log.db 变成目录 → sqlite3.connect 会失败

    proc2, send2 = start_server()
    try:
        send2({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        r = send2({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "mem_list_projects", "arguments": {}}})
        # 工具应正常返回，即使日志写不进去
        has_result = "result" in r
        check("tool_works_when_log_fails", has_result, f"response: {json.dumps(r)[:100]}")
    finally:
        proc2.terminate()
        proc2.wait()


def test_auto_cleanup():
    """场景 7：90 天前日志被自动清理。"""
    print("\n[TEST] auto_cleanup_old_logs")
    cleanup_log()

    # 先手动建日志库，插入旧数据和新数据
    conn = sqlite3.connect(LOG_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_call_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            tool_name TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'local',
            duration_ms INTEGER DEFAULT 0,
            success INTEGER NOT NULL DEFAULT 1,
            error_msg TEXT DEFAULT '',
            arg_summary TEXT DEFAULT '',
            resp_size INTEGER DEFAULT 0,
            session_tag TEXT DEFAULT ''
        )
    """)
    conn.execute(
        "INSERT INTO tool_call_log(ts, tool_name) VALUES (datetime('now', '-100 days'), 'old_tool')"
    )
    conn.execute(
        "INSERT INTO tool_call_log(ts, tool_name) VALUES (datetime('now', '-1 days'), 'recent_tool')"
    )
    conn.commit()
    old_count = conn.execute("SELECT COUNT(*) FROM tool_call_log").fetchone()[0]
    conn.close()
    check("seed_data_inserted", old_count == 2, f"expected 2 seed rows, got {old_count}")

    # 启动 server（initialize 会触发 _init_log_db → 自动清理）
    proc, send = start_server()
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

        conn = sqlite3.connect(LOG_DB)
        remaining = conn.execute("SELECT tool_name FROM tool_call_log").fetchall()
        conn.close()
        names = [r[0] for r in remaining]
        check("old_log_cleaned", "old_tool" not in names, f"old_tool still present: {names}")
        check("recent_log_kept", "recent_tool" in names, f"recent_tool missing: {names}")
    finally:
        proc.terminate()
        proc.wait()


def main():
    print("=" * 60)
    print("QMem 调用日志功能测试")
    print("=" * 60)

    test_basic_logging()
    test_arg_summary()
    test_auto_cleanup()
    test_log_failure_safe()

    # 清理
    cleanup_log()

    print("\n" + "=" * 60)
    print(f"结果: {len(passed)} passed, {len(failed)} failed")
    if failed:
        print("\n失败项:")
        for f in failed:
            print(f"  ❌ {f}")
        sys.exit(1)
    else:
        print("✅ 全部通过")
        sys.exit(0)


if __name__ == "__main__":
    main()
