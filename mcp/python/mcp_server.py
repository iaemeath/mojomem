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
LOG_DBPATH = os.path.join(_ROOT, "call_log.db")

_REQUIRED_COLUMNS = {
    "title": "TEXT DEFAULT ''",
    "type": "TEXT DEFAULT 'manual'",
    "tier": "TEXT NOT NULL DEFAULT 'q4'",
    "origin_project": "TEXT DEFAULT ''",
    "content_hash": "TEXT DEFAULT ''",
    "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
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
            "add_consensus_ref", "list_consensus_projects", "mem_get_full",
            "cross_project_health_check", "mem_consolidate_project",
        }
        # 调用日志：每次 MCP 工具调用的审计记录，供后续优化分析
        self._session_tag = time.strftime("%Y%m%d_%H%M%S")
        self._log_db_ready = False

    def _get_conn(self):
        conn = sqlite3.connect(DBPATH)
        conn.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(conn)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_log_conn(self):
        """调用日志库连接（独立文件，WAL 模式，不加载 vec 扩展）。"""
        conn = sqlite3.connect(LOG_DBPATH)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_log_db(self):
        """初始化调用日志库：建表、开 WAL、清理 90 天前旧日志。"""
        try:
            conn = self._get_log_conn()
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tool_call_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    tool_name   TEXT NOT NULL,
                    source      TEXT NOT NULL DEFAULT 'local',
                    duration_ms INTEGER DEFAULT 0,
                    success     INTEGER NOT NULL DEFAULT 1,
                    error_msg   TEXT DEFAULT '',
                    arg_summary TEXT DEFAULT '',
                    resp_size   INTEGER DEFAULT 0,
                    session_tag TEXT DEFAULT ''
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_log_ts ON tool_call_log(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_log_tool ON tool_call_log(tool_name)")
            # 清理 90 天前的日志
            conn.execute("DELETE FROM tool_call_log WHERE ts < datetime('now', '-90 days')")
            conn.commit()
            conn.close()
            self._log_db_ready = True
        except Exception as e:
            print(f"[init_log] failed: {e}", file=sys.stderr)
            self._log_db_ready = False

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

        # 一次性迁移：旧版 _ 前缀去前缀（v3.0 不再用 _ 前缀区分共识域）
        try:
            n = conn.execute(
                "UPDATE memory_facts SET project=SUBSTR(project,2) "
                "WHERE project LIKE '\\_%' ESCAPE '\\' AND tier='consensus' AND deleted_at IS NULL"
            ).rowcount
            if n:
                print(f"[init] stripped _ prefix from {n} consensus project names", file=sys.stderr)
            conn.execute(
                "UPDATE project_refs SET ref_project=SUBSTR(ref_project,2) "
                "WHERE ref_project LIKE '\\_%' ESCAPE '\\'"
            )
        except Exception as e:
            print(f"[init] _ prefix migration skipped: {e}", file=sys.stderr)

        conn.commit()
        try:
            conn.execute("VACUUM")
        except Exception as e:
            print(f"[init] vacuum failed: {e}", file=sys.stderr)

        conn.close()

        # 初始化调用日志库（独立文件，不影响核心库）
        self._init_log_db()

        return {"protocolVersion": "2024-11-05", "serverInfo": {"name": "qmem-mcp", "version": "3.3"}, "capabilities": {"tools": {}}}

    def _tools_list(self):
        local_tools = [
            {"name": "mem_save", "description": "保存记忆（Push）。type 为必填（决定生命周期），topic_key 可选（仅记忆多时用于主题分类）。topic_key 命中时自动 upsert（仅 tier=q4 范围）。★ 写入门禁：纯新增时若本项目已有相似度>0.85 的 q4 记忆，返回 candidates 拦截（防污染）；确认是新主题请传 force=true 放行，是同主题更新请改用 mem_update。", "inputSchema": {"type": "object", "properties": {"project_id": {"type": "string"}, "topic_key": {"type": "string", "description": "可选，主题分类（arch/workflow/m1.4 等）。记忆少时留空"}, "content": {"type": "string"}, "title": {"type": "string"}, "type": {"type": "string", "enum": ["reference", "progress", "decision", "bugfix", "learning", "manual"], "description": "必填。reference=稳定知识, progress=易过期进度(超30天需复审), decision=决策, bugfix=已修, learning=经验, manual=手动"}, "force": {"type": "boolean", "description": "默认 false。写入门禁拦截后，确认确为新主题时传 true 放行（绕过近邻检查）"}}, "required": ["project_id", "content", "type"]}},
            {"name": "mem_recall", "description": "RRF 混合检索：搜当前项目动态记忆 + 引用的共识域（单次同源 RRF，三步法配额截取）。", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "current_project": {"type": "string"}, "min_similarity": {"type": "number", "default": 0.5}, "limit": {"type": "integer", "default": 10}}, "required": ["query"]}},
            {"name": "mem_search", "description": "精确/过滤查找：按 project/type 过滤动态记忆（tier=q4）。", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "project": {"type": "string"}, "type": {"type": "string"}, "limit": {"type": "integer", "default": 20}}, "required": []}},
            {"name": "mem_update", "description": "按 obs_id 局部更新（content/title/type），自动重算向量。操作 consensus 行需 confirm_consensus=true；改 consensus content 时需声明 origin_project 去留。", "inputSchema": {"type": "object", "properties": {"obs_id": {"type": "string"}, "content": {"type": "string"}, "title": {"type": "string"}, "type": {"type": "string"}, "confirm_consensus": {"type": "boolean"}, "origin_project": {"type": "string"}}, "required": ["obs_id"]}},
            {"name": "mem_context", "description": "开场召回：加载项目自身动态记忆 + 引用的共识。返回摘要索引（title + content前100字 + obs_id），不再返回全文。用 mem_get_full(obs_id=) 拉指定记忆的完整内容。★ 附带 review_queue：本项目超 30 天的 progress 记忆（可能过时），开场顺手用 mem_get_full 核实，已推进的 upsert 更新、已作废的软删。", "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}, "consensus_limit": {"type": "integer", "default": 5, "description": "共识域返回条数上限（防爆）"}}, "required": ["project"]}},
            {"name": "mem_get_full", "description": "按 obs_id 拉取记忆完整内容。从 mem_context 返回的摘要索引中拿到 obs_id 后，用此工具拉全文。支持批量（逗号分隔多个 obs_id）。", "inputSchema": {"type": "object", "properties": {"obs_id": {"type": "string", "description": "单个 obs_id 或逗号分隔的多个 obs_id（上限20条）"}}, "required": ["obs_id"]}},
            {"name": "mem_delete", "description": "默认软删除记忆（标记 deleted_at，可恢复，防误删）。传 hard=true 走物理删除（清 FTS/向量索引）。删 consensus 行需 confirm_consensus=true。", "inputSchema": {"type": "object", "properties": {"obs_id": {"type": "string"}, "confirm_consensus": {"type": "boolean"}, "hard": {"type": "boolean", "description": "默认 false 软删除；true 物理删除"}}, "required": ["obs_id"]}},
            {"name": "memory_promote", "description": "将经验提取为跨项目共识：UPDATE tier=consensus + project 改域名 + origin_project 记录来源 + 建 ref。不挪数据不重算向量。", "inputSchema": {"type": "object", "properties": {"obs_id": {"type": "string"}, "consensus_domain": {"type": "string", "description": "共识域名（普通名称，无前缀），如 java-cloud-common"}}, "required": ["obs_id", "consensus_domain"]}},
            {"name": "memory_demote", "description": "将共识降级回动态记忆：UPDATE tier=q4 + project 改回 origin_project。origin_project 为空（已融合多源）则拒绝降级。", "inputSchema": {"type": "object", "properties": {"obs_id": {"type": "string"}}, "required": ["obs_id"]}},
            {"name": "consensus_recall", "description": "专搜共识库（tier=consensus）。查阅通用经验/架构陷阱/踩坑根因时调用。", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "min_similarity": {"type": "number", "default": 0.5}, "limit": {"type": "integer", "default": 10}}, "required": ["query"]}},
            {"name": "consensus_health_check", "description": "检查共识域健康度：找出内容高度相似的共识记录（embedding>0.85），提示 AI 精炼合并。建议 promote 后调用。", "inputSchema": {"type": "object", "properties": {"consensus_domain": {"type": "string"}}, "required": []}},
            {"name": "add_consensus_ref", "description": "手动建立项目→共识域引用（ref_source=manual）。项目未 promote 过但需要知道相关共识时使用。", "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}, "consensus_project": {"type": "string"}}, "required": ["project", "consensus_project"]}},
            {"name": "list_consensus_projects", "description": "列出所有共识域（tier=consensus 的 project）及其记忆数，供 promote 选择目标。", "inputSchema": {"type": "object", "properties": {}}, },
            {"name": "mem_list_projects", "description": "列出所有动态记忆的 project 及其记忆数。", "inputSchema": {"type": "object", "properties": {}}, },
            {"name": "init_project_context", "description": "探测目录身份线索（git remote/pom/package.json），生成 Q3 户口本。", "inputSchema": {"type": "object", "properties": {"directory": {"type": "string"}}, "required": ["directory"]}},
            {"name": "cross_project_health_check", "description": "检测跨项目 q4 记忆的语义重复（embedding 相似度>阈值），发现可提取为共识的候选。仅检测不同 project 之间的高相似对，提示 AI 用 memory_promote 提取。", "inputSchema": {"type": "object", "properties": {"threshold": {"type": "number", "default": 0.85, "description": "相似度阈值，默认0.85"}, "limit": {"type": "integer", "default": 20, "description": "返回的高相似对数量上限"}}, "required": []}},
            {"name": "mem_consolidate_project", "description": "★ 单项目内高相似簇检测：找出同一 project 内互相相似的 q4 记忆对（embedding>阈值），提示 AI 合并精炼以消化单项目堆积（如 changzhou 39 条）。检测到后用 mem_update 融合内容（传 origin_project='' 视为融合多源），再 mem_delete 软删冗余条目。", "inputSchema": {"type": "object", "properties": {"project": {"type": "string", "description": "要检测的单个 project 名"}, "threshold": {"type": "number", "default": 0.85, "description": "相似度阈值，默认0.85"}, "limit": {"type": "integer", "default": 30, "description": "返回的高相似对数量上限"}}, "required": ["project"]}},
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
        source = "local" if name in self.local_tools else "cbm"
        t0 = time.time()
        success, error_msg, resp_size = True, "", 0
        try:
            result = self._dispatch_tool(name, args, params)
            try:
                resp_size = len(json.dumps(result, ensure_ascii=False))
            except Exception:
                resp_size = 0
            return result
        except Exception as e:
            success = False
            error_msg = str(e)[:500]
            raise
        finally:
            duration_ms = int((time.time() - t0) * 1000)
            try:
                self._log_call(name, source, duration_ms, success, error_msg, args, resp_size)
            except Exception as log_err:
                print(f"[log] _log_call failed: {log_err}", file=sys.stderr)

    def _log_call(self, tool_name, source, duration_ms, success, error_msg, args, resp_size):
        """记录一次 MCP 工具调用到 call_log.db。失败静默，绝不影响工具返回。"""
        if not self._log_db_ready:
            self._init_log_db()
            if not self._log_db_ready:
                return
        # args JSON 序列化后截断至 500 字符
        try:
            arg_summary = json.dumps(args, ensure_ascii=False)[:500]
        except Exception:
            arg_summary = str(args)[:500]
        conn = None
        try:
            conn = self._get_log_conn()
            conn.execute(
                "INSERT INTO tool_call_log(tool_name, source, duration_ms, success, error_msg, arg_summary, resp_size, session_tag) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (tool_name, source, duration_ms, 1 if success else 0, error_msg, arg_summary, resp_size, self._session_tag)
            )
            conn.commit()
        except Exception as e:
            print(f"[log] insert failed: {e}", file=sys.stderr)
        finally:
            if conn:
                conn.close()

    def _dispatch_tool(self, name, args, params):
        """实际工具分发逻辑（从原 _tools_call 提取，零逻辑改动）。"""
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
            "mem_get_full": lambda: self._get_full(args),
            "mem_delete": lambda: self._delete(args),
            "memory_promote": lambda: self._promote(args),
            "memory_demote": lambda: self._demote(args),
            "consensus_recall": lambda: self._consensus_recall(args),
            "consensus_health_check": lambda: self._consensus_health_check(args),
            "add_consensus_ref": lambda: self._add_consensus_ref(args),
            "list_consensus_projects": lambda: self._list_consensus_projects(args),
            "mem_list_projects": lambda: self._list_projects(args),
            "init_project_context": lambda: self._init_project_context(args),
            "cross_project_health_check": lambda: self._cross_project_health_check(args),
            "mem_consolidate_project": lambda: self._consolidate_project(args),
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
        force = bool(args.get("force"))  # ★ 门禁绕过：AI 确认确实是新主题时传 force=true
        if not project_id or not content:
            return {"error": "project_id and content are required"}
        if not obs_type or obs_type == "manual" and not args.get("type"):
            return {"error": "type is required (reference/progress/decision/bugfix/learning/manual)"}

        # ★ 共识域守卫：project_id 是已有共识域名时，拦截 q4 写入防孤儿
        conn = self._get_conn()
        is_consensus_domain = conn.execute(
            "SELECT 1 FROM memory_facts WHERE project=? AND tier='consensus' AND deleted_at IS NULL LIMIT 1",
            (project_id,)
        ).fetchone()
        if is_consensus_domain:
            conn.close()
            return {
                "warning": (
                    f"'{project_id}' 是共识域（已有 tier=consensus 记忆）。"
                    f"直接用 mem_save 写入会产生 tier=q4 的孤儿记忆（不被任何项目召回）。"
                    f"共识域内容的正确写入方式：① 先用 mem_save 写到来源项目，"
                    f"再用 memory_promote 提取到共识域；"
                    f"② 或用 list_consensus_projects() 确认共识域名后，"
                    f"用 mem_update 更新已有共识条目。"
                )
            }

        content_hash = hashlib.sha256((title + content).encode("utf-8")).hexdigest()[:16]
        vec = self.embedder.embed(title + " " + content)
        vec_bytes = np.array(vec, dtype=np.float32).tobytes()

        # conn 已在共识域守卫处打开，复用
        # upsert：同 project+topic_key+tier=q4 存在则更新（★ tier 守卫，不碰 consensus 行）
        existing = None
        if topic_key:
            existing = conn.execute(
                "SELECT id, obs_uuid FROM memory_facts WHERE project=? AND topic_key=? "
                "AND tier='q4' AND deleted_at IS NULL",
                (project_id, topic_key)
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
            conn.commit()
            conn.close()
            return {"obs_id": oid, "action": "updated", "id": fid, "via": "topic_key_upsert"}

        # ★ 写入门禁（对抗记忆污染源头）：纯新增路径才检查。
        # 用新内容的向量在【本项目 q4】里查近邻，命中高相似已有记忆时
        # 返回候选让 AI 决策（upsert 哪条 / 确实新建），而不是无脑落库。
        # 仅查本项目 q4（跨项目重叠归 cross_project_health_check 管；consensus 归 promote 流程管）。
        if not force:
            dup = self._nearest_neighbor(conn, vec, project_id, tier="q4", threshold=0.85, limit=3)
            if dup:
                conn.close()
                cands = [
                    {"obs_id": d["obs_uuid"], "title": d["title"], "type": d["type"],
                     "topic_key": d["topic_key"], "similarity": round(d["sim"], 3)}
                    for d in dup
                ]
                return {
                    "warning": "写入门禁：本项目已有高度相似的 q4 记忆。直接新建会产生冗余/污染。",
                    "candidates": cands,
                    "hint": (
                        "请决策：① 这是同一主题的更新 → 用 mem_update(obs_id=候选, ...) 更新该条；"
                        "② 确实是新主题（候选只是表面相似）→ 重新调用 mem_save 并传 force=true 放行。"
                    ),
                }

        oid = uuid.uuid4().hex[:12]
        conn.execute(
            "INSERT INTO memory_facts(obs_uuid, project, topic_key, title, content, type, content_hash) "
            "VALUES(?,?,?,?,?,?,?)",
            (oid, project_id, topic_key, title, content, obs_type, content_hash)
        )
        fid = conn.execute("SELECT id FROM memory_facts WHERE obs_uuid=?", (oid,)).fetchone()[0]
        conn.execute("INSERT INTO memory_vectors(rowid, embedding) VALUES (?, ?)", (fid, vec_bytes))
        conn.commit()
        conn.close()
        return {"obs_id": oid, "action": "created", "id": fid}

    def _nearest_neighbor(self, conn, query_vec, project, tier="q4", threshold=0.85, limit=3):
        """写入门禁/去重共用的近邻查询：在指定 project+tier 范围内找 query_vec 的相似记忆。
        返回相似度 > threshold 的存活记忆列表（含 obs_uuid/title/type/topic_key/sim），按相似度降序，最多 limit 条。
        用 vec_distance_cosine 函数式 API（与 search_rrf.semantic_search 同款），先按 project+tier 过滤再算距离排序。
        vec0 的 MATCH/k 语法不能与普通列 WHERE 混用，故不采用。"""
        import json as _json
        try:
            v = _json.dumps(list(query_vec)) if isinstance(query_vec, (list, tuple)) else query_vec
            rows = conn.execute(
                "SELECT mf.obs_uuid, mf.title, mf.type, mf.topic_key, "
                "vec_distance_cosine(mv.embedding, ?) as distance "
                "FROM memory_vectors mv JOIN memory_facts mf ON mv.rowid = mf.id "
                "WHERE mf.project=? AND mf.tier=? AND mf.deleted_at IS NULL "
                "ORDER BY distance LIMIT ?",
                (v, project, tier, max(limit * 3, 10))
            ).fetchall()
        except Exception as e:
            print(f"[nearest_neighbor] query failed, gate disabled: {e}", file=sys.stderr)
            return []  # 向量查询失败不应阻断写入，降级为不检查（但打印告警，便于发现门禁失效）

        results = []
        for r in rows:
            sim = 1.0 - float(r["distance"])  # vec0 cosine distance ∈ [0,2]，相似度 = 1 - distance
            if sim <= threshold:
                continue
            results.append({
                "obs_uuid": r["obs_uuid"], "title": r["title"],
                "type": r["type"], "topic_key": r["topic_key"], "sim": sim,
            })
        results.sort(key=lambda x: x["sim"], reverse=True)
        return results[:limit]

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
        hard = bool(args.get("hard"))  # 默认软删除（可恢复）；hard=true 走物理删除
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

        # ★ 用 cursor.rowcount 精确计数目标表受影响行数。
        # conn.total_changes 是累计值，会把 FTS/向量同步触发器(trg_fts_*/trg_vector_*)
        # 引发的额外变更一起算进去（软删 1 行实际返回 5），误导调用方。
        if hard:
            # 物理删除：清 memory_facts 行 + 触发器自动清 FTS/向量
            cur = conn.execute("DELETE FROM memory_facts WHERE obs_uuid=?", (obs_id,))
            n = cur.rowcount
        else:
            # ★ 软删除：标记 deleted_at，可恢复（误删防护）
            # 所有检索路径均带 deleted_at IS NULL 守卫，软删行不会被命中
            cur = conn.execute(
                "UPDATE memory_facts SET deleted_at=CURRENT_TIMESTAMP WHERE obs_uuid=? AND deleted_at IS NULL",
                (obs_id,)
            )
            n = cur.rowcount

        # ★ 空域清理：删 consensus 后检查域是否清空（按 deleted_at IS NULL 计存活）
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
        return {"soft_deleted" if not hard else "hard_deleted": n, "hard": hard}

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
        final = final[:total_limit]

        # ★ 去重 + 质量信号：堵检索出口污染
        final = self._enrich_results(final, dedup_threshold=0.9)
        return {"results": final, "query": query, "count": len(final)}

    def _enrich_results(self, results, dedup_threshold=0.9):
        """检索结果后处理（去重 + 质量信号）。_recall / _context / _consensus_recall 共用。
        1) 近邻去重：结果集内两两相似度 > dedup_threshold 的，只留 score 最高的那条，其余标记为被去重。
        2) staleness：progress 类距今天数 + 是否超 30 天复审线。
        3) is_superseded：本项目内是否有同 topic_key 且 updated_at 更大的存活记忆覆盖它。
        全程异常容错，绝不阻断主检索返回。"""
        import datetime as _dt
        import numpy as _np
        if not results:
            return results
        conn = None
        try:
            conn = self._get_conn()
            obs_ids = [r.get("obs_uuid") for r in results if r.get("obs_uuid")]
            # 批量取 updated_at + topic_key + project + type（用于质量信号）
            meta = {}
            if obs_ids:
                placeholders = ",".join("?" * len(obs_ids))
                rows = conn.execute(
                    f"SELECT obs_uuid, topic_key, project, type, created_at, updated_at "
                    f"FROM memory_facts WHERE obs_uuid IN ({placeholders}) AND deleted_at IS NULL",
                    obs_ids
                ).fetchall()
                for r in rows:
                    meta[r["obs_uuid"]] = dict(r)
            # 批量取向量（用于去重两两点积）
            id_to_rowid = {}
            if obs_ids:
                placeholders = ",".join("?" * len(obs_ids))
                vrows = conn.execute(
                    f"SELECT mf.obs_uuid, mf.id, mv.embedding FROM memory_facts mf "
                    f"JOIN memory_vectors mv ON mv.rowid = mf.id "
                    f"WHERE mf.obs_uuid IN ({placeholders})",
                    obs_ids
                ).fetchall()
                for r in vrows:
                    id_to_rowid[r["obs_uuid"]] = (r["id"], r["embedding"])
        except Exception as e:
            print(f"[enrich] meta/vector fetch failed, skip enrichment: {e}", file=sys.stderr)
            if conn:
                conn.close()
            return results
        if conn:
            conn.close()

        # —— 质量信号 ——
        def days_since(ts):
            if not ts:
                return None
            try:
                # SQLite CURRENT_TIMESTAMP 格式 'YYYY-MM-DD HH:MM:SS' 或 'YYYY-MM-DDTHH:MM:SS'
                s = str(ts).replace("T", " ")[:19]
                d = _dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
                return (_dt.datetime.now() - d).days
            except Exception:
                return None

        enriched = []
        for r in results:
            oid = r.get("obs_uuid")
            m = meta.get(oid, {})
            rtype = r.get("type") or m.get("type", "")
            age = days_since(m.get("updated_at") or m.get("created_at"))
            if rtype == "progress":
                r["staleness"] = {"age_days": age, "overdue": bool(age is not None and age > 30)}
            else:
                r["staleness"] = None
            enriched.append(r)

        # —— is_superseded：本项目内同 topic_key 且 updated_at 更大的存活记忆 ——
        # 用 meta 里已有的 updated_at 做参数化比较，避免相关子查询。
        try:
            conn2 = self._get_conn()
            for r in enriched:
                oid = r.get("obs_uuid")
                m = meta.get(oid, {})
                tk = m.get("topic_key", "")
                proj = m.get("project", "")
                ts = m.get("updated_at") or "1970-01-01"
                if not tk:
                    r["is_superseded"] = False
                    continue
                sup = conn2.execute(
                    "SELECT 1 FROM memory_facts WHERE project=? AND topic_key=? AND tier='q4' "
                    "AND deleted_at IS NULL AND obs_uuid<>? AND updated_at > ? LIMIT 1",
                    (proj, tk, oid, ts)
                ).fetchone()
                r["is_superseded"] = bool(sup)
            conn2.close()
        except Exception as e:
            print(f"[enrich] superseded check failed: {e}", file=sys.stderr)
            for r in enriched:
                r.setdefault("is_superseded", False)

        # —— 近邻去重：两两相似度 > threshold 的，留 score 最高的 ——
        vecs = {}
        for r in enriched:
            oid = r.get("obs_uuid")
            if oid in id_to_rowid:
                _, emb = id_to_rowid[oid]
                v = _np.frombuffer(emb, dtype=_np.float32)
                n = _np.linalg.norm(v)
                if n > 0:
                    vecs[oid] = v / n
        # 按 score 降序，高分优先保留
        enriched.sort(key=lambda x: x.get("score", 0), reverse=True)
        kept = []
        suppressed = []
        for r in enriched:
            oid = r.get("obs_uuid")
            if oid not in vecs:
                kept.append(r)
                continue
            dup_with_kept = False
            for k in kept:
                ko = k.get("obs_uuid")
                if ko not in vecs:
                    continue
                sim = float(_np.dot(vecs[oid], vecs[ko]))
                if sim > dedup_threshold:
                    dup_with_kept = True
                    break
            if dup_with_kept:
                r["deduped"] = True  # 被更高分的同义条目压下
                suppressed.append(r)
            else:
                kept.append(r)
        return kept

    def _consensus_recall(self, args):
        query = args.get("query", "")
        min_sim = float(args.get("min_similarity", 0.5))
        limit = int(args.get("limit", 10))
        vec = self.embedder.embed(query)
        results = self.searcher.hybrid_search_rrf(
            query, vec, tiers=["consensus"], min_similarity=min_sim, limit=limit
        )
        # ★ 去重 + 质量信号（共识库也可能因多次 promote 产生近似条目）
        results = self._enrich_results(results, dedup_threshold=0.9)
        return {"results": results, "query": query, "count": len(results)}

    def _search(self, args):
        query = args.get("query", "")
        project = args.get("project")
        obs_type = args.get("type")
        limit = int(args.get("limit", 20))
        conn = self._get_conn()
        if not query:
            sql = ("SELECT obs_uuid, project, topic_key, title, type, tier, created_at "
                   "FROM memory_facts WHERE deleted_at IS NULL AND tier='q4'")
            params = []
            if project: sql += " AND project=?"; params.append(project)
            if obs_type: sql += " AND type=?"; params.append(obs_type)
            sql += " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            return {"results": [dict(r) for r in rows], "count": len(rows)}
        safe = query.replace('"', '""')
        sql = ("SELECT mf.obs_uuid, mf.project, mf.topic_key, mf.title, mf.content, mf.type, mf.tier, mf.created_at "
               "FROM memory_facts_fts JOIN memory_facts mf ON mf.id = memory_facts_fts.rowid "
               "WHERE memory_facts_fts MATCH ? AND mf.deleted_at IS NULL AND mf.tier='q4'")
        params = [f'"{safe}"']
        if project: sql += " AND mf.project=?"; params.append(project)
        if obs_type: sql += " AND mf.type=?"; params.append(obs_type)
        sql += " ORDER BY bm25(memory_facts_fts) LIMIT ?"; params.append(limit)
        try:
            rows = conn.execute(sql, params).fetchall()
            res = [dict(r) for r in rows]
        except Exception:
            p = f"%{query}%"
            sql = ("SELECT obs_uuid, project, topic_key, title, content, type, tier, created_at "
                   "FROM memory_facts WHERE (content LIKE ? OR title LIKE ?) AND deleted_at IS NULL AND tier='q4'")
            params = [p, p]
            if project: sql += " AND project=?"; params.append(project)
            if obs_type: sql += " AND type=?"; params.append(obs_type)
            sql += " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            res = [dict(r) for r in rows]
        conn.close()
        return {"results": res, "count": len(res)}

    def _context(self, args):
        """开场召回：返回摘要索引（title + content前100字），不再返回全文。
        q4 动态记忆全量返回（硬上限100），共识域仍防爆截断。
        AI 看完索引后用 mem_get_full(obs_id=) 拉指定记忆全文。
        ★ 拉模型过期复审：附带 review_queue（本项目 type=progress 且超 30 天的存活记忆），
        提示 AI 核实后 upsert 更新或软删。零定时任务，寄生系统友好。"""
        project = args.get("project")
        consensus_limit = int(args.get("consensus_limit", 5))
        MAX_OWN = 100  # q4 硬保护上限
        PREVIEW_LEN = 100
        PROGRESS_REVIEW_DAYS = 30  # progress 类复审线（天）

        conn = self._get_conn()

        # 1. 自身动态记忆（全量，不再 limit 截断）
        own = conn.execute(
            "SELECT obs_uuid, project, topic_key, title, content, type, created_at "
            "FROM memory_facts WHERE deleted_at IS NULL AND project=? AND tier='q4' "
            "ORDER BY created_at DESC LIMIT ?",
            (project, MAX_OWN)
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
                f"SELECT obs_uuid, project, topic_key, title, content, type, created_at "
                f"FROM memory_facts WHERE tier='consensus' AND project IN ({placeholders}) "
                f"AND deleted_at IS NULL ORDER BY updated_at DESC LIMIT ?",
                ref_list + [consensus_limit]
            ).fetchall()

        # 4. ★ 过期复审队列：本项目 type=progress 且超 PROGRESS_REVIEW_DAYS 天未更新的存活记忆
        review_queue = conn.execute(
            "SELECT obs_uuid, title, topic_key, type, created_at, updated_at "
            "FROM memory_facts "
            "WHERE deleted_at IS NULL AND project=? AND tier='q4' AND type='progress' "
            f"AND updated_at < datetime('now', ?) "
            "ORDER BY updated_at ASC",
            (project, f'-{PROGRESS_REVIEW_DAYS} days')
        ).fetchall()

        conn.close()

        # 摘要化：content → content_preview + has_more
        def summarize(rows):
            result = []
            for r in rows:
                d = dict(r)
                full = d.pop("content", "")
                d["content_preview"] = full[:PREVIEW_LEN] + ("..." if len(full) > PREVIEW_LEN else "")
                d["has_more"] = len(full) > PREVIEW_LEN
                result.append(d)
            return result

        return {
            "project": project,
            "own_memories": summarize(own),
            "consensus_memories": summarize(consensus),
            "consensus_domains": ref_list,
            "own_count": len(own),
            "consensus_count": len(consensus),
            "truncated": len(own) >= MAX_OWN,
            "review_queue": [dict(r) for r in review_queue],
            "review_count": len(review_queue),
        }

    def _get_full(self, args):
        """按 obs_id 拉取记忆完整内容。支持批量（逗号分隔），上限 20 条。"""
        raw = args.get("obs_id", "")
        if isinstance(raw, list):
            ids = [str(x).strip() for x in raw if str(x).strip()]
        else:
            ids = [x.strip() for x in str(raw).split(",") if x.strip()]
        if not ids:
            return {"error": "obs_id is required"}
        MAX_BATCH = 20
        truncated_by_batch = len(ids) > MAX_BATCH
        ids = ids[:MAX_BATCH]

        conn = self._get_conn()
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT obs_uuid, project, topic_key, title, content, type, tier, "
            f"origin_project, created_at, updated_at "
            f"FROM memory_facts WHERE obs_uuid IN ({placeholders}) AND deleted_at IS NULL",
            ids
        ).fetchall()
        conn.close()

        found = {r["obs_uuid"] for r in rows}
        not_found = [i for i in ids if i not in found]

        return {
            "memories": [dict(r) for r in rows],
            "count": len(rows),
            "not_found": not_found,
            "batch_truncated": truncated_by_batch,
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
        cur = conn.execute(
            "INSERT OR IGNORE INTO project_refs (project, ref_project, ref_source) VALUES (?, ?, 'manual')",
            (project, consensus_project)
        )
        n = cur.rowcount  # INSERT OR IGNORE 命中已存在时为 0；rowcount 比 total_changes（累计值）语义更准
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

    def _cross_project_health_check(self, args):
        """检测跨项目 q4 记忆的语义重复（embedding 相似度 > 阈值）。
        仅检测不同 project 之间的高相似对，发现可提取为共识的候选。"""
        threshold = float(args.get("threshold", 0.85))
        limit = int(args.get("limit", 20))

        conn = self._get_conn()
        rows = conn.execute(
            "SELECT mf.id, mf.obs_uuid, mf.project, mf.title, mf.type, mv.embedding "
            "FROM memory_facts mf JOIN memory_vectors mv ON mv.rowid = mf.id "
            "WHERE mf.deleted_at IS NULL AND mf.tier = 'q4'"
        ).fetchall()
        conn.close()

        # 预算归一化向量
        vecs, meta = {}, {}
        for r in rows:
            v = np.frombuffer(r["embedding"], dtype=np.float32)
            norm = np.linalg.norm(v)
            if norm > 0:
                vecs[r["id"]] = v / norm
                meta[r["id"]] = dict(r)

        ids = list(vecs.keys())
        pairs = []
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                if meta[a]["project"] == meta[b]["project"]:
                    continue  # 跳过同项目
                sim = float(np.dot(vecs[a], vecs[b]))
                if sim >= threshold:
                    pairs.append((sim, meta[a], meta[b]))

        pairs.sort(reverse=True)
        pairs = pairs[:limit]

        results = []
        for sim, ma, mb in pairs:
            results.append({
                "similarity": round(sim, 3),
                "project_a": ma["project"], "obs_a": ma["obs_uuid"],
                "title_a": ma["title"], "type_a": ma["type"],
                "project_b": mb["project"], "obs_b": mb["obs_uuid"],
                "title_b": mb["title"], "type_b": mb["type"],
                "suggestion": (
                    f"跨项目语义重复。若内容是同一知识（非同构模板），"
                    f"建议用 memory_promote 将较完善的一方提取到共识域，"
                    f"再用 add_consensus_ref 让另一方引用。"
                )
            })

        return {
            "cross_project_duplicates": results,
            "total_checked": len(rows),
            "pairs_found": len(results),
            "threshold": threshold,
        }

    def _consolidate_project(self, args):
        """★ 单项目内高相似簇检测：找出同一 project 内互相相似的 q4 记忆对，
        提示 AI 合并精炼以消化单项目堆积。cross_project_health_check 的单项目版。
        检测后建议用 mem_update 融合 + mem_delete 软删冗余。"""
        project = args.get("project")
        if not project:
            return {"error": "project is required"}
        threshold = float(args.get("threshold", 0.85))
        limit = int(args.get("limit", 30))

        conn = self._get_conn()
        rows = conn.execute(
            "SELECT mf.id, mf.obs_uuid, mf.project, mf.topic_key, mf.title, mf.type, "
            "mf.created_at, mf.updated_at, mv.embedding "
            "FROM memory_facts mf JOIN memory_vectors mv ON mv.rowid = mf.id "
            "WHERE mf.deleted_at IS NULL AND mf.tier = 'q4' AND mf.project = ?",
            (project,)
        ).fetchall()
        conn.close()

        # 预算归一化向量
        vecs, meta = {}, {}
        for r in rows:
            v = np.frombuffer(r["embedding"], dtype=np.float32)
            norm = np.linalg.norm(v)
            if norm > 0:
                vecs[r["id"]] = v / norm
                meta[r["id"]] = dict(r)

        ids = list(vecs.keys())
        pairs = []
        from itertools import combinations
        for i, j in combinations(range(len(ids)), 2):
            a, b = ids[i], ids[j]
            sim = float(np.dot(vecs[a], vecs[b]))
            if sim >= threshold:
                # 新写的排前面（更适合作为合并后保留的版本）
                ua = str(meta[a].get("updated_at") or "")
                ub = str(meta[b].get("updated_at") or "")
                keep_a = ua >= ub
                pairs.append((sim, meta[a], meta[b], keep_a))

        pairs.sort(key=lambda x: x[0], reverse=True)
        pairs = pairs[:limit]

        results = []
        for sim, ma, mb, keep_a in pairs:
            keeper, redundant = (ma, mb) if keep_a else (mb, ma)
            results.append({
                "similarity": round(sim, 3),
                "keep_obs_id": keeper["obs_uuid"], "keep_title": keeper["title"],
                "keep_updated": keeper.get("updated_at"),
                "redundant_obs_id": redundant["obs_uuid"], "redundant_title": redundant["title"],
                "redundant_updated": redundant.get("updated_at"),
                "suggestion": (
                    "单项目内语义重复。建议：用 mem_get_full 拉两条全文核实，"
                    "若确为同主题 → 用 mem_update(obs_id=keep_obs_id) 融合内容，"
                    "再 mem_delete(obs_id=redundant_obs_id) 软删冗余（可恢复）。"
                )
            })

        return {
            "project": project,
            "intra_project_duplicates": results,
            "total_checked": len(rows),
            "pairs_found": len(results),
            "threshold": threshold,
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
