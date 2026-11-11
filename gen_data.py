# -*- coding: utf-8 -*-
"""
可复现模拟业务数据生成器。

本地：写 data.db（SQLite，等价 D1）；同时导出 CSV 到 r2/（等价 R2 原始层落地物）。
生产：把 r2/*.csv 用 wrangler r2 object put 上传，D1 用 wrangler d1 execute 灌入。
固定随机种子 + 固定 anchor_date，保证 CI 每次结果一致。
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

SEED = 42
ANCHOR = date(2026, 11, 12)      # 与投递时间对齐；改这里即可整体平移
WEEKS = 8                        # 生成 8 周数据，保证 7 日留存有完整观察窗
N_USERS = 8000
REGIONS = ["华东", "华北", "华南", "华中", "西南", "西北", "东北"]
CHANNELS = ["自然量", "信息流", "应用商店", "社交裂变", "搜索"]
EVENT_TYPES = ["app_open", "view_item", "add_cart", "checkout", "pay"]


def _d(ts):
    return ts.strftime("%Y-%m-%d")


def build():
    random.seed(SEED)
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
        ("seed", str(SEED)),
        ("note", "模拟数据，仅用于演示；口径见 metrics.py"),
    ])

    start = ANCHOR - timedelta(weeks=WEEKS)
    days = [(start + timedelta(days=i)) for i in range((ANCHOR - start).days + 1)]

    # --- 用户：注册日按周权重分布，区域带偏斜（华东占比最高） ---
    users = []
    region_weights = [30, 18, 16, 12, 10, 8, 6]
    for uid in range(1, N_USERS + 1):
        reg = random.choice(days[:-7])  # 至少留出 7 天观察窗
        region = random.choices(REGIONS, weights=region_weights, k=1)[0]
        users.append((uid, _d(reg), region, random.choice(CHANNELS)))
    c.executemany("INSERT INTO ods_users VALUES (?,?,?,?)", users)

    # --- 事件：按「留存衰减曲线」生成回访日 ---
    # 真实业务形态：D+1 回访约 28%，D+2~D+7 稀疏回访，D+8~D+30 长尾；
    # 由此 7 日留存落在 ~40%，次日留存 ~28%，避免「98% 留存」这种一眼假的数据。
    events, eid = [], 1
    for uid, reg_s, region, _ in users:
        reg = date.fromisoformat(reg_s)
        max_off = (ANCHOR - reg).days
        offsets = {0}                       # 注册当天必有 app_open（不计入留存）
        if max_off >= 1 and random.random() < 0.28:
            offsets.add(1)                  # D+1 回访
        for off in range(2, min(8, max_off + 1)):
            if random.random() < 0.030:     # D+2~D+7 稀疏回访
                offsets.add(off)
        for off in range(8, min(61, max_off + 1)):
            if random.random() < 0.012:     # 长尾回访（延伸到锚点，保证末期每日有量）
                offsets.add(off)
        for off in sorted(offsets):
            dt = reg + timedelta(days=off)
            rand = random.random()
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
        amount = round(random.choice([19.9, 39.0, 59.9, 99.0, 129.0, 199.0, 299.0])
                       * random.uniform(0.8, 1.6), 2)
        status = "refunded" if random.random() < 0.04 else "paid"
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
        "anchor": _d(ANCHOR), "users": len(users), "events": len(events),
        "orders": len(orders), "days": len(days), "db": DB,
    }
    conn.close()
    return stats


if __name__ == "__main__":
    s = build()
    print("[gen_data] 数据生成完成：")
    for k, v in s.items():
        print(f"  {k:8s} = {v}")
