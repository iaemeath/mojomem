import os
import re
import subprocess

class ProjectContextProbe:
    """
    Q3 户口本探针：扫描目录线索（git remote / pom.xml / package.json / 端口 / schema），
    生成项目身份摘要，供冷启动时填充 CLAUDE.md / AGENTS.md。

    对齐原体系的"身份戳"机制（docs/AI记忆体系方案.md 弱点 4 的补救）。
    """

    def __init__(self, base_dir="."):
        self.base_dir = os.path.abspath(base_dir)

    def probe(self, directory=None):
        """探测指定目录，返回身份摘要 dict。"""
        d = os.path.abspath(directory) if directory else self.base_dir
        info = {"directory": d}
        info.update(self._git_remote(d))
        info.update(self._pom_info(d))
        info.update(self._package_json(d))
        info.update(self._claude_md(d))
        return info

    def _git_remote(self, d):
        """读 git remote origin URL。"""
        try:
            r = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=d, capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0 and r.stdout.strip():
                return {"gitea_remote": r.stdout.strip()}
        except Exception:
            pass
        return {}

    def _pom_info(self, d):
        """读 pom.xml 的 groupId/artifactId（Java 项目识别）。"""
        pom = os.path.join(d, "pom.xml")
        if not os.path.exists(pom):
            return {}
        try:
            with open(pom, encoding="utf-8") as f:
                txt = f.read()
            gid = re.search(r"<groupId>([^<]+)</groupId>", txt)
            aid = re.search(r"<artifactId>([^<]+)</artifactId>", txt)
            out = {"build": "maven"}
            if gid: out["group_id"] = gid.group(1).strip()
            if aid: out["artifact_id"] = aid.group(1).strip()
            return out
        except Exception:
            return {"build": "maven"}

    def _package_json(self, d):
        """读 package.json 的 name/scripts.dev（前端项目识别 + 端口）。"""
        pj = os.path.join(d, "package.json")
        if not os.path.exists(pj):
            return {}
        try:
            import json
            with open(pj, encoding="utf-8") as f:
                pkg = json.load(f)
            out = {"build": "npm", "frontend_name": pkg.get("name", "")}
            scripts = pkg.get("scripts", {})
            dev = scripts.get("dev", "")
            out["dev_script"] = dev
            # 尝试从 dev 脚本提取端口号
            m = re.search(r"--port\s+(\d+)", dev) or re.search(r":(\d{4,5})", dev)
            if m:
                out["port"] = m.group(1)
            return out
        except Exception:
            return {"build": "npm"}

    def _claude_md(self, d):
        """检查是否已有 CLAUDE.md / AGENTS.md（身份戳可能写在里面）。"""
        out = {}
        for fn in ("CLAUDE.md", "AGENTS.md"):
            p = os.path.join(d, fn)
            if os.path.exists(p):
                out[fn] = True
        return out

    def generate_context_text(self, directory=None):
        """生成可读的户口本摘要文本。"""
        info = self.probe(directory)
        lines = ["# 项目身份探针结果（mojomem init_project_context）", ""]
        for k, v in info.items():
            lines.append(f"- **{k}**: {v}")
        lines.append("")
        lines.append("> 若 gitea_remote 与预期不符，可能处于 clone 出走状态，停手核对。")
        return "\n".join(lines)


if __name__ == "__main__":
    import sys
    probe = ProjectContextProbe()
    d = sys.argv[1] if len(sys.argv) > 1 else "."
    print(probe.generate_context_text(d))
