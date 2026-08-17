"""本机文件管理器操作(定位/打开文件)与触发门禁。

从 main.py 抽出(2026-08-17 模块化):contents、collections、share-download
三个域共用。仅允许本机回环地址的同源页面触发,防远程滥用。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from moss.common.windowing import (EXPLORER_WINDOW_CLASSES, bring_window_to_front, capture_window_snapshot)

_file_manager_lock = threading.Lock()


def _require_local_action(request: Request, action: str = "reveal") -> None:
    """仅允许本机 CreatorHub 页面触发文件管理器操作。"""
    def is_loopback(host: str) -> bool:
        host = host.split("%", 1)[0].casefold()
        if host == "localhost":
            return True
        try:
            return ip_address(host).is_loopback
        except ValueError:
            return False

    client_host = request.client.host if request.client else ""
    page_host = request.url.hostname or ""
    try:
        origin = urlsplit(request.headers.get("origin", ""))
        same_origin = (origin.scheme == request.url.scheme and
                       (origin.hostname or "").casefold() == page_host.casefold() and
                       origin.port == request.url.port)
    except ValueError:
        same_origin = False
    local_action = request.headers.get("x-creatorhub-local-action") == action
    if not is_loopback(client_host) or not is_loopback(page_host) or \
            not same_origin or not local_action:
        raise HTTPException(403, "仅允许从本机 CreatorHub 页面执行文件操作")


def _reveal_in_file_manager(path: Path):
    """Open a directory or select a file in the host OS file manager."""
    target = str(path)
    if sys.platform == "win32":
        with _file_manager_lock:
            folder = path if path.is_dir() else path.parent
            snapshot = capture_window_snapshot(EXPLORER_WINDOW_CLASSES)
            if path.is_dir():
                os.startfile(target)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["explorer.exe", "/select,", target])
            bring_window_to_front(snapshot, EXPLORER_WINDOW_CLASSES,
                                  title_hint=folder.name, timeout=2.5)
        return
    if sys.platform == "darwin":
        args = ["open", target] if path.is_dir() else ["open", "-R", target]
    else:
        args = ["xdg-open", target if path.is_dir() else str(path.parent)]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def _open_local_path(path: Path):
    """Open a local media file with the host OS default application."""
    target = str(path)
    if sys.platform == "win32":
        os.startfile(target)  # type: ignore[attr-defined]
        return
    args = ["open", target] if sys.platform == "darwin" else ["xdg-open", target]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
