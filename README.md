# CreatorHub

> 本地运行的多平台内容运营面板：**抖音 / 小红书 / 快手 / 视频号 / 百家号 / 今日头条 / 微信公众号**。

Python + FastAPI 统一 Web 界面：账号管理、作品与评论监控、内容下载、发布转发、自动评论、通知推送。账号登录态、数据库、媒体文件全部保存在本地。

视频平台的浏览器自动化由 [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) 驱动；图文平台（百家号/头条/公众号）与快手、视频号的发布走**纯协议 HTTP**，零浏览器。

## 平台能力

| 功能 | 抖音 | 小红书 | 快手 | 视频号 | 百家号 | 头条 | 公众号 |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| 登录 | 扫码/创作者/Cookie | 扫码/创作者/Cookie | 扫码/创作者/Cookie | 扫码 | 登录页自动/Cookie | 登录页自动/Cookie | 登录页自动/Cookie+token |
| 作品监控 | ✅ | ✅ 创作者/关键词 | ✅ | 仅本账号 | — | — | — |
| 评论监控 | ✅ | ✅ | ✅ | 仅本账号 | — | — | — |
| 弹幕监控 | ✅ | — | — | — | — | — | — |
| 关键词批量采集 | ✅ 作品/评论/媒体 | 计划中 | — | — | — | — | — |
| 内容下载 | ✅ 可选画质 | ✅ 图集/视频 | ✅ | — | — | — | — |
| 发布 | ✅ | ✅ | ✅ 纯协议 | ✅ 纯协议 | ✅ 纯协议 | ✅ 纯协议 | 草稿 |
| 自动评论/回复 | ✅ | ✅ | ✅ | — | — | — | — |
| 本账号管理 | 作品/关注/粉丝/私信 | 作品/关注/粉丝/私信 | 作品/关注/粉丝 | 作品/数据/评论 | — | — | — |

- 视频号只支持创作者助手中的本账号数据。
- 图文平台走 Markdown 正文 + 封面图发布：头条 `save=1` 即真发布；百家号封面必填 1 或 3 张；公众号只建草稿（群发需管理员在手机端扫码确认），登录要 Cookie + 后台 URL 里的 `token` 两样。
- 发布类接口是非幂等写操作，客户端**不做自动重试**（超时后服务端可能已受理）；403/429 风控信号统一进梯度冷却。

### 命令行发布工具（零浏览器，可独立于面板运行）

| 平台 | 入口 | 说明 |
|---|---|---|
| 快手 | `python -m application.kuaishou.publish_cli <视频> <文案> [--account 4] [--dry]` | 七步 HTTP + 两套签名。首次先跑 `python application/kuaishou/_sign/fetch_sdk.py` |
| 视频号 | `python -m application.channels.publish_cli <期目录> [account_id] [--dry]` | 走创作者助手接口，封面自动抽帧 |
| 视频号(浏览器版) | `python -m application.channels.publish_browser_cli <期目录>` | 回落用；发布后可用 `python -m application.channels.check_online_cli` 核对线上 |

登录态从 `data/creatorhub.db` 读，不抢正在跑的服务。快手签名 SDK（`jose.js`/`sig3sdk.js`）不入库，运行时抓取。

## 快速开始

### 环境要求

- Python 3.10+（本仓库开发环境 3.13）
- 桌面环境（扫码登录需要弹出浏览器）
- Google Chrome 稳定版（可选；小红书登录建议安装）
- Node.js 18+（百家号 Acs-Token 生成、小红书 API 发布兼容模式需要）
- ffmpeg（可选；未装时用 Python 依赖附带的）

### 一键启动

```bash
git clone https://github.com/Wanghaibo10/CreatorHub.git
cd CreatorHub
./start.sh          # Windows 用 .\start.cmd
```

首次运行自动建虚拟环境、装依赖和 Chromium、生成 `config.yaml`，然后打开 `http://127.0.0.1:8000`。

常用命令：`install`（更新依赖）、`check`（自检）、`--no-open`、`--port 8080`、`--reload`。

<details>
<summary>手动安装</summary>

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m patchright install chromium
npm install                                          # 百家号/小红书 API 模式需要
cp config.example.yaml config.yaml
python selftest.py
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

</details>

## 代码结构

顶层三包，依赖方向单向 `app → application → moss`：

```text
app/            # Web 层
├─ main.py      #   启动装配(lifespan + router 挂载),不含业务
├─ api/         #   HTTP 路由,一域一文件(14 个)
├─ service/     #   跨路由共用的领域逻辑
└─ static/      #   前端(index.html + app.js)
application/    # 平台业务层
├─ douyin/ xhs/ kuaishou/ channels/ baijiahao/ toutiao/ wechat_mp/  # 一平台一包
├─ registry.py  #   平台声明唯一事实源,加平台改这里 + 平台包
├─ base.py      #   图文平台协议基类(风控翻译/重试策略)
├─ browser/     #   Patchright 浏览器自动化(account_hub/ 按功能域分包)
└─ engine/      #   任务引擎(MonitorEngine = 7 个职责 Mixin)
moss/           # 基础设施层
├─ common/      #   db / 日志 / 通知渠道 / curl_cffi 发包基座(requests_extension)
├─ model/       #   SQLModel 表定义
└─ core/        #   config / 运行时容器 rt / 风控 / 键值设置
```

开发规范见 [`CLAUDE.md`](CLAUDE.md)。

## 界面预览

> 截图为脱敏示例数据，配色随平台切换。

![总览面板](assets/screenshots/overview-douyin.png)

<details>
<summary>更多界面</summary>

<table>
  <tr>
    <td width="50%"><strong>小红书浅色主题</strong><br><a href="assets/screenshots/overview-xiaohongshu.png"><img src="assets/screenshots/overview-xiaohongshu.png" alt="小红书总览面板"></a></td>
    <td width="50%"><strong>账号与代理</strong><br><a href="assets/screenshots/accounts-proxy.png"><img src="assets/screenshots/accounts-proxy.png" alt="账号登录与代理池"></a></td>
  </tr>
  <tr>
    <td width="50%"><strong>作品监控</strong><br><a href="assets/screenshots/monitor-posts.png"><img src="assets/screenshots/monitor-posts.png" alt="作品监控与下载"></a></td>
    <td width="50%"><strong>评论监控</strong><br><a href="assets/screenshots/monitor-comments.png"><img src="assets/screenshots/monitor-comments.png" alt="评论监控"></a></td>
  </tr>
  <tr>
    <td width="50%"><strong>内容发布</strong><br><a href="assets/screenshots/publish-workflow.png"><img src="assets/screenshots/publish-workflow.png" alt="内容发布与任务队列"></a></td>
    <td width="50%"><strong>链接下载</strong><br><a href="assets/screenshots/share-download.png"><img src="assets/screenshots/share-download.png" alt="分享链接解析与下载历史"></a></td>
  </tr>
  <tr>
    <td width="50%"><strong>自动评论</strong><br><a href="assets/screenshots/autocomment-rules.png"><img src="assets/screenshots/autocomment-rules.png" alt="自动评论规则与任务记录"></a></td>
    <td width="50%"><strong>本账号管理与私信</strong><br><a href="assets/screenshots/account-hub-dm.png"><img src="assets/screenshots/account-hub-dm.png" alt="本账号数据与私信管理"></a></td>
  </tr>
</table>

</details>

## 基本使用

**添加账号**：顶部选平台 → 左侧「账号」→ 扫码 / 创作者登录 / Cookie 粘贴。图文平台点「打开登录页」会弹出平台官方后台，登录成功后自动抓取 Cookie 并验证（公众号的 `token` 也自动从 URL 提取）；也可以手动粘贴 Cookie。

**监控与下载**：添加创作者主页、作品链接、短链或平台 ID 即可监控；关键词批量采集（抖音）一次最多 20 词，支持 Excel 导出；链接下载粘贴完整分享文案即可。新目标默认只监控订阅后的新作品，可选回填。

**发布与转发**：小红书支持图集/视频/定时；已下载的抖音作品可转发到小红书或视频号，反向亦可；图文平台在发布面板写 Markdown 正文 + 选封面。

**本账号与通知**：同步自己的作品/关注/粉丝/私信；作品健康监控（零播放/违规/下架提醒）；通知渠道 Bark / 钉钉 / Telegram。

**小红书 Chrome CDP 模式**：默认每个账号一个独立可见的系统 Chrome 会话 + 独立 Profile（与个人日常 Chrome 完全分开）；未装 Chrome 回退 Patchright Chromium。提交按钮只点一次，缺少成功证据时标记「结果待确认」而不自动重试。

## 配置

首次启动从 `config.example.yaml` 生成 `config.yaml`，常用项也可在面板「设置」里改：

```yaml
engine:
  scan_interval_seconds: 300         # 轮询间隔
  monitor_initial_backfill_count: 0  # 0=只看新作品,-1=尽量回填
  worker_pool_size: 2                # 下载并发
  media_dir: ./data/media
storage:
  db_path: ./data/creatorhub.db
proxies: []                          # http:// 或 socks5://,一号一代理
```

完整参数见 [`config.example.yaml`](config.example.yaml)。数据库字段启动时自动迁移。

## 统一平台风控

默认 `risk_control.mode: conservative`，所有平台操作统一过持久化闸门：

- 同账号的评论/私信/关注/发布共享写间隔与额度，「立即执行」也不绕过冷却
- 同一网络出口的账号串行访问；命中 403/429/461/471 或验证码后按 30 分钟 → 2 小时 → 6 小时 → 24 小时梯度冷却，连续 3 次轻探测成功才降级
- 被拦下的任务保持 `pending`，服务重启后恢复执行态
- 新账号走 `native` 画像（不注入指纹脚本），写操作过硬门禁：系统 Chrome + 有头页面 + 独立 Profile + 已验证代理出口基线
- 账号 Profile 有跨进程占用保护，防多进程打开同一目录

完整参数见 `config.example.yaml` 的 `risk_control` 段。

## 常见问题

| 问题 | 处理 |
|---|---|
| Patchright 启动失败/找不到浏览器 | `python -m patchright install chromium` |
| macOS 装依赖报 clang++ 错误 | 删旧 `.venv` 后重跑 `./start.sh install` |
| 扫码没弹窗 | 需要桌面环境；抖音/小红书/快手也可 Cookie 登录 |
| 公众号 Cookie 登录报 400 | 还需要后台页面 URL 里的 `token=` 参数 |
| 百家号发布提示缺 Acs-Token 生成器 | 装 Node.js 18+ 并 `npm install` |
| 抓取不到作品/评论 | 查登录态、链接和网络，重新登录并降频 |
| 小红书链接解析失败 | 重新复制含 `xsec_token` 的完整链接 |
| Windows 下 Patchright 子进程错误 | 单 worker 启动，不要加 `--workers` |

## 数据目录

```text
data/
├─ creatorhub.db   # SQLite(WAL 模式)
├─ media/          # 下载内容
└─ profiles/       # 账号浏览器 Profile 与登录态
```

备份时带上 `config.yaml` 和整个 `data/`。

## 致谢与声明

本项目基于 [3441293738/creatorhub](https://github.com/3441293738/creatorhub) 二次开发（新增图文平台、纯协议发布线与三层架构重构）。

仅用于技术学习和个人内容管理，不提供账号、Cookie、代理或平台数据。使用时请遵守目标平台规则及所在地法律法规，尊重内容版权与个人隐私。
