"""下载开源代码评审/缺陷数据集，转换为统一的 KBChunk JSONL。

数据源（均为公开研究数据集）：
- microsoft/code_review（CodeReviewer, ICSE 2022）：真实 PR 的评审意见
- microsoft/code_x_glue_cc_defect_detection：函数级缺陷样本

使用流式读取并按 limit 截断，避免全量下载。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_CODE_LIMIT_CHARS = 2000
_COMMENT_LIMIT_CHARS = 1500


def download_all(data_dir: str, limit: int = 500) -> dict[str, int]:
    try:
        from datasets import load_dataset  # noqa: F401  # 校验可选依赖
    except ImportError as exc:
        raise SystemExit("缺少 datasets 依赖：pip install 'reviewhive[datasets]'") from exc

    out_dir = Path(data_dir) / "downloads"
    out_dir.mkdir(parents=True, exist_ok=True)
    stats = {
        "code_review": _convert_code_review(out_dir, limit),
        "codexglue_defect": _convert_defect_detection(out_dir, limit),
    }
    return stats


def _convert_code_review(out_dir: Path, limit: int) -> int:
    from datasets import load_dataset

    path = out_dir / "code_review.jsonl"
    written = 0
    stream = load_dataset("microsoft/code_review", split="train", streaming=True)
    with path.open("w", encoding="utf-8") as fp:
        for row in stream:
            if written >= limit:
                break
            comment = str(row.get("comment") or "").strip()
            code = str(row.get("head_contents") or row.get("code") or "")
            if len(comment) < 15 or not code:
                continue
            record = {
                "source": "microsoft/code_review",
                "kind": "review_example",
                "language": "java" if "class " in code[:400] else "",
                "title": f"评审样例：{comment[:40]}",
                "content": comment[:_COMMENT_LIMIT_CHARS],
                "code": code[:_CODE_LIMIT_CHARS],
            }
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1
    logger.info("code_review 写入 %d 条", written)
    return written


def _convert_defect_detection(out_dir: Path, limit: int) -> int:
    from datasets import load_dataset

    path = out_dir / "codexglue_defect.jsonl"
    written = 0
    stream = load_dataset("microsoft/code_x_glue_cc_defect_detection", split="train", streaming=True)
    with path.open("w", encoding="utf-8") as fp:
        for row in stream:
            if written >= limit:
                break
            code = str(row.get("code") or "").strip()
            label = row.get("defect", row.get("label"))
            if not code or label in (0, False, None):
                continue
            record = {
                "source": "microsoft/code_x_glue_cc_defect_detection",
                "kind": "vulnerability",
                "language": "c",
                "title": "缺陷函数样例",
                "content": "该函数被标注为存在缺陷（来源：CodeXGLUE defect detection）。评审类似结构时注意资源处理、边界条件与内存安全。",
                "code": code[:_CODE_LIMIT_CHARS],
            }
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1
    logger.info("codexglue_defect 写入 %d 条", written)
    return written
