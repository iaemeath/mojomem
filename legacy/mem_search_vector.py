#!/usr/bin/env python3
"""
mem_search_vector — 基于 bge-small-zh + sqlite-vec 的中文语义搜索 MCP 代理
=====================================================================
寄生在 Engram 的 SQLite 数据库上，提供中文向量语义检索。

替代 trigram FTS5 路线（trigram 对双字中文词盲区,业务高频词如
"保供/达梦/断面/状态"全部 0 命中）。

路线裁决:
  bge-small-zh-v1.5(24M 参数,512 维) + ONNX Runtime(裸用,非 torch)
  + sqlite-vec(向量库) + Python 3.13
  8 个 agent 一致决策,详见 OFFINE_PACK_BRIEF.md

架构:
    engram.db (原始 DB)
        ↑ 只读
    mem_search_vector.py
        ├── bge-small-zh via ONNX Runtime (模型常驻内存)
        └── engram_vector.db (sqlite-vec 向量存储)
            ├── obs_vectors(obs_id, embedding BLOB, metadata)
            └── _sync_meta(增量同步水位线)
"""

import json, sys, sqlite3, os, re, time, struct, threading
from pathlib import Path
from typing import NamedTuple, Optional

# ── 同步批次的数据结构(命名清晰, 避免位置元组 r[3]/r[4] 混淆) ──
class ObsBatchItem(NamedTuple):
    """IndexSyncer 批量同步的单条记录。"""
    src_id: Optional[int]   # engram 的 observations.id(水位线用; None=更新场景不推进水位)
    obs_id: str             # sync_id, 向量库主键依据
    text: str               # title + content, 喂给 embed
    title: str
    content: str
    project: str
    topic_key: str

# ── 配置 ──────────────────────────────────────────────
ENGRAM_DATA_DIR = Path(os.environ.get("ENGRAM_DATA_DIR", Path.home() / ".engram"))
ENGRAM_DB = ENGRAM_DATA_DIR / "engram.db"
VECTOR_DB = ENGRAM_DATA_DIR / "engram_vector.db"
MODEL_DIR = Path(os.environ.get("BGE_MODEL_DIR", r"D:\embed\bge-small-zh-v1.5-ONNX"))

# ── JSON-RPC MCP 辅助 ────────────────────────────────
def log(text):
    print(text, file=sys.stderr, flush=True)

def respond(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()

def text_content(text):
    return {"type": "text", "text": str(text)}

# ── ONNX Embedding 引擎 ─────────────────────────────
class ONNXEmbedder:
    """bge-small-zh-v1.5 ONNX 推理封装。"""

    def __init__(self, model_dir: Path):
        self.model_dir = model_dir
        self.session = None
        self.tokenizer = None
        self.input_name = None
        self.output_name = None

    def load(self):
        import onnxruntime
        from tokenizers import Tokenizer

        model_path = self.model_dir / "onnx" / "model.onnx"
        if not model_path.exists():
            # 备选文件名
            for alt in ["model_fp16.onnx", "model_quantized.onnx"]:
                p = self.model_dir / "onnx" / alt
                if p.exists():
                    model_path = p
                    break
            else:
                raise FileNotFoundError(f"ONNX model not found in {self.model_dir}/onnx/")

        log(f"[embed] loading model: {model_path}")
        self.session = onnxruntime.InferenceSession(
            str(model_path),
            providers=["CPUExecutionProvider"],
        )

        # 打印输入输出名（不同导出可能不固定）
        all_inputs = [i.name for i in self.session.get_inputs()]
        all_outputs = [o.name for o in self.session.get_outputs()]
        log(f"[embed] inputs:  {all_inputs}")
        log(f"[embed] outputs: {all_outputs}")
        self.input_names = all_inputs
        # 优先用 sentence_embedding(模型已 pool 好, 更准); 没有则取最后输出做 mean pool
        if "sentence_embedding" in all_outputs:
            self.output_name = "sentence_embedding"
            self.use_pooled = True
            log("[embed] using pre-pooled sentence_embedding output")
        else:
            self.output_name = all_outputs[-1]
            self.use_pooled = False
            log(f"[embed] using {self.output_name}, will mean-pool manually")

        # 加载 tokenizer
        tok_path = self.model_dir / "tokenizer.json"
        self.tokenizer = Tokenizer.from_file(str(tok_path))
        # tokenizers 库: truncation/padding 是只读属性, 必须用 enable_* 方法
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding(length=None)  # 动态 padding 到批次内最长
        log(f"[embed] tokenizer loaded, truncation=512, dynamic padding enabled")

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量计算 embedding。优先用模型的 sentence_embedding; 否则 mean pooling。"""
        if not self.session or not self.tokenizer:
            raise RuntimeError("model not loaded")

        encoded = self.tokenizer.encode_batch(texts)
        import numpy as np

        # 用 tokenizer 自动 padding 后的 ids/mask, 统一长度
        input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        token_type_ids = np.zeros_like(input_ids)

        # 构造 feed dict, 只传模型声明的输入(有些模型无 token_type_ids)
        feed = {}
        if "input_ids" in self.input_names:
            feed["input_ids"] = input_ids
        if "attention_mask" in self.input_names:
            feed["attention_mask"] = attention_mask
        if "token_type_ids" in self.input_names:
            feed["token_type_ids"] = token_type_ids

        out = self.session.run([self.output_name], feed)[0]

        if self.use_pooled:
            # sentence_embedding 已是 (batch, dim) 且通常已归一化, 仍补一次 L2 normalize 保险
            vectors = out
        else:
            # mean pooling over token embeddings (out shape: batch, seq_len, dim)
            mask = attention_mask.astype(np.float32)[:, :, None]
            summed = (out * mask).sum(axis=1)
            counts = mask.sum(axis=1).clip(min=1e-9)
            vectors = summed / counts

        # L2 normalize
        norms = np.linalg.norm(vectors, axis=1, keepdims=True).clip(min=1e-9)
        vectors = vectors / norms

        return vectors.tolist()


# ── 向量存储引擎 ─────────────────────────────────────
class VectorStore:
    """基于 sqlite-vec 的向量存储与检索。"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = None
        self.dim = 512

    def open(self):
        import sqlite_vec

        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)

        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=OFF")

        # vec0 虚拟表: sqlite-vec 要求 integer rowid 作为向量主键
        # obs_id (TEXT) 放元数据表,用 integer rowid 关联
        self.conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS obs_vectors USING vec0(
                embedding float[{self.dim}] distance_metric=cosine
            )
        """)
        # 元数据表: rowid 与 obs_vectors 对应, obs_id 是 Engram 的 observation id
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS obs_meta (
                rowid INTEGER PRIMARY KEY,
                obs_id TEXT UNIQUE,
                content_hash TEXT,
                title TEXT,
                content_preview TEXT,
                project TEXT,
                topic_key TEXT,
                updated_at TEXT
            )
        """)
        # 同步水位线
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS _sync_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        self.conn.commit()

    def upsert_vector(self, obs_id: str, embedding: list[float], title="",
                      content="", project="", topic_key=""):
        """写入单条向量和元数据。obs_id 已存在则覆盖(基于 obs_id 查旧 rowid)。"""
        import numpy as np
        import hashlib
        vec_bytes = np.array(embedding, dtype=np.float32).tobytes()
        content_hash = hashlib.sha256((title + content).encode("utf-8")).hexdigest()[:16]

        # 查是否已有此 obs_id
        cur = self.conn.execute("SELECT rowid FROM obs_meta WHERE obs_id = ?", (obs_id,))
        row = cur.fetchone()
        if row:
            rowid = row[0]
            # vec0 不支持 UPDATE embedding, 删旧插新
            self.conn.execute("DELETE FROM obs_vectors WHERE rowid = ?", (rowid,))
            self.conn.execute("INSERT INTO obs_vectors(rowid, embedding) VALUES (?, ?)", (rowid, vec_bytes))
        else:
            # 新增: rowid 必须从 obs_vectors(向量表, 真正持有 rowid)取 MAX+1,
            # 不能从 obs_meta 取(删行后 obs_meta MAX 回退, 会分配到已用 rowid 导致主键冲突)
            cur2 = self.conn.execute("SELECT COALESCE(MAX(rowid), 0) + 1 FROM obs_vectors")
            rowid = cur2.fetchone()[0]
            self.conn.execute("INSERT INTO obs_vectors(rowid, embedding) VALUES (?, ?)", (rowid, vec_bytes))

        self.conn.execute(
            "INSERT OR REPLACE INTO obs_meta(rowid, obs_id, content_hash, title, content_preview, project, topic_key, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (rowid, obs_id, content_hash, (title or "")[:200], (content or "")[:500],
             project or "", topic_key or "", time.strftime("%Y-%m-%dT%H:%M:%S"))
        )
        self.conn.commit()

    def batch_upsert(self, rows: list[tuple]):
        """批量写入。rows: [(obs_id, embedding, title, content, project, topic_key)]
        逐条处理(vec0 的 rowid 需显式管理, executemany 不便), 内部用 upsert_vector。"""
        for obs_id, embedding, title, content, project, topic_key in rows:
            self.upsert_vector(obs_id, embedding, title, content, project, topic_key)

    def delete_vector(self, obs_id: str) -> bool:
        """删除一条向量(连同元数据)。返回是否删除成功。用于反向清理(engram 删除 → 向量库清理)。"""
        cur = self.conn.execute("SELECT rowid FROM obs_meta WHERE obs_id = ?", (obs_id,))
        row = cur.fetchone()
        if not row:
            return False
        rowid = row[0]
        # vec0 虚拟表删除: 先向量后元数据
        self.conn.execute("DELETE FROM obs_vectors WHERE rowid = ?", (rowid,))
        self.conn.execute("DELETE FROM obs_meta WHERE rowid = ?", (rowid,))
        self.conn.commit()
        return True

    def list_obs_ids(self) -> list[str]:
        """返回向量库里所有 obs_id(用于反向清理扫描)。"""
        cur = self.conn.execute("SELECT obs_id FROM obs_meta")
        return [row[0] for row in cur]

    def cleanup_orphans(self) -> int:
        """清理孤儿向量: obs_vectors 有但 obs_meta 无的 rowid。
        这类孤儿是 delete_vector 中途异常或历史残留产生的, _cleanup_deleted 扫不到
        (它从 obs_meta 取 obs_id), 长期会堆积。本方法直接按 rowid 反向扫。
        不影响召回(search JOIN obs_meta 会过滤孤儿), 但占空间。"""
        orphans = self.conn.execute("""
            SELECT v.rowid FROM obs_vectors v
            LEFT JOIN obs_meta m ON v.rowid = m.rowid
            WHERE m.rowid IS NULL
        """).fetchall()
        if not orphans:
            return 0
        for (rid,) in orphans:
            self.conn.execute("DELETE FROM obs_vectors WHERE rowid = ?", (rid,))
        self.conn.commit()
        return len(orphans)

    def search(self, query_embedding: list[float], limit: int = 10,
               project: str = None) -> list[dict]:
        """余弦相似度搜索。"""
        import numpy as np
        query_bytes = np.array(query_embedding, dtype=np.float32).tobytes()

        # 先按 project 过滤候选 obs_id 集合, 再做 KNN(若不过滤则全表 KNN 后过滤)
        # sqlite-vec 的 vec0 KNN 语法: SELECT rowid, distance FROM t WHERE embedding MATCH ? ORDER BY distance LIMIT k
        if project:
            sql = """
                SELECT v.rowid, v.distance, m.obs_id, m.title, m.content_preview, m.project, m.topic_key
                FROM obs_vectors v
                JOIN obs_meta m ON v.rowid = m.rowid
                WHERE v.embedding MATCH ? AND k = ? AND m.project = ?
                ORDER BY v.distance
            """
            params = [query_bytes, limit * 3, project]
        else:
            sql = """
                SELECT v.rowid, v.distance, m.obs_id, m.title, m.content_preview, m.project, m.topic_key
                FROM obs_vectors v
                JOIN obs_meta m ON v.rowid = m.rowid
                WHERE v.embedding MATCH ? AND k = ?
                ORDER BY v.distance
            """
            params = [query_bytes, limit * 3]

        cur = self.conn.execute(sql, params)
        results = []
        seen = set()
        for r in cur:
            obs_id = r[2]
            if obs_id in seen:
                continue
            seen.add(obs_id)
            similarity = 1.0 - r[1]
            results.append({
                "obs_id": obs_id,
                "title": r[3] or "",
                "content_preview": r[4] or "",
                "project": r[5] or "",
                "topic_key": r[6] or "",
                "similarity": round(similarity, 4),
            })
            if len(results) >= limit:
                break

        return results

    def get_sync_watermark(self) -> int:
        cur = self.conn.execute("SELECT value FROM _sync_meta WHERE key='max_rowid'")
        r = cur.fetchone()
        return int(r[0]) if r else 0

    def set_sync_watermark(self, rowid: int):
        self.conn.execute(
            "INSERT OR REPLACE INTO _sync_meta (key, value) VALUES ('max_rowid', ?)",
            (str(rowid),)
        )
        self.conn.commit()

    def get_obs_content_hash(self, obs_id: str):
        cur = self.conn.execute("SELECT content_hash FROM obs_meta WHERE obs_id = ?", (obs_id,))
        r = cur.fetchone()
        return r[0] if r else None

    def count(self) -> int:
        cur = self.conn.execute("SELECT COUNT(*) FROM obs_meta")
        return cur.fetchone()[0]

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None


# ── 同步器 ───────────────────────────────────────────
class IndexSyncer:
    """从 engram.db 增量同步到向量索引。"""

    def __init__(self, embedder: ONNXEmbedder, store: VectorStore):
        self.embedder = embedder
        self.store = store

    def sync(self, full_rebuild=False) -> int:
        engram_path = ENGRAM_DB
        if not engram_path.exists():
            log(f"[sync] engram.db not found: {engram_path}")
            return 0

        src = sqlite3.connect(f"file:{engram_path}?mode=ro", uri=True)
        src.execute("PRAGMA query_only=ON")

        last_rowid = 0 if full_rebuild else self.store.get_sync_watermark()
        batch_size = 50

        # 第一阶段: 按 id 水位线增量(新增的 obs)
        # 真实表结构: id(INTEGER主键) / sync_id(稳定外部ID如 obs-xxx) / title / content / project / scope / topic_key / deleted_at
        # 用 id 作为水位线(单调递增), 用 sync_id 作为向量库的 obs_id(跨设备稳定)
        cur = src.execute(
            "SELECT id, sync_id, title, content, project, topic_key "
            "FROM observations WHERE id > ? AND deleted_at IS NULL AND sync_id IS NOT NULL ORDER BY id",
            (last_rowid,)
        )

        total = 0
        batch = []
        max_rowid = last_rowid

        for r in cur:
            content = r[3] or ""
            title = r[2] or ""
            if not content.strip() and not title.strip():
                max_rowid = max(max_rowid, r[0])
                continue

            text = (title + " " + content).strip()
            batch.append(ObsBatchItem(
                src_id=r[0], obs_id=r[1], text=text, title=title, content=content,
                project=r[4] or "", topic_key=r[5] or "",
            ))

            if len(batch) >= batch_size:
                self._process_batch(batch)
                total += len(batch)
                max_rowid = max(max_rowid, batch[-1][0])
                log(f"[sync] +{total}...")
                batch = []

        if batch:
            self._process_batch(batch)
            total += len(batch)
            max_rowid = max(max_rowid, batch[-1][0])

        if total > 0:
            # 水位线只跟"存活 obs"走, 防止已删/异常 obs 把水位线推过真实数据
            # (否则后续合法新增会被 WHERE id > wm 永久跳过)
            alive_max = src.execute(
                "SELECT MAX(id) FROM observations WHERE deleted_at IS NULL"
            ).fetchone()[0] or max_rowid
            self.store.set_sync_watermark(min(max_rowid, alive_max))

        # 第二阶段: 检测已索引 obs 的 content 变更(mem_update 改了内容, rowid 不变)
        # 全量重建时跳过(刚全建完, 无需再查)
        if not full_rebuild:
            total += self._sync_updated(src)
            # 第三阶段: 反向清理 — 删掉向量库里"engram 已删除或不存在"的 obs 向量
            total += self._cleanup_deleted(src)

        src.close()
        return total

    def _sync_updated(self, src) -> int:
        """重新 embed 那些 content_hash 与 engram 当前内容不一致的 obs。"""
        import hashlib
        # 取所有已索引 obs 的 (obs_id, 当前 content_hash)
        cur_meta = self.store.conn.execute("SELECT obs_id, content_hash FROM obs_meta")
        existing = {row[0]: row[1] for row in cur_meta}
        if not existing:
            return 0

        # 扫描 engram 里这些 sync_id 的当前内容, 比对 hash
        placeholders = ",".join("?" * len(existing))
        cur_src = src.execute(
            f"SELECT sync_id, title, content, project, topic_key "
            f"FROM observations WHERE sync_id IN ({placeholders}) AND deleted_at IS NULL",
            tuple(existing.keys())
        )

        to_update = []
        for r in cur_src:
            obs_id = r[0]
            title = r[1] or ""
            content = r[2] or ""
            new_hash = hashlib.sha256((title + content).encode("utf-8")).hexdigest()[:16]
            if new_hash != existing.get(obs_id):
                text = (title + " " + content).strip()
                to_update.append(ObsBatchItem(
                    src_id=None, obs_id=obs_id, text=text, title=title, content=content,
                    project=r[3] or "", topic_key=r[4] or "",
                ))

        if not to_update:
            return 0

        log(f"[sync] {len(to_update)} updated obs need re-embed")
        self._process_batch(to_update)
        return len(to_update)

    def _cleanup_deleted(self, src) -> int:
        """反向清理: 删掉向量库里"engram 已删除(deleted_at 非空)或不存在"的 obs 向量。
        覆盖 mem_delete --hard(物理删)/ 普通软删(deleted_at)/ obs_id 在 engram 里彻底消失 三种情况。"""
        vec_ids = self.store.list_obs_ids()
        if not vec_ids:
            return 0

        # 查这些 obs_id 在 engram 里是否还"存活"(存在且 deleted_at IS NULL)
        placeholders = ",".join("?" * len(vec_ids))
        cur = src.execute(
            f"SELECT sync_id FROM observations "
            f"WHERE sync_id IN ({placeholders}) AND deleted_at IS NULL",
            tuple(vec_ids)
        )
        alive_ids = {row[0] for row in cur}

        to_delete = [oid for oid in vec_ids if oid not in alive_ids]

        deleted = 0
        for oid in to_delete:
            if self.store.delete_vector(oid):
                deleted += 1
        if deleted > 0:
            log(f"[sync] cleaned {deleted} stale vector(s) (engram deleted/gone)")

        # 再清孤儿向量(obs_vectors 有但 obs_meta 无的 rowid, 历史残留/中途异常产生)
        # 注意: 不能因 to_delete 为空就提前 return, 否则孤儿永远清不到
        orphan_n = self.store.cleanup_orphans()
        if orphan_n > 0:
            log(f"[sync] cleaned {orphan_n} orphan vector(s) (no metadata)")
            deleted += orphan_n

        return deleted

    def _process_batch(self, batch: list[ObsBatchItem]):
        texts = [item.text for item in batch]
        try:
            embeddings = self.embedder.embed(texts)
            rows = []
            for i, item in enumerate(batch):
                rows.append((item.obs_id, embeddings[i], item.title, item.content, item.project, item.topic_key))
            self.store.batch_upsert(rows)
        except Exception as e:
            log(f"[sync] batch failed ({len(batch)} items): {e}")
            # 逐条 fallback
            for item in batch:
                try:
                    emb = self.embedder.embed([item.text])
                    self.store.upsert_vector(item.obs_id, emb[0], item.title, item.content, item.project, item.topic_key)
                except Exception as e2:
                    log(f"[sync] skip {item.obs_id}: {e2}")


# ── MCP Server ───────────────────────────────────────
def main():
    log("[mcp] mem_search_vector starting...")

    # 检查模型
    if not MODEL_DIR.exists():
        log(f"[mcp] ERROR: Model dir not found: {MODEL_DIR}")
        log(f"[mcp] Set BGE_MODEL_DIR env var or place model at {MODEL_DIR}")
        log("[mcp] See OFFLINE_PACK_BRIEF.md for download instructions")
        sys.exit(1)

    # 加载 embedding 模型
    embedder = ONNXEmbedder(MODEL_DIR)
    try:
        embedder.load()
    except Exception as e:
        log(f"[mcp] FATAL: model load failed: {e}")
        sys.exit(1)

    # 初始化向量存储
    VECTOR_DB.parent.mkdir(parents=True, exist_ok=True)
    store = VectorStore(VECTOR_DB)
    try:
        store.open()
    except Exception as e:
        log(f"[mcp] FATAL: vector store init failed: {e}")
        sys.exit(1)

    syncer = IndexSyncer(embedder, store)

    # 首次启动自动同步
    count = syncer.sync()
    log(f"[mcp] initial sync: +{count} vectors (total: {store.count()})")

    # 后台 watcher: 每 60s 增量同步, 让 mem_save 写的新 obs 自动进向量库
    # 解决"engram MCP 写 / vector MCP 读"两进程隔离导致的写入断点
    # 注意: SQLite 连接默认不能跨线程, watcher 每次新建独立 VectorStore(共享 embedder,
    # 因 ONNX session 只读线程安全), 避免与主线程 store.conn 抢用
    SYNC_INTERVAL = int(os.environ.get("MEM_VECTOR_SYNC_INTERVAL", "60"))
    stop_flag = threading.Event()

    def _watcher():
        while not stop_flag.wait(SYNC_INTERVAL):
            wstore = None
            try:
                wstore = VectorStore(VECTOR_DB)
                wstore.open()
                wsyncer = IndexSyncer(embedder, wstore)
                n = wsyncer.sync()
                if n > 0:
                    log(f"[watcher] +{n} vectors synced (total: {wstore.count()})")
            except Exception as e:
                log(f"[watcher] sync error: {e}")
            finally:
                if wstore:
                    try: wstore.close()
                    except: pass

    watcher_t = threading.Thread(target=_watcher, name="vec-sync-watcher", daemon=True)
    watcher_t.start()
    log(f"[mcp] watcher started: incremental sync every {SYNC_INTERVAL}s")

    log("[mcp] ready, waiting for MCP client...")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_id = msg.get("id")
        method = msg.get("method")

        if method == "initialize":
            respond({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {}
                    },
                    "serverInfo": {
                        "name": "mem-search-vector",
                        "version": "2.0.0"
                    }
                }
            })

        elif method == "notifications/initialized":
            respond({"jsonrpc": "2.0", "id": msg_id, "result": {}})

        elif method == "tools/list":
            respond({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "tools": [
                        {
                            "name": "mem_search_vector",
                            "description": "中文语义搜索: 基于 bge-small-zh + sqlite-vec 的向量检索,"
                                           "对双字中文词(保供/达梦/断面/状态)和同义替换都有效",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string", "description": "搜索关键词(中文自动语义匹配)"},
                                    "limit": {"type": "integer", "description": "返回条数上限", "default": 10},
                                    "project": {"type": "string", "description": "按项目过滤(可选)"},
                                    "min_similarity": {
                                        "type": "number",
                                        "description": "最低相似度阈值 0-1",
                                        "default": 0.5
                                    }
                                },
                                "required": ["query"]
                            }
                        },
                        {
                            "name": "mem_sync_vector",
                            "description": "手动增量同步 Engram 数据到向量索引",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "full_rebuild": {
                                        "type": "boolean",
                                        "description": "全量重建索引(耗时较长)",
                                        "default": False
                                    }
                                }
                            }
                        },
                        {
                            "name": "mem_vector_stats",
                            "description": "查看向量索引统计信息",
                            "inputSchema": {
                                "type": "object",
                                "properties": {}
                            }
                        }
                    ]
                }
            })

        elif method == "tools/call":
            name = msg.get("params", {}).get("name", "")
            args = msg.get("params", {}).get("arguments", {})

            try:
                result = None

                if name == "mem_search_vector":
                    query = args.get("query", "")
                    limit = int(args.get("limit", 10))
                    project = args.get("project")
                    min_sim = float(args.get("min_similarity", 0.5))

                    if not query.strip():
                        result = {"results": [], "note": "empty query"}

                    else:
                        query_emb = embedder.embed([query])[0]
                        results = store.search(query_emb, limit=limit, project=project)
                        # 过滤低分
                        results = [r for r in results if r["similarity"] >= min_sim]
                        total_vec = store.count()
                        result = {
                            "results": results,
                            "total_indexed": total_vec,
                            "query": query,
                            "engine": "bge-small-zh-v1.5 + sqlite-vec",
                        }

                elif name == "mem_sync_vector":
                    full_rebuild = args.get("full_rebuild", False)
                    count = syncer.sync(full_rebuild=full_rebuild)
                    result = {
                        "synced": count,
                        "total_indexed": store.count(),
                        "status": "ok",
                    }

                elif name == "mem_vector_stats":
                    result = {
                        "total_indexed": store.count(),
                        "vector_dim": store.dim,
                        "engine": "bge-small-zh-v1.5 + ONNX Runtime + sqlite-vec",
                        "model_dir": str(MODEL_DIR),
                    }

                else:
                    result = {"error": f"unknown tool: {name}"}

                respond({
                    "jsonrpc": "2.0", "id": msg_id,
                    "result": {"content": [text_content(json.dumps(result, ensure_ascii=False, indent=2))]}
                })

            except Exception as e:
                log(f"[mcp] ERROR: {name}: {e}")
                respond({
                    "jsonrpc": "2.0", "id": msg_id,
                    "result": {
                        "content": [text_content(json.dumps({"error": str(e)}, ensure_ascii=False))]
                    }
                })

        else:
            log(f"[mcp] unhandled: {method}")


if __name__ == "__main__":
    main()
