# CreatorHub 项目规则

## 代码结构（2026-08-17 定型：顶层三包，对齐 mosshotel）

```
creatorhub/
├── app/                  # Web 层
│   ├── main.py           #   只做装配:lifespan 启动顺序 + router 挂载 + / 与 /health。不加业务!
│   ├── api/              #   HTTP 路由,一域一文件,只做出入参与持久化(14 个)
│   ├── service/          #   领域逻辑:跨路由共用的函数(账号资料/代理探测/报表辅助/画像…)
│   └── static/           #   前端(index.html + app.js)
├── application/          # 平台业务层
│   ├── douyin/ xhs/ kuaishou/ channels/ baijiahao/ toutiao/ wechat_mp/   # 一平台一包
│   ├── registry.py       #   平台声明唯一事实源,加平台只改这里 + 平台包本身
│   ├── base.py           #   图文平台协议基类(PlatformError/no_retry/风控翻译)
│   ├── browser/          #   浏览器自动化(Patchright);account_hub/ 按功能域分包
│   └── engine/           #   任务引擎:MonitorEngine 按职责拆为 7 个 Mixin
│                         #   (gates/scanning/watches/publishing/commenting/
│                         #    actions/collections),加新能力去对应文件
└── moss/                 # 基础设施层
    ├── common/           #   db / logging_setup / windowing / notifier / requests_extension
    ├── model/            #   SQLModel 表定义(from moss.model import …)
    └── core/             #   config / runtime(rt) / risk / settings
```

启动命令不变：`python -m uvicorn app.main:app --host 0.0.0.0 --port 8000`

## 铁律

1. **main.py 不加路由**。新接口进 `app/api/`（已有域加进对应文件，新域新建文件并在 main.py 挂载）。
2. **运行时状态一律走 `rt`**（`from moss.core.runtime import rt`）。禁止模块级全局存 browser/engine——
   拆出去的模块拿到的是导入时的 None 快照（2026-08-17 全面整改的起因之一）。
3. **api 文件之间不互相 import**。两个路由要共用的函数下沉到 `app/service/`。
4. **平台判断走 `application/registry.py`**，不要在业务代码里硬编码平台名分支。
5. **依赖方向只往下**：app → application → moss；moss 不许运行时 import 上层
   （moss/core/runtime.py 的 TYPE_CHECKING 类型提示除外）。代理解析等纯工具在
   moss/common/proxy_plan.py，别在 browser/ 里再长出同类纯函数。
6. 全项目**绝对导入**（`from moss.common.db import get_session`），不用相对导入。
7. 测试 mock 时 patch 函数**定义所在模块**；api 层对可 mock 依赖用模块引用
   （`from app.service import account_profile as _profile`）而非 from-import。
8. 日志用 `from moss.common.logging_setup import get_logger`；不要 print（CLI 工具脚本除外）。
9. 回归底线：`.venv/bin/python -m pytest tests/ -q` 须 **403 passed**
   （test_xhs_login_flow 的 2 个失败是 patchright 1.60.1 版本差异的既有问题）。
   tests/test_static_hygiene.py 会跑 pyflakes 全仓扫 undefined name——
   新增代码先过 `.venv/bin/python -m pyflakes <文件>` 再提交。

## 部署

- 生产在 Windows `C:\creatorhub`（8000 端口），与本仓库是两套代码，同步前先 diff。
  ⚠️ 本次目录重组后与 win 的 diff 是全量级别，同步要整仓覆盖而不是拷单文件。
- SQLite 已加 WAL/busy_timeout/foreign_keys（moss/common/db.py），不要绕过 get_session 直连。
