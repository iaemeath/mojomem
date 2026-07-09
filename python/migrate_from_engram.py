"""
Engram → QMem 数据迁移脚本

把 ~/.engram/engram.db 的 observations 全量导入 QMem 的 core_memory.db。
只读打开 Engram（mode=ro），逐条 embed + INSERT。

用法：python migrate_from_engram.py [--engram <path>] [--dry-run]
"""
import os
import sys
import sqlite3
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from embedding import BGEEmbedding

_DIR = os.path.dirname(os.path.abspath(__file__))
QMem_DB = os.path.join(_DIR, "core_memory.db")
Default_ENGRAM = os.path.expanduser("~/.engram/engram.db")


def open_engram_ro(engram_path):
    """只读打开 Engram（双保险：URI mode=ro + query_only）。"""
    conn = sqlite3.connect(f"file:{engram_path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def open_QMem():
    conn = sqlite3.connect(QMem_DB)
    conn.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(conn)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn):
    """确保 QMem 表结构是最新版（复用 mcp_server 的 schema.sql + 迁移）。"""
    with open(os.path.join(_DIR, "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    required = {
        "title": "TEXT DEFAULT ''", "type": "TEXT DEFAULT 'manual'",
        "scope": "TEXT NOT NULL DEFAULT 'project'", "content_hash": "TEXT DEFAULT ''",
        "session_id": "TEXT DEFAULT ''", "pinned": "INTEGER NOT NULL DEFAULT 0",
        "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "review_after": "TIMESTAMP", "deleted_at": "TIMESTAMP",
    }
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_facts)").fetchall()}
    for col, td in required.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE memory_facts ADD COLUMN {col} {td}")
    conn.commit()


def migrate(engram_path, dry_run=False):
    print(f"[1] 读取 Engram: {engram_path}")
    econn = open_engram_ro(engram_path)
    rows = econn.execute(
        "SELECT id, sync_id, type, title, content, project, scope, topic_key, "
        "created_at, pinned, review_after, deleted_at FROM observations ORDER BY id"
    ).fetchall()
    alive = [r for r in rows if r["deleted_at"] is None]
    print(f"    源 observations: {len(rows)} 条（存活 {len(alive)}，软删 {len(rows) - len(alive)}）")
    econn.close()
    if dry_run:
        print("[dry-run] 不写入，仅展示前 3 条：")
        for r in alive[:3]:
            print(f"    [{r['project']}] <{r['topic_key']}> {r['title'][:40]}")
        return

    print("[2] 初始化 QMem schema + 加载 embedding 模型...")
    mconn = open_QMem()
    ensure_schema(mconn)
    embedder = BGEEmbedding()

    print("[3] 逐条迁移（embed + INSERT）...")
    migrated, skipped = 0, 0
    for r in alive:
        sync_id = r["sync_id"]
        # 幂等：已存在则跳过（重跑不重复）
        exists = mconn.execute("SELECT id FROM memory_facts WHERE obs_uuid=?", (sync_id,)).fetchone()
        if exists:
            skipped += 1
            continue
        title = r["title"] or ""
        content = r["content"] or ""
        text = (title + " " + content).strip()
        vec = embedder.embed(text) if text else [0.0] * 512
        content_hash = hashlib.sha256((title + content).encode("utf-8")).hexdigest()[:16]
        mconn.execute(
            "INSERT INTO memory_facts(obs_uuid, project, topic_key, title, content, type, scope, "
            "content_hash, session_id, pinned, created_at, review_after) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (sync_id, r["project"], r["topic_key"], title, content, r["type"],
             r["scope"] or "project", content_hash, "", r["pinned"] or 0,
             r["created_at"], r["review_after"])
        )
        fid = mconn.execute("SELECT id FROM memory_facts WHERE obs_uuid=?", (sync_id,)).fetchone()[0]
        mconn.execute("INSERT INTO memory_vectors(rowid, embedding) VALUES (?, ?)",
                      (fid, np.array(vec, dtype=np.float32).tobytes()))
        migrated += 1
        if migrated % 5 == 0:
            print(f"    ...{migrated}/{len(alive)}")
    mconn.commit()

    print(f"[4] 完成：迁移 {migrated}，跳过(已存在) {skipped}")
    # 核对
    total = mconn.execute("SELECT COUNT(*) FROM memory_facts WHERE deleted_at IS NULL").fetchone()[0]
    projects = mconn.execute(
        "SELECT project, COUNT(*) n FROM memory_facts WHERE deleted_at IS NULL GROUP BY project ORDER BY n DESC"
    ).fetchall()
    print(f"    QMem 现有存活记忆: {total} 条")
    for p in projects:
        print(f"      {p['project']:30s} {p['n']}")
    mconn.close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--engram", default=Default_ENGRAM)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not os.path.exists(a.engram):
        print(f"ERROR: Engram db not found: {a.engram}")
        sys.exit(1)
    migrate(a.engram, a.dry_run)
