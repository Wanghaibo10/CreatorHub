"""公众号发布适配层:对齐 monitor.py 的 `publish_*` 契约 `-> (ok, url, err)`。

⚠️ 语义差异:公众号**只能到草稿**(群发要管理员扫码)。这里成功即视为完成,
调用方拿到的 url 是草稿箱地址,PublishTask 应标 done 并提示人工发送。
凭证是 cookie + token 两样,token 不在 cookie 里,用 `cookie` 参数传
"<cookie串>||<token>" 或显式给 token 参数。
"""
from __future__ import annotations

import logging
from pathlib import Path

from application.base import ArticleDraft, AuthExpired, PlatformError
from application.wechat_mp.api import WechatMpSession

logger = logging.getLogger("creatorhub.platforms.wechat_mp")


def split_creds(raw: str) -> tuple[str, str]:
    """账号库里用 "<cookie>||<token>" 一列存两样凭证,这里拆开。"""
    if "||" in (raw or ""):
        ck, tk = raw.split("||", 1)
        return ck.strip(), tk.strip()
    return (raw or "").strip(), ""


async def publish_wechat_mp(cookie: str, title: str, desc: str, files: list[str], *,
                            token: str = "", markdown: str = "",
                            covers: list[str] | None = None, digest: str = "",
                            author: str = "", proxy: str = "", ua: str = "",
                            account_id: int | None = None,
                            publish: bool = False,
                            original: bool = True) -> tuple[bool, str, str]:
    ck, tk_in_cookie = split_creds(cookie)
    tk = token or tk_in_cookie
    cover_list = [Path(c) for c in (covers or files or [])[:1] if c]
    draft = ArticleDraft(
        title=title,
        markdown=markdown or desc,
        base_dir=Path(files[0]).parent if files else Path.cwd(),
        covers=cover_list, digest=digest, author=author,
        extras={"original": original},
    )
    try:
        async with WechatMpSession(ck, token=tk, proxy=proxy, ua=ua,
                                   account_id=account_id) as api:
            info = await api.check_login()
            result = await api.publish_article(draft)
            logger.info("[公众号] %s 草稿已建 appmsgid=%s",
                        info.get("nickname"), result.article_id)
            return result.ok, "https://mp.weixin.qq.com/cgi-bin/appmsg", ""
    except AuthExpired as e:
        return False, "", str(e)
    except PlatformError as e:
        return False, "", str(e)
    except Exception as e:                       # noqa: BLE001
        logger.exception("[公众号] 发布异常")
        return False, "", f"发布异常: {e!r}"
