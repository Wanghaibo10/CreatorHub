// 百度 heicha Acs-Token 脱环境：最小浏览器环境 + 加载 acs-2083.js
// 实证结论（2026-08-08）：SDK 只摸 12 个全局属性，不需要 jsdom / canvas。
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const SDK = path.join(__dirname, 'sdk');
const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36';
const HREF = 'https://baijiahao.baidu.com/builder/rc/edit?type=news';

function makeEnv(opts = {}) {
  const trace = opts.trace ? [] : null;
  const elStub = (tag) => ({
    tagName: String(tag).toUpperCase(), style: {},
    setAttribute() {}, getAttribute: () => null, appendChild() {},
    addEventListener() {}, removeEventListener() {},
    getContext: () => null, toDataURL: () => 'data:image/png;base64,',
    width: 0, height: 0,
  });

  const sandbox = {
    navigator: {
      userAgent: UA, platform: 'MacIntel', language: 'zh-CN',
      languages: ['zh-CN', 'zh'], hardwareConcurrency: 16,
      appName: 'Netscape', appVersion: '5.0 (Macintosh)', appCodeName: 'Mozilla',
      vendor: 'Google Inc.', product: 'Gecko', productSub: '20030107',
      cookieEnabled: true, onLine: true, doNotTrack: null, maxTouchPoints: 0,
      deviceMemory: 8, plugins: { length: 5 }, mimeTypes: { length: 2 },
      webdriver: false, javaEnabled: () => false,
      userAgentData: { brands: [], mobile: false, platform: 'macOS' },
    },
    document: {
      createElement: elStub, createElementNS: (_ns, t) => elStub(t),
      getElementsByTagName: () => [], getElementById: () => null,
      querySelector: () => null, querySelectorAll: () => [],
      addEventListener() {}, removeEventListener() {},
      documentElement: elStub('html'), head: elStub('head'), body: elStub('body'),
      cookie: opts.cookie || '', referrer: HREF, URL: HREF, domain: 'baijiahao.baidu.com',
      title: '百家号', readyState: 'complete', characterSet: 'UTF-8', visibilityState: 'visible',
    },
    location: {
      href: HREF, host: 'baijiahao.baidu.com', hostname: 'baijiahao.baidu.com',
      protocol: 'https:', origin: 'https://baijiahao.baidu.com',
      pathname: '/builder/rc/edit', search: '?type=news', hash: '', port: '',
    },
    screen: { width: 1920, height: 1080, availWidth: 1920, availHeight: 1055,
              availLeft: 0, availTop: 25, colorDepth: 24, pixelDepth: 24 },
    history: { length: 3, pushState() {}, replaceState() {} },
    performance: { now: () => Date.now() - 1000, timing: { navigationStart: Date.now() - 5000 } },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {}, clear() {}, length: 0 },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {}, clear() {}, length: 0 },
    XMLHttpRequest: function () {
      this.open = () => {}; this.send = () => {}; this.setRequestHeader = () => {};
      this.readyState = 0; this.status = 0; this.responseText = '';
      this.addEventListener = () => {};
    },
    fetch: () => Promise.resolve({ ok: true, text: () => Promise.resolve(''), json: () => Promise.resolve({}) }),
    setTimeout, setInterval, clearTimeout, clearInterval,
    setImmediate, queueMicrotask,
    console: opts.silent ? { log() {}, error() {}, warn() {}, info() {}, debug() {} } : console,
    btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
    atob: (s) => Buffer.from(s, 'base64').toString('binary'),
    innerWidth: 1920, innerHeight: 900, outerWidth: 1920, outerHeight: 1055,
    devicePixelRatio: 2, isSecureContext: true, crossOriginIsolated: false,
    addEventListener() {}, removeEventListener() {}, dispatchEvent: () => true,
    matchMedia: () => ({ matches: false, addListener() {}, removeListener() {} }),
    requestAnimationFrame: (cb) => setTimeout(() => cb(Date.now()), 16),
    cancelAnimationFrame: clearTimeout,
    Image: function () { this.src = ''; this.onload = null; },
    WebSocket: function () { this.send = () => {}; this.close = () => {}; },
    Notification: { permission: 'default' },
    // eval 必须挂（SDK 里 window.eval 被摸过；缺了走的是 Function 兜底路径）
  };
  // 标准内置对象全量透传
  for (const k of ['Math','Date','JSON','String','Number','Boolean','Array','Object','Function',
    'RegExp','Error','TypeError','RangeError','SyntaxError','Promise','Symbol','Map','Set',
    'WeakMap','WeakSet','Proxy','Reflect','BigInt','ArrayBuffer','DataView','SharedArrayBuffer',
    'Uint8Array','Int8Array','Uint16Array','Int16Array','Uint32Array','Int32Array',
    'Float32Array','Float64Array','Uint8ClampedArray','TextEncoder','TextDecoder','URL',
    'URLSearchParams','encodeURIComponent','decodeURIComponent','encodeURI','decodeURI',
    'escape','unescape','parseInt','parseFloat','isNaN','isFinite','Intl','Infinity','NaN'])
    if (typeof globalThis[k] !== 'undefined') sandbox[k] = globalThis[k];

  // DOM 构造函数族：SDK 用 `x instanceof Window` 之类做环境校验，
  // 缺一个就在回调里抛 ReferenceError（实测第一个卡住的就是 Window）。
  const ctors = ['Window','Document','HTMLDocument','Navigator','Screen','Location','History',
    'Element','HTMLElement','Node','EventTarget','CharacterData','Text','Comment',
    'HTMLCanvasElement','CanvasRenderingContext2D','WebGLRenderingContext','WebGL2RenderingContext',
    'HTMLDivElement','HTMLIFrameElement','HTMLScriptElement','HTMLImageElement','HTMLAnchorElement',
    'HTMLInputElement','HTMLFormElement','HTMLBodyElement','HTMLHeadElement','HTMLStyleElement',
    'Storage','Performance','PerformanceTiming','Event','UIEvent','MouseEvent','KeyboardEvent',
    'TouchEvent','Touch','TouchList','PointerEvent','CustomEvent','MessageEvent','ErrorEvent',
    'Plugin','PluginArray','MimeType','MimeTypeArray','NodeList','HTMLCollection','DOMTokenList',
    'CSSStyleDeclaration','DOMParser','XMLSerializer','MutationObserver','IntersectionObserver',
    'ResizeObserver','FileReader','Blob','File','FormData','Headers','Request','Response',
    'AudioContext','OfflineAudioContext','SpeechSynthesis','RTCPeerConnection','MediaDevices',
    'BatteryManager','Worker','SharedWorker','ServiceWorker','Crypto','SubtleCrypto',
    'CSS','Selection','Range','ShadowRoot','CustomElementRegistry','VisualViewport'];
  for (const c of ctors) {
    if (sandbox[c]) continue;
    const F = new Function(`return function ${c}(){}`)();
    F.prototype.toString = function () { return `[object ${c}]`; };
    sandbox[c] = F;
  }

  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.top = sandbox;
  sandbox.parent = sandbox;
  sandbox.globalThis = sandbox;

  // 让 instanceof 校验成立：window/document/navigator 各自继承对应构造函数的原型
  Object.setPrototypeOf(sandbox, sandbox.Window.prototype);
  Object.setPrototypeOf(sandbox.document, sandbox.HTMLDocument.prototype);
  Object.setPrototypeOf(sandbox.HTMLDocument.prototype, sandbox.Document.prototype);
  Object.setPrototypeOf(sandbox.navigator, sandbox.Navigator.prototype);
  Object.setPrototypeOf(sandbox.screen, sandbox.Screen.prototype);
  Object.setPrototypeOf(sandbox.location, sandbox.Location.prototype);
  Object.setPrototypeOf(sandbox.history, sandbox.History.prototype);
  Object.setPrototypeOf(sandbox.performance, sandbox.Performance.prototype);
  Object.setPrototypeOf(sandbox.localStorage, sandbox.Storage.prototype);
  Object.setPrototypeOf(sandbox.sessionStorage, sandbox.Storage.prototype);

  const ctx = vm.createContext(sandbox);
  // eval 要在 context 内求值才指向沙箱
  vm.runInContext('window.eval = eval;', ctx);

  return { sandbox, ctx, trace };
}

function loadSdk(ctx, files) {
  for (const f of files) {
    const code = fs.readFileSync(path.join(SDK, f), 'utf8');
    vm.runInContext(code, ctx, { filename: f, timeout: 30000 });
  }
}

module.exports = { makeEnv, loadSdk, UA, HREF, SDK };
