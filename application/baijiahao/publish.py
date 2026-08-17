"""百家号发布适配层:对齐 monitor.py 里 `publish_*` 的既有契约 `-> (ok, url, err)`。"""
from __future__ import annotations

import logging
from pathlib import Path

from application.base import ArticleDraft, AuthExpired, PlatformError
from application.baijiahao.api import BaijiahaoSession

logger = logging.getLogger("creatorhub.platforms.baijiahao")


async def publish_baijiahao(cookie: str, title: str, desc: str, files: list[str], *,
                            markdown: str = "", covers: list[str] | None = None,
                            topics: str = "", proxy: str = "", ua: str = "",
                            account_id: int | None = None,
                            publish: bool = True,
                            ai_generated: bool = True) -> tuple[bool, str, str]:
    """发一篇百家号图文。返回 (ok, url, err)。

    封面平台必填,只认 1 张或 3 张:covers 不给时取 files 里的图(≥3 张用三图版式,
    否则取第 1 张单图)。ai_generated 默认开——管线产出是 AI 生成的,平台规则要求声明。
    """
    pics = [c for c in (covers or files or []) if c]
    cover_list = [Path(p) for p in (pics[:3] if len(pics) >= 3 else pics[:1])]
    draft = ArticleDraft(
        title=title,
        markdown=markdown or desc,
        base_dir=Path(files[0]).parent if files else Path.cwd(),
        covers=cover_list,
        topics=[t for t in (topics or "").split(",") if t.strip()],
        ai_generated=ai_generated,
    )
    try:
        async with BaijiahaoSession(cookie, proxy=proxy, ua=ua,
                                    account_id=account_id) as api:
            await api.check_login()
            result = await api.publish_article(draft, publish=publish)
            return result.ok, result.url, ""
    except AuthExpired as e:
        return False, "", str(e)
    except PlatformError as e:
        return False, "", str(e)
    except Exception as e:                       # noqa: BLE001
        logger.exception("[百家号] 发布异常")
        return False, "", f"发布异常: {e!r}"
