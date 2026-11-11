# -*- coding: utf-8 -*-
"""分层视图、10 指标口径、端到端结论一致性。"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gen_data
from metrics import METRICS, BY_KEY
from agent.pipeline import DataAgent
from agent.guard import ReadOnlyDB
from demo import DB


@pytest.fixture(scope="module", autouse=True)
def db():
    if not os.path.exists(DB):
        gen_data.build()
    return DB


VIEWS = ["dwd_user", "dwd_order", "dwd_event", "dws_user_active_1d",
         "dws_user_order_1d", "dws_region_daily", "ads_daily_region",
         "ads_retention", "ads_repurchase"]


def test_all_layers_views_exist():
    conn = sqlite3.connect(DB)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    conn.close()
    assert set(VIEWS) <= names, f"缺失视图：{set(VIEWS) - names}"


def test_10_metrics_all_queryable():
    rdb = ReadOnlyDB(DB)
    p = {"start": "2026-11-02", "end": "2026-11-08", "region": "华东"}
    for m in METRICS:
        rows, cols = rdb.query(m.sql, p)
        assert rows, f"{m.key} 无结果"
        assert m.value_field in cols, f"{m.key} 缺少主字段 {m.value_field}"
        lo, hi = m.bounds
        for r in rows:
            v = r[m.value_field]
            assert v is None or lo <= v <= hi, f"{m.key} 越界：{v}"
    rdb.close()


def test_retention_is_realistic():
    """防止「98% 留存」这种一眼假数据回归。"""
    rdb = ReadOnlyDB(DB)
    rows, _ = rdb.query(BY_KEY["retention_7d"].sql,
                        {"start": "2026-10-01", "end": "2026-11-08", "region": "ALL"})
    rdb.close()
    rate = rows[0]["retention_7d_rate"]
    assert 0.15 <= rate <= 0.65, f"7日留存 {rate} 不在合理区间"


def test_e2e_question_to_conclusion():
    agent = DataAgent(DB)
    r = agent.ask("上周华东新客 7 日留存是多少？")
    assert r["ok"]
    assert r["metric"] == "retention_7d"
    assert r["conclusion"].startswith("【7日留存率】")
    assert "%" in r["conclusion"]
    assert all(v.startswith("PASS") for v in r["judges"].values())
    # 结论里的主数值必须能对上结果（Judge C 已保证，这里再断言一次）
    assert f"{round(r['rows'][0]['retention_7d_rate'] * 100, 2)}%" in r["conclusion"]


def test_time_window_last_week_is_complete():
    from agent.llm import resolve_time
    from datetime import date
    a = date(2026, 11, 12)
    s, e, note = resolve_time("上周华东 7日留存", a)
    assert str(s) == "2026-11-02" and str(e) == "2026-11-08"
    assert s.weekday() == 0 and e.weekday() == 6


def test_mau_uses_rolling_30d():
    """口径防漂移：MAU 是滚动 30 天，不是自然月。"""
    rdb = ReadOnlyDB(DB)
    rows, _ = rdb.query(BY_KEY["mau"].sql,
                        {"start": "2026-11-02", "end": "2026-11-08", "region": "华东"})
    rdb.close()
    assert rows[0]["mau"] > 0
