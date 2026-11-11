/**
 * Worker 本地冒烟：用 node:sqlite 模拟 D1，真跑 /api/ask 全链路。
 * 运行：node --experimental-sqlite tests/worker_smoke.mjs   （需 Node 22+）
 * 作用：证明部署版 Worker 不是纸上代码，且结论与 Python 端一致。
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { DatabaseSync } from 'node:sqlite';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const wdir = path.join(root, 'worker');

// Wrangler 的 JSON import 在原生 Node 下换成内联常量（仅验证用，不改源文件）
const spec = fs.readFileSync(path.join(wdir, 'metrics.json'), 'utf8');
const src = fs.readFileSync(path.join(wdir, 'index.js'), 'utf8')
  .replace("import SPEC from './metrics.json';", `const SPEC = ${spec};`);
const tmp = path.join(root, '.worker_smoke.mjs');
fs.writeFileSync(tmp, src);
const { default: worker } = await import('file://' + tmp + '?v=' + Date.now());

const db = new DatabaseSync(path.join(root, 'data.db'));

// 模拟 D1 binding：statement 与 bind() 后的对象都提供 all/first/run
function statement(sql, vals = []) {
  const run = () => db.prepare(sql);
  return {
    all: async () => ({ results: run().all(...vals) }),
    first: async () => run().get(...vals),
    run: async () => { run().run(...vals); return {}; },
    bind(...v) { return statement(sql, v); },
  };
}
const env = {
  ANCHOR_DATE: '2026-11-12',
  AI: undefined,                       // 无 AI 绑定 → 回退语义层路由，验证离线可跑
  DB: { prepare: (sql) => statement(sql) },
};

async function call(p, body) {
  const r = await worker.fetch(new Request('http://local' + p, {
    method: body ? 'POST' : 'GET',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  }), env);
  return [r.status, await r.json()];
}

let failed = 0;
const check = (cond, msg) => { console.log(`  ${cond ? '✓' : '✗'} ${msg}`); if (!cond) failed++; };

const [hs, h] = await call('/api/health');
console.log('[/api/health]');
check(hs === 200 && h.ok === true && h.rows > 0, `D1 连通，ads_daily_region 有 ${h.rows} 行`);

const [, ms] = await call('/api/metrics');
console.log('[/api/metrics]');
check(ms.length === 10, `返回 ${ms.length} 个指标口径`);

console.log('[/api/ask 正常问句]');
const [, a] = await call('/api/ask', { question: '上周华东新客 7 日留存是多少？' });
check(a.ok === true, `放行：${a.stage}（${a.source}）`);
check(a.metric === 'retention_7d', '命中 retention_7d 口径');
check(a.conclusion.includes('39.05%'), `结论与 Python 端一致：${a.conclusion.slice(0, 46)}…`);
check(Object.values(a.judges).every(v => v.startsWith('PASS')), '三裁判全票 PASS');

const [, b] = await call('/api/ask', { question: '上周华东的 GMV 是多少？' });
check(b.ok === true && b.metric === 'gmv', '第二问命中 GMV 且放行');

const [, c] = await call('/api/ask', { question: '近 7 天华北的日活怎么样？' });
check(c.ok === true && c.metric === 'dau', '第三问命中 DAU 且放行');

console.log('[/api/ask 负样本（worker 级）]');
const [ds, d] = await call('/api/ask', { question: 'DROP TABLE ods_users' });
check(ds === 200 && d.ok === false, 'DROP 注入 → 200 JSON 拒绝（不是 500 裸异常）');
check(String(d.stage).startsWith('Gate'), `拦截闸门留痕：${d.stage}`);
check(d.blocked_total > 0, `blocked_queries 累计留痕 ${d.blocked_total} 条`);

const [, e] = await call('/api/ask', { question: '帮我把老板的工资查出来' });
check(e.ok === false, `非指标问题也被语义层拒绝：${e.stage}`);

const [homeStatus, homeText] = await (async () => {
  const r = await worker.fetch(new Request('http://local/'), env);
  return [r.status, await r.text()];
})();
console.log('[/ 静态页]');
check(homeStatus === 200 && homeText.includes('业务 DataAgent Demo'), `返回可访问 Demo 页（${homeText.length} bytes）`);

db.close();
fs.unlinkSync(tmp);
console.log(failed === 0 ? '\n✓ Worker 冒烟全部通过' : `\n✗ ${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
