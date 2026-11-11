/**
 * 业务 DataAgent · Cloudflare Worker
 *
 * 绑定：D1(DB) / Workers AI(AI) / R2(R2，原始层落地物，可选)
 * 指标口径来自 metrics.json（由 export_metrics.py 从 metrics.py 导出，单一真源）。
 *
 * 链路：自然语言 → Workers AI 生成候选 SQL（失败回退语义层路由）
 *      → Gate1 静态白名单 → Gate2 表/字段校验 → 只读查询 D1
 *      → Gate3 结果量级校验 → Gate4 三裁判(fail-closed) → 结论
 * 任何一道拦截都写入 D1 的 blocked_queries 审计表。
 *
 * 安全边界：用户传入的是「自然语言」而非 SQL，注入面在 LLM 输出侧，
 * 四道闸防的就是模型臆造/越权 SQL；D1 侧建议再按 README 做读写库分离。
 */
import SPEC from './metrics.json';

const M = Object.fromEntries(SPEC.metrics.map(m => [m.key, m]));
const ALLOWED = new Set(SPEC.allowed_tables);
const RATE_KEYS = new Set(['retention_7d', 'retention_1d', 'repurchase_rate', 'order_cvr', 'pay_cvr']);
const FORBIDDEN = /\b(insert|update|delete|drop|alter|create|attach|detach|pragma|reindex|vacuum|replace|trigger|load_extension|writefile|readfile)\b/i;

// ---------- 语义层（与 Python 侧同一套口径） ----------
function resolveMetric(q) {
  let best = null, bestLen = -1;
  for (const m of SPEC.metrics) {
    for (const kw of m.keywords) {
      if (q.toLowerCase().includes(kw.toLowerCase()) && kw.length > bestLen) {
        best = m; bestLen = kw.length;
      }
    }
  }
  return best;
}

function resolveRegion(q) {
  return SPEC.regions.find(r => q.includes(r)) || 'ALL';
}

function resolveTime(q, anchor) {
  const a = new Date(anchor + 'T00:00:00Z');
  const iso = d => d.toISOString().slice(0, 10);
  const shift = n => { const d = new Date(a); d.setUTCDate(d.getUTCDate() + n); return d; };
  const wd = (a.getUTCDay() + 6) % 7;                 // 周一=0
  const mon = shift(-wd);
  if (q.includes('上上周')) return { start: iso(shift(-wd - 14)), end: iso(shift(-wd - 8)), note: '上上周（完整周）' };
  if (q.includes('上周'))   return { start: iso(shift(-wd - 7)),  end: iso(shift(-wd - 1)),  note: '上周（完整周）' };
  if (q.includes('本周'))   return { start: iso(mon),              end: anchor,               note: '本周（周一至今日）' };
  if (q.includes('昨日') || q.includes('昨天')) return { start: iso(shift(-1)), end: iso(shift(-1)), note: '昨日' };
  if (q.includes('近30天') || q.includes('最近一个月')) return { start: iso(shift(-29)), end: anchor, note: '近 30 天滚动窗口' };
  if (q.includes('本月'))   return { start: anchor.slice(0, 8) + '01', end: anchor, note: '本自然月' };
  return { start: iso(shift(-6)), end: anchor, note: q.includes('近7天') || q.includes('最近一周') ? '近 7 天滚动窗口' : '默认近 7 天滚动窗口' };
}

function semanticRoute(q, anchor) {
  const m = resolveMetric(q);
  if (!m) throw new Error('未识别到指标，语义层无对应口径（拒绝臆造 SQL）');
  const t = resolveTime(q, anchor);
  const note = m.key === 'repurchase_rate' ? '全周期口径（该指标不按时间窗切分）' : t.note;
  return {
    sql: m.sql, metric_key: m.key, source: 'semantic-route', note,
    params: { start: t.start, end: t.end, region: resolveRegion(q) },
    reasoning: `命中指标口径 ${m.key}(${m.name})；仅注入该指标的 schema 片段（pcache 命中）`,
  };
}

function semanticContext() {
  const lines = ['可用指标口径（只能使用以下视图与字段，禁止臆造表/字段）：'];
  for (const m of SPEC.metrics) lines.push(`- ${m.key} | ${m.name} | ${m.desc} | 维度:${m.dims.join(',')}`);
  lines.push(`可查询视图：${SPEC.allowed_tables.join(', ')}`);
  lines.push('参数：:start :end 为 YYYY-MM-DD，:region 为区域名或 ALL。');
  lines.push('约束：只允许单条 SELECT/WITH；禁止 SELECT *；必须自带 LIMIT 或聚合。');
  return lines.join('\n');
}

async function generateCandidate(env, q, anchor) {
  if (!env.AI) return semanticRoute(q, anchor);
  const t = resolveTime(q, anchor);
  const sys = [
    '你是 Text-to-SQL 生成器。严格依据给定口径输出 JSON，不要输出其它文字。',
    '格式：{"sql":"...","params":{"start":"YYYY-MM-DD","end":"YYYY-MM-DD","region":"..."},"metric_key":"..."}',
    '只允许单条 SELECT/WITH，禁止 SELECT *，必须参数化，禁止臆造表或字段。',
    '', semanticContext(), '',
    `时间参考：今天=${anchor}，${t.note} => start=${t.start}, end=${t.end}。区域=${resolveRegion(q)}。`,
  ].join('\n');
  const model = env.LLM_MODEL || '@cf/meta/llama-3.1-8b-instruct';
  const resp = await env.AI.run(model, {
    messages: [{ role: 'system', content: sys }, { role: 'user', content: q }],
    max_tokens: 512,
  });
  const text = typeof resp === 'string' ? resp : (resp.response || resp.text || JSON.stringify(resp));
  const hit = text.match(/\{[\s\S]*\}/);
  if (!hit) throw new Error('模型输出未包含 JSON');
  const obj = JSON.parse(hit[0]);
  return {
    sql: obj.sql, params: obj.params, metric_key: obj.metric_key,
    source: `workers-ai:${model}`, note: t.note,
    reasoning: `${model} 生成，上下文为 semanticContext() 缓存片段`,
  };
}

// D1 的 bind 是位置参数，把 :name 占位符按出现顺序转成 ? 并收集值
function toPositional(sql, params) {
  const values = [];
  const q = sql.replace(/:([a-zA-Z_]\w*)/g, (_, k) => {
    if (!(k in params)) throw new Error('未知参数 ' + k);
    values.push(params[k]);
    return '?';
  });
  return { sql: q, values };
}

// ---------- 四道闸 ----------
function gate1(sql) {
  if (/\/\*|--|#/.test(sql)) return [false, '检测到注释，拒绝（防注释绕过白名单）'];
  const s = sql.trim();
  if (!s) return [false, '空语句'];
  const hit = s.match(FORBIDDEN);
  if (hit) return [false, `命中危险关键字：${hit[0].toUpperCase()}`];
  if (!/^(select|with)\b/i.test(s)) return [false, '非 SELECT/WITH 起始，拒绝'];
  const body = s.replace(/;+\s*$/, '');
  if (body.includes(';')) return [false, '检测到多语句拼接'];
  return [true, '静态白名单通过（仅单条 SELECT/WITH）'];
}

function gate2(sql) {
  if (/select\s+\*/i.test(sql)) return [false, '禁止 SELECT * 全列扫描'];
  const tables = [...sql.matchAll(/\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)/gi)].map(x => x[1]);
  const illegal = [...new Set(tables.filter(t => !ALLOWED.has(t)))];
  if (illegal.length) return [false, `引用了白名单外的表：${illegal.join(',')}`];
  if (!tables.length) return [false, '未识别到合法来源表'];
  const hasLimit = /\blimit\s+\d+/i.test(sql);
  const hasAgg = /\b(sum|count|avg|min|max)\s*\(/i.test(sql) || /\bgroup\s+by\b/i.test(sql);
  if (!hasLimit && !hasAgg) return [false, '缺少 LIMIT 或聚合，存在全表返回风险'];
  const lm = sql.match(/\blimit\s+(\d+)/i);
  if (lm && Number(lm[1]) > 200) return [false, 'LIMIT 超过 200 行上限'];
  return [true, `表 ${[...new Set(tables)].join(',')} 均在白名单；D1 查询只读`];
}

function gate3(rows, m) {
  if (!rows.length) return [false, '结果为空，拒绝输出结论（防把空结果编成数字）'];
  if (rows.length > m.max_rows) return [false, `返回 ${rows.length} 行，超过 ${m.max_rows} 行上限`];
  const cols = Object.keys(rows[0]);
  if (!cols.includes(m.value_field)) return [false, `结果缺少主指标列 ${m.value_field}，疑似串指标`];
  let numeric = 0;
  for (const r of rows) {
    const v = r[m.value_field];
    if (typeof v === 'number') {
      numeric++;
      if (v < m.bounds[0] || v > m.bounds[1])
        return [false, `${m.value_field}=${v} 越出口径合法区间 [${m.bounds[0]}, ${m.bounds[1]}]`];
    }
  }
  if (numeric < rows.length * 0.8) return [false, '主指标列空值率 >20%，量级异常'];
  return [true, `${rows.length} 行、${m.value_field} 全部落在 [${m.bounds[0]}, ${m.bounds[1]}]`];
}

function buildConclusion(cand, m, rows) {
  const f = m.value_field, isRate = RATE_KEYS.has(m.key);
  const region = cand.params.region || 'ALL';
  const head = `【${m.name}】${cand.note}，${region === 'ALL' ? '全区域' : region}`;
  const fmt = v => isRate ? `${Math.round(v * 10000) / 100}%` : `${Math.round(v * 100) / 100}`;
  if (rows.length === 1 || !('dt' in rows[0] || 'cohort_dt' in rows[0])) {
    const r = rows[0];
    let extra = '';
    if (m.key === 'retention_7d') extra = `（观察窗已满的新客 ${r.new_users} 人，注册后 1~7 日内回访 ${r.retained_7d} 人）`;
    else if (m.key === 'retention_1d') extra = `（新客 ${r.new_users} 人，次日回访 ${r.retained_1d} 人）`;
    else if (m.key === 'repurchase_rate') extra = `（付费用户 ${r.total_users} 人，其中复购 ${r.repurchase_users} 人）`;
    return `${head}：${fmt(r[f])}${extra}。口径：${m.desc}`;
  }
  const r = rows[rows.length - 1];
  const dt = r.dt || r.cohort_dt;
  return `${head}：共返回 ${rows.length} 行按日/区域明细，其中 ${dt} 的${m.name}为 ${fmt(r[f])}。口径：${m.desc}`;
}

function gate4(q, sql, m, rows, conclusion) {
  // A 意图一致性：独立再解析一次
  const independent = resolveMetric(q);
  const a = !!independent && independent.key === m.key;
  const reasonA = `独立意图解析=${independent ? independent.key : 'None'}，候选=${m.key} → ${a ? '一致' : '不一致'}`;
  // B 口径一致性
  const missing = m.required_tokens.filter(t => !sql.includes(t));
  const hasMain = sql.includes(m.value_field);
  const hasUnion = /\bunion\b/i.test(sql);
  const b = !missing.length && hasMain && !hasUnion;
  const reasonB = `必需片段缺失=${missing.length ? missing.join(',') : '无'}；主输出字段${m.value_field}=${hasMain ? '存在' : '缺失'}；UNION=${hasUnion ? '有' : '无'} → ${b ? '口径一致' : '口径可疑'}`;
  // C 结论一致性：先剥日期/年份，只拿主数值对账，防日期数字误撞
  let clean = conclusion.replace(/\d{4}-\d{1,2}-\d{1,2}/g, ' ').replace(/\b(?:19|20)\d{2}\b/g, ' ');
  const main = [...clean.matchAll(/(?:为|：|:)\s*(-?\d+(?:\.\d+)?)\s*%?/g)].map(x => Math.abs(Number(x[1])));
  const nums = main.length ? main : [...clean.matchAll(/-?\d+(?:\.\d+)?/g)].map(x => Math.abs(Number(x[0])));
  const vals = rows.map(r => r[m.value_field]).filter(v => typeof v === 'number').map(Math.abs);
  const matched = nums.find(n => vals.some(v => Math.abs(n - v) < 1e-6 || Math.abs(n - v * 100) < 0.011));
  const c = matched !== undefined;
  const reasonC = `主数值 ${nums.slice(0, 4)} vs 结果 ${vals.slice(0, 4)} → ${c ? `命中 ${matched}，可对账` : '全部对不上'}`;
  const judges = {
    A: `${a ? 'PASS' : 'FAIL'} | ${reasonA}`,
    B: `${b ? 'PASS' : 'FAIL'} | ${reasonB}`,
    C: `${c ? 'PASS' : 'FAIL'} | ${reasonC}`,
  };
  const votes = [a, b, c].filter(Boolean).length;
  const passed = a && b && c;   // fail-closed：全票才放行
  return [passed, judges, passed ? `三裁判 ${votes}/3 票，全票通过` : `三裁判 ${votes}/3 票，任一异议即拦截(fail-closed)`];
}

async function logBlock(env, q, sql, stage, reason, judges) {
  try {
    await env.DB.prepare(
      'INSERT INTO blocked_queries(ts,question,sql,stage,reason,judge_a,judge_b,judge_c) VALUES (?,?,?,?,?,?,?,?)'
    ).bind(new Date().toISOString(), q, sql, stage, reason,
           judges.A || '', judges.B || '', judges.C || '').run();
  } catch (_) { /* 审计日志失败不影响拦截结论 */ }
}

async function blockedTotal(env) {
  try {
    // 统一用 all() 而非 first()：D1 与各本地模拟驱动都稳定支持，少一个 API 差异点
    const r = await env.DB.prepare('SELECT COUNT(*) AS n FROM blocked_queries').all();
    return r.results && r.results[0] ? r.results[0].n : 0;
  } catch (_) { return 0; }
}

async function handleAsk(request, env) {
  let q;
  try { q = (await request.json()).question || ''; } catch (_) { q = ''; }
  const anchor = env.ANCHOR_DATE || '2026-11-12';
  let cand;
  try {
    cand = await generateCandidate(env, q, anchor);
  } catch (e) {
    cand = semanticRoute(q, anchor);
    cand.source = 'semantic-route(fallback)';
    cand.reasoning = `Workers AI 不可用(${e.message})，回退语义层基线`;
  }

  const fail = (stage, reason, judges = {}, rows = []) => ({ stage, reason, judges, rows });
  let judges = {}, rows = [];
  const g1 = gate1(cand.sql);
  if (!g1[0]) return await reply(env, q, cand, fail('Gate1-静态白名单', g1[1]));

  const g2 = gate2(cand.sql);
  if (!g2[0]) return await reply(env, q, cand, fail('Gate2-字段表校验', g2[1]));

  const m = M[cand.metric_key];
  if (!m) return await reply(env, q, cand, fail('Gate3-量级校验', '候选指标不在语义层注册表中'));

  let positional;
  try { positional = toPositional(cand.sql, cand.params); }
  catch (e) { return await reply(env, q, cand, fail('Gate2-参数绑定', e.message)); }

  try {
    const res = await env.DB.prepare(positional.sql).bind(...positional.values).all();
    rows = res.results || [];
  } catch (e) {
    return await reply(env, q, cand, fail('Gate2-只读执行', `执行失败（字段臆造/参数错误）：${e.message}`));
  }

  const g3 = gate3(rows, m);
  if (!g3[0]) return await reply(env, q, cand, fail('Gate3-量级校验', g3[1], {}, rows));

  const conclusion = buildConclusion(cand, m, rows);
  const g4 = gate4(q, cand.sql, m, rows, conclusion);
  judges = g4[1];
  if (!g4[0]) return await reply(env, q, cand, fail('Gate4-三裁判', g4[2], judges, rows));

  return await reply(env, q, cand, {
    stage: '全部通过', reason: '四道闸全部通过', judges, rows, conclusion,
  });
}

async function reply(env, q, cand, g) {
  const ok = g.stage === '全部通过';
  if (!ok) await logBlock(env, q, cand.sql, g.stage, g.reason, g.judges || {});
  return json({
    ok, question: q, source: cand.source, metric: cand.metric_key,
    metric_name: M[cand.metric_key] ? M[cand.metric_key].name : '?',
    sql: cand.sql.replace(/\s+/g, ' ').trim(), params: cand.params,
    reasoning: cand.reasoning, note: cand.note,
    stage: g.stage, reason: g.reason, judges: g.judges || {},
    rows: g.rows || [], conclusion: ok ? g.conclusion : '',
    blocked_total: await blockedTotal(env),
  });
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj, null, 2), {
    status,
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Headers': 'Content-Type',
    },
  });
}

const PAGE = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>业务 DataAgent Demo</title>
<style>
*{box-sizing:border-box}body{margin:0;padding:24px;max-width:820px;margin:auto;
font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#1a1a1a;line-height:1.6}
h1{font-size:20px;margin:0 0 4px}.sub{color:#777;font-size:13px;margin-bottom:20px}
.row{display:flex;gap:8px}input{flex:1;padding:10px 12px;border:1px solid #ccc;border-radius:4px;font-size:14px}
button{padding:10px 18px;border:0;background:#222;color:#fff;border-radius:4px;cursor:pointer;font-size:14px}
.chips{margin:12px 0;display:flex;flex-wrap:wrap;gap:6px}
.chip{border:1px solid #ddd;border-radius:14px;padding:3px 10px;font-size:12px;cursor:pointer;color:#555}
pre{background:#f6f6f6;padding:12px;border-radius:4px;overflow-x:auto;font-size:12px;white-space:pre-wrap}
.sec{margin-top:16px;border-top:1px solid #eee;padding-top:12px}
.tag{display:inline-block;padding:1px 8px;border-radius:3px;font-size:12px;background:#eee}
.ok{background:#e8f5e9}.no{background:#ffebee}
.conc{font-size:15px;background:#fafafa;border-left:3px solid #222;padding:10px 14px;margin-top:8px}
</style></head><body>
<h1>业务 DataAgent Demo</h1>
<div class="sub">自然语言 → 指标口径 → SQL → 四道闸 → 三裁判(fail-closed) → 结论 · D1 + Workers AI</div>
<div class="row"><input id="q" value="上周华东新客 7 日留存是多少？"><button onclick="ask()">查询</button></div>
<div class="chips" id="chips"></div>
<div id="out"></div>
<script>
const samples=['上周华东新客 7 日留存是多少？','上周华东的 GMV 是多少？','近 7 天华北的日活怎么样？','上周华南的支付转化率是多少？','全区域的复购率是多少？'];
document.getElementById('chips').innerHTML=samples.map((s,i)=>'<span class="chip" data-i="'+i+'">'+s+'</span>').join('');
document.getElementById('chips').addEventListener('click',function(e){
  if(!e.target.classList.contains('chip'))return;
  document.getElementById('q').value=samples[Number(e.target.dataset.i)];ask();
});
async function ask(){
  const q=document.getElementById('q').value;const out=document.getElementById('out');
  out.innerHTML='<div class="sec">查询中…</div>';
  try{
    const r=await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q})}).then(x=>x.json());
    let h='<div class="sec"><span class="tag '+(r.ok?'ok':'no')+'">'+(r.ok?'✓ 放行':'✗ 拦截')+'</span> '+
      r.stage+' · '+r.reason+'<div class="sub">'+r.source+' | '+r.metric_name+' | 累计拦截 '+r.blocked_total+' 条</div></div>';
    h+='<div class="sec"><b>候选 SQL</b><pre>'+esc(r.sql)+'</pre><div class="sub">参数 '+esc(JSON.stringify(r.params))+'</div></div>';
    h+='<div class="sec"><b>三裁判</b><pre>'+esc(Object.entries(r.judges||{}).map(([k,v])=>k+': '+v).join('\\n')||'（未进入该闸门）')+'</pre></div>';
    if(r.conclusion)h+='<div class="sec"><b>结论</b><div class="conc">'+esc(r.conclusion)+'</div></div>';
    if(r.rows&&r.rows.length)h+='<div class="sec"><b>结果样本</b><pre>'+esc(JSON.stringify(r.rows.slice(0,3),null,2))+'</pre></div>';
    out.innerHTML=h;
  }catch(e){out.innerHTML='<div class="sec">请求失败：'+e+'</div>'}
}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
</script></body></html>`;

export default {
  async fetch(request, env) {
    const u = new URL(request.url);
    if (request.method === 'OPTIONS') return json({}, 204);
    if (u.pathname === '/') return new Response(PAGE, { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
    if (u.pathname === '/api/health') {
      try {
        const r = await env.DB.prepare('SELECT COUNT(*) AS n FROM ads_daily_region').all();
        const n = r.results && r.results[0] ? r.results[0].n : 0;
        return json({ ok: true, d1: 'connected', rows: n, ai: !!env.AI, anchor: env.ANCHOR_DATE });
      } catch (e) { return json({ ok: false, error: e.message }, 500); }
    }
    if (u.pathname === '/api/metrics') {
      return json(SPEC.metrics.map(m => ({ key: m.key, name: m.name, desc: m.desc, dims: m.dims })));
    }
    if (u.pathname === '/api/ask' && request.method === 'POST') return handleAsk(request, env);
    return json({ error: 'not found' }, 404);
  },
};
