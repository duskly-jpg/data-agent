-- D1 物化迁移（一次性）：四张重视图 → 物化表 + 影子视图，worker 零改动
-- 口径真源仍是 sql/schema.sql；本文件顺序敏感：先建表灌数，再换影子视图
-- 刷新走 worker 的 cron（02:40 北京）或 POST /api/admin/refresh（X-Refresh-Token）

CREATE TABLE IF NOT EXISTS dws_region_daily_mv AS SELECT * FROM dws_region_daily WHERE 0;
CREATE TABLE IF NOT EXISTS ads_daily_region_mv AS SELECT * FROM ads_daily_region WHERE 0;
CREATE TABLE IF NOT EXISTS ads_retention_mv AS SELECT * FROM ads_retention WHERE 0;
CREATE TABLE IF NOT EXISTS ads_repurchase_mv AS SELECT * FROM ads_repurchase WHERE 0;
INSERT INTO dws_region_daily_mv SELECT * FROM dws_rdev_a
UNION ALL
SELECT * FROM dws_rdev_b;
INSERT INTO ads_daily_region_mv SELECT dt, region,
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
INSERT INTO ads_retention_mv WITH cohort AS (
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
INSERT INTO ads_repurchase_mv WITH u AS (
  SELECT user_id, region, COUNT(DISTINCT dt) AS buy_days
  FROM dwd_order WHERE status='paid'
  GROUP BY user_id, region
)
SELECT region,
       SUM(CASE WHEN buy_days>=2 THEN 1 ELSE 0 END) AS repurchase_users,
       COUNT(*) AS total_users,
       ROUND(1.0*SUM(CASE WHEN buy_days>=2 THEN 1 ELSE 0 END)/COUNT(*), 4) AS repurchase_rate
FROM u GROUP BY region;

DROP VIEW IF EXISTS dws_region_daily;
DROP VIEW IF EXISTS ads_daily_region;
DROP VIEW IF EXISTS ads_retention;
DROP VIEW IF EXISTS ads_repurchase;
CREATE VIEW IF NOT EXISTS dws_region_daily AS SELECT * FROM dws_region_daily_mv;
CREATE VIEW IF NOT EXISTS ads_daily_region AS SELECT * FROM ads_daily_region_mv;
CREATE VIEW IF NOT EXISTS ads_retention AS SELECT * FROM ads_retention_mv;
CREATE VIEW IF NOT EXISTS ads_repurchase AS SELECT * FROM ads_repurchase_mv;
