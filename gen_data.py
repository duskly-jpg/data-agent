# -*- coding: utf-8 -*-
"""
可复现模拟业务数据生成器。

本地：写 data.db（SQLite，等价 D1）；同时导出 CSV 到 r2/（等价 R2 原始层落地物）。
生产：把 r2/*.csv 用 wrangler r2 object put 上传，D1 用 wrangler d1 execute 灌入。

随机性可配置（环境变量，二选一/都用）：
  DATA_SEED=42        默认。固定种子 → 数据完全可复现（CI / 演示用）
  DATA_SEED=random    真随机 → 每次生成的业务数据都不同（更接近真实）
  DATA_ANCHOR=2026-11-12  数据锚点，改它可整体平移时间窗

注意：固定种子只固定「数据」，不固定「问题」——问题由自然语言输入决定。
"""
import csv
import os
import random
import sqlite3
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data.db")
R2 = os.path.join(HERE, "r2")
SCHEMA = os.path.join(HERE, "sql", "schema.sql")

_seed_cfg = os.environ.get("DATA_SEED", "42").strip().lower()
SEED = None if _seed_cfg in ("random", "none", "") else int(_seed_cfg)
ANCHOR = date.fromisoformat(os.environ.get("DATA_ANCHOR", "2026-11-12"))
WEEKS = 8                        # 生成 8 周数据，保证 7 日留存有完整观察窗
N_USERS = 8000
REGIONS = ["华东", "华北", "华南", "华中", "西南", "西北", "东北"]
CHANNELS = ["自然量", "信息流", "应用商店", "社交裂变", "搜索"]
EVENT_TYPES = ["app_open", "view_item", "add_cart", "checkout", "pay"]


def _d(ts):
    return ts.strftime("%Y-%m-%d")


def build(seed=None):
    """seed 传 None 且 DATA_SEED=random 时为真随机；否则固定种子可复现。"""
    s = SEED if seed is None else seed
    rng = random.Random(s)   # s=None → 由系统熵播种，真随机
    os.makedirs(R2, exist_ok=True)
    if os.path.exists(DB):
        os.remove(DB)
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE ods_users  (user_id INTEGER PRIMARY KEY, register_date TEXT, region TEXT, channel TEXT);
        CREATE TABLE ods_orders (order_id INTEGER PRIMARY KEY, user_id INTEGER, order_ts TEXT,
                                 amount REAL, status TEXT, region TEXT);
        CREATE TABLE ods_events (event_id INTEGER PRIMARY KEY, user_id INTEGER, event_ts TEXT,
                                 event_type TEXT, region TEXT);
        CREATE TABLE blocked_queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, question TEXT, sql TEXT,
            stage TEXT, reason TEXT, judge_a TEXT, judge_b TEXT, judge_c TEXT
        );
        CREATE INDEX ix_orders_user ON ods_orders(user_id, order_ts);
        CREATE INDEX ix_events_user ON ods_events(user_id, event_ts);
        CREATE INDEX ix_events_dt   ON ods_events(event_ts);
    """)
    c.executemany("INSERT INTO meta VALUES (?,?)", [
        ("anchor_date", _d(ANCHOR)),
        ("data_version", "v1.0"),
        ("seed", "random" if s is None else str(s)),
        ("note", "模拟数据，仅用于演示；口径见 metrics.py"),
    ])

    start = ANCHOR - timedelta(weeks=WEEKS)
    days = [(start + timedelta(days=i)) for i in range((ANCHOR - start).days + 1)]

    # --- 用户：注册日按周权重分布，区域带偏斜（华东占比最高） ---
    users = []
    region_weights = [30, 18, 16, 12, 10, 8, 6]
    for uid in range(1, N_USERS + 1):
        reg = rng.choice(days[:-7])  # 至少留出 7 天观察窗
        region = rng.choices(REGIONS, weights=region_weights, k=1)[0]
        users.append((uid, _d(reg), region, rng.choice(CHANNELS)))
    c.executemany("INSERT INTO ods_users VALUES (?,?,?,?)", users)

    # --- 事件：按「留存衰减曲线」生成回访日 ---
    # 真实业务形态：D+1 回访约 28%，D+2~D+7 稀疏回访，D+8~D+30 长尾；
    # 由此 7 日留存落在 ~40%，次日留存 ~28%，避免「98% 留存」这种一眼假的数据。
    events, eid = [], 1
    for uid, reg_s, region, _ in users:
        reg = date.fromisoformat(reg_s)
        max_off = (ANCHOR - reg).days
        offsets = {0}                       # 注册当天必有 app_open（不计入留存）
        if max_off >= 1 and rng.random() < 0.28:
            offsets.add(1)                  # D+1 回访
        for off in range(2, min(8, max_off + 1)):
            if rng.random() < 0.030:     # D+2~D+7 稀疏回访
                offsets.add(off)
        for off in range(8, min(61, max_off + 1)):
            if rng.random() < 0.012:     # 长尾回访
                offsets.add(off)
        # 约 22% 用户为长期活跃用户，周期性回访直到锚点，保证末期每日 DAU 不塌。
        # 只在 D+8 以后追加，因此完全不影响次日/7 日留存口径。
        if rng.random() < 0.22:
            step = rng.choice([4, 5, 7, 10])
            for off in range(8 + rng.randint(0, step - 1), min(max_off + 1, 61), step):
                offsets.add(off)
        for off in sorted(offsets):
            dt = reg + timedelta(days=off)
            rand = rng.random()
            seq = ["app_open"]
            if rand < 0.72: seq.append("view_item")
            if rand < 0.38: seq.append("add_cart")
            if rand < 0.24: seq.append("checkout")
            if rand < 0.19: seq.append("pay")
            for et in seq:
                events.append((eid, uid, _d(dt), et, region))
                eid += 1
    c.executemany("INSERT INTO ods_events VALUES (?,?,?,?,?)", events)

    # --- 订单：与 pay 事件对齐，金额带长尾 ---
    orders, oid = [], 1
    for _, uid, dt, et, region in events:
        if et != "pay":
            continue
        amount = round(rng.choice([19.9, 39.0, 59.9, 99.0, 129.0, 199.0, 299.0])
                       * rng.uniform(0.8, 1.6), 2)
        status = "refunded" if rng.random() < 0.04 else "paid"
        orders.append((oid, uid, dt, amount, status, region))
        oid += 1
    c.executemany("INSERT INTO ods_orders VALUES (?,?,?,?,?,?)", orders)
    conn.commit()

    # --- 建分层视图 ---
    with open(SCHEMA, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()

    # --- 导出 R2 原始层 CSV（生产用 wrangler r2 object put 上传） ---
    for table in ("ods_users", "ods_orders", "ods_events"):
        cur = conn.execute(f"SELECT * FROM {table}")
        cols = [d[0] for d in cur.description]
        with open(os.path.join(R2, f"{table}.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(cur.fetchall())

    stats = {
        "anchor": _d(ANCHOR), "seed": "random" if s is None else s,
        "users": len(users), "events": len(events),
        "orders": len(orders), "days": len(days), "db": DB,
    }
    conn.close()
    return stats


if __name__ == "__main__":
    s = build()
    print("[gen_data] 数据生成完成：")
    for k, v in s.items():
        print(f"  {k:8s} = {v}")
