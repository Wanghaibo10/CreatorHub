"""监控目标/评论监控共用的元数据辅助(分组名、别名、标签)。

从 main.py 抽出(2026-08-17 模块化):monitors、comment-watches、reports 三个域共用。
"""
from __future__ import annotations

import json

from moss.model import CommentWatch, MonitorTarget


def _meta_text(value: str | None, max_len: int) -> str:
    """清理用于界面管理的分组名/别名。"""
    return " ".join((value or "").strip().split())[:max_len]


def _meta_tags(value: list[str] | None) -> list[str] | None:
    """清理、去重标签；None 表示更新时不修改。"""
    if value is None:
        return None
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        tag = " ".join(str(raw or "").strip().split())[:24]
        key = tag.casefold()
        if not tag or key in seen:
            continue
        seen.add(key)
        result.append(tag)
        if len(result) >= 12:
            break
    return result


def _load_meta_tags(raw: str | None) -> list[str]:
    """兼容 JSON 与早期手工逗号分隔格式。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return _meta_tags([str(item) for item in data]) or []
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return _meta_tags(str(raw).replace("，", ",").split(",")) or []


def _dump_meta_tags(tags: list[str] | None) -> str:
    return json.dumps(tags or [], ensure_ascii=False, separators=(",", ":"))


def _meta_matches(item: MonitorTarget | CommentWatch, group_name: str, tag: str) -> bool:
    if group_name and item.group_name != group_name:
        return False
    if tag and tag not in _load_meta_tags(item.tags):
        return False
    return True
