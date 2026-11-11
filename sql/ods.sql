-- D1 部署用：ODS 原始层物理表 + 元信息（schema.sql 只管视图，两者分开便于重跑）
-- 执行：wrangler d1 execute data-agent-db --remote --file=sql/ods.sql

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS ods_users (
    user_id INTEGER PRIMARY KEY, register_date TEXT, region TEXT, channel TEXT
);
CREATE TABLE IF NOT EXISTS ods_orders (
    order_id INTEGER PRIMARY KEY, user_id INTEGER, order_ts TEXT,
    amount REAL, status TEXT, region TEXT
);
CREATE TABLE IF NOT EXISTS ods_events (
    event_id INTEGER PRIMARY KEY, user_id INTEGER, event_ts TEXT,
    event_type TEXT, region TEXT
);
CREATE INDEX IF NOT EXISTS ix_orders_user ON ods_orders(user_id, order_ts);
CREATE INDEX IF NOT EXISTS ix_events_user ON ods_events(user_id, event_ts);
CREATE INDEX IF NOT EXISTS ix_events_dt   ON ods_events(event_ts);

INSERT OR REPLACE INTO meta VALUES
    ('anchor_date',  '2026-11-12'),
    ('data_version', 'v1.0'),
    ('seed',         '42'),
    ('note',         '模拟数据，仅用于演示；口径见 metrics.py');

-- 灌数据：本地 python3 gen_data.py 生成 data.db 后，可用
--   sqlite3 data.db .dump | wrangler d1 execute data-agent-db --remote --stdin
-- 或按 README 第 7 节用 R2 里的 CSV 导入。
