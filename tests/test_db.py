import sqlite3
import os
import sys

DB_PATH = 'core_memory.db'
SCHEMA_PATH = 'schema.sql'

def init_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        
    conn = sqlite3.connect(DB_PATH)
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        print("✅ sqlite-vec extension loaded successfully.")
    except Exception as e:
        print(f"⚠️ Warning: sqlite-vec not loaded. Vector table creation might fail if not statically compiled. Error: {e}")

    with open(SCHEMA_PATH, 'r', encoding='utf-8') as f:
        schema = f.read()
        
    try:
        conn.executescript(schema)
        print("✅ Schema executed successfully.")
    except Exception as e:
        print(f"❌ Schema execution failed: {e}")
        return

    try:
        cursor = conn.cursor()
        # Insert using obs_uuid instead of string id
        cursor.execute("INSERT INTO memory_facts (obs_uuid, project, topic_key, content) VALUES (?, ?, ?, ?)", 
                       ('obs_1', 'test_proj', 'VUE2_ROUTER', 'Vue 2.6 requires history mode config in nginx.'))
        conn.commit()
        print("✅ Inserted fact obs_1.")
        
        cursor.execute("DELETE FROM memory_facts WHERE obs_uuid = 'obs_1'")
        conn.commit()
        print("✅ Deleted fact obs_1 (Trigger trg_auto_vector_delete fired silently).")
        
    except Exception as e:
        print(f"❌ Test failed: {e}")

    conn.close()

if __name__ == '__main__':
    init_db()
