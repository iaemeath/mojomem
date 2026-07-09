-- ============================================================
-- Mojomem schema v2 —— 对齐 Engram 能力
-- ============================================================

-- DDL 1: 记忆事实表（对齐 Engram observations 的核心列）
CREATE TABLE IF NOT EXISTS memory_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    obs_uuid TEXT UNIQUE NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    topic_key TEXT DEFAULT '',
    title TEXT DEFAULT '',
    content TEXT NOT NULL,
    type TEXT DEFAULT 'manual',              -- decision/bugfix/reference/learning/manual/...
    scope TEXT NOT NULL DEFAULT 'project',   -- project/personal
    is_global INTEGER NOT NULL DEFAULT 0,    -- Q2 全局共识标记（memory_promote）
    content_hash TEXT DEFAULT '',            -- sha256(title+content)[:16]，变更检测
    session_id TEXT DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0,       -- 置顶（开场必读）
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    review_after TIMESTAMP,                  -- 易过期信息复审时间
    deleted_at TIMESTAMP                     -- 软删除（NULL=存活）
);

-- ============================================================
-- 增量升级：对已存在的旧表 ADD COLUMN（IF NOT EXISTS 语义用 pragma 兜底）
-- SQLite 没有 ALTER TABLE ADD COLUMN IF NOT EXISTS，靠应用层 idempotent 执行
-- 迁移在 _init() Python 代码里做（_migrate_schema），这里只定义目标态。
-- ============================================================

-- DDL 2: sqlite-vec 向量虚表（512 维 BGE-small-zh，cosine 距离）
CREATE VIRTUAL TABLE IF NOT EXISTS memory_vectors USING vec0(
    embedding float[512] distance_metric=cosine
);

-- DDL 3: FTS5 全文索引（external-content contentless，指向 memory_facts）
-- 用 BM25 排序替代 LIKE，英文标识符精确匹配更强（中文盲区由向量路补）
CREATE VIRTUAL TABLE IF NOT EXISTS memory_facts_fts USING fts5(
    title, content, topic_key, type, project,
    content='memory_facts',
    content_rowid='id',
    tokenize='unicode61'
);

-- DDL 4: Q2 全局资产表（跨项目公共知识，保留原设计）
CREATE TABLE IF NOT EXISTS q2_assets (
    name TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- 触发器：memory_facts ↔ memory_vectors / memory_facts_fts 同步
-- ============================================================

-- 插入事实 → FTS 索引（向量由应用层 save 时显式写入，不用触发器占位，
-- 因为 vec0 不支持 UPDATE embedding，触发器建占位再 UPDATE 会出错）
CREATE TRIGGER IF NOT EXISTS trg_fts_insert
AFTER INSERT ON memory_facts
BEGIN
    INSERT INTO memory_facts_fts(rowid, title, content, topic_key, type, project)
    VALUES (new.id, new.title, new.content, new.topic_key, new.type, new.project);
END;

-- 更新事实 → FTS 索引同步
CREATE TRIGGER IF NOT EXISTS trg_fts_update
AFTER UPDATE ON memory_facts
BEGIN
    INSERT INTO memory_facts_fts(memory_facts_fts, rowid, title, content, topic_key, type, project)
    VALUES ('delete', old.id, old.title, old.content, old.topic_key, old.type, old.project);
    INSERT INTO memory_facts_fts(rowid, title, content, topic_key, type, project)
    VALUES (new.id, new.title, new.content, new.topic_key, new.type, new.project);
END;

-- 删除事实（软删/硬删）→ FTS 索引 + 向量级联清理
CREATE TRIGGER IF NOT EXISTS trg_fts_delete
AFTER DELETE ON memory_facts
BEGIN
    INSERT INTO memory_facts_fts(memory_facts_fts, rowid, title, content, topic_key, type, project)
    VALUES ('delete', old.id, old.title, old.content, old.topic_key, old.type, old.project);
END;

CREATE TRIGGER IF NOT EXISTS trg_vector_delete
AFTER DELETE ON memory_facts
BEGIN
    DELETE FROM memory_vectors WHERE rowid = old.id;
END;

-- ============================================================
-- 索引
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_facts_project ON memory_facts(project);
CREATE INDEX IF NOT EXISTS idx_facts_topic ON memory_facts(topic_key);
CREATE INDEX IF NOT EXISTS idx_facts_global ON memory_facts(is_global);
CREATE INDEX IF NOT EXISTS idx_facts_type ON memory_facts(type);
CREATE INDEX IF NOT EXISTS idx_facts_scope ON memory_facts(scope);
CREATE INDEX IF NOT EXISTS idx_facts_deleted ON memory_facts(deleted_at);
CREATE INDEX IF NOT EXISTS idx_facts_created ON memory_facts(created_at);
CREATE INDEX IF NOT EXISTS idx_facts_pinned ON memory_facts(pinned);
