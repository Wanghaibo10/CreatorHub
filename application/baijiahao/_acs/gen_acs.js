// 纯 Node 生成百度 heicha 的 Acs-Token（不开浏览器）。
//
// 2026-08-08 逆向实录（推翻了此前"必须开浏览器"的结论）：
//   · acs-2112.js 是 VMP，API 名全藏在字节码里，静态搜 `navigator.`/`canvas` 命中数为 0，
//     所以此前**从静态特征推测**要补 canvas/webgl 指纹、判定是周级工作量 —— 实跑推翻了：
//   · 在 vm 沙箱里裸跑，它只摸 12 个全局属性（window/document/navigator/screen/location
//     /setTimeout 系/eval/Function），不碰 canvas，也不需要 jsdom
//   · 唯一的坎是 `Window is not defined` —— 它拿 DOM **构造函数**做环境校验。
//     把 Window/Document/Navigator… 这一族补上（见 env.js），token 就出来了
//
// 生成的 token 结构 `13_13_408`：
//   段1 = 1786165500014 —— SDK 内的常量，和浏览器里一模一样，不是会话相关值
//   段2 = 生成时刻毫秒时间戳（所以 token 是一次性的，取完立刻用）
//   段3 = 加密载荷；浏览器里是 344 字符，这里 408，**内容不同但服务端都收**
//         （publish 接口实测不校验它，详见 baijiahao_publish.py 顶部注释）
//
// 用法：
//   node scripts/bjh_acs/gen_acs.js          → stdout 打出 token
//   node scripts/bjh_acs/gen_acs.js --json   → {"token":…,"segments":[…]}

const { makeEnv, loadSdk } = require('./env');

function gen(input = '', timeoutMs = 8000) {
  return new Promise((resolve, reject) => {
    const { sandbox, ctx } = makeEnv({ silent: true });
    try {
      loadSdk(ctx, ['acs-2112.js']);
    } catch (e) {
      return reject(new Error(`加载 acs-2112.js 失败：${e.message}`));
    }
    if (!sandbox.ACS_2112 || typeof sandbox.ACS_2112.gs !== 'function')
      return reject(new Error('SDK 没挂上 ACS_2112.gs —— 百度可能改版了，重下 sdk/acs-2112.js'));
    const timer = setTimeout(() => reject(new Error('生成超时')), timeoutMs);
    // gs(callback, input)：成功时 callback 第一个参数就是 token 字符串；
    // 环境不全时给的是 Error 对象（第二参数为 {}），据此区分
    sandbox.ACS_2112.gs(function (tok) {
      clearTimeout(timer);
      if (typeof tok === 'string' && tok.split('_').length === 3) return resolve(tok);
      const why = tok && tok.message ? tok.message : JSON.stringify(tok);
      reject(new Error(`环境不全，SDK 返回：${why}`));
    }, input);
  });
}

if (require.main === module) {
  gen(process.argv.includes('--input') ? process.argv[process.argv.indexOf('--input') + 1] : '')
    .then((tok) => {
      if (process.argv.includes('--json'))
        process.stdout.write(JSON.stringify({ token: tok, segments: tok.split('_').map(s => s.length) }));
      else process.stdout.write(tok);
    })
    .catch((e) => { process.stderr.write(String(e.message)); process.exit(1); });
}

module.exports = { gen };
