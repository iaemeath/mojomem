"""
QMem CLI —— 命令行入口（save/search/context/projects/init）。
对齐 mcp_server v3.2 的工具签名（args dict）。
"""
import argparse
import sys
import json
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser(description="QMem CLI (v3.2)")
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

    p_detect = subparsers.add_parser(
        "detect-changes",
        help="代码变更影响分析（转发 codebase-memory）。分析 git diff，列出本次改动影响的函数/调用方/文件。只读，不更新图谱。")
    p_detect.add_argument("--project", "-p", required=True, help="已索引的项目名（文件夹名）")
    p_detect.add_argument("--scope", default=None, help="可选，限定作用域（如某子模块路径）")
    p_detect.add_argument("--base-branch", default="main", help="对比的基线分支，默认 main")
    p_detect.add_argument("--since", default=None, help="对比起点（git ref 或日期，如 HEAD~5 或 2026-01-01）")
    p_detect.add_argument("--depth", type=int, default=2, help="影响传播深度，默认 2")

    args = parser.parse_args()

    from mcp_server import QMemMCP
    srv = QMemMCP()

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
                  f"sim={r.get('similarity', r.get('vec_dist', '?'))}")
            print(f"    {(r.get('title') or r.get('content', ''))[:80]}")
        print(f"\n  共 {res.get('count', 0)} 条")

    elif args.command == "context":
        res = srv._context({"project": args.project})
        own = res.get("own_memories", [])
        cons = res.get("consensus_memories", [])
        print(f"📂 {args.project} 最近记忆（共 {res.get('own_count', len(own))} 条）：")
        for r in own:
            preview = (r.get("title") or r.get("content_preview", ""))[:50]
            print(f"   [{r.get('type', '?')}] {preview}")
        if cons:
            print(f"\n🤝 引用的共识域 {res.get('consensus_domains', [])}（{res.get('consensus_count', len(cons))} 条）：")
            for r in cons:
                preview = (r.get("title") or r.get("content_preview", ""))[:50]
                print(f"   [{r.get('type', '?')}] {preview}")

    elif args.command == "projects":
        res = srv._list_projects({})
        print("🏷️  项目清单：")
        for p in res.get("projects", []):
            print(f"  {p['project']:30s} {p['n']} 条")

    elif args.command == "init":
        res = srv._init_project_context({"directory": args.dir})
        print(res.get("context", res.get("message", "")))

    elif args.command == "detect-changes":
        # detect_changes 是 codebase-memory(CBM)工具，不在 local_tools，走转发
        # 与 mcp_server._dispatch_tool 非本地工具路径一致
        call_params = {
            "name": "detect_changes",
            "arguments": {
                "project": args.project,
                "base_branch": args.base_branch,
                "depth": args.depth,
            },
        }
        if args.scope is not None:
            call_params["arguments"]["scope"] = args.scope
        if args.since is not None:
            call_params["arguments"]["since"] = args.since
        print(f"🔍 代码变更影响分析: project={args.project} base={args.base_branch} depth={args.depth}")
        res = srv.cbm.send_request("tools/call", call_params)
        if "error" in res:
            print(f"❌ CBM error: {json.dumps(res['error'], ensure_ascii=False)}", file=sys.stderr)
            sys.exit(1)
        if "result" in res:
            result = res["result"]
            # MCP tools/call 返回 {"content": [{"type":"text","text":"..."}]}
            if isinstance(result, dict) and "content" in result:
                for block in result["content"]:
                    if block.get("type") == "text":
                        print(block.get("text", ""))
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(res, ensure_ascii=False, indent=2))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
