// ⚠️ 重定向 console —— SDK 内部会 console.log 埋点错误(如 sendBeacon 缺失),
// 那些字会混进 stdout 把签名污染掉:实测 httpx 报
// `Invalid non-printable ASCII character in URL, '\n' at position 405`。
// stdout 必须只有签名本身。
console.log = (...a) => process.stderr.write(a.map(String).join(" ") + "\n");
console.info = console.warn = console.debug = console.log;
// 最小浏览器环境。VM 的 realm 对多数全局用 typeof 判断可缺省,
// 但 d.prototype.call 与内部 log 里有裸引用(window / location),必须给。
const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";
const loc = {
  href: "https://cp.kuaishou.com/article/publish/video",
  origin: "https://cp.kuaishou.com",
  protocol: "https:", host: "cp.kuaishou.com", hostname: "cp.kuaishou.com",
  port: "", pathname: "/article/publish/video", search: "", hash: "",
  toString(){ return this.href; },
};
const nav = { userAgent: UA, appVersion: UA.replace("Mozilla/", ""),
              platform: "MacIntel", language: "zh-CN", languages: ["zh-CN","zh"],
              vendor: "Google Inc.", hardwareConcurrency: 8, webdriver: false,
              plugins: {length: 0}, mimeTypes: {length: 0} };
const store = () => {
  const m = {};
  return { getItem: k => (k in m ? m[k] : null), setItem: (k,v) => { m[k] = String(v); },
           removeItem: k => { delete m[k]; }, clear: () => {}, key: () => null, length: 0 };
};
global.window = global;
global.location = loc;
global.navigator = nav;
global.localStorage = store();
global.sessionStorage = store();
global.screen = { width: 1920, height: 1080, availWidth: 1920, availHeight: 1055,
                  colorDepth: 24, pixelDepth: 24 };
global.history = { length: 1, state: null };
global.document = {
  cookie: "", referrer: "", title: "快手创作者服务平台", readyState: "complete",
  location: loc, documentElement: {}, body: {},
  createElement: () => ({ style: {}, setAttribute(){}, getContext: () => null,
                          appendChild(){}, getBoundingClientRect: () => ({}) }),
  getElementsByTagName: () => [], querySelector: () => null,
  querySelectorAll: () => [], addEventListener(){}, removeEventListener(){},
};
// 埋点上报:给个空实现,不然 SDK 内部 log 会抛(不影响签名,只是噪音)
nav.sendBeacon = () => true;
global.fetch = global.fetch || (() => Promise.resolve({ ok: true, text: () => Promise.resolve("") }));
global.XMLHttpRequest = function () {
  return { open(){}, send(){}, setRequestHeader(){}, addEventListener(){} };
};
global.addEventListener = () => {};
global.removeEventListener = () => {};
module.exports = { UA, loc, nav };
