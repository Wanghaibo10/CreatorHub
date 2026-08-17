#!/usr/bin/env node
/**
 * sig4 签名器 —— `__NS_hxfalcon`，只有 3 个接口用（含 video/pc/submit）。
 *
 *   node sign4.js '<pathname>' '<原样要发出去的 body 字符串>'
 *   → __NS_hxfalcon=<sig>&caver=<n>
 *
 * ⚠️ 与 sig3 不同：SDK 吃的是**对象**（`requestBody`），内部自己 stringify。
 * 所以这里 parse 一遍再交进去，然后**把 SDK 用的 signInput 与传进来的原始
 * 字符串比对**：不一致就直接报错，而不是签出一个和实际请求对不上的签名
 * ——那种错会以「服务端说参数有问题」的面目出现，极难定位。
 *
 * signInput 形状实证：pathname + "caver=2" + JSON.stringify(requestBody)
 */
require("./env.js");

const pathname = process.argv[2] || "";
const rawBody = process.argv[3] || "{}";
let bodyObj;
try {
  bodyObj = JSON.parse(rawBody);
} catch (e) {
  console.error("SIGN_ERROR body 不是合法 JSON:", e.message);
  process.exit(1);
}

const Jose = require("./jose.js");
const caver = String(Jose.call("$getCatVersion") || "");
if (!caver) {
  console.error("SIGN_ERROR 拿不到 caver（$getCatVersion 返回空）");
  process.exit(1);
}

const cfg = {
  url: pathname,
  query: { caver },
  form: null,
  requestBody: bodyObj,
  projectInfo: { appKey: "mMovf2dVDF", radarId: "f4821aa475",
                 sampling: 1, debug: false },
};

Jose.call("$encode", [cfg, {
  suc: (sig, signInput) => {
    const used = String(signInput || "");
    // signInput 尾部应当就是我们要发的那串 body
    if (used && !used.endsWith(rawBody)) {
      console.error("SIGN_ERROR signInput 与实际 body 不一致");
      console.error("  SDK 用的尾部:", used.slice(-160));
      console.error("  实际要发的  :", rawBody.slice(-160));
      process.exit(2);
    }
    process.stdout.write("__NS_hxfalcon=" + sig + "&caver=" + caver);
  },
  err: (e) => { console.error("SIGN_ERROR", String(e)); process.exit(1); },
}]);
