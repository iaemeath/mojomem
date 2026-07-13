# mcp_server.mojo
# Main MCP server entry point.

from std.collections import List
from std.memory import UnsafePointer, alloc

from json_utils import (
    json_get_string, json_get_int, json_get_float, json_get_id, json_get_obj,
    json_obj, json_kv, json_kv_str, json_kv_int, json_arr, json_str, json_kv_bool
)
from sqlite_ffi import SQLiteDB, SQLITE_ROW
from ort_ffi import OrtSession
from tokenizer import WordPieceTokenizer

comptime DbPtr = UnsafePointer[UInt8, MutAnyOrigin]

# ── Utilities ─────────────────────────────────────────────────────────────────

fn generate_uuid(c: Int) -> String:
    """Generate a pseudo-UUID string based on a counter."""
    var hex_chars = "0123456789abcdef"
    var res = String()
    
    # 12 static characters
    res += "a1b2c3d4e5f6"
    
    # 4 characters from counter
    var val = c
    for _ in range(4):
        var idx = Int(val & 15)
        res += String(StringSlice(ptr=hex_chars.unsafe_ptr() + idx, length=1))
        val >>= 4
    return res

fn simple_hash(s: String) -> String:
    """FNV-1a hash to 16 hex chars."""
    var hash: UInt64 = 14695981039346656037
    var ptr = s.unsafe_ptr()
    for i in range(len(s)):
        hash ^= UInt64(ptr.load(i))
        hash *= 1099511628211
    
    var hex_chars = "0123456789abcdef"
    var res = String()
    for _ in range(16):
        var idx = Int(hash & 15)
        res += String(StringSlice(ptr=hex_chars.unsafe_ptr() + idx, length=1))
        hash >>= 4
    return res

fn float32_to_bytes(f: Float32) -> UInt32:
    var p = alloc[Float32](1)
    p.store(0, f)
    var u32_ptr = p.bitcast[UInt32]()
    var res = u32_ptr.load(0)
    p.free()
    return res

fn json_kv_float(k: String, v: Float64) -> String:
    return json_str(k) + ": " + String(v)

# ── Server ────────────────────────────────────────────────────────────────────

struct QMemMCP:
    var db: SQLiteDB
    var ort: OrtSession
    var tok: WordPieceTokenizer
    var uuid_counter: Int

    fn __init__(out self: Self) raises:
        self.uuid_counter = 0
        var _dir = "."
        self.db = SQLiteDB(_dir + "/libmj_sqlite.so", _dir + "/core_memory.db")
        self.ort = OrtSession(_dir + "/libort_helper.so", _dir + "/bge-small-zh-v1.5-onnx/onnx/model.onnx", 512)
        self.tok = WordPieceTokenizer(_dir + "/bge-small-zh-v1.5-onnx/tokenizer.json")
        
        self.db.enable_load_extension()
        self.db.load_extension(_dir + "/vec0.so")
        
        self.db.exec("CREATE TABLE IF NOT EXISTS memory_facts (id INTEGER PRIMARY KEY AUTOINCREMENT, obs_uuid TEXT UNIQUE NOT NULL, project TEXT NOT NULL DEFAULT '', topic_key TEXT DEFAULT '', title TEXT DEFAULT '', content TEXT NOT NULL, type TEXT DEFAULT 'manual', scope TEXT NOT NULL DEFAULT 'project', content_hash TEXT DEFAULT '', session_id TEXT DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, review_after TIMESTAMP, deleted_at TIMESTAMP)")
        self.db.exec("CREATE VIRTUAL TABLE IF NOT EXISTS memory_vectors USING vec0(embedding float[512] distance_metric=cosine)")
        self.db.exec("CREATE VIRTUAL TABLE IF NOT EXISTS memory_facts_fts USING fts5(title, content, topic_key, type, project, content='memory_facts', content_rowid='id', tokenize='unicode61')")
        
        self.db.exec("CREATE TRIGGER IF NOT EXISTS trg_fts_insert AFTER INSERT ON memory_facts BEGIN INSERT INTO memory_facts_fts(rowid, title, content, topic_key, type, project) VALUES (new.id, new.title, new.content, new.topic_key, new.type, new.project); END;")
        self.db.exec("CREATE TRIGGER IF NOT EXISTS trg_fts_update AFTER UPDATE ON memory_facts BEGIN INSERT INTO memory_facts_fts(memory_facts_fts, rowid, title, content, topic_key, type, project) VALUES ('delete', old.id, old.title, old.content, old.topic_key, old.type, old.project); INSERT INTO memory_facts_fts(rowid, title, content, topic_key, type, project) VALUES (new.id, new.title, new.content, new.topic_key, new.type, new.project); END;")
        self.db.exec("CREATE TRIGGER IF NOT EXISTS trg_fts_delete AFTER DELETE ON memory_facts BEGIN INSERT INTO memory_facts_fts(memory_facts_fts, rowid, title, content, topic_key, type, project) VALUES ('delete', old.id, old.title, old.content, old.topic_key, old.type, old.project); END;")
        self.db.exec("CREATE TRIGGER IF NOT EXISTS trg_vector_delete AFTER DELETE ON memory_facts BEGIN DELETE FROM memory_vectors WHERE rowid = old.id; END;")
        
        # Async physical deletion space recovery
        self.db.exec("VACUUM")

    fn embed(self, text: String) raises -> List[Float32]:
        var ids = self.tok.encode(text, 512)
        var mask = List[Int]()
        for i in range(len(ids)):
            if ids[i] != 0: mask.append(1)
            else: mask.append(0)
        return self.ort.infer(ids, mask)

    fn handle_request(mut self, req_json: String) -> String:
        var method = json_get_string(req_json, "method")
        var rid = json_get_id(req_json)
        
        var res = String()
        var error = String()

        try:
            if method == "initialize":
                res = json_obj(
                    json_kv_str("protocolVersion", "2024-11-05"),
                    json_kv("serverInfo", json_obj(json_kv_str("name", "qmem-mcp"), json_kv_str("version", "2.1"))),
                    json_kv("capabilities", json_obj(json_kv("tools", "{}")))
                )
            elif method == "tools/list":
                res = self._tools_list()
            elif method == "tools/call":
                var params = json_get_obj(req_json, "params")
                res = self._tools_call(params)
            else:
                error = json_obj(json_kv_int("code", -32601), json_kv_str("message", "Method not found"))
        except e:
            error = json_obj(json_kv_int("code", -32603), json_kv_str("message", String(e)))

        if len(error) > 0:
            return json_obj(json_kv_str("jsonrpc", "2.0"), '"id": ' + rid, json_kv("error", error))
        return json_obj(json_kv_str("jsonrpc", "2.0"), '"id": ' + rid, json_kv("result", res))

    fn _tools_list(self) -> String:
        var tools = List[String]()
        
        var req1 = List[String]()
        req1.append('"project_id"')
        req1.append('"content"')
        
        var req2 = List[String]()
        req2.append('"query"')
        
        var req_obsid = List[String]()
        req_obsid.append('"obs_id"')
        
        var req_obsid_proj = List[String]()
        req_obsid_proj.append('"obs_id"')
        
        var req_project = List[String]()
        req_project.append('"project"')

        tools.append(json_obj(
            json_kv_str("name", "mem_save"),
            json_kv_str("description", "Save memory (Push)."),
            json_kv("inputSchema", json_obj(
                json_kv_str("type", "object"),
                json_kv("properties", json_obj(
                    json_kv("project_id", json_obj(json_kv_str("type", "string"))),
                    json_kv("content", json_obj(json_kv_str("type", "string"))),
                    json_kv("title", json_obj(json_kv_str("type", "string")))
                )),
                json_kv("required", json_arr(req1))
            ))
        ))
        tools.append(json_obj(
            json_kv_str("name", "mem_recall"),
            json_kv_str("description", "Search memory (Pull)."),
            json_kv("inputSchema", json_obj(
                json_kv_str("type", "object"),
                json_kv("properties", json_obj(
                    json_kv("query", json_obj(json_kv_str("type", "string"))),
                    json_kv("limit", json_obj(json_kv_str("type", "integer")))
                )),
                json_kv("required", json_arr(req2))
            ))
        ))
        
        tools.append(json_obj(
            json_kv_str("name", "mem_search"),
            json_kv_str("description", "Search with FTS5 or list memory"),
            json_kv("inputSchema", json_obj(
                json_kv_str("type", "object"),
                json_kv("properties", json_obj(
                    json_kv("query", json_obj(json_kv_str("type", "string"))),
                    json_kv("project", json_obj(json_kv_str("type", "string"))),
                    json_kv("limit", json_obj(json_kv_str("type", "integer")))
                )),
                json_kv("required", "[]")
            ))
        ))

        tools.append(json_obj(
            json_kv_str("name", "mem_update"),
            json_kv_str("description", "Update memory by obs_id"),
            json_kv("inputSchema", json_obj(
                json_kv_str("type", "object"),
                json_kv("properties", json_obj(
                    json_kv("obs_id", json_obj(json_kv_str("type", "string"))),
                    json_kv("content", json_obj(json_kv_str("type", "string"))),
                    json_kv("title", json_obj(json_kv_str("type", "string")))
                )),
                json_kv("required", json_arr(req_obsid))
            ))
        ))

        tools.append(json_obj(
            json_kv_str("name", "mem_delete"),
            json_kv_str("description", "Hard delete memory by obs_id and wipe vector index"),
            json_kv("inputSchema", json_obj(
                json_kv_str("type", "object"),
                json_kv("properties", json_obj(
                    json_kv("obs_id", json_obj(json_kv_str("type", "string")))
                )),
                json_kv("required", json_arr(req_obsid))
            ))
        ))

        tools.append(json_obj(
            json_kv_str("name", "memory_promote"),
            json_kv_str("description", "将本条经验提炼并抽取到全局 Q2 Skill (部分项目共识) 中，实现物理隔离。"),
            json_kv("inputSchema", json_obj(
                json_kv_str("type", "object"),
                json_kv("properties", json_obj(
                    json_kv("obs_id", json_obj(json_kv_str("type", "string")))
                )),
                json_kv("required", json_arr(req_obsid_proj))
            ))
        ))

        tools.append(json_obj(
            json_kv_str("name", "mem_context"),
            json_kv_str("description", "Get context for project"),
            json_kv("inputSchema", json_obj(
                json_kv_str("type", "object"),
                json_kv("properties", json_obj(
                    json_kv("project", json_obj(json_kv_str("type", "string"))),
                    json_kv("limit", json_obj(json_kv_str("type", "integer")))
                )),
                json_kv("required", json_arr(req_project))
            ))
        ))

        tools.append(json_obj(
            json_kv_str("name", "mem_list_projects"),
            json_kv_str("description", "List all projects"),
            json_kv("inputSchema", json_obj(
                json_kv_str("type", "object"),
                json_kv("properties", "{}"),
                json_kv("required", "[]")
            ))
        ))

        return json_obj(json_kv("tools", json_arr(tools)))

    fn _tools_call(mut self, params: String) raises -> String:
        var name = json_get_string(params, "name")
        var args = json_get_obj(params, "arguments")
        
        var result_content = String()
        
        if name == "mem_save":
            var project_id = json_get_string(args, "project_id")
            var content = json_get_string(args, "content")
            var title = json_get_string(args, "title")
            if len(title) == 0:
                if len(content) > 30: title = String(StringSlice(ptr=content.unsafe_ptr(), length=30)) + "..."
                else: title = content
            result_content = self._save(project_id, title, content)
        elif name == "mem_recall":
            var query = json_get_string(args, "query")
            var limit = json_get_int(args, "limit", 10)
            result_content = self._recall(query, limit)
        elif name == "mem_search":
            var query = json_get_string(args, "query")
            var project = json_get_string(args, "project")
            var limit = json_get_int(args, "limit", 20)
            result_content = self._search(query, project, limit)
        elif name == "mem_update":
            var obs_id = json_get_string(args, "obs_id")
            var content = json_get_string(args, "content")
            var title = json_get_string(args, "title")
            result_content = self._update(obs_id, title, content)
        elif name == "mem_delete":
            var obs_id = json_get_string(args, "obs_id")
            result_content = self._delete(obs_id)
        elif name == "memory_promote":
            var obs_id = json_get_string(args, "obs_id")
            result_content = self._promote(obs_id)
        elif name == "mem_context":
            var project = json_get_string(args, "project")
            var limit = json_get_int(args, "limit", 10)
            result_content = self._context(project, limit)
        elif name == "mem_list_projects":
            result_content = self._list_projects()
        else:
            result_content = json_obj(json_kv_str("error", "Unknown tool: " + name))
            
        var content_arr_list = List[String]()
        content_arr_list.append(json_obj(json_kv_str("type", "text"), json_kv_str("text", result_content)))
        
        return json_obj(json_kv("content", json_arr(content_arr_list)))

    fn _save(mut self, project_id: String, title: String, content: String) raises -> String:
        if len(project_id) == 0 or len(content) == 0:
            return json_obj(json_kv_str("error", "project_id and content are required"))
            
        var content_hash = simple_hash(title + content)
        var vec = self.embed(title + " " + content)
        
        var vec_bytes = alloc[UInt8](len(vec) * 4)
        for i in range(len(vec)):
            var bits = float32_to_bytes(vec[i])
            vec_bytes.store(i * 4 + 0, UInt8((bits >> 0) & 0xFF))
            vec_bytes.store(i * 4 + 1, UInt8((bits >> 8) & 0xFF))
            vec_bytes.store(i * 4 + 2, UInt8((bits >> 16) & 0xFF))
            vec_bytes.store(i * 4 + 3, UInt8((bits >> 24) & 0xFF))

        var oid = generate_uuid(self.uuid_counter)
        self.uuid_counter += 1
        var stmt = self.db.prepare("INSERT INTO memory_facts(obs_uuid, project, title, content, content_hash) VALUES(?,?,?,?,?)")
        self.db.bind_text(stmt, 1, oid)
        self.db.bind_text(stmt, 2, project_id)
        self.db.bind_text(stmt, 3, title)
        self.db.bind_text(stmt, 4, content)
        self.db.bind_text(stmt, 5, content_hash)
        _ = self.db.step(stmt)
        self.db.finalize(stmt)
        
        var stmt_id = self.db.prepare("SELECT id FROM memory_facts WHERE obs_uuid=?")
        self.db.bind_text(stmt_id, 1, oid)
        var fid = Int64(0)
        if self.db.step(stmt_id) == SQLITE_ROW:
            fid = self.db.col_int64(stmt_id, 0)
        self.db.finalize(stmt_id)
        var stmt_vec = self.db.prepare("INSERT INTO memory_vectors(rowid, embedding) VALUES (?, ?)")
        self.db.bind_int64(stmt_vec, 1, fid)
        self.db.bind_blob(stmt_vec, 2, vec_bytes, len(vec) * 4)
        _ = self.db.step(stmt_vec)
        self.db.finalize(stmt_vec)
        
        vec_bytes.free()
        
        return json_obj(json_kv_str("obs_id", oid), json_kv_str("action", "created"), json_kv_int("id", Int(fid)))

    fn _recall(self, query: String, limit: Int) raises -> String:
        var vec = self.embed(query)
        
        var vec_bytes = alloc[UInt8](len(vec) * 4)
        for i in range(len(vec)):
            var bits = float32_to_bytes(vec[i])
            vec_bytes.store(i * 4 + 0, UInt8((bits >> 0) & 0xFF))
            vec_bytes.store(i * 4 + 1, UInt8((bits >> 8) & 0xFF))
            vec_bytes.store(i * 4 + 2, UInt8((bits >> 16) & 0xFF))
            vec_bytes.store(i * 4 + 3, UInt8((bits >> 24) & 0xFF))

        var sql = "SELECT rowid, distance FROM memory_vectors WHERE embedding MATCH ? ORDER BY distance LIMIT ?"
        var stmt = self.db.prepare(sql)
        self.db.bind_blob(stmt, 1, vec_bytes, len(vec) * 4)
        self.db.bind_int64(stmt, 2, Int64(limit))
        
        var results = List[String]()
        var rc = self.db.step(stmt)
        while rc == SQLITE_ROW:
            var id = self.db.col_int64(stmt, 0)
            var dist = self.db.col_double(stmt, 1)
            
            var stmt_fact = self.db.prepare("SELECT obs_uuid, title, content FROM memory_facts WHERE id=?")
            self.db.bind_int64(stmt_fact, 1, id)
            if self.db.step(stmt_fact) == SQLITE_ROW:
                var obj = json_obj(
                    json_kv_str("obs_uuid", self.db.col_text(stmt_fact, 0)),
                    json_kv_str("title", self.db.col_text(stmt_fact, 1)),
                    json_kv_str("content", self.db.col_text(stmt_fact, 2)),
                    json_kv_float("distance", dist)
                )
                results.append(obj)
            self.db.finalize(stmt_fact)
            rc = self.db.step(stmt)
            
        self.db.finalize(stmt)
        vec_bytes.free()
        
        return json_obj(json_kv("results", json_arr(results)))

    fn _list_projects(self) raises -> String:
        var sql = "SELECT project, COUNT(*) as n FROM memory_facts WHERE deleted_at IS NULL GROUP BY project ORDER BY n DESC"
        var stmt = self.db.prepare(sql)
        var results = List[String]()
        var rc = self.db.step(stmt)
        while rc == SQLITE_ROW:
            var p = self.db.col_text(stmt, 0)
            var n = Int(self.db.col_int64(stmt, 1))
            results.append(json_obj(json_kv_str("project", p), json_kv_int("count", n)))
            rc = self.db.step(stmt)
        self.db.finalize(stmt)
        return json_obj(json_kv("projects", json_arr(results)))

    fn _delete(mut self, obs_id: String) raises -> String:
        var stmt = self.db.prepare("DELETE FROM memory_facts WHERE obs_uuid=?")
        self.db.bind_text(stmt, 1, obs_id)
        _ = self.db.step(stmt)
        self.db.finalize(stmt)
        
        return json_obj(json_kv_str("status", "deleted"), json_kv_str("obs_id", obs_id))

    fn _promote(mut self, obs_id: String) raises -> String:
        if len(obs_id) == 0:
            return json_obj(json_kv_str("error", "obs_id required"))
            
        var stmt_read = self.db.prepare("SELECT title, content FROM memory_facts WHERE obs_uuid=?")
        self.db.bind_text(stmt_read, 1, obs_id)
        var title = String()
        var content = String()
        if self.db.step(stmt_read) == SQLITE_ROW:
            title = self.db.col_text(stmt_read, 0)
            content = self.db.col_text(stmt_read, 1)
        else:
            self.db.finalize(stmt_read)
            return json_obj(json_kv_str("error", "not found"))
        self.db.finalize(stmt_read)
        
        # Use Python interop to write file and create dirs safely
        from python import Python
        var os = Python.import_module("os")
        var skill_dir = os.path.expanduser("~/.agents/skills/q2-consensus")
        os.makedirs(skill_dir, True) # exist_ok=True
        var skill_file = os.path.join(skill_dir, "SKILL.md")
        
        var is_new = not os.path.exists(skill_file)
        var already_exists = False
        if not is_new:
            var f_read = Python.evaluate("open")(skill_file, "r", encoding="utf-8")
            var existing_text = String(f_read.read())
            f_read.close()
            var check_title = "## " + title
            if check_title in existing_text and content in existing_text:
                already_exists = True
                
        if not already_exists:
            var f_write = Python.evaluate("open")(skill_file, "a", encoding="utf-8")
            if is_new:
                f_write.write("---\nname: q2-consensus\ndescription: 部分项目共识\n---\n\n")
            f_write.write("## " + title + "\n\n" + content + "\n\n")
            f_write.close()
        
        var stmt_del = self.db.prepare("DELETE FROM memory_facts WHERE obs_uuid=?")
        self.db.bind_text(stmt_del, 1, obs_id)
        _ = self.db.step(stmt_del)
        self.db.finalize(stmt_del)
        
        if already_exists:
            return json_obj(json_kv_str("status", "promoted_skipped_duplicate"), json_kv_str("obs_id", obs_id))
        return json_obj(json_kv_str("status", "promoted_to_skill"), json_kv_str("obs_id", obs_id))

    fn _context(self, project: String, limit: Int) raises -> String:
        var sql = "SELECT obs_uuid, title, content FROM memory_facts WHERE deleted_at IS NULL AND project=? ORDER BY pinned DESC, created_at DESC LIMIT ?"
        var stmt = self.db.prepare(sql)
        self.db.bind_text(stmt, 1, project)
        self.db.bind_int64(stmt, 2, Int64(limit))
        var results = List[String]()
        var rc = self.db.step(stmt)
        while rc == SQLITE_ROW:
            var obs_uuid = self.db.col_text(stmt, 0)
            var title = self.db.col_text(stmt, 1)
            var content = self.db.col_text(stmt, 2)
            results.append(json_obj(
                json_kv_str("obs_uuid", obs_uuid),
                json_kv_str("title", title),
                json_kv_str("content", content)
            ))
            rc = self.db.step(stmt)
        self.db.finalize(stmt)
        return json_obj(json_kv("observations", json_arr(results)))

    fn _search(self, query: String, project: String, limit: Int) raises -> String:
        var sql = String()
        if len(query) == 0:
            sql = "SELECT obs_uuid, title, content FROM memory_facts WHERE deleted_at IS NULL "
            if len(project) > 0: sql += "AND project=? "
            sql += "ORDER BY created_at DESC LIMIT ?"
        else:
            sql = "SELECT mf.obs_uuid, mf.title, mf.content FROM memory_facts_fts JOIN memory_facts mf ON mf.id = memory_facts_fts.rowid WHERE memory_facts_fts MATCH ? AND mf.deleted_at IS NULL "
            if len(project) > 0: sql += "AND mf.project=? "
            sql += "ORDER BY bm25(memory_facts_fts) LIMIT ?"
            
        var stmt = self.db.prepare(sql)
        var bind_idx = 1
        if len(query) > 0:
            var safe_query = '"' + query + '"'
            self.db.bind_text(stmt, bind_idx, safe_query)
            bind_idx += 1
        if len(project) > 0:
            self.db.bind_text(stmt, bind_idx, project)
            bind_idx += 1
        self.db.bind_int64(stmt, bind_idx, Int64(limit))
        
        var results = List[String]()
        var rc = self.db.step(stmt)
        while rc == SQLITE_ROW:
            var obs_uuid = self.db.col_text(stmt, 0)
            var title = self.db.col_text(stmt, 1)
            var content = self.db.col_text(stmt, 2)
            results.append(json_obj(
                json_kv_str("obs_uuid", obs_uuid),
                json_kv_str("title", title),
                json_kv_str("content", content)
            ))
            rc = self.db.step(stmt)
        self.db.finalize(stmt)
        return json_obj(json_kv("results", json_arr(results)))

    fn _update(mut self, obs_id: String, title: String, content: String) raises -> String:
        if len(obs_id) == 0:
            return json_obj(json_kv_str("error", "obs_id is required"))
        if len(title) == 0 and len(content) == 0:
            return json_obj(json_kv_str("error", "nothing to update"))
            
        var sql = String("UPDATE memory_facts SET ")
        if len(title) > 0: sql += "title=?, "
        if len(content) > 0: sql += "content=?, "
        sql += "updated_at=CURRENT_TIMESTAMP WHERE obs_uuid=?"
        
        var stmt = self.db.prepare(sql)
        var bind_idx = 1
        if len(title) > 0:
            self.db.bind_text(stmt, bind_idx, title)
            bind_idx += 1
        if len(content) > 0:
            self.db.bind_text(stmt, bind_idx, content)
            bind_idx += 1
        self.db.bind_text(stmt, bind_idx, obs_id)
        _ = self.db.step(stmt)
        self.db.finalize(stmt)
        
        var stmt_id = self.db.prepare("SELECT id, title, content FROM memory_facts WHERE obs_uuid=?")
        self.db.bind_text(stmt_id, 1, obs_id)
        if self.db.step(stmt_id) == SQLITE_ROW:
            var fid = self.db.col_int64(stmt_id, 0)
            var t = self.db.col_text(stmt_id, 1)
            var c = self.db.col_text(stmt_id, 2)
            var vec = self.embed(t + " " + c)
            var vec_bytes = alloc[UInt8](len(vec) * 4)
            for i in range(len(vec)):
                var bits = float32_to_bytes(vec[i])
                vec_bytes.store(i * 4 + 0, UInt8((bits >> 0) & 0xFF))
                vec_bytes.store(i * 4 + 1, UInt8((bits >> 8) & 0xFF))
                vec_bytes.store(i * 4 + 2, UInt8((bits >> 16) & 0xFF))
                vec_bytes.store(i * 4 + 3, UInt8((bits >> 24) & 0xFF))
                
            var stmt_del = self.db.prepare("DELETE FROM memory_vectors WHERE rowid=?")
            self.db.bind_int64(stmt_del, 1, fid)
            _ = self.db.step(stmt_del)
            self.db.finalize(stmt_del)
            
            var stmt_vec = self.db.prepare("INSERT INTO memory_vectors(rowid, embedding) VALUES (?, ?)")
            self.db.bind_int64(stmt_vec, 1, fid)
            self.db.bind_blob(stmt_vec, 2, vec_bytes, len(vec) * 4)
            _ = self.db.step(stmt_vec)
            self.db.finalize(stmt_vec)
            vec_bytes.free()
            
            var content_hash = simple_hash(t + c)
            var stmt_hash = self.db.prepare("UPDATE memory_facts SET content_hash=? WHERE id=?")
            self.db.bind_text(stmt_hash, 1, content_hash)
            self.db.bind_int64(stmt_hash, 2, fid)
            _ = self.db.step(stmt_hash)
            self.db.finalize(stmt_hash)
            
        self.db.finalize(stmt_id)
        return json_obj(json_kv_str("obs_id", obs_id), json_kv_str("action", "updated"))

fn serve() raises:
    var server = QMemMCP()
    while True:
        try:
            var line = input()
            if len(line) == 0:
                continue
            var res = server.handle_request(line)
            print(res)
        except e:
            var err_str = String(e)
            if err_str == "EOF":
                break
            # Ignore other errors

fn main() raises:
    serve()
