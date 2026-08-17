"""进程级运行时状态容器。

拆 main.py 的前置条件。原来 `browser` / `engine` / `im_receiver` 是 main.py 的模块级
全局,在 lifespan 里用 `global` 重新赋值——一旦把路由拆进别的模块,
`from .main import browser` 拿到的是**导入那一刻的快照(None)**,启动后永远不会更新,
所有依赖浏览器的接口都会 503。

所以状态放在一个对象里,各模块 `from moss.core.runtime import rt` 拿到的是同一个引用,
lifespan 改的是它的属性,谁都能看到最新值。

用法:
    from moss.core.runtime import rt
    if rt.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    rt.require_browser()          # 等价写法,少写一次判断
"""
from __future__ import annotations

from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:                       # 只为类型提示,运行时不导入(避免循环依赖)
    from application.browser.manager import BrowserManager
    from application.engine.monitor import MonitorEngine


class Runtime:
    """整个进程共享的一份可变状态。故意不是 dataclass —— 属性要能被 lifespan 改。"""

    def __init__(self) -> None:
        self.browser: Optional["BrowserManager"] = None
        self.engine: Optional["MonitorEngine"] = None
        self.im_receiver: Any = None
        # 扫码登录任务:task_id -> {status, qr, account_id, ...}
        self.login_tasks: Dict[str, dict] = {}
        # 用户手动开着的浏览器窗口:account_id -> context
        self.open_browsers: Dict[int, Any] = {}

    def require_browser(self) -> "BrowserManager":
        from fastapi import HTTPException
        if self.browser is None:
            raise HTTPException(503, "浏览器未就绪")
        return self.browser

    def require_engine(self) -> "MonitorEngine":
        from fastapi import HTTPException
        if self.engine is None:
            raise HTTPException(503, "监控引擎未就绪")
        return self.engine

    def reset(self) -> None:
        """测试用:清干净,避免用例之间互相串状态。"""
        self.browser = self.engine = self.im_receiver = None
        self.login_tasks.clear()
        self.open_browsers.clear()


rt = Runtime()
