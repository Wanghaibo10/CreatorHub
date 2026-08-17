#!/usr/bin/env node
/**
 * 用**原始 body 字符串**签名(不 parse 再 stringify)。
 *
 *   node signraw.js sig3 '<原样要发出去的 body 字符串>'
 *
 * ⚠️ 为什么不接对象:签名算的是 `md5(query串 + JSON.stringify(body))`,
 * 而 Python 的 json.dumps 与 JS 的 JSON.stringify 在空格、Unicode 转义、
 * 数字格式上**可能不同**。只要差一个字节 md5 就不同、签名就废。
 * 让调用方把真正要发的那串字节交进来,签的和发的保证是同一个东西。
 */
require("./env.js");
const crypto = require("crypto");

const raw = process.argv[3] || "";
const query = process.argv[4] ? JSON.parse(process.argv[4]) : {};
const joinQuery = (o) =>
  Object.keys(o).sort().reduce((a, k) => a + k + "=" + encodeURI(o[k]), "");

const input = crypto.createHash("md5")
  .update(joinQuery(query) + raw, "utf8").digest("hex");

const sdk = require("./sig3sdk.js");
sdk.call("$encode", [input, {
  suc: (v) => process.stdout.write("__NS_sig3=" + v),
  err: (e) => { console.error("SIGN_ERROR", String(e)); process.exit(1); },
}]);
