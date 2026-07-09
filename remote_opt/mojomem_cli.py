"""
Mojomem CLI —— 仿 Engram 的命令行入口（save/search/architecture/init）。
对齐 mcp_server v2 的新工具签名（args dict）。
"""
import argparse
import sys
import json
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser(description="Mojomem CLI (Inspired by Engram)")
    subparsers = parser.add_subparsers(dest="command")

    p_save = subparsers.add_parser("save", help="保存记忆 (Push)")
    p_save.add_argument("--project", "-p", required=True)
    p_save.add_argument("--topic", "-t", default="")
    p_save.add_argument("--content", "-c", required=True)
    p_save.add_argument("--title", default="")
    p_save.add_argument("--type", default="manual")

    p_search = subparsers.add_parser("search", help="RRF 混合检索 (Pull)")
    p_search.add_argument("query")
    p_search.add_argument("--project", "-p", default="")
    p_search.add_argument("--min-sim", type=float, default=0.5)

    p_ctx = subparsers.add_parser("context", help="开场召回某 project 最近记忆")
    p_ctx.add_argument("--project", "-p", required=True)

    p_projects = subparsers.add_parser("projects", help="列出所有 project")

    p_init = subparsers.add_parser("init", help="探测目录生成户口本")
    p_init.add_argument("--dir", "-d", default=".")

    args = parser.parse_args()

    from mcp_server import MojomemMCP
    srv = MojomemMCP()

    if args.command == "save":
        res = srv._save({"project_id": args.project, "topic_key": args.topic,
                         "content": args.content, "title": args.title, "type": args.type})
        print(f"📦 saved: {res}")

    elif args.command == "search":
        print(f"🔍 RRF 混合检索: '{args.query}'")
        res = srv._recall({"query": args.query, "current_project": args.project or None,
                           "min_similarity": args.min_sim})
        for r in res.get("results", []):
            print(f"\n  [{r.get('project', '?')}] <{r.get('topic_key', '')}> "
                  f"global={'★' if r.get('is_global') else ' '} "
                  f"sim={r.get('similarity', '?')}")
            print(f"    {(r.get('title') or r.get('content', ''))[:80]}")
        print(f"\n  共 {res.get('count', 0)} 条")

    elif args.command == "context":
        res = srv._context({"project": args.project})
        print(f"📂 {args.project} 最近记忆（pinned 优先）：")
        for r in res.get("observations", []):
            pin = "📌 " if r.get("pinned") else "   "
            print(f"{pin}[{r.get('type', '?')}] {r.get('title', '')[:50]}")

    elif args.command == "projects":
        res = srv._list_projects({})
        print("🏷️  项目清单：")
        for p in res.get("projects", []):
            print(f"  {p['project']:30s} {p['n']} 条")

    elif args.command == "init":
        res = srv._init_project_context({"directory": args.dir})
        print(res.get("context", res.get("message", "")))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
