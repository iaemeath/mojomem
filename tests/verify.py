"""
Mojomem 实测验证：10 项场景，全部通过才算"能替代"。

用法：python verify.py
每项独立运行，一项失败不阻断后续（收集全部结果统一报告）。
"""
import os
import sys
import json
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_DIR = os.path.dirname(os.path.abspath(__file__))

# 用临时 DB，不污染正式库
TEST_DB = os.path.join(_DIR, "verify_test.db")
os.environ["VERIFY_MODE"] = "1"

import numpy as np
import sqlite3
import sqlite_vec

results = []  # (name, ok, detail)


def report(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    results.append((name, ok, detail))
    print(f"  [{'✅' if ok else '❌'}] {name}: {detail}" if detail else f"  [{'✅' if ok else '❌'}] {name}")


def fresh_db():
    """建干净的测试库，应用 schema.sql。"""
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    conn = sqlite3.connect(TEST_DB)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    with open(os.path.join(_DIR, "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()


def get_conn():
    conn = sqlite3.connect(TEST_DB)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.row_factory = sqlite3.Row
    return conn


def test_1_import():
    """测试1：核心模块能 import（无缺失文件/语法错误）。"""
    try:
        import mcp_server
        import search_rrf
        import cbm_wrapper
        import init_project_context
        report("1.import 模块加载", True)
    except Exception as e:
        report("1.import 模块加载", False, str(e))


def test_2_schema():
    """测试2：schema 建表 + 新列都在。"""
    try:
        fresh_db()
        conn = get_conn()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_facts)").fetchall()}
        required = {"id", "obs_uuid", "project", "topic_key", "title", "content", "type",
                    "scope", "is_global", "content_hash", "session_id", "pinned",
                    "created_at", "updated_at", "review_after", "deleted_at"}
        missing = required - cols
        # FTS 表存在性
        fts_ok = bool(conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_facts_fts'"
        ).fetchone())
        conn.close()
        report("2.schema 建表+15列+FTS5", not missing and fts_ok,
               f"missing={missing}" if missing else "all 15 columns + FTS5 present")
    except Exception as e:
        report("2.schema 建表+15列+FTS5", False, str(e))


def test_3_save_with_metadata():
    """测试3：mem_save 带 title/type，向量正确入库。"""
    try:
        from mcp_server import MojomemMCP
        srv = MojomemMCP()
        srv._init()  # 用正式库 schema，但测试后清理
        # 用正式库测（因为 MojomemMCP 绑定了 DBPATH）
        res = srv._save({
            "project_id": "verify_proj", "topic_key": "test-kb",
            "content": "测试保供达梦 IS_DELETE 中文值陷阱", "title": "测试标题",
            "type": "bugfix"
        })
        oid = res["obs_id"]
        conn = srv._get_conn()
        row = conn.execute("SELECT title, type, content_hash FROM memory_facts WHERE obs_uuid=?", (oid,)).fetchone()
        vec_row = conn.execute("SELECT rowid FROM memory_vectors WHERE rowid=?", (res["id"],)).fetchone()
        conn.close()
        ok = row and row["title"] == "测试标题" and row["type"] == "bugfix" and row["content_hash"] and vec_row
        # 清理
        srv._delete({"obs_id": oid})
        report("3.mem_save 带title/type+向量", ok, f"obs_id={oid}")
    except Exception as e:
        report("3.mem_save 带title/type+向量", False, str(e))


def test_4_upsert():
    """测试4：同 topic_key 再 save，应 UPDATE 不新增。"""
    try:
        from mcp_server import MojomemMCP
        srv = MojomemMCP()
        srv._init()
        r1 = srv._save({"project_id": "verify_proj", "topic_key": "upsert-test", "content": "v1内容"})
        r2 = srv._save({"project_id": "verify_proj", "topic_key": "upsert-test", "content": "v2新内容"})
        conn = srv._get_conn()
        count = conn.execute(
            "SELECT COUNT(*) FROM memory_facts WHERE project='verify_proj' AND topic_key='upsert-test' AND deleted_at IS NULL"
        ).fetchone()[0]
        content = conn.execute(
            "SELECT content FROM memory_facts WHERE obs_uuid=?", (r2["obs_id"],)
        ).fetchone()[0]
        conn.close()
        # 清理
        srv._delete({"obs_id": r1["obs_id"]})
        ok = count == 1 and r1["obs_id"] == r2["obs_id"] and content == "v2新内容" and r2["action"] == "updated"
        report("4.topic_key upsert", ok, f"count={count} action={r2['action']}")
    except Exception as e:
        report("4.topic_key upsert", False, str(e))


def test_5_hybrid_recall():
    """测试5：中文语义召回 + 英文词法召回。"""
    try:
        from mcp_server import MojomemMCP
        srv = MojomemMCP()
        srv._init()
        # 存两条
        a = srv._save({"project_id": "v5", "content": "FeignClient must have contextId annotation"})
        b = srv._save({"project_id": "v5", "content": "保供管控的达梦数据库连接配置"})
        # 中文语义查
        rc = srv._recall({"query": "保供达梦", "min_similarity": 0.0})
        # 英文词法查
        re_ = srv._recall({"query": "FeignClient", "min_similarity": 0.0})
        # 清理
        srv._delete({"obs_id": a["obs_id"]})
        srv._delete({"obs_id": b["obs_id"]})
        cn_ok = rc["count"] > 0
        en_ok = re_["count"] > 0
        report("5.混合检索 中文+英文", cn_ok and en_ok,
               f"中文召回={rc['count']} 英文召回={re_['count']}")
    except Exception as e:
        report("5.混合检索 中文+英文", False, str(e))


def test_6_promote_boost():
    """测试6：promote 后，内容相近时该条排名靠前（is_global bonus 生效）。"""
    try:
        from mcp_server import MojomemMCP
        srv = MojomemMCP()
        srv._init()
        # 两条内容几乎相同 → 语义相似度接近 → boost 应让 promoted 的排前面
        a = srv._save({"project_id": "v6b", "content": "达梦数据库连接配置说明"})
        b = srv._save({"project_id": "v6b", "content": "达梦数据库连接配置详细说明"})
        srv._promote({"obs_id": b["obs_id"]})
        res = srv._recall({"query": "达梦数据库连接配置", "min_similarity": 0.0, "limit": 5})
        top_id = res["results"][0]["obs_id"] if res["results"] else None
        srv._delete({"obs_id": a["obs_id"]})
        srv._delete({"obs_id": b["obs_id"]})
        report("6.is_global boost", top_id == b["obs_id"],
               f"top={top_id[:8]} expected={b['obs_id'][:8]}")
    except Exception as e:
        report("6.is_global boost", False, str(e))


def test_7_project_filter():
    """测试7：mem_recall 带 project 只返回该 project。"""
    try:
        from mcp_server import MojomemMCP
        srv = MojomemMCP()
        srv._init()
        a = srv._save({"project_id": "projA", "content": "项目A的保供配置"})
        b = srv._save({"project_id": "projB", "content": "项目B的保供配置"})
        res = srv._recall({"query": "保供", "current_project": "projA", "min_similarity": 0.0})
        srv._delete({"obs_id": a["obs_id"]})
        srv._delete({"obs_id": b["obs_id"]})
        all_a = all(r["project"] == "projA" for r in res["results"])
        report("7.project 过滤", res["count"] > 0 and all_a,
               f"count={res['count']} all_projA={all_a}")
    except Exception as e:
        report("7.project 过滤", False, str(e))


def test_8_context():
    """测试8：mem_context 返回最近 N 条 + pinned 优先。"""
    try:
        from mcp_server import MojomemMCP
        srv = MojomemMCP()
        srv._init()
        a = srv._save({"project_id": "v8", "content": "普通条目"})
        b = srv._save({"project_id": "v8", "content": "置顶条目"})
        conn = srv._get_conn()
        conn.execute("UPDATE memory_facts SET pinned=1 WHERE obs_uuid=?", (b["obs_id"],))
        conn.commit()
        conn.close()
        res = srv._context({"project": "v8", "limit": 10})
        top_pinned = res["observations"][0]["pinned"] if res["observations"] else None
        srv._delete({"obs_id": a["obs_id"]})
        srv._delete({"obs_id": b["obs_id"]})
        report("8.mem_context+pinned优先", res["count"] >= 2 and top_pinned == 1,
               f"count={res['count']} top_pinned={top_pinned}")
    except Exception as e:
        report("8.mem_context+pinned优先", False, str(e))


def test_9_migration():
    """测试9：Engram 迁移 dry-run。"""
    try:
        import migrate_from_engram as mig
        engram_path = os.path.expanduser("~/.engram/engram.db")
        if not os.path.exists(engram_path):
            report("9.迁移dry-run", False, "engram.db not found")
            return
        econn = mig.open_engram_ro(engram_path)
        n = econn.execute("SELECT COUNT(*) FROM observations WHERE deleted_at IS NULL").fetchone()[0]
        econn.close()
        report("9.迁移dry-run(读取源)", n > 0, f"engram alive observations: {n}")
    except Exception as e:
        report("9.迁移dry-run", False, str(e))


def test_10_init_probe():
    """测试10：init_project_context 真实探测。"""
    try:
        from mcp_server import MojomemMCP
        srv = MojomemMCP()
        # 探测 D:\code\bfo_zj_yxyd（Java 项目，有 pom.xml）
        res = srv._init_project_context({"directory": r"D:\code\bfo_zj_yxyd"})
        ok = res["status"] == "success" and "context" in res
        build = res.get("probe", {}).get("build", "")
        report("10.init_project_context探测", ok,
               f"build={build} dir_found={'directory' in res.get('probe', {})}")
    except Exception as e:
        report("10.init_project_context探测", False, str(e))


def main():
    print("=" * 60)
    print("🚀 Mojomem 10 项实测验证")
    print("=" * 60)
    tests = [
        ("测试1", test_1_import),
        ("测试2", test_2_schema),
        ("测试3", test_3_save_with_metadata),
        ("测试4", test_4_upsert),
        ("测试5", test_5_hybrid_recall),
        ("测试6", test_6_promote_boost),
        ("测试7", test_7_project_filter),
        ("测试8", test_8_context),
        ("测试9", test_9_migration),
        ("测试10", test_10_init_probe),
    ]
    for label, fn in tests:
        print(f"\n▶ {label}")
        try:
            fn()
        except Exception as e:
            import traceback
            report(label, False, f"UNHANDLED: {e}\n{traceback.format_exc()}")

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"🎯 结果: {passed}/{total} 通过")
    print("=" * 60)
    for name, ok, detail in results:
        print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail[:80]}" if detail else ""))
    # 清理临时 db
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
