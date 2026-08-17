"""静态卫生:全仓 undefined-name 扫描。

2026-08-17 第二轮审查在三包迁移后的代码里抓到 4 个 NameError 运行时炸弹
(collections 两处 `if engine:`、publishing 漏 import log、ks_fetcher 漏
import json 且被 suppress 吞成静默失效)。共性是「只在特定分支触发 + 测试
没覆盖」,但 pyflakes 一条命令全能抓到——所以把它挂进回归。
"""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class UndefinedNameTests(unittest.TestCase):
    def test_no_undefined_names(self):
        try:
            import pyflakes  # noqa: F401
        except ImportError:
            self.skipTest("pyflakes 未安装(pip install pyflakes)")
        proc = subprocess.run(
            [sys.executable, "-m", "pyflakes", "app", "application", "moss"],
            cwd=ROOT, capture_output=True, text=True)
        bad = [line for line in proc.stdout.splitlines()
               if "undefined name" in line]
        self.assertEqual(bad, [], "存在未定义名(运行时 NameError 炸弹):\n"
                         + "\n".join(bad))


if __name__ == "__main__":
    unittest.main()
