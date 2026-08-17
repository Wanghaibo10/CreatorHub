"""按资源域拆分的 API 路由。

main.py 曾经是 6500 行、147 个路由的单文件。这里按资源域拆成 APIRouter,
每个模块只管自己那摊事;共享的领域逻辑放 app/services/,
进程级运行时状态(browser/engine)走 app/runtime.py 的 rt 单例
——**不要** `from ..main import browser`,那样拿到的是导入时的 None 快照。
"""
