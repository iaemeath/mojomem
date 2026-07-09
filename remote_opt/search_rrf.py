import sqlite3
import json
import re

class HybridSearcher:
    """
    RRF 混合检索：FTS5 词法（BM25）+ 向量语义（cosine）融合排序。

    三项补齐（vs 原型）：
    1. lexical 用 FTS5 MATCH（替代 LIKE，英文标识符精确匹配更强 + BM25 排序）
    2. is_global=1 的条目 RRF 分数 ×1.5（memory_promote 的 Q2 共识 boost）
    3. 支持 project 过滤 + min_similarity 阈值
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

    def lexical_search(self, keyword, project=None, limit=20):
        """FTS5 词法检索（BM25 排序）。中文无分词盲区由向量路补。"""
        # FTS5 query 安全化：特殊字符用引号包裹，避免被当操作符
        safe = keyword.replace('"', '""')
        fts_query = f'"{safe}"'
        sql = (
            "SELECT mf.obs_uuid as obs_id, mf.project, mf.topic_key, mf.title, mf.content, "
            "mf.is_global, mf.type, mf.created_at, "
            "bm25(memory_facts_fts) as rank "
            "FROM memory_facts_fts JOIN memory_facts mf ON mf.id = memory_facts_fts.rowid "
            "WHERE memory_facts_fts MATCH ? AND mf.deleted_at IS NULL"
        )
        params = [fts_query]
        if project:
            sql += " AND mf.project = ?"
            params.append(project)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        try:
            conn = self._get_conn()
            cur = conn.execute(sql, params)
            res = [dict(r) for r in cur.fetchall()]
            conn.close()
            return res
        except Exception as e:
            # FTS5 MATCH 失败（如全中文无分词）→ 降级到 LIKE
            print(f"[lex] FTS5 fallback to LIKE: {e}")
            return self._lexical_like(keyword, project, limit)

    def _lexical_like(self, keyword, project=None, limit=20):
        """LIKE 降级路径（FTS5 MATCH 无命中或出错时）。"""
        p = f"%{keyword}%"
        sql = (
            "SELECT obs_uuid as obs_id, project, topic_key, title, content, is_global, type, created_at "
            "FROM memory_facts WHERE (content LIKE ? OR title LIKE ? OR topic_key LIKE ?) "
            "AND deleted_at IS NULL"
        )
        params = [p, p, p]
        if project:
            sql += " AND project = ?"
            params.append(project)
        sql += " LIMIT ?"
        params.append(limit)
        try:
            conn = self._get_conn()
            cur = conn.execute(sql, params)
            res = [dict(r) for r in cur.fetchall()]
            conn.close()
            return res
        except Exception as e:
            print(f"[lex] LIKE also failed: {e}")
            return []

    def semantic_search(self, vec, project=None, limit=10):
        """向量语义检索（cosine），返回 distance 供换算 similarity。"""
        v = json.dumps(vec) if isinstance(vec, list) else vec
        sql = (
            "SELECT mf.obs_uuid as obs_id, mf.project, mf.topic_key, mf.title, mf.content, "
            "mf.is_global, mf.type, mf.created_at, "
            "vec_distance_cosine(mv.embedding, ?) as distance "
            "FROM memory_vectors mv JOIN memory_facts mf ON mv.rowid = mf.id "
            "WHERE mf.deleted_at IS NULL"
        )
        params = [v]
        if project:
            sql += " AND mf.project = ?"
            params.append(project)
        sql += " ORDER BY distance LIMIT ?"
        params.append(limit)
        try:
            conn = self._get_conn()
            cur = conn.execute(sql, params)
            res = [dict(r) for r in cur.fetchall()]
            conn.close()
            # 换算 similarity = 1 - cosine_distance
            for r in res:
                r["similarity"] = round(1.0 - r["distance"], 4)
            return res
        except Exception as e:
            print(f"[sem] error: {e}")
            return []

    def hybrid_search_rrf(self, query_text, query_vector, project=None, min_similarity=0.0, limit=10, k=60):
        """
        RRF 融合排序。is_global=1 的条目 RRF 分数 ×1.5 boost。
        min_similarity 过滤（仅作用于语义路，词法路不感知相似度）。
        """
        lex = self.lexical_search(query_text, project=project, limit=20)
        # 向量路多取 3 倍候选，给 project/去重/阈值过滤留余量
        sem = self.semantic_search(query_vector, project=project, limit=limit * 3)

        scores = {}
        items = {}

        # is_global 条目给予 RRF 等效 rank=0 的加分垫底（保证排在前列）
        GLOBAL_BONUS = 1.0 / (k + 1)  # 等效 rank=0 的 RRF 分

        for rank, item in enumerate(lex):
            iid = item["obs_id"]
            items[iid] = item
            boost = 1.5 if item.get("is_global") else 1.0
            scores[iid] = scores.get(iid, 0) + boost * (1.0 / (k + rank + 1))

        for rank, item in enumerate(sem):
            iid = item["obs_id"]
            if item.get("similarity", 0) < min_similarity:
                continue
            if iid in items:
                items[iid].update({kk: vv for kk, vv in item.items() if kk not in items[iid]})
            else:
                items[iid] = item
            boost = 1.5 if item.get("is_global") else 1.0
            scores[iid] = scores.get(iid, 0) + boost * (1.0 / (k + rank + 1))

        # is_global 的固定加分（让 promote 的共识条目稳定靠前）
        for iid, item in items.items():
            if item.get("is_global"):
                scores[iid] = scores.get(iid, 0) + GLOBAL_BONUS

        ranked_ids = sorted(scores, key=lambda x: scores[x], reverse=True)[:limit]
        results = []
        for iid in ranked_ids:
            item = items[iid]
            item["rrf_score"] = round(scores[iid], 6)
            results.append(item)
        return results
