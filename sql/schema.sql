-- ============================================================
-- 快手 DataAgent Demo · 数仓分层（ODS → DWD → DWS → ADS）
-- 跑在 Cloudflare D1 / 本地 SQLite，CREATE VIEW IF NOT EXISTS 可重复执行
-- ODS 三张物理表由 gen_data.py 建；以下全部为视图，零 ETL 调度开销
-- ============================================================

-- 拦截审计表：四道闸每次拦截都留痕，形成「拦截 N 条错误查询」的可验证证据
CREATE TABLE IF NOT EXISTS blocked_queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, question TEXT, sql TEXT,
    stage TEXT, reason TEXT, judge_a TEXT, judge_b TEXT, judge_c TEXT
);

-- ---------- DWD 明细层：清洗、去重、规范化 ----------
CREATE VIEW IF NOT EXISTS dwd_user AS
SELECT user_id,
       date(register_date) AS reg_dt,
       CASE WHEN region IN ('华东','华北','华南','华中','西南','西北','东北')
            THEN region ELSE '未知' END AS region,
       channel
FROM ods_users
WHERE user_id IS NOT NULL AND register_date IS NOT NULL;

CREATE VIEW IF NOT EXISTS dwd_order AS
SELECT order_id, user_id, date(order_ts) AS dt, amount,
       CASE WHEN status IN ('paid','refunded','unpaid') THEN status ELSE 'unknown' END AS status,
       region
FROM ods_orders
WHERE amount >= 0;

CREATE VIEW IF NOT EXISTS dwd_event AS
SELECT event_id, user_id, date(event_ts) AS dt, event_type,
       CASE WHEN region IN ('华东','华北','华南','华中','西南','西北','东北')
            THEN region ELSE '未知' END AS region
FROM ods_events
WHERE event_type IN ('app_open','view_item','add_cart','checkout','pay');

-- ---------- DWS 汇总层：轻度汇总，长表便于扩展指标 ----------
CREATE VIEW IF NOT EXISTS dws_user_active_1d AS
SELECT dt, user_id, region, 1 AS is_active
FROM dwd_event WHERE event_type = 'app_open'
GROUP BY dt, user_id, region;

CREATE VIEW IF NOT EXISTS dws_user_order_1d AS
SELECT dt, user_id,
       COUNT(*) AS order_cnt, SUM(amount) AS pay_amount
FROM dwd_order WHERE status = 'paid'
GROUP BY dt, user_id;

CREATE VIEW IF NOT EXISTS dws_region_daily AS
SELECT dt, region, 'dau' AS metric, COUNT(DISTINCT user_id)*1.0 AS val
  FROM dwd_event WHERE event_type='app_open' GROUP BY dt, region
UNION ALL
SELECT date(reg_dt), region, 'new_users', COUNT(*)*1.0 FROM dwd_user GROUP BY date(reg_dt), region
UNION ALL
SELECT dt, region, 'gmv', SUM(amount) FROM dwd_order WHERE status='paid' GROUP BY dt, region
UNION ALL
SELECT dt, region, 'paid_orders', COUNT(*)*1.0 FROM dwd_order WHERE status='paid' GROUP BY dt, region
UNION ALL
SELECT dt, region, 'pay_uv', COUNT(DISTINCT user_id)*1.0 FROM dwd_order WHERE status='paid' GROUP BY dt, region
UNION ALL
SELECT dt, region, 'view_uv', COUNT(DISTINCT user_id)*1.0 FROM dwd_event WHERE event_type='view_item' GROUP BY dt, region
UNION ALL
SELECT dt, region, 'cart_uv', COUNT(DISTINCT user_id)*1.0 FROM dwd_event WHERE event_type='add_cart' GROUP BY dt, region
UNION ALL
SELECT dt, region, 'checkout_uv', COUNT(DISTINCT user_id)*1.0 FROM dwd_event WHERE event_type='checkout' GROUP BY dt, region;

-- ---------- ADS 应用层：面向指标/应用的宽表 ----------
CREATE VIEW IF NOT EXISTS ads_daily_region AS
SELECT dt, region,
  SUM(CASE WHEN metric='dau'          THEN val ELSE 0 END) AS dau,
  SUM(CASE WHEN metric='new_users'    THEN val ELSE 0 END) AS new_users,
  SUM(CASE WHEN metric='gmv'          THEN val ELSE 0 END) AS gmv,
  SUM(CASE WHEN metric='paid_orders'  THEN val ELSE 0 END) AS paid_orders,
  SUM(CASE WHEN metric='pay_uv'       THEN val ELSE 0 END) AS pay_uv,
  SUM(CASE WHEN metric='view_uv'      THEN val ELSE 0 END) AS view_uv,
  SUM(CASE WHEN metric='cart_uv'      THEN val ELSE 0 END) AS cart_uv,
  SUM(CASE WHEN metric='checkout_uv'  THEN val ELSE 0 END) AS checkout_uv
FROM dws_region_daily
GROUP BY dt, region;

-- 7 日留存：只统计观察窗已满 7 天的 cohort，口径统一收口在此视图
CREATE VIEW IF NOT EXISTS ads_retention AS
WITH cohort AS (
  SELECT u.user_id, u.region, u.reg_dt
  FROM dwd_user u, meta m
  WHERE m.key = 'anchor_date'
    AND u.reg_dt <= date(m.value, '-7 day')
),
rev AS (
  SELECT c.user_id, c.reg_dt, c.region,
    MAX(CASE WHEN e.dt > c.reg_dt AND e.dt <= date(c.reg_dt,'+1 day')
              AND e.event_type='app_open' THEN 1 ELSE 0 END) AS r1,
    MAX(CASE WHEN e.dt > c.reg_dt AND e.dt <= date(c.reg_dt,'+7 day')
              AND e.event_type='app_open' THEN 1 ELSE 0 END) AS r7
  FROM cohort c LEFT JOIN dwd_event e ON e.user_id = c.user_id
  GROUP BY c.user_id, c.reg_dt, c.region
)
SELECT reg_dt AS cohort_dt, region,
       COUNT(*) AS new_users,
       SUM(r1)  AS retained_1d,
       ROUND(1.0*SUM(r1)/COUNT(*), 4) AS retention_1d_rate,
       SUM(r7)  AS retained_7d,
       ROUND(1.0*SUM(r7)/COUNT(*), 4) AS retention_7d_rate
FROM rev GROUP BY reg_dt, region;

CREATE VIEW IF NOT EXISTS ads_repurchase AS
WITH u AS (
  SELECT user_id, region, COUNT(DISTINCT dt) AS buy_days
  FROM dwd_order WHERE status='paid'
  GROUP BY user_id, region
)
SELECT region,
       SUM(CASE WHEN buy_days>=2 THEN 1 ELSE 0 END) AS repurchase_users,
       COUNT(*) AS total_users,
       ROUND(1.0*SUM(CASE WHEN buy_days>=2 THEN 1 ELSE 0 END)/COUNT(*), 4) AS repurchase_rate
FROM u GROUP BY region;
