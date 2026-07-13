-- ============================================================
-- QMem schema v3 —— 方案10：单表全家桶 + 虚拟外键引用
-- ============================================================

-- DDL 1: 记忆事实表（动态记忆 + 共识同表，tier 字段区分）
CREATE TABLE IF NOT EXISTS memory_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    obs_uuid TEXT UNIQUE NOT NULL,
    project TEXT NOT NULL DEFAULT '',     -- 动态记忆填自身项目名，共识填共识域名（如 _java-cloud-common）
    topic_key TEXT DEFAULT '',
    title TEXT DEFAULT '',
    content TEXT NOT NULL,
    type TEXT DEFAULT 'manual',           -- decision/bugfix/reference/learning/manual
    scope TEXT NOT NULL DEFAULT 'project',
    tier TEXT NOT NULL DEFAULT 'q4',      -- q4(动态草稿) / consensus(跨项目共识)
    origin_project TEXT DEFAULT '',       -- promote 前的原始 project（供 demote 回溯；空=已融合多源，拒绝降级）
    content_hash TEXT DEFAULT '',         -- sha256(title+content)[:16]
    session_id TEXT DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    review_after TIMESTAMP,
    deleted_at TIMESTAMP
);

-- DDL 2: sqlite-vec 向量虚表（512 维 BGE-small-zh，cosine 距离）
CREATE VIRTUAL TABLE IF NOT EXISTS memory_vectors USING vec0(
    embedding float[512] distance_metric=cosine
);

-- DDL 3: FTS5 全文索引（external-content，指向 memory_facts）
CREATE VIRTUAL TABLE IF NOT EXISTS memory_facts_fts USING fts5(
    title, content, topic_key, type, project,
    content='memory_facts',
    content_rowid='id',
    tokenize='unicode61'
);

-- DDL 4: 项目→共识域 多对多关联表（虚拟外键引用图谱）
CREATE TABLE IF NOT EXISTS project_refs (
    project TEXT NOT NULL,                -- 当前项目（如 bfo_zj_yxyd）
    ref_project TEXT NOT NULL,            -- 依赖的共识域（如 _java-cloud-common）
    ref_source TEXT NOT NULL DEFAULT 'promote',  -- promote(自动建) / manual(add_consensus_ref 手动建)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (project, ref_project)
);

-- ============================================================
-- 触发器：memory_facts ↔ memory_vectors / memory_facts_fts 同步
-- ============================================================

CREATE TRIGGER IF NOT EXISTS trg_fts_insert
AFTER INSERT ON memory_facts
BEGIN
    INSERT INTO memory_facts_fts(rowid, title, content, topic_key, type, project)
    VALUES (new.id, new.title, new.content, new.topic_key, new.type, new.project);
END;

CREATE TRIGGER IF NOT EXISTS trg_fts_update
AFTER UPDATE ON memory_facts
BEGIN
    INSERT INTO memory_facts_fts(memory_facts_fts, rowid, title, content, topic_key, type, project)
    VALUES ('delete', old.id, old.title, old.content, old.topic_key, old.type, old.project);
    INSERT INTO memory_facts_fts(rowid, title, content, topic_key, type, project)
    VALUES (new.id, new.title, new.content, new.topic_key, new.type, new.project);
END;

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
CREATE INDEX IF NOT EXISTS idx_facts_type ON memory_facts(type);
CREATE INDEX IF NOT EXISTS idx_facts_scope ON memory_facts(scope);
CREATE INDEX IF NOT EXISTS idx_facts_tier ON memory_facts(tier);
CREATE INDEX IF NOT EXISTS idx_facts_origin ON memory_facts(origin_project);
CREATE INDEX IF NOT EXISTS idx_facts_deleted ON memory_facts(deleted_at);
CREATE INDEX IF NOT EXISTS idx_facts_created ON memory_facts(created_at);
CREATE INDEX IF NOT EXISTS idx_facts_pinned ON memory_facts(pinned);
CREATE INDEX IF NOT EXISTS idx_refs_project ON project_refs(project);
CREATE INDEX IF NOT EXISTS idx_refs_ref ON project_refs(ref_project);
