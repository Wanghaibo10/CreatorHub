"""统一日志配置。

为什么需要:此前全项目没有任何 basicConfig/dictConfig,root logger 零 handler、
级别 WARNING——**32 处 `logger.info(...)` 全部被丢弃**,写了等于没写。
同时 88 处 `print()` 直接打到 stdout,没有时间戳/级别/来源,出问题无从追。

这里给出唯一的日志出口:
  · 控制台:带时间/级别/logger 名,uvicorn 的访问日志沿用它自己的格式
  · 文件:data/logs/creatorhub.log,按大小滚动(默认 10MB × 5 份)
  · 第三方库降噪:patchright/httpx/curl_cffi 的 DEBUG 太吵,统一压到 WARNING

调用时机:main.py 里 init_db 之前(越早越好,晚了早期日志就丢了)。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

# 项目自己的 logger 树根。业务代码一律 logging.getLogger("creatorhub.xxx"),
# 这样能整棵树统一调级别,也不会影响第三方库。
ROOT_NAME = "creatorhub"

_FMT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%m-%d %H:%M:%S"

# 这些库的 INFO/DEBUG 会把有用信息淹掉
_NOISY = ("patchright", "playwright", "httpx", "httpcore", "curl_cffi",
          "urllib3", "asyncio", "PIL", "multipart")

_configured = False


def setup_logging(level: str = "", log_dir: str | Path = "",
                  max_bytes: int = 10 * 1024 * 1024, backups: int = 5) -> logging.Logger:
    """配置日志。重复调用是安全的(只生效一次)。

    level 取值优先级:显式参数 > 环境变量 CREATORHUB_LOG_LEVEL > INFO
    """
    global _configured
    root = logging.getLogger()
    if _configured:
        return logging.getLogger(ROOT_NAME)

    lv_name = (level or os.environ.get("CREATORHUB_LOG_LEVEL") or "INFO").upper()
    lv = getattr(logging, lv_name, logging.INFO)

    fmt = logging.Formatter(_FMT, datefmt=_DATEFMT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(lv)
    root.addHandler(console)

    # 文件日志失败不能拖垮启动(只读目录/权限问题都可能),降级成只有控制台
    try:
        d = Path(log_dir or Path("data") / "logs")
        d.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            d / "creatorhub.log", maxBytes=max_bytes, backupCount=backups,
            encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(lv)
        root.addHandler(fh)
    except OSError as e:
        console.handle(logging.LogRecord(
            ROOT_NAME, logging.WARNING, __file__, 0,
            "文件日志不可用(%s),只输出到控制台", (e,), None))

    root.setLevel(lv)
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)

    _configured = True
    lg = logging.getLogger(ROOT_NAME)
    lg.debug("日志已初始化 level=%s", lv_name)
    return lg


def get_logger(name: str = "") -> logging.Logger:
    """取项目 logger。`get_logger("engine.monitor")` → creatorhub.engine.monitor"""
    return logging.getLogger(f"{ROOT_NAME}.{name}" if name else ROOT_NAME)
