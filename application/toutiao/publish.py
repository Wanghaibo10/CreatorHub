"""今日头条发布适配层:对齐 monitor.py 里 `publish_*` 的既有契约。

统一签名 `-> (ok, url, err)`,与 publish_douyin/publish_xhs/publish_channels 一致,
这样 monitor 的发布分发只多一条分支,不需要改它的结果处理与风控记账。

与浏览器系平台的差异:图文平台不吃 browser/identity/state 三个参数,
凭证是账号上的 Cookie。为了让分发处保持同一个调用形状,这里照样接收它们并忽略。
"""
from __future__ import annotations

import logging
from pathlib import Path

from application.base import ArticleDraft, AuthExpired, PlatformError
from application.toutiao.api import ToutiaoSession

logger = logging.getLogger("creatorhub.platforms.toutiao")


async def publish_toutiao(cookie: str, title: str, desc: str, files: list[str], *,
                          markdown: str = "", covers: list[str] | None = None,
                          topics: str = "", proxy: str = "", ua: str = "",
                          account_id: int | None = None,
                          publish: bool = True) -> tuple[bool, str, str]:
    """发一篇头条图文。返回 (ok, url, err)。

    正文优先用 markdown;没给就把 desc 当正文(纯文本会被包成 <p class="pgc-p">)。
    covers 不给时取 files 里第一张图当封面——头条**不会**自动拿正文图当封面,
    不给封面就是无封面文章(早期 17 篇的教训)。
    """
    cover_list = [Path(c) for c in (covers or files or [])[:1] if c]
    draft = ArticleDraft(
        title=title,
        markdown=markdown or desc,
        base_dir=Path(files[0]).parent if files else Path.cwd(),
        covers=cover_list,
        topics=[t for t in (topics or "").split(",") if t.strip()],
    )
    try:
        async with ToutiaoSession(cookie, proxy=proxy, ua=ua,
                                  account_id=account_id) as api:
            await api.check_login()
            result = await api.publish_article(draft, publish=publish)
            return result.ok, result.url, ""
    except AuthExpired as e:
        return False, "", str(e)
    except PlatformError as e:
        return False, "", str(e)
    except Exception as e:                       # noqa: BLE001 —— 兜底翻译给调用方
        logger.exception("[头条] 发布异常")
        return False, "", f"发布异常: {e!r}"
