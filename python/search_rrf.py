import sqlite3
import json
import re
import sys


class HybridSearcher:
    """
    RRF 混合检索：FTS5 词法（BM25）+ 向量语义（cosine）融合排序。

    方案10 v3：支持 projects（复数 IN 查询）和 tiers（复数），
    用于 mem_recall 单次查询同时覆盖 q4 动态记忆 + consensus 共识。
    """

    def __init__(self, db_path='core_memory.db'):
        self.db_path = db_path

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(conn)
        return conn

    def _build_project_filter(self, projects, prefix="mf"):
        """构建 project IN (...) 过滤条件，返回 (sql_fragment, params) 或 (None, [])。"""
        if not projects:
            return None, []
        placeholders = ",".join("?" * len(projects))
        return f" AND {prefix}.project IN ({placeholders})", list(projects)

    def _build_tier_filter(self, tiers, prefix="mf"):
        """构建 tier IN (...) 过滤条件，返回 (sql_fragment, params) 或 (None, [])。"""
        if not tiers:
            return None, []
        placeholders = ",".join("?" * len(tiers))
        return f" AND {prefix}.tier IN ({placeholders})", list(tiers)

    def lexical_search(self, keyword, projects=None, tiers=None, limit=20):
        """FTS5 词法检索（BM25 排序）。中文无分词盲区由向量路补。"""
        safe = keyword.replace('"', '""')
        fts_query = f'"{safe}"'
        sql = (
            "SELECT mf.obs_uuid as obs_id, mf.project, mf.topic_key, mf.title, mf.content, "
            "mf.type, mf.tier, mf.created_at, "
            "bm25(memory_facts_fts) AS fts_score "
            "FROM memory_facts_fts JOIN memory_facts mf ON mf.id = memory_facts_fts.rowid "
            "WHERE memory_facts_fts MATCH ? AND mf.deleted_at IS NULL"
        )
        params = [fts_query]
        pf, pp = self._build_project_filter(projects)
        if pf:
            sql += pf
            params += pp
        tf, tp = self._build_tier_filter(tiers)
        if tf:
            sql += tf
            params += tp
        sql += " ORDER BY fts_score LIMIT ?"
        params.append(limit)
        try:
            conn = self._get_conn()
            cur = conn.execute(sql, params)
            res = [dict(r) for r in cur.fetchall()]
            conn.close()
            return res
        except Exception as e:
            print(f"[lex] FTS5 fallback to LIKE: {e}", file=sys.stderr)
            return self._lexical_like(keyword, projects, tiers, limit)

    def _lexical_like(self, keyword, projects=None, tiers=None, limit=20):
        """LIKE 降级路径（FTS5 MATCH 无命中或出错时）。"""
        p = f"%{keyword}%"
        sql = (
            "SELECT obs_uuid as obs_id, project, topic_key, title, content, type, tier, created_at "
            "FROM memory_facts WHERE (content LIKE ? OR title LIKE ? OR topic_key LIKE ?) "
            "AND deleted_at IS NULL"
        )
        params = [p, p, p]
        if projects:
            placeholders = ",".join("?" * len(projects))
            sql += f" AND project IN ({placeholders})"
            params += list(projects)
        if tiers:
            placeholders = ",".join("?" * len(tiers))
            sql += f" AND tier IN ({placeholders})"
            params += list(tiers)
        sql += " LIMIT ?"
        params.append(limit)
        try:
            conn = self._get_conn()
            cur = conn.execute(sql, params)
            res = [dict(r) for r in cur.fetchall()]
            conn.close()
            return res
        except Exception as e:
            print(f"[lex] LIKE also failed: {e}", file=sys.stderr)
            return []

    def semantic_search(self, vec, projects=None, tiers=None, limit=10):
        """向量语义检索（cosine），返回 distance 供换算 similarity。"""
        v = json.dumps(vec) if isinstance(vec, list) else vec
        sql = (
            "SELECT mf.obs_uuid as obs_id, mf.project, mf.topic_key, mf.title, mf.content, "
            "mf.type, mf.tier, mf.created_at, "
            "vec_distance_cosine(mv.embedding, ?) as distance "
            "FROM memory_vectors mv JOIN memory_facts mf ON mv.rowid = mf.id "
            "WHERE mf.deleted_at IS NULL"
        )
        params = [v]
        pf, pp = self._build_project_filter(projects)
        if pf:
            sql += pf
            params += pp
        tf, tp = self._build_tier_filter(tiers)
        if tf:
            sql += tf
            params += tp
        sql += " ORDER BY distance LIMIT ?"
        params.append(limit)
        try:
            conn = self._get_conn()
            cur = conn.execute(sql, params)
            res = [dict(r) for r in cur.fetchall()]
            conn.close()
            for r in res:
                r["similarity"] = round(1.0 - r["distance"], 4)
            return res
        except Exception as e:
            print(f"[sem] error: {e}", file=sys.stderr)
            return []

    def hybrid_search_rrf(self, query_text, query_vector, projects=None, tiers=None,
                          min_similarity=0.0, limit=10, k=60):
        """
        RRF 融合排序。projects/tiers 同时作用于词法路和向量路（同一次查询，rank 空间统一）。
        min_similarity 过滤（仅作用于语义路）。
        """
        lex = self.lexical_search(query_text, projects=projects, tiers=tiers, limit=20)
        sem = self.semantic_search(query_vector, projects=projects, tiers=tiers, limit=limit * 3)

        scores = {}
        items = {}

        for rank, item in enumerate(lex):
            iid = item["obs_id"]
            items[iid] = item
            scores[iid] = scores.get(iid, 0) + (1.0 / (k + rank + 1))

        for rank, item in enumerate(sem):
            iid = item["obs_id"]
            if item.get("similarity", 0) < min_similarity:
                continue
            if iid in items:
                items[iid].update({kk: vv for kk, vv in item.items() if kk not in items[iid]})
            else:
                items[iid] = item
            scores[iid] = scores.get(iid, 0) + (1.0 / (k + rank + 1))

        sorted_items = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]

        final_results = []
        for iid, r_score in sorted_items:
            item = items[iid]
            final_results.append({
                "obs_uuid": item["obs_id"],
                "project": item.get("project", ""),
                "topic_key": item.get("topic_key", ""),
                "title": item.get("title", ""),
                "content": item.get("content", ""),
                "type": item.get("type", "manual"),
                "tier": item.get("tier", "q4"),
                "created_at": item.get("created_at", ""),
                "score": round(r_score, 6),
                "fts_score": item.get("fts_score"),
                "vec_dist": item.get("distance")
            })
        return final_results
