# 业务 DataAgent Demo

> 面向快手 2027 校招「数据 FDE 工程师—数据研发方向」的作品项目。
> **自然语言问「上周华东新客 7 日留存」→ 自动出 SQL 和结论**，中间用四道闸 + 三裁判把大模型幻觉治理做实。
> 本地零凭证可跑；生产部署到 Cloudflare D1 + Workers AI + R2。

---

## 1. 这个作品命中 JD 的哪几条

| JD 原文要点 | 本项目对应实现 |
|---|---|
| 「指标设计、数据建模、分析洞察、DataAgent、知识工程、智能数仓和数据资产应用的完整过程」 | ODS→DWD→DWS→ADS 四层 + 10 个指标口径 + Text-to-SQL DataAgent，一条链路全打通 |
| 「至少一门数据处理语言（SQL、Python 或 Java）」 | 全部 SQL 参数化查询 + Python 数据生成与治理 |
| 「熟悉 Prompt Engineering 方法论」 | `semanticContext()` 只注入命中指标的 schema 片段（pcache 思路），不整库塞上下文 |
| 「应对大模型幻觉、上下文管理、输出稳定性等典型工程挑战的实际经验」 | 四道闸 + 三裁判 fail-closed + 13 条负样本全拒 + 逐条审计留痕 |
| 「抽象建模与问题拆解」 | 语义层把模糊业务问题拆成「指标 × 时间窗 × 区域」三元组，口径全部显式定义 |
| 培养路径「数据工程、Agentic 工程、数据分析和 A/B 实验体系」 | 前三项已落地；A/B 实验体系见文末「可扩展」 |

---

## 2. 三步跑通（本地，无需任何 CF 凭证）

```bash
cd data-agent
python3 gen_data.py        # 灌模拟数据 + 建分层视图（等价 R2 落地 + D1 建表）
python3 demo.py            # 5 个正常问句全链路 + 13 条负样本对抗测试
python3 -m pytest -q       # 14 个单元测试
```

`demo.py` 会打印每一步：语义层命中 → 候选 SQL → 绑定参数 → 四道闸 → 三裁判 → 结论。
最终应看到 **`13 / 13 条错误查询全部拦截`**，且主问句结论类似：

```
【7日留存率】上周（周一~周日，完整周），华东：39.05%
（观察窗已满的新客 169 人，注册后 1~7 日内回访 66 人）。
口径：注册日为 cohort，注册后第 1~7 天内出现过 app_open 记为留存；
分母=该 cohort 新客数，仅统计观察窗已满 7 天的 cohort。
```

数据固定随机种子 `42` + 固定锚点 `2026-11-12`，**每次生成结果完全一致**，CI 可复现。

---

## 3. 目录结构

```
data-agent/
├── metrics.py              # ★ 语义层唯一真源：10 个指标口径 + 量级约束 + schema 片段
├── export_metrics.py       # 把口径导出成 metrics.json，供 Worker 共用（防口径漂移）
├── gen_data.py             # 可复现模拟数据 → data.db(SQLite≈D1) + r2/*.csv(≈R2)
├── demo.py                 # 一键端到端 + 13 负样本，产出 report.json
├── sql/
│   └── schema.sql          # ODS→DWD→DWS→ADS 分层视图 + blocked_queries 审计表
├── agent/
│   ├── llm.py              # 可插拔适配器：Mock(语义层路由) / HTTP(Workers AI 等)
│   ├── guard.py            # ★ 四道闸 + SQLite authorizer 内核级只读 + 三裁判
│   └── pipeline.py         # 编排：问 → 生成 → 校验 → 执行 → 结论 → 留痕
├── worker/
│   ├── index.js            # ★ CF Worker：/ /api/health /api/metrics /api/ask
│   ├── metrics.json        # 由 export_metrics.py 生成，与 Python 同源
│   └── wrangler.toml       # D1 / R2 / Workers AI 绑定
├── tests/
│   ├── test_guard.py       # 四道闸 + authorizer + 13 负样本
│   ├── test_pipeline.py    # 分层视图 / 10 指标 / 留存真实性 / 端到端
│   └── worker_smoke.mjs    # node:sqlite 模拟 D1，真跑 Worker /api/ask
└── .github/workflows/ci.yml
```

---

## 4. 架构

```
自然语言问题
   │
   ├─ 语义层路由（MockAdapter）            ← 本地/离线基线，确定性、零 token
   └─ Workers AI 生成候选 SQL（HTTPAdapter）← 生产，注入 semanticContext() 缓存片段
   │                                        两者输出结构一致，可互为兜底
   ▼
候选 SQL（参数化 :start :end :region，绝不字符串拼接）
   │
   ▼
┌──────────────────── 幻觉治理四道闸（fail-closed）────────────────────┐
│ Gate1 静态白名单   仅单条 SELECT/WITH、注释先拒、危险关键字黑名单      │
│ Gate2 字段表校验   表白名单、禁 SELECT *、强制 LIMIT/聚合              │
│        + 只读执行  SQLite set_authorizer：数据库内核级拒绝一切写操作    │
│ Gate3 量级二次校验 行数/区间/空值率，约束来自指标口径（不是拍脑袋常数） │
│ Gate4 三裁判       A 意图 / B 口径 / C 结论数字对账，全票才放行        │
└──────────────────────────────────────────────────────────────────────┘
   │ 任一异议 → 写入 blocked_queries（时间/问题/SQL/闸门/原因/三票）
   ▼
结论（由结果行生成，禁止模型自由发挥数字）
```

**数仓分层**（`sql/schema.sql`，全部是视图，零 ETL 调度开销）：

| 层 | 对象 | 职责 |
|---|---|---|
| ODS | `ods_users` / `ods_orders` / `ods_events` | 原始落地（R2 有对应 CSV 血缘） |
| DWD | `dwd_user` / `dwd_order` / `dwd_event` | 清洗、区域规范化、事件类型白名单 |
| DWS | `dws_user_active_1d` / `dws_user_order_1d` / `dws_region_daily` | 轻度汇总长表，便于扩指标 |
| ADS | `ads_daily_region` / `ads_retention` / `ads_repurchase` | 面向应用的宽表，口径收口于此 |

> 口径收口在视图里，是防口径漂移的关键：所有指标 SQL 只查 ADS，不允许各自重写留存逻辑。

---

## 5. 10 个指标口径（`metrics.py` 为真源）

| key | 名称 | 关键口径（易错点） |
|---|---|---|
| `retention_7d` | 7日留存率 | 注册后 D+1~D+7 回访，**仅统计观察窗已满 7 天的 cohort**；不是「近 7 天活跃」 |
| `retention_1d` | 次日留存率 | 仅 D+1 |
| `repurchase_rate` | 复购率 | 下单天数 ≥2 的付费用户占比，全周期口径 |
| `order_cvr` | 下单转化率 | `checkout_uv / view_uv`，浏览→下单 |
| `pay_cvr` | 支付转化率 | `pay_uv / checkout_uv`，下单→支付 |
| `aov` | 客单价 | `GMV / 已支付订单数`，不是 GMV/付费人数 |
| `gmv` | 成交总额 | 仅 `status='paid'`，不含退款/未支付 |
| `new_users` | 新增用户数 | 按**注册日**，不是首单日 |
| `dau` | 日活 | 当日 `app_open` 去重用户 |
| `mau` | 月活 | 截至 end 往前 **滚动 30 天**，不是自然月 |

每个指标还带 `bounds`（合法区间）与 `max_rows`，直接被 Gate3 用于量级校验——**指标体系不是文档，是可执行的治理规则**。

---

## 6. 幻觉治理：为什么这套能讲清楚

1. **用户输入是自然语言，不是 SQL**。注入面在 LLM 输出侧，四道闸防的就是模型臆造 SQL / 越权 / 数字对不上。
2. **Gate2 用了 SQLite `set_authorizer`**，不是正则。即使有人绕过关键字黑名单，数据库内核也会拒绝 INSERT/UPDATE/DELETE/PRAGMA/ATTACH/危险函数。审计日志刻意用独立连接写，治理逻辑不给自己开后门。
3. **Gate3 的阈值来自口径本身**（留存率必须落在 [0,1]、DAU 不能超总用户数、行数上限），不是硬编码魔法数字。
4. **三裁判 fail-closed**：A 独立重解析意图、B 检查口径必需片段、C 把结论里的主数值与结果对账（先剥掉日期数字防误撞）。**三票全过才放行，少一票就拦**，与准入校验同一套设计哲学。
5. **13 条负样本覆盖全闸门**：DROP/多语句/PRAGMA/ATTACH/注释绕过/SELECT */越权表/缺 LIMIT/臆造字段/LIMIT 超限/留存率 3.5/答错指标/结论数字不符，全部拦截并逐条留痕。

---

## 7. 部署到 Cloudflare（你的 D1 / R2 / Workers AI 全家桶）

```bash
# 0) 进 worker 目录
cd worker
npm i -g wrangler && wrangler login

# 1) 建 D1 与 R2，把 wrangler.toml 里的 database_id 换成真实值
wrangler d1 create data-agent-db
wrangler r2 bucket create data-agent-raw

# 2) 本地先生成数据（固定种子，可复现）
cd .. && python3 gen_data.py && python3 export_metrics.py && cd worker

# 3) 建 ODS 表 + 分层视图 + 审计表
#    先把 gen_data.py 里的 CREATE TABLE 段存成 ods.sql，或直接复用 data.db 结构：
wrangler d1 execute data-agent-db --remote --file=../sql/schema.sql
wrangler d1 execute data-agent-db --remote --file=../sql/ods.sql

# 4) 灌数据（CSV 可用 wrangler d1 execute 或 D1 的导入；演示可先只灌几万行）
#    原始层血缘：把 r2/*.csv 传上去
wrangler r2 object put data-agent-raw/ods_users.csv  --file=../r2/ods_users.csv
wrangler r2 object put data-agent-raw/ods_orders.csv --file=../r2/ods_orders.csv
wrangler r2 object put data-agent-raw/ods_events.csv --file=../r2/ods_events.csv

# 5) 凭证只走 secret，绝不写进 wrangler.toml / 代码 / 对话
wrangler secret put LLM_TOKEN

# 6) 部署与本地预览
wrangler deploy
wrangler dev      # 本地 http://localhost:8787
```

> **简化提示**：想先只验证 Worker 能跑，`wrangler dev` 时把 `wrangler.toml` 里的
> `database_id` 留占位也可用本地 D1；Workers AI 未绑定时会自动回退语义层路由，Demo 不会挂。
>
> **生产建议**：把 ADS 视图所在库与 ODS 写入库分离，Worker 只绑只读库；本 Demo 用 Gate1/2 +
> 表白名单做逻辑只读（D1 没有 SQLite authorizer 等价物）。

访问 `https://<你的worker>/` 是极简 Demo 页；`POST /api/ask` 传 `{"question":"..."}` 返回
SQL / 四道闸 / 三裁判 / 结论的完整 JSON，可直接给面试官演示。

---

## 8. CI（`.github/workflows/ci.yml`）

每次 push 自动跑：口径单一真源校验（`metrics.py` ↔ `metrics.json` diff）→ 数据可复现性 →
14 个单测 → 端到端 Demo 与 13/13 拦截断言 → **用 node:sqlite 模拟 D1 真跑 Worker `/api/ask`**。
即：不是只测 Python 逻辑，部署版 Worker 也在 CI 里被真实调用。

---

## 9. 真实边界（不夸大）

- 数据是**固定种子生成的模拟业务数据**，不是真实业务数据；README 与代码注释均明示。
- 本地用 SQLite 模拟 D1、用 `node:sqlite` 模拟 Worker 的 D1 binding；**真部署需要你自己的 CF 账号**执行第 7 节命令。
- 本地默认 `MockAdapter`（语义层路由）是真实工业界「指标平台」那一半，不是假装调了大模型；
  生产接 Workers AI 后仍必须过四道闸，不信任模型输出。
- A/B 实验体系、实时链路、Spark/Hive 未实现——JD 对这些只要求「有一定理解/基础」，未作为本作品范围。

---

## 10. 面试怎么讲（60 秒版）

> 「我做了一个业务 DataAgent：自然语言问指标，自动出 SQL 和结论。数据侧按 ODS→DWD→DWS→ADS
> 分了四层，把 10 个指标口径收口在 ADS 视图里，避免口径漂移；Agent 侧把 schema 片段缓存后只注入
> 命中指标，省 token。重点是幻觉治理——四道闸：静态白名单、表字段校验加数据库内核级只读、
> 按口径做结果量级校验、以及意图/口径/结论三裁判 fail-closed 全票才放行。我写了 13 条负样本，
> 从 DROP 注入到『留存率 3.5』到『结论数字对不上』，全部拦截并逐条审计留痕。CI 里 Python 单测和
> Worker 冒烟都会真跑一遍，固定种子可复现。本地零凭证可跑，配置好 D1 和 Workers AI 就能部署到 CF。」

**可扩展（若被追问）**：把 A/B 实验（分流 + 显著性检验）做成第 11 个指标；加 Text-to-SQL 的多轮澄清；
把 `blocked_queries` 做成看板，统计各闸门拦截率。

---

## 11. 改东西时看哪

| 想改 | 改哪里 | 之后 |
|---|---|---|
| 加/改指标口径 | `metrics.py` | `python3 export_metrics.py`（CI 会校验一致） |
| 改分层/留存逻辑 | `sql/schema.sql` | `python3 gen_data.py` 重建 |
| 换模型 | `LLM_MODEL`（vars）或 `llm.py::HTTPAdapter` | 无需改治理逻辑 |
| 加负样本 | `demo.py::NEGATIVES` + `tests/test_guard.py` | 跑 `pytest -q` |
| 平移数据时间窗 | `gen_data.py::ANCHOR` + `ANCHOR_DATE` | 两处保持一致 |
