"""微博平台包(登录态管理,不做发布)。

微博在 CreatorHub 里的角色是给产线供 Cookie:quote-video 热点线抓
s.weibo.com 热搜与正文都要 weibo.com 登录态(WEIBO_COOKIE)。登录/判活/
三层续期在这边统一维护,产线经 creatorhub_creds.py 桥接自动取用。
"""
from application.weibo.api import WeiboSession

__all__ = ["WeiboSession"]
