import sys, os, json, sqlite3, uuid, hashlib, time
import numpy as np
from typing import Dict, Any
from embedding import BGEEmbedding
from search_rrf import HybridSearcher
from init_project_context import ProjectContextProbe
from cbm_wrapper import CBMWrapper

_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_DIR)
DBPATH = os.path.join(_ROOT, "core_memory.db")

# 需要确保存在的列（用于从旧版 schema 增量升级）
_REQUIRED_COLUMNS = {
    "title": "TEXT DEFAULT ''",
    "type": "TEXT DEFAULT 'manual'",
    "scope": "TEXT NOT NULL DEFAULT 'project'",
    "content_hash": "TEXT DEFAULT ''",
    "session_id": "TEXT DEFAULT ''",
    "pinned": "INTEGER NOT NULL DEFAULT 0",
    "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    "review_after": "TIMESTAMP",
    "deleted_at": "TIMESTAMP",
}

class QMemMCP:
    def __init__(self):
        self.embedder = BGEEmbedding()
        self.searcher = HybridSearcher(DBPATH)
        self.context_initializer = ProjectContextProbe(_DIR)
        self.cbm = CBMWrapper()
        self.local_tools = {
            "mem_save", "mem_recall", "mem_delete", "memory_promote",
            "init_project_context", "mem_search", "mem_update", "mem_context", "mem_list_projects"
        }

    def _get_conn(self):
        conn = sqlite3.connect(DBPATH)
        conn.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(conn)
        conn.row_factory = sqlite3.Row
        return conn

    def _migrate_schema(self, conn):
        """增量升级旧表：补齐缺失列 + 重建 FTS。"""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_facts)").fetchall()}
        for col, typedef in _REQUIRED_COLUMNS.items():
            if col not in cols:
                try:
                    conn.execute(f"ALTER TABLE memory_facts ADD COLUMN {col} {typedef}")
                except Exception as e:
                    print(f"[migrate] skip {col}: {e}", file=sys.stderr)
        conn.commit()

    def handle_request(self, req):
        method = req.get("method")
        params = req.get("params", {})
        rid = req.get("id")
        try:
            if method == "initialize": res = self._init()
            elif method == "tools/list": res = self._tools_list()
            elif method == "tools/call": res = self._tools_call(params)
            else: raise ValueError(f"unknown method: {method}")
            return {"jsonrpc": "2.0", "id": rid, "result": res}
        except Exception as e:
            import traceback
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e), "data": traceback.format_exc()}}

    def _init(self):
        conn = self._get_conn()
        with open(os.path.join(_DIR, "schema.sql"), encoding="utf-8") as f:
            conn.executescript(f.read())
        self._migrate_schema(conn)
        
        # Async physical deletion space recovery
        try:
            conn.execute("VACUUM")
        except Exception as e:
            print(f"[init] vacuum failed: {e}", file=sys.stderr)
            
        conn.close()
        return {"protocolVersion": "2024-11-05", "serverInfo": {"name": "qmem-mcp", "version": "2.1"}, "capabilities": {"tools": {}}}

    def _tools_list(self):
        local_tools = [
            {"name": "mem_save", "description": "保存记忆（Push）。支持 title/type/scope/topic_key，topic_key 命中时自动 upsert。", "inputSchema": {"type": "object", "properties": {"project_id": {"type": "string"}, "topic_key": {"type": "string"}, "content": {"type": "string"}, "title": {"type": "string"}, "type": {"type": "string", "enum": ["decision", "bugfix", "reference", "learning", "manual"]}, "scope": {"type": "string", "enum": ["project", "personal"]}}, "required": ["project_id", "content"]}},
            {"name": "mem_recall", "description": "RRF 混合检索（Pull）：FTS5 词法 + 向量语义融合排序，is_global 条目 boost。", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "current_project": {"type": "string"}, "min_similarity": {"type": "number", "default": 0.5}, "limit": {"type": "integer", "default": 10}}, "required": ["query"]}},
            {"name": "mem_search", "description": "精确/过滤查找：按 project/type/scope 过滤 + 关键词 FTS5 MATCH。", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "project": {"type": "string"}, "type": {"type": "string"}, "scope": {"type": "string"}, "limit": {"type": "integer", "default": 20}}, "required": []}},
            {"name": "mem_update", "description": "按 obs_id 局部更新（content/title/type），自动重算向量。", "inputSchema": {"type": "object", "properties": {"obs_id": {"type": "string"}, "content": {"type": "string"}, "title": {"type": "string"}, "type": {"type": "string"}}, "required": ["obs_id"]}},
            {"name": "mem_context", "description": "开场召回：按 project 返回最近 N 条 + pinned 优先。", "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}, "limit": {"type": "integer", "default": 10}}, "required": ["project"]}},
            {"name": "mem_delete", "description": "彻底硬删除记忆及向量索引，立即生效。", "inputSchema": {"type": "object", "properties": {"obs_id": {"type": "string"}}, "required": ["obs_id"]}},
            {"name": "memory_promote", "description": "将本条经验提炼并抽取到全局 Q2 Skill (部分项目共识) 中，实现物理隔离。", "inputSchema": {"type": "object", "properties": {"obs_id": {"type": "string"}}, "required": ["obs_id"]}},
            {"name": "mem_list_projects", "description": "列出所有 project 及其记忆数。", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "init_project_context", "description": "探测目录身份线索（git remote/pom/package.json），生成 Q3 户口本。", "inputSchema": {"type": "object", "properties": {"directory": {"type": "string"}}, "required": ["directory"]}},
        ]

        # 转发 CBM 的工具（代码查询类：get_architecture/search_graph/trace_path 等）
        cbm_tools = []
        try:
            cbm_resp = self.cbm.send_request("tools/list")
            if "result" in cbm_resp and "tools" in cbm_resp["result"]:
                cbm_tools = cbm_resp["result"]["tools"]
        except Exception as e:
            print(f"[tools/list] CBM unavailable: {e}", file=sys.stderr)

        return {"tools": local_tools + cbm_tools}

    def _tools_call(self, params: Dict[str, Any]):
        name = params.get("name")
        args = params.get("arguments", {})

        if name not in self.local_tools:
            res = self.cbm.send_request("tools/call", params)
            if "error" in res:
                return {"content": [{"type": "text", "text": json.dumps(res["error"], ensure_ascii=False)}], "isError": True}
            if "result" in res:
                return res["result"]
            return {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}]}

        dispatch = {
            "mem_save": lambda: self._save(args),
            "mem_recall": lambda: self._recall(args),
            "mem_search": lambda: self._search(args),
            "mem_update": lambda: self._update(args),
            "mem_context": lambda: self._context(args),
            "mem_delete": lambda: self._delete(args),
            "memory_promote": lambda: self._promote(args),
            "mem_list_projects": lambda: self._list_projects(args),
            "init_project_context": lambda: self._init_project_context(args),
        }
        if name not in dispatch:
            raise ValueError(f"Local tool not found: {name}")
        result = dispatch[name]()
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}

    # ==================== 写入 ====================

    def _save(self, args):
        project_id = args.get("project_id", "")
        topic_key = args.get("topic_key", "")
        content = args.get("content", "")
        title = args.get("title") or content[:30]
        obs_type = args.get("type", "manual")
        scope = args.get("scope", "project")
        if not project_id or not content:
            return {"error": "project_id and content are required"}

        content_hash = hashlib.sha256((title + content).encode("utf-8")).hexdigest()[:16]
        vec = self.embedder.embed(title + " " + content)
        vec_bytes = np.array(vec, dtype=np.float32).tobytes()

        conn = self._get_conn()
        # upsert：同 project+scope+topic_key 存在则更新
        existing = None
        if topic_key:
            existing = conn.execute(
                "SELECT id, obs_uuid FROM memory_facts WHERE project=? AND scope=? AND topic_key=? AND deleted_at IS NULL",
                (project_id, scope, topic_key)
            ).fetchone()

        if existing:
            fid = existing["id"]
            oid = existing["obs_uuid"]
            conn.execute(
                "UPDATE memory_facts SET title=?, content=?, type=?, content_hash=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (title, content, obs_type, content_hash, fid)
            )
            # vec0 不支持 UPDATE embedding → 删后重建
            conn.execute("DELETE FROM memory_vectors WHERE rowid=?", (fid,))
            conn.execute("INSERT INTO memory_vectors(rowid, embedding) VALUES (?, ?)", (fid, vec_bytes))
            action = "updated"
        else:
            oid = uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT INTO memory_facts(obs_uuid, project, topic_key, title, content, type, scope, content_hash) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (oid, project_id, topic_key, title, content, obs_type, scope, content_hash)
            )
            fid = conn.execute("SELECT id FROM memory_facts WHERE obs_uuid=?", (oid,)).fetchone()[0]
            conn.execute("INSERT INTO memory_vectors(rowid, embedding) VALUES (?, ?)", (fid, vec_bytes))
            action = "created"

        conn.commit()
        conn.close()
        return {"obs_id": oid, "action": action, "id": fid}

    def _update(self, args):
        obs_id = args.get("obs_id")
        if not obs_id:
            return {"error": "obs_id is required"}
        conn = self._get_conn()
        row = conn.execute("SELECT id FROM memory_facts WHERE obs_uuid=?", (obs_id,)).fetchone()
        if not row:
            conn.close()
            return {"error": "not found"}
        fid = row[0]
        sets, params = [], []
        new_title = None
        for col in ("content", "title", "type"):
            if col in args and args[col] is not None:
                sets.append(f"{col}=?")
                params.append(args[col])
                if col == "title":
                    new_title = args[col]
        if not sets:
            conn.close()
            return {"error": "nothing to update"}
        sets.append("updated_at=CURRENT_TIMESTAMP")
        params.append(fid)
        conn.execute(f"UPDATE memory_facts SET {', '.join(sets)} WHERE id=?", params)

        # 若 content 或 title 变了 → 重算向量 + content_hash
        if "content" in args or "title" in args:
            row = conn.execute("SELECT title, content FROM memory_facts WHERE id=?", (fid,)).fetchone()
            t, c = row["title"], row["content"]
            content_hash = hashlib.sha256((t + c).encode("utf-8")).hexdigest()[:16]
            conn.execute("UPDATE memory_facts SET content_hash=? WHERE id=?", (content_hash, fid))
            vec = self.embedder.embed(t + " " + c)
            conn.execute("DELETE FROM memory_vectors WHERE rowid=?", (fid,))
            conn.execute("INSERT INTO memory_vectors(rowid, embedding) VALUES (?, ?)", (fid, np.array(vec, dtype=np.float32).tobytes()))

        conn.commit()
        conn.close()
        return {"obs_id": obs_id, "action": "updated"}

    def _delete(self, args):
        obs_id = args.get("obs_id")
        if not obs_id:
            return {"error": "obs_id is required"}
        conn = self._get_conn()
        conn.execute("DELETE FROM memory_facts WHERE obs_uuid=?", (obs_id,))
        n = conn.total_changes
        conn.commit()
        conn.close()
        return {"hard_deleted": n}

    def _promote(self, args):
        obs_id = args.get("obs_id")
        if not obs_id:
            return {"error": "obs_id is required"}
            
        conn = self._get_conn()
        row = conn.execute("SELECT title, content FROM memory_facts WHERE obs_uuid=?", (obs_id,)).fetchone()
        if not row:
            conn.close()
            return {"error": "observation not found"}
            
        title = row["title"]
        content = row["content"]
        
        # 物理写入全局 ~/.agents/skills/q2-consensus/SKILL.md
        skill_dir = os.path.expanduser("~/.agents/skills/q2-consensus")
        os.makedirs(skill_dir, exist_ok=True)
        skill_file = os.path.join(skill_dir, "SKILL.md")
        
        is_new = not os.path.exists(skill_file)
        already_exists = False
        if not is_new:
            with open(skill_file, "r", encoding="utf-8") as f:
                existing_text = f.read()
                if f"## {title}" in existing_text and content in existing_text:
                    already_exists = True
        
        if not already_exists:
            with open(skill_file, "a", encoding="utf-8") as f:
                if is_new:
                    f.write("---\nname: q2-consensus\ndescription: 部分项目共识\n---\n\n")
                f.write(f"## {title}\n\n{content}\n\n")
            
        # 物理硬删除原始记录，完成绝对隔离
        conn.execute("DELETE FROM memory_facts WHERE obs_uuid=?", (obs_id,))
        n = conn.total_changes
        conn.commit()
        conn.close()
        return {"promoted_to_skill": skill_file, "hard_deleted": n, "appended": not already_exists}

    # ==================== 读取 ====================

    def _recall(self, args):
        query = args.get("query", "")
        project = args.get("current_project")
        min_sim = float(args.get("min_similarity", 0.5))
        limit = int(args.get("limit", 10))
        vec = self.embedder.embed(query)
        results = self.searcher.hybrid_search_rrf(query, vec, project=project, min_similarity=min_sim, limit=limit)
        return {"results": results, "query": query, "count": len(results)}

    def _search(self, args):
        query = args.get("query", "")
        project = args.get("project")
        obs_type = args.get("type")
        scope = args.get("scope")
        limit = int(args.get("limit", 20))
        conn = self._get_conn()
        # 无 query → 纯过滤列表
        if not query:
            sql = "SELECT obs_uuid, project, topic_key, title, type, scope, created_at FROM memory_facts WHERE deleted_at IS NULL"
            params = []
            if project: sql += " AND project=?"; params.append(project)
            if obs_type: sql += " AND type=?"; params.append(obs_type)
            if scope: sql += " AND scope=?"; params.append(scope)
            sql += " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            return {"results": [dict(r) for r in rows], "count": len(rows)}
        # 有 query → FTS5 MATCH + 过滤
        safe = query.replace('"', '""')
        sql = ("SELECT mf.obs_uuid, mf.project, mf.topic_key, mf.title, mf.content, mf.type, mf.scope, mf.created_at "
               "FROM memory_facts_fts JOIN memory_facts mf ON mf.id = memory_facts_fts.rowid "
               "WHERE memory_facts_fts MATCH ? AND mf.deleted_at IS NULL")
        params = [f'"{safe}"']
        if project: sql += " AND mf.project=?"; params.append(project)
        if obs_type: sql += " AND mf.type=?"; params.append(obs_type)
        if scope: sql += " AND mf.scope=?"; params.append(scope)
        sql += " ORDER BY bm25(memory_facts_fts) LIMIT ?"; params.append(limit)
        try:
            rows = conn.execute(sql, params).fetchall()
            res = [dict(r) for r in rows]
        except Exception:
            # FTS5 失败降级 LIKE
            p = f"%{query}%"
            sql = ("SELECT obs_uuid, project, topic_key, title, content, type, scope, created_at "
                   "FROM memory_facts WHERE (content LIKE ? OR title LIKE ?) AND deleted_at IS NULL")
            params = [p, p]
            if project: sql += " AND project=?"; params.append(project)
            if obs_type: sql += " AND type=?"; params.append(obs_type)
            if scope: sql += " AND scope=?"; params.append(scope)
            sql += " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            res = [dict(r) for r in rows]
        conn.close()
        return {"results": res, "count": len(res)}

    def _context(self, args):
        project = args.get("project")
        limit = int(args.get("limit", 10))
        conn = self._get_conn()
        sql = ("SELECT obs_uuid, project, topic_key, title, content, type, pinned, created_at "
               "FROM memory_facts WHERE deleted_at IS NULL AND project=? "
               "ORDER BY pinned DESC, created_at DESC LIMIT ?")
        rows = conn.execute(sql, (project, limit)).fetchall()
        conn.close()
        return {"project": project, "observations": [dict(r) for r in rows], "count": len(rows)}

    def _list_projects(self, args):
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT project, COUNT(*) as n FROM memory_facts WHERE deleted_at IS NULL GROUP BY project ORDER BY n DESC"
        ).fetchall()
        conn.close()
        return {"projects": [dict(r) for r in rows]}

    def _init_project_context(self, args):
        directory = args.get("directory", ".")
        try:
            text = self.context_initializer.generate_context_text(directory)
            info = self.context_initializer.probe(directory)
            return {"status": "success", "context": text, "probe": info}
        except Exception as e:
            return {"status": "error", "message": str(e)}


def serve():
    server = QMemMCP()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            res = server.handle_request(req)
            sys.stdout.write(json.dumps(res, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception as e:
            import traceback
            sys.stderr.write(f"Error: {e}\n{traceback.format_exc()}\n")


if __name__ == "__main__":
    serve()
