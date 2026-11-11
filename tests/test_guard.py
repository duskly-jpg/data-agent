# -*- coding: utf-8 -*-
"""四道闸单元测试：13 负样本全拒 + SQLite authorizer 内核级只读。"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.guard import (ReadOnlyDB, gate1_static, gate2_schema, gate3_volume,
                         gate4_judges, blocked_count, _log_block)
from metrics import BY_KEY
from demo import NEGATIVES, DB


@pytest.fixture(scope="module", autouse=True)
def db():
    if not os.path.exists(DB):
        import gen_data
        gen_data.build()
    conn = sqlite3.connect(DB)
    conn.execute("DELETE FROM blocked_queries")
    conn.commit()
    conn.close()
    yield DB


def test_gate1_rejects_injection():
    bad = [
        "DROP TABLE ods_users",
        "SELECT dau FROM ads_daily_region LIMIT 1; DELETE FROM ods_users",
        "PRAGMA table_info(ods_users)",
        "ATTACH DATABASE '/tmp/x.db' AS x",
        "SELECT dau FROM ads_daily_region /*x*/ LIMIT 5",
        "SELECT dau FROM ads_daily_region -- bypass",
    ]
    for sql in bad:
        ok, _ = gate1_static(sql)
        assert not ok, f"Gate1 漏放：{sql}"


def test_gate1_allows_clean_select():
    ok, _ = gate1_static("SELECT dau FROM ads_daily_region LIMIT 10")
    assert ok


def test_gate2_rejects_bad_schema():
    bad = [
        "SELECT * FROM ads_daily_region LIMIT 5",
        "SELECT name FROM sqlite_master LIMIT 5",
        "SELECT dau FROM ads_daily_region",
        "SELECT dau FROM ads_daily_region LIMIT 999999",
    ]
    for sql in bad:
        ok, _ = gate2_schema(sql)
        assert not ok, f"Gate2 漏放：{sql}"


def test_authorizer_blocks_write_at_sqlite_level():
    """核心防线：即使绕过正则，数据库内核也拒绝任何写操作。"""
    rdb = ReadOnlyDB(DB)
    with pytest.raises(sqlite3.Error):
        rdb.query("DELETE FROM ods_users", {})
    with pytest.raises(sqlite3.Error):
        rdb.query("UPDATE ods_users SET region='x'", {})
    with pytest.raises(sqlite3.Error):
        rdb.query("PRAGMA table_info(ods_users)", {})
    rdb.close()


def test_readonly_can_still_query():
    rdb = ReadOnlyDB(DB)
    rows, cols = rdb.query("SELECT COUNT(*) AS n FROM ods_users", {})
    rdb.close()
    assert rows[0]["n"] > 0


def test_gate3_volume_bounds():
    m = BY_KEY["retention_7d"]
    ok, why = gate3_volume([{"retention_7d_rate": 3.5}], ["retention_7d_rate"], m)
    assert not ok and "越出" in why
    ok, _ = gate3_volume([{"retention_7d_rate": 0.39}], ["retention_7d_rate"], m)
    assert ok
    ok, why = gate3_volume([], ["retention_7d_rate"], m)
    assert not ok and "空" in why


def test_gate4_fail_closed():
    m = BY_KEY["retention_7d"]
    sql = m.sql
    rows = [{"retention_7d_rate": 0.39}]
    # 意图不符：问 GMV 却答留存 → Judge A 异议 → 全票制拦截
    ok, judges, why = gate4_judges("上周华东 GMV 是多少？", sql, m, rows, "留存率 39%")
    assert not ok and judges["A"].startswith("FAIL")
    # 结论数字对不上 → Judge C 异议 → 拦截
    ok, judges, _ = gate4_judges("上周华东 7日留存是多少？", sql, m, rows, "留存率 99.99%")
    assert not ok and judges["C"].startswith("FAIL")
    # 全票通过
    ok, judges, _ = gate4_judges("上周华东 7日留存是多少？", sql, m, rows, "留存率 39%")
    assert ok and all(v.startswith("PASS") for v in judges.values())


def test_13_negatives_all_blocked():
    """端到端：13 条对抗样例必须全部拦截，且每条都落库留痕。"""
    from agent.pipeline import DataAgent
    from agent.llm import MockAdapter
    agent = DataAgent(DB)
    n = 0
    for name, q, cand in NEGATIVES:
        assert not agent.ask(q, candidate_override=cand)["ok"], f"漏放：{name}"
        n += 1
    cand = MockAdapter().generate("上周华东 7日留存是多少？", agent.anchor)
    assert not agent.ask("上周华东 7日留存是多少？", candidate_override=cand,
                         conclusion_override="留存率 99.99%")["ok"]
    n += 1
    assert n == 13
    assert blocked_count(DB) >= 13
