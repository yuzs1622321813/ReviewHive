"""下载官方文档语料到 data/docs/：OWASP 安全指南（精选）+ 阿里 Java 规范 / p3c（GitHub Markdown）。

目录约定（决定入库后的 kind）：
  data/docs/vulnerability/   -> vulnerability
  data/docs/best_practice/   -> best_practice
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_UA = {"User-Agent": "reviewhive/0.1 (knowledge base fetch)"}

# OWASP Cheat Sheet Series 中与代码评审最相关的篇目
OWASP_SHEETS = [
    "SQL_Injection_Prevention_Cheat_Sheet",
    "Injection_Prevention_Cheat_Sheet",
    "Input_Validation_Cheat_Sheet",
    "Deserialization_Cheat_Sheet",
    "Password_Storage_Cheat_Sheet",
    "Cryptographic_Storage_Cheat_Sheet",
    "Secrets_Management_Cheat_Sheet",
    "Authentication_Cheat_Sheet",
    "Session_Management_Cheat_Sheet",
    "Logging_Cheat_Sheet",
    "Cross_Site_Scripting_Prevention_Cheat_Sheet",
    "DOM_based_XSS_Prevention_Cheat_Sheet",
    "Mass_Assignment_Cheat_Sheet",
    "XML_External_Entity_Prevention_Cheat_Sheet",
    "Access_Control_Cheat_Sheet",
    "LDAP_Injection_Prevention_Cheat_Sheet",
]


def _get(url: str, timeout: int = 60, retries: int = 2) -> bytes:
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers=_UA)
            return urllib.request.urlopen(request, timeout=timeout).read()
        except Exception:
            if attempt >= retries:
                raise
            time.sleep(2 * (attempt + 1))


def fetch_owasp(docs_root: Path) -> dict:
    out_dir = docs_root / "vulnerability" / "owasp"
    out_dir.mkdir(parents=True, exist_ok=True)
    failed: list[str] = []
    ok = 0
    for name in OWASP_SHEETS:
        url = f"https://raw.githubusercontent.com/OWASP/CheatSheetSeries/master/cheatsheets/{name}.md"
        dest = out_dir / f"{name}.md"
        if dest.exists():
            ok += 1
            continue
        try:
            dest.write_bytes(_get(url))
            ok += 1
            logger.info("OWASP ✓ %s", name)
        except Exception as exc:
            failed.append(name)
            logger.warning("OWASP ✗ %s: %s", name, exc)
    return {"owasp_ok": ok, "owasp_failed": failed}


def fetch_github_md(owner: str, repo: str, out_dir: Path, limit: int = 80) -> int:
    """下载仓库中全部 .md（单文件 <300KB，最多 limit 个），文件名拍平存储。"""
    tree: list | None = None
    branch = ""
    for candidate in ("master", "main"):
        try:
            url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{candidate}?recursive=1"
            data = json.loads(_get(url, timeout=30))
            tree = data.get("tree", [])
            branch = candidate
            break
        except Exception as exc:
            logger.warning("GitHub 树获取失败 %s/%s (%s): %s", owner, repo, candidate, exc)
    if tree is None:
        return 0

    paths = [
        item["path"]
        for item in tree
        if item.get("type") == "blob" and item["path"].endswith(".md") and item.get("size", 0) < 300_000
    ][:limit]

    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in paths:
        dest = out_dir / path.replace("/", "__")
        if dest.exists():
            count += 1
            continue
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{urllib.parse.quote(path, safe='/')}"
        try:
            dest.write_bytes(_get(url))
            count += 1
        except Exception as exc:
            logger.warning("下载失败 %s: %s", path, exc)
    return count


def fetch_all(docs_root: Path) -> dict:
    stats: dict = fetch_owasp(docs_root)
    stats["alibaba_java_spec"] = fetch_github_md(
        "mysterin", "alibaba-java-specification", docs_root / "best_practice" / "alibaba-java"
    )
    stats["p3c"] = fetch_github_md("alibaba", "p3c", docs_root / "best_practice" / "p3c")
    total_md = len(list(docs_root.rglob("*.md")))
    stats["total_md_files"] = total_md
    return stats
