#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
QMem 记忆可视化 - 只读 HTTP 服务
零依赖（仅 Python 标准库），只读连接 core_memory.db，绝不写库。

启动:  cd <QMem根>/gui && python server.py   （如 D:\\cly-marketplace\\qmem\\mcp\\gui）
访问:  http://localhost:8765
"""

import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ── 配置 ──────────────────────────────────────────────
PORT = 8765
GUI_DIR = os.path.dirname(os.path.abspath(__file__))
# core_memory.db 在上一级目录（与 mcp_server.py 的 DBPATH 同源）
DB_PATH = os.path.normpath(os.path.join(GUI_DIR, "..", "core_memory.db"))
DB_URI = "file:%s?mode=ro" % DB_PATH.replace("\\", "/")
DEFAULT_LIMIT = 200


# ── 数据库只读连接 ────────────────────────────────────
def query(sql, params=()):
    """执行只读查询，返回 list[dict]"""
    conn = sqlite3.connect(DB_URI, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        return rows
    finally:
        conn.close()


# ── API 实现 ──────────────────────────────────────────
def api_memories(params):
    """记忆列表，支持 project / tier / type / q 过滤"""
    where = ["deleted_at IS NULL"]
    args = []

    p = params.get("project", [None])[0]
    if p:
        where.append("project = ?")
        args.append(p)

    t = params.get("tier", [None])[0]
    if t:
        where.append("tier = ?")
        args.append(t)

    ty = params.get("type", [None])[0]
    if ty:
        where.append("type = ?")
        args.append(ty)

    q = params.get("q", [None])[0]
    if q:
        where.append("(title LIKE ? OR content LIKE ?)")
        like = "%%%s%%" % q
        args += [like, like]

    where_clause = " AND ".join(where)
    sql = (
        "SELECT obs_uuid, project, topic_key, title, content, type, "
        "       tier, origin_project, created_at, updated_at "
        "FROM memory_facts "
        "WHERE %s "
        "ORDER BY updated_at DESC LIMIT ?" % where_clause
    )
    args.append(DEFAULT_LIMIT)
    return query(sql, args)


def api_stats(_params):
    """统计汇总：总数 / 各维度分布 / 各 project 记忆数"""
    stats = {}
    stats["total"] = query(
        "SELECT COUNT(*) AS n FROM memory_facts WHERE deleted_at IS NULL"
    )[0]["n"]

    for dim in ("type", "tier"):
        stats[dim] = query(
            "SELECT %s AS k, COUNT(*) AS n FROM memory_facts "
            "WHERE deleted_at IS NULL GROUP BY %s ORDER BY n DESC" % (dim, dim)
        )

    stats["by_project"] = query(
        "SELECT project AS k, tier, COUNT(*) AS n FROM memory_facts "
        "WHERE deleted_at IS NULL GROUP BY project, tier "
        "ORDER BY tier, n DESC"
    )
    return stats


def api_graph(_params):
    """project_refs 引用图：节点 + 边 + 各节点记忆数/类型统计"""
    refs = query(
        "SELECT project, ref_project, ref_source FROM project_refs"
    )
    # 收集节点（去重），区分项目 / 共识域
    node_set = {}
    for r in refs:
        node_set[r["project"]] = "project"
        node_set[r["ref_project"]] = "consensus"

    # 补充：没有 ref 关联的孤立项目/共识域也展示
    for row in query(
        "SELECT DISTINCT project, tier FROM memory_facts WHERE deleted_at IS NULL"
    ):
        if row["project"] and row["project"] not in node_set:
            node_set[row["project"]] = "consensus" if row["tier"] == "consensus" else "project"

    # 各节点记忆数 + type 分布
    count_map = {}
    for row in query(
        "SELECT project, type, COUNT(*) AS n FROM memory_facts "
        "WHERE deleted_at IS NULL GROUP BY project, type"
    ):
        c = count_map.setdefault(row["project"], {"total": 0, "types": {}})
        c["total"] += row["n"]
        c["types"][row["type"]] = row["n"]

    nodes = []
    for nid, ntype in node_set.items():
        c = count_map.get(nid, {"total": 0, "types": {}})
        # 出入度（边数）
        deg = sum(1 for r in refs if r["project"] == nid or r["ref_project"] == nid)
        nodes.append({
            "id": nid, "type": ntype,
            "count": c["total"], "types": c["types"], "degree": deg,
        })

    edges = [
        {"source": r["project"], "target": r["ref_project"],
         "source_type": "promote" if r["ref_source"] == "promote" else "manual"}
        for r in refs
    ]
    return {"nodes": nodes, "edges": edges}


# ── 路由表 ────────────────────────────────────────────
API_ROUTES = {
    "/api/memories": api_memories,
    "/api/stats": api_stats,
    "/api/graph": api_graph,
}


# ── HTTP Handler ──────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        # API 路由
        if path in API_ROUTES:
            try:
                data = API_ROUTES[path](params)
                self._json(data)
            except Exception as e:
                self._json({"error": str(e)}, code=500)
            return

        # 静态文件：友好路由映射
        if path == "/":
            path = "/index.html"
        elif path == "/graph":
            path = "/graph.html"

        # 安全：禁止目录穿越
        rel = path.lstrip("/")
        abs_path = os.path.normpath(os.path.join(GUI_DIR, rel))
        if not abs_path.startswith(GUI_DIR):
            self._text(403, "Forbidden")
            return

        if os.path.isfile(abs_path):
            self._serve_file(abs_path)
        else:
            self._text(404, "Not Found")

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code, msg):
        body = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, abs_path):
        ext = os.path.splitext(abs_path)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".svg": "image/svg+xml",
        }.get(ext, "application/octet-stream")

        with open(abs_path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # 简化日志，只打请求行
        print("  %s" % (self.address_string(),))


def main():
    if not os.path.isfile(DB_PATH):
        print("[ERROR] 数据库不存在: %s" % DB_PATH)
        raise SystemExit(1)

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("=" * 50)
    print("  QMem 记忆可视化 (只读)")
    print("  数据库: %s" % DB_PATH)
    print("  访问:   http://localhost:%d" % PORT)
    print("=" * 50)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
