"""QMem v3 DB 状态检查脚本。用法：python check_db.py"""
import sqlite3, json, os

# 定位真实记忆库：与 mcp_server.py 同款逻辑（脚本所在目录的 core_memory.db）
_DBPATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'core_memory.db')

try:
    conn = sqlite3.connect(_DBPATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 总量
    total = cur.execute('SELECT COUNT(*) FROM memory_facts WHERE deleted_at IS NULL').fetchone()[0]
    # tier 分布
    tiers = cur.execute('SELECT tier, COUNT(*) n FROM memory_facts WHERE deleted_at IS NULL GROUP BY tier').fetchall()
    # project_refs
    refs = cur.execute('SELECT COUNT(*) FROM project_refs').fetchone()[0]
    # 列检查
    cols = [r[1] for r in cur.execute('PRAGMA table_info(memory_facts)').fetchall()]

    result = {
        'version': '3.3',
        'total_facts': total,
        'tier_distribution': {r['tier']: r['n'] for r in tiers},
        'project_refs': refs,
        'has_origin_project': 'origin_project' in cols,
        'has_tier': 'tier' in cols,
    }

    # project 分布
    projects = cur.execute(
        'SELECT project, tier, COUNT(*) n FROM memory_facts WHERE deleted_at IS NULL GROUP BY project, tier ORDER BY n DESC'
    ).fetchall()
    result['projects'] = [dict(r) for r in projects]

    print(json.dumps(result, indent=2, ensure_ascii=False))
except Exception as e:
    print('Failed:', str(e))
