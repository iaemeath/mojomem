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

_REQUIRED_COLUMNS = {
    "title": "TEXT DEFAULT ''",
    "type": "TEXT DEFAULT 'manual'",
    "scope": "TEXT NOT NULL DEFAULT 'project'",
    "tier": "TEXT NOT NULL DEFAULT 'q4'",
    "origin_project": "TEXT DEFAULT ''",
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
            "mem_save", "mem_recall", "mem_delete", "memory_promote", "memory_demote",
            "init_project_context", "mem_search", "mem_update", "mem_context",
            "mem_list_projects", "consensus_recall", "consensus_health_check",
            "add_consensus_ref", "list_consensus_projects",
        }

    def _get_conn(self):
        conn = sqlite3.connect(DBPATH)
        conn.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(conn)
        conn.row_factory = sqlite3.Row
        return conn

    def _migrate_schema(self, conn):
        """增量升级：补齐缺失列。"""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_facts)").fetchall()}
        for col, typedef in _REQUIRED_COLUMNS.items():
            if col not in cols:
                try:
                    conn.execute(f"ALTER TABLE memory_facts ADD COLUMN {col} {typedef}")
                    print(f"[migrate] added column {col}", file=sys.stderr)
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
        # 先迁移列（ADD COLUMN），再执行 schema.sql（含依赖新列的索引）
        self._migrate_schema(conn)
        with open(os.path.join(_DIR, "schema.sql"), encoding="utf-8") as f:
            conn.executescript(f.read())

        # 一次性迁移：现有 _ 前缀记忆标记为 consensus
        try:
            n = conn.execute(
                "UPDATE memory_facts SET tier='consensus' "
                "WHERE project LIKE '\\_%' ESCAPE '\\' AND tier!='consensus' AND deleted_at IS NULL"
            ).rowcount
            if n:
                print(f"[init] migrated {n} _-prefix memories to tier=consensus", file=sys.stderr)
        except Exception as e:
            print(f"[init] consensus migration skipped: {e}", file=sys.stderr)

        conn.commit()
        try:
            conn.execute("VACUUM")
        except Exception as e:
            print(f"[init] vacuum failed: {e}", file=sys.stderr)

        conn.close()
        return {"protocolVersion": "2024-11-05", "serverInfo": {"name": "qmem-mcp", "version": "3.0"}, "capabilities": {"tools": {}}}

    def _tools_list(self):
        local_tools = [
            {"name": "mem_save", "description": "保存记忆（Push）。支持 title/type/scope/topic_key，topic_key 命中时自动 upsert（仅 tier=q4 范围）。", "inputSchema": {"type": "object", "properties": {"project_id": {"type": "string"}, "topic_key": {"type": "string"}, "content": {"type": "string"}, "title": {"type": "string"}, "type": {"type": "string", "enum": ["decision", "bugfix", "reference", "learning", "manual"]}, "scope": {"type": "string", "enum": ["project", "personal"]}}, "required": ["project_id", "content"]}},
            {"name": "mem_recall", "description": "RRF 混合检索：搜当前项目动态记忆 + 引用的共识域（单次同源 RRF，三步法配额截取）。", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "current_project": {"type": "string"}, "min_similarity": {"type": "number", "default": 0.5}, "limit": {"type": "integer", "default": 10}}, "required": ["query"]}},
            {"name": "mem_search", "description": "精确/过滤查找：按 project/type/scope 过滤动态记忆（tier=q4）。", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "project": {"type": "string"}, "type": {"type": "string"}, "scope": {"type": "string"}, "limit": {"type": "integer", "default": 20}}, "required": []}},
            {"name": "mem_update", "description": "按 obs_id 局部更新（content/title/type），自动重算向量。操作 consensus 行需 confirm_consensus=true；改 consensus content 时需声明 origin_project 去留。", "inputSchema": {"type": "object", "properties": {"obs_id": {"type": "string"}, "content": {"type": "string"}, "title": {"type": "string"}, "type": {"type": "string"}, "confirm_consensus": {"type": "boolean"}, "origin_project": {"type": "string"}}, "required": ["obs_id"]}},
            {"name": "mem_context", "description": "开场召回：加载项目自身动态记忆 + 引用的共识（防爆 top-N）。", "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}, "limit": {"type": "integer", "default": 10}, "consensus_limit": {"type": "integer", "default": 5}}, "required": ["project"]}},
            {"name": "mem_delete", "description": "彻底硬删除记忆及向量索引。删 consensus 行需 confirm_consensus=true。", "inputSchema": {"type": "object", "properties": {"obs_id": {"type": "string"}, "confirm_consensus": {"type": "boolean"}}, "required": ["obs_id"]}},
            {"name": "memory_promote", "description": "将经验提取为跨项目共识：UPDATE tier=consensus + project 改域名 + origin_project 记录来源 + 建 ref。不挪数据不重算向量。", "inputSchema": {"type": "object", "properties": {"obs_id": {"type": "string"}, "consensus_domain": {"type": "string", "description": "共识域名（_前缀），如 _java-cloud-common"}}, "required": ["obs_id", "consensus_domain"]}},
            {"name": "memory_demote", "description": "将共识降级回动态记忆：UPDATE tier=q4 + project 改回 origin_project。origin_project 为空（已融合多源）则拒绝降级。", "inputSchema": {"type": "object", "properties": {"obs_id": {"type": "string"}}, "required": ["obs_id"]}},
            {"name": "consensus_recall", "description": "专搜共识库（tier=consensus）。查阅通用经验/架构陷阱/踩坑根因时调用。", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "min_similarity": {"type": "number", "default": 0.5}, "limit": {"type": "integer", "default": 10}}, "required": ["query"]}},
            {"name": "consensus_health_check", "description": "检查共识域健康度：找出内容高度相似的共识记录（embedding>0.85），提示 AI 精炼合并。建议 promote 后调用。", "inputSchema": {"type": "object", "properties": {"consensus_domain": {"type": "string"}}, "required": []}},
            {"name": "add_consensus_ref", "description": "手动建立项目→共识域引用（ref_source=manual）。项目未 promote 过但需要知道相关共识时使用。", "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}, "consensus_project": {"type": "string"}}, "required": ["project", "consensus_project"]}},
            {"name": "list_consensus_projects", "description": "列出所有共识域（tier=consensus 的 project）及其记忆数，供 promote 选择目标。", "inputSchema": {"type": "object", "properties": {}}, },
            {"name": "mem_list_projects", "description": "列出所有动态记忆的 project 及其记忆数。", "inputSchema": {"type": "object", "properties": {}}, },
            {"name": "init_project_context", "description": "探测目录身份线索（git remote/pom/package.json），生成 Q3 户口本。", "inputSchema": {"type": "object", "properties": {"directory": {"type": "string"}}, "required": ["directory"]}},
        ]

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
            "memory_demote": lambda: self._demote(args),
            "consensus_recall": lambda: self._consensus_recall(args),
            "consensus_health_check": lambda: self._consensus_health_check(args),
            "add_consensus_ref": lambda: self._add_consensus_ref(args),
            "list_consensus_projects": lambda: self._list_consensus_projects(args),
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
        # upsert：同 project+scope+topic_key+tier=q4 存在则更新（★ tier 守卫，不碰 consensus 行）
        existing = None
        if topic_key:
            existing = conn.execute(
                "SELECT id, obs_uuid FROM memory_facts WHERE project=? AND scope=? AND topic_key=? "
                "AND tier='q4' AND deleted_at IS NULL",
                (project_id, scope, topic_key)
            ).fetchone()

        if existing:
            fid = existing["id"]
            oid = existing["obs_uuid"]
            conn.execute(
                "UPDATE memory_facts SET title=?, content=?, type=?, content_hash=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (title, content, obs_type, content_hash, fid)
            )
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
        row = conn.execute("SELECT id, tier, origin_project FROM memory_facts WHERE obs_uuid=?", (obs_id,)).fetchone()
        if not row:
            conn.close()
            return {"error": "not found"}
        fid, tier, cur_origin = row["id"], row["tier"], row["origin_project"]

        # ★ 越权确认：consensus 行需 confirm_consensus=true
        if tier == "consensus" and not args.get("confirm_consensus"):
            refs = conn.execute("SELECT COUNT(*) FROM project_refs WHERE ref_project=?",
                                (conn.execute("SELECT project FROM memory_facts WHERE id=?", (fid,)).fetchone()[0],)).fetchone()[0]
            conn.close()
            return {"warning": f"这是一条全局共识，被 {refs} 个项目引用。确认修改请传 confirm_consensus=true。如需降级请用 memory_demote。"}

        # ★ 脐带剪断强制检查：改 consensus content 时必须声明 origin_project 去留
        if tier == "consensus" and "content" in args and "origin_project" not in args:
            conn.close()
            return {"warning": "你正在修改共识内容。请显式声明溯源状态：传 origin_project=''（合并多源共识时剪断脐带），或传 origin_project=<原值>（仅更新措辞，溯源不变）。"}

        sets, params = [], []
        for col in ("content", "title", "type", "origin_project"):
            if col in args and args[col] is not None:
                sets.append(f"{col}=?")
                params.append(args[col])
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
        row = conn.execute("SELECT tier, project FROM memory_facts WHERE obs_uuid=?", (obs_id,)).fetchone()
        if not row:
            conn.close()
            return {"error": "not found"}

        # ★ 越权确认：consensus 行需 confirm_consensus=true
        if row["tier"] == "consensus" and not args.get("confirm_consensus"):
            refs = conn.execute("SELECT COUNT(*) FROM project_refs WHERE ref_project=?", (row["project"],)).fetchone()[0]
            conn.close()
            return {"warning": f"这是一条全局共识，被 {refs} 个项目引用。确认删除请传 confirm_consensus=true。"}

        tier, project = row["tier"], row["project"]
        conn.execute("DELETE FROM memory_facts WHERE obs_uuid=?", (obs_id,))
        n = conn.total_changes

        # ★ 空域清理：删 consensus 后检查域是否清空
        if tier == "consensus":
            remaining = conn.execute(
                "SELECT COUNT(*) FROM memory_facts WHERE tier='consensus' AND project=? AND deleted_at IS NULL",
                (project,)
            ).fetchone()[0]
            if remaining == 0:
                conn.execute("DELETE FROM project_refs WHERE ref_project=? AND ref_source='promote'", (project,))

        # ★ 项目全删检查：该项目的 q4 全没了 → 清理出向 promote refs
        if tier == "q4":
            remaining_q4 = conn.execute(
                "SELECT COUNT(*) FROM memory_facts WHERE tier='q4' AND project=? AND deleted_at IS NULL",
                (project,)
            ).fetchone()[0]
            if remaining_q4 == 0:
                conn.execute("DELETE FROM project_refs WHERE project=? AND ref_source='promote'", (project,))

        conn.commit()
        conn.close()
        return {"hard_deleted": n}

    def _promote(self, args):
        obs_id = args.get("obs_id")
        consensus_domain = args.get("consensus_domain")
        if not obs_id:
            return {"error": "obs_id is required"}
        if not consensus_domain:
            return {"error": "consensus_domain is required"}

        conn = self._get_conn()
        row = conn.execute("SELECT project FROM memory_facts WHERE obs_uuid=?", (obs_id,)).fetchone()
        if not row:
            conn.close()
            return {"error": "observation not found"}
        origin_project = row["project"]

        # 原地飞升：改 tier + project + origin_project
        conn.execute(
            "UPDATE memory_facts SET tier='consensus', project=?, origin_project=?, updated_at=CURRENT_TIMESTAMP WHERE obs_uuid=?",
            (consensus_domain, origin_project, obs_id)
        )
        # 建立血缘图谱
        conn.execute(
            "INSERT OR IGNORE INTO project_refs (project, ref_project, ref_source) VALUES (?, ?, 'promote')",
            (origin_project, consensus_domain)
        )
        conn.commit()
        conn.close()
        return {"promoted": obs_id, "tier": "consensus", "consensus_domain": consensus_domain,
                "origin_project": origin_project}

    def _demote(self, args):
        obs_id = args.get("obs_id")
        if not obs_id:
            return {"error": "obs_id is required"}

        conn = self._get_conn()
        row = conn.execute("SELECT origin_project, project FROM memory_facts WHERE obs_uuid=?", (obs_id,)).fetchone()
        if not row:
            conn.close()
            return {"error": "observation not found"}

        # ★ 溯源黑洞防护：origin_project 为空 = 已融合多源，拒绝降级
        if not row["origin_project"]:
            conn.close()
            return {"error": "溯源黑洞：该记录没有 origin_project（已在合并精炼中被清空）。"
                    "它已是融合后的多源共识，无法降级私有化。请用 mem_delete(confirm_consensus=true) 删除。"}

        origin = row["origin_project"]
        conn.execute(
            "UPDATE memory_facts SET tier='q4', project=?, origin_project='', updated_at=CURRENT_TIMESTAMP WHERE obs_uuid=?",
            (origin, obs_id)
        )
        # ★ 不清理 project_refs（防过桥抽板）
        conn.commit()
        conn.close()
        return {"demoted": obs_id, "tier": "q4", "project": origin}

    # ==================== 读取 ====================

    def _recall(self, args):
        query = args.get("query", "")
        project = args.get("current_project")
        min_sim = float(args.get("min_similarity", 0.5))
        total_limit = int(args.get("limit", 10))
        vec = self.embedder.embed(query)

        conn = self._get_conn()
        # 查引用的共识域
        refs = []
        if project:
            ref_rows = conn.execute("SELECT ref_project FROM project_refs WHERE project=?", (project,)).fetchall()
            refs = [r[0] for r in ref_rows]
        conn.close()

        # 单次查询同源 RRF：q4 + consensus 在同一候选池
        search_projects = ([project] if project else []) + refs
        ranked = self.searcher.hybrid_search_rrf(
            query, vec,
            projects=search_projects if search_projects else None,
            min_similarity=min_sim, limit=50
        )

        # ★ 三步法配额截取
        q4_min, cons_min = 2, 2
        q4_items = [x for x in ranked if x.get("tier") == "q4"]
        cons_items = [x for x in ranked if x.get("tier") == "consensus"]

        final = q4_items[:q4_min] + cons_items[:cons_min]
        remaining = q4_items[q4_min:] + cons_items[cons_min:]
        slots_left = total_limit - len(final)
        if slots_left > 0:
            remaining.sort(key=lambda x: x.get("score", 0), reverse=True)
            final.extend(remaining[:slots_left])

        final.sort(key=lambda x: x.get("score", 0), reverse=True)
        return {"results": final[:total_limit], "query": query, "count": len(final[:total_limit])}

    def _consensus_recall(self, args):
        query = args.get("query", "")
        min_sim = float(args.get("min_similarity", 0.5))
        limit = int(args.get("limit", 10))
        vec = self.embedder.embed(query)
        results = self.searcher.hybrid_search_rrf(
            query, vec, tiers=["consensus"], min_similarity=min_sim, limit=limit
        )
        return {"results": results, "query": query, "count": len(results)}

    def _search(self, args):
        query = args.get("query", "")
        project = args.get("project")
        obs_type = args.get("type")
        scope = args.get("scope")
        limit = int(args.get("limit", 20))
        conn = self._get_conn()
        if not query:
            sql = ("SELECT obs_uuid, project, topic_key, title, type, scope, tier, created_at "
                   "FROM memory_facts WHERE deleted_at IS NULL AND tier='q4'")
            params = []
            if project: sql += " AND project=?"; params.append(project)
            if obs_type: sql += " AND type=?"; params.append(obs_type)
            if scope: sql += " AND scope=?"; params.append(scope)
            sql += " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            return {"results": [dict(r) for r in rows], "count": len(rows)}
        safe = query.replace('"', '""')
        sql = ("SELECT mf.obs_uuid, mf.project, mf.topic_key, mf.title, mf.content, mf.type, mf.scope, mf.tier, mf.created_at "
               "FROM memory_facts_fts JOIN memory_facts mf ON mf.id = memory_facts_fts.rowid "
               "WHERE memory_facts_fts MATCH ? AND mf.deleted_at IS NULL AND mf.tier='q4'")
        params = [f'"{safe}"']
        if project: sql += " AND mf.project=?"; params.append(project)
        if obs_type: sql += " AND mf.type=?"; params.append(obs_type)
        if scope: sql += " AND mf.scope=?"; params.append(scope)
        sql += " ORDER BY bm25(memory_facts_fts) LIMIT ?"; params.append(limit)
        try:
            rows = conn.execute(sql, params).fetchall()
            res = [dict(r) for r in rows]
        except Exception:
            p = f"%{query}%"
            sql = ("SELECT obs_uuid, project, topic_key, title, content, type, scope, tier, created_at "
                   "FROM memory_facts WHERE (content LIKE ? OR title LIKE ?) AND deleted_at IS NULL AND tier='q4'")
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
        consensus_limit = int(args.get("consensus_limit", 5))
        conn = self._get_conn()

        # 1. 自身动态记忆
        own = conn.execute(
            "SELECT obs_uuid, project, topic_key, title, content, type, pinned, created_at "
            "FROM memory_facts WHERE deleted_at IS NULL AND project=? AND tier='q4' "
            "ORDER BY pinned DESC, created_at DESC LIMIT ?",
            (project, limit)
        ).fetchall()

        # 2. 查引用的共识域
        refs = conn.execute(
            "SELECT ref_project FROM project_refs WHERE project=?", (project,)
        ).fetchall()
        ref_list = [r[0] for r in refs]

        # 3. 加载引用的共识（防爆 top-N）
        consensus = []
        if ref_list:
            placeholders = ",".join("?" * len(ref_list))
            consensus = conn.execute(
                f"SELECT obs_uuid, project, topic_key, title, content, type, pinned, created_at "
                f"FROM memory_facts WHERE tier='consensus' AND project IN ({placeholders}) "
                f"AND deleted_at IS NULL ORDER BY pinned DESC, updated_at DESC LIMIT ?",
                ref_list + [consensus_limit]
            ).fetchall()

        conn.close()
        return {
            "project": project,
            "own_memories": [dict(r) for r in own],
            "consensus_memories": [dict(r) for r in consensus],
            "consensus_domains": ref_list,
            "own_count": len(own),
            "consensus_count": len(consensus)
        }

    def _list_projects(self, args):
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT project, COUNT(*) as n FROM memory_facts WHERE deleted_at IS NULL AND tier='q4' GROUP BY project ORDER BY n DESC"
        ).fetchall()
        conn.close()
        return {"projects": [dict(r) for r in rows]}

    def _list_consensus_projects(self, args):
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT project as consensus_domain, COUNT(*) as n "
            "FROM memory_facts WHERE tier='consensus' AND deleted_at IS NULL "
            "GROUP BY project ORDER BY n DESC"
        ).fetchall()
        conn.close()
        return {"consensus_domains": [dict(r) for r in rows]}

    def _add_consensus_ref(self, args):
        project = args.get("project")
        consensus_project = args.get("consensus_project")
        if not project or not consensus_project:
            return {"error": "project and consensus_project are required"}
        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO project_refs (project, ref_project, ref_source) VALUES (?, ?, 'manual')",
            (project, consensus_project)
        )
        n = conn.total_changes
        conn.commit()
        conn.close()
        return {"project": project, "consensus_project": consensus_project, "created": n > 0}

    def _consensus_health_check(self, args):
        consensus_domain = args.get("consensus_domain")
        conn = self._get_conn()
        if consensus_domain:
            rows = conn.execute(
                "SELECT obs_uuid, project, title, content FROM memory_facts "
                "WHERE tier='consensus' AND project=? AND deleted_at IS NULL",
                (consensus_domain,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT obs_uuid, project, title, content FROM memory_facts "
                "WHERE tier='consensus' AND deleted_at IS NULL"
            ).fetchall()
        conn.close()

        # 按共识域分组，embedding 两两比对
        from itertools import combinations
        domains = {}
        for r in rows:
            domains.setdefault(r["project"], []).append(dict(r))

        duplicates = []
        for domain, items in domains.items():
            if len(items) < 2:
                continue
            # 预算 embedding
            embeddings = []
            for item in items:
                vec = self.embedder.embed(item["title"] + " " + item["content"])
                embeddings.append(np.array(vec, dtype=np.float32))
            for i, j in combinations(range(len(items)), 2):
                sim = float(np.dot(embeddings[i], embeddings[j]) /
                            (np.linalg.norm(embeddings[i]) * np.linalg.norm(embeddings[j])))
                if sim > 0.85:
                    duplicates.append({
                        "domain": domain,
                        "obs_a": items[i]["obs_uuid"], "title_a": items[i]["title"],
                        "obs_b": items[j]["obs_uuid"], "title_b": items[j]["title"],
                        "similarity": round(sim, 3),
                        "suggestion": "内容高度相似，建议精炼合并：用 mem_update 融合内容并传 origin_project=''，随后 mem_delete 删除冗余旧版。"
                    })

        return {
            "duplicates": duplicates,
            "total_checked": len(rows),
            "needs_action": len(duplicates)
        }

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
