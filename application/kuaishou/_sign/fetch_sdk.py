#!/usr/bin/env python3
"""从快手创作平台抓签名 SDK,抠出两个 webpack 模块落到本目录。

    python fetch_sdk.py [--force]

产出 `sig3sdk.js`(模块 75407)与 `jose.js`(模块 34005)。

⚠️ **这两个文件是快手的代码,不入库**(见 .gitignore)。
项目铁律:第三方逆向代码只用不收 —— vendor 进公开仓库既是许可污染,
也让 SDK 更新之后我们还抱着旧的跑。改成运行时抓,hash 变了自动跟上。

页面 HTML 里就有 `<script src=...chunk-vendors.*.js>`,不需要浏览器。
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = "https://cp.kuaishou.com/article/publish/video"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

#: (模块 id, 产出文件, 该模块定义的起始特征)
#: ⚠️ 两个模块的**写法不同**:75407 是 `75407:e=>{…}`(箭头函数、单参数无括号),
#: 34005 是 `34005:(a,b,c)=>{…}`。用 `\d+:\(` 这种正则会漏掉前者 ——
#: 我就因此一度断定「75407 不在 vendors 里」。
MODULES = [
    ("75407", "sig3sdk.js", "75407:e=>{"),
    ("34005", "jose.js", "34005:(__unused_webpack___webpack_module__"),
]


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def find_vendors_url() -> str:
    html = _get(PAGE)
    m = re.search(r'src="(//[^"]*chunk-vendors\.[a-f0-9]+\.js)"', html)
    if not m:
        raise SystemExit("HTML 里没找到 chunk-vendors —— 页面结构变了?")
    return "https:" + m.group(1)


def extract_block(src: str, marker: str) -> str:
    """从 marker 处的 `=>{` 开始括号配平,取出模块体(不含最外层花括号)。"""
    start = src.index(marker)
    i = src.index("=>{", start) + 2
    depth, j, in_str, esc = 0, i, None, False
    while j < len(src):
        ch = src[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == in_str:
                in_str = None
        else:
            if ch in "\"'`":
                in_str = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
        j += 1
    return src[i + 1:j]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="已存在也重抓")
    args = ap.parse_args()

    if not args.force and all((HERE / f).is_file() for _, f, _ in MODULES):
        print("SDK 已在,跳过(--force 强制重抓)")
        return 0

    url = find_vendors_url()
    print(f"chunk-vendors: {url}")
    src = _get(url)
    print(f"  {len(src)} 字节")

    for mod, out, marker in MODULES:
        if marker not in src:
            raise SystemExit(f"模块 {mod} 的特征串没命中 —— 快手改版了,"
                             f"去 vendors 里重新找它的定义写法")
        body = extract_block(src, marker)
        if out == "jose.js":
            #: 34005 是 webpack ESM 形态:去掉 `__webpack_require__.d(...)`
            #: 与末尾的 `__WEBPACK_DEFAULT_EXPORT__=`,导出 Jose 本身
            body = body.replace(
                "__webpack_require__.d(__webpack_exports__,{I:()=>Jose});", "", 1)
            body = body.replace(",__WEBPACK_DEFAULT_EXPORT__=Jose", "")
            if body.endswith("__WEBPACK_DEFAULT_EXPORT__=Jose"):
                body = body[:-len("__WEBPACK_DEFAULT_EXPORT__=Jose")]
            js = body + "\nmodule.exports = Jose;\n"
        else:
            #: 75407 是 UMD 形态:`!function(t,n){e.exports=n()}(window, …)`,
            #: 给它一个 module 壳把 exports 接出来
            js = ("require('./env.js');\nvar e = {exports: {}};\n" + body
                  + "\nmodule.exports = e.exports;\n")
        (HERE / out).write_text(js)
        print(f"  → {out}  {len(js)} 字节")

    print("\n完成。自检:node signraw.js sig3 '{\"a\":1}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
