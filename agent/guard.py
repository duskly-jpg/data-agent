# -*- coding: utf-8 -*-
"""
幻觉治理四道闸 —— 本项目最核心的部分。

Gate 1  SQL 静态白名单：仅 SELECT/WITH、单语句、去注释、禁危险关键字
Gate 2  字段/表校验 + 只读执行：表白名单、禁 SELECT *、强制 LIMIT，
        并用 SQLite set_authorizer 做「数据库级只读」硬防线（比正则强得多）
Gate 3  结果量级二次校验：行数、数值区间、非空率，量级约束来自指标口径
Gate 4  三裁判交叉评审：意图一致性 / 口径一致性 / 结论一致性，>=2 票通过才放行

任何一道拦截都写入 blocked_queries 表，形成「拦截 N 条错误查询」的可验证证据。
"""
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from metrics import ALLOWED_TABLES, BY_KEY

FORBIDDEN_KW = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|reindex|"
    r"vacuum|replace|trigger|load_extension|writefile|readfile)\b", re.I)
DENY_FUNCS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"}
_ALLOWED_ACTIONS = {
    getattr(sqlite3, "SQLITE_SELECT", None),
    getattr(sqlite3, "SQLITE_READ", None),
    getattr(sqlite3, "SQLITE_FUNCTION", None),
    getattr(sqlite3, "SQLITE_RECURSIVE", None),
    getattr(sqlite3, "SQLITE_TRANSACTION", None),
}
_ALLOWED_ACTIONS.discard(None)


def _authorizer(action, a1, a2, dbname, source):
    """SQLite 级只读：任何写操作/危险函数在此被数据库内核拒绝，绕过正则也无效。"""
    if action in _ALLOWED_ACTIONS:
        if action == sqlite3.SQLITE_FUNCTION:
            fn = str(a2 or a1 or "").lower()
            if fn in DENY_FUNCS:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


class ReadOnlyDB:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.set_authorizer(_authorizer)

    def query(self, sql, params):
        cur = self.conn.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        cols = [d[0] for d in cur.description] if cur.description else []
        return rows, cols

    # 拦截日志刻意用独立连接写（见 _log_block）：只读连接自己也不许写，
    # 保证「治理逻辑不给自己开后门」。
    def close(self):
        self.conn.close()


def _log_block(db_path, question, sql, stage, reason, judges):
    """日志写入用独立连接（只读连接故意不允许写，避免治理逻辑自己破防）。"""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO blocked_queries(ts,question,sql,stage,reason,judge_a,judge_b,judge_c) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), question, sql, stage, reason,
         judges.get("A", ""), judges.get("B", ""), judges.get("C", "")),
    )
    conn.commit()
    conn.close()


@dataclass
class GateResult:
    passed: bool
    stage: str = ""
    reason: str = ""
    judges: dict = field(default_factory=dict)
    rows: list = field(default_factory=list)
    cols: list = field(default_factory=list)


def gate1_static(sql):
    """闸门 1：SQL 静态白名单。注释一律先拒（防「去掉注释再校验」被绕过）。"""
    if re.search(r"/\*|--|#", sql):
        return False, "检测到注释，拒绝（防注释绕过白名单）"
    s = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    s = re.sub(r"--[^\n]*", " ", s).strip()
    if not s:
        return False, "空语句"
    hit = FORBIDDEN_KW.search(s)
    if hit:
        return False, f"命中危险关键字：{hit.group(0).upper()}"
    if not re.match(r"^(select|with)\b", s, re.I):
        return False, "非 SELECT/WITH 起始，拒绝"
    # 单语句：去掉末尾分号后不允许再出现分号
    body = s.rstrip().rstrip(";")
    if ";" in body:
        return False, "检测到多语句拼接"
    if re.search(r"/\*|--|#", body):
        return False, "检测到注释绕过"
    return True, "静态白名单通过（仅单条 SELECT/WITH）"


def gate2_schema(sql):
    """闸门 2：表/字段白名单、禁 SELECT *、强制 LIMIT 或聚合。"""
    s = sql.strip()
    if re.search(r"select\s+\*", s, re.I):
        return False, "禁止 SELECT * 全列扫描"
    tables = set(re.findall(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", s, re.I))
    illegal = tables - ALLOWED_TABLES
    if illegal:
        return False, f"引用了白名单外的表：{', '.join(sorted(illegal))}"
    if not tables:
        return False, "未识别到合法来源表"
    has_limit = re.search(r"\blimit\s+\d+", s, re.I)
    has_agg = re.search(r"\b(sum|count|avg|min|max|group\s+by)\s*\(", s, re.I) or \
              re.search(r"\bgroup\s+by\b", s, re.I)
    if not (has_limit or has_agg):
        return False, "缺少 LIMIT 或聚合，存在全表返回风险"
    m = re.search(r"\blimit\s+(\d+)", s, re.I)
    if m and int(m.group(1)) > 200:
        return False, "LIMIT 超过 200 行上限"
    # 字段层面：只允许查视图，视图字段固定；臆造字段会在执行时由 SQLite 编译报错
    return True, f"表 {', '.join(sorted(tables))} 均在白名单；只读 authorizer 已装载"


def gate3_volume(rows, cols, metric):
    """闸门 3：结果量级二次校验（约束直接来自指标口径，不是拍脑袋的常数）。"""
    if not rows:
        return False, "结果为空，拒绝输出结论（防把空结果编成数字）"
    if len(rows) > metric.max_rows:
        return False, f"返回 {len(rows)} 行，超过 {metric.max_rows} 行上限"
    f = metric.value_field
    if f not in cols:
        return False, f"结果缺少主指标列 {f}，疑似串指标输出"
    vals = [r.get(f) for r in rows]
    non_null = [v for v in vals if isinstance(v, (int, float))]
    if len(non_null) < len(vals) * 0.8:
        return False, "主指标列空值率 >20%，量级异常"
    lo, hi = metric.bounds
    for v in non_null:
        if not (lo <= v <= hi):
            return False, f"{f}={v} 越出口径合法区间 [{lo}, {hi}]"
    if all(v == 0 for v in non_null):
        return False, "主指标全为 0，疑似口径或过滤条件错误"
    return True, f"{len(rows)} 行、{f} 全部落在 [{metric.bounds[0]}, {metric.bounds[1]}]"


def gate4_judges(question, sql, metric, rows, conclusion):
    """闸门 4：三裁判交叉评审（迁移 offense-lab 方法论，规则+模型可混合）。"""
    from agent.llm import resolve_metric

    # 裁判 A：意图一致性 —— 独立再解析一次问题，与候选指标比对
    independent = resolve_metric(question)
    a = independent is not None and independent.key == metric.key
    reason_a = (f"独立意图解析={independent.key if independent else 'None'}，"
                f"候选={metric.key} → {'一致' if a else '不一致'}")

    # 裁判 B：口径一致性 —— 必需片段齐全 + 主输出字段存在 + 不得用 UNION 拼接指标
    missing = [t for t in metric.required_tokens if t not in sql]
    has_main_field = metric.value_field in sql
    has_union = bool(re.search(r"\bunion\b", sql, re.I))
    b = (not missing) and has_main_field and (not has_union)
    reason_b = (f"必需片段缺失={missing or '无'}；主输出字段{metric.value_field}="
                f"{'存在' if has_main_field else '缺失'}；UNION={'有' if has_union else '无'}"
                f" → {'口径一致' if b else '口径可疑'}")

    # 裁判 C：结论一致性 —— 只拿「主数值」对账，先剥掉日期/年份等干扰数字，
    # 避免 「2026-11-08」里的 11 恰好撞上某个 0.11 的比率造成误放行。
    clean = re.sub(r"\d{4}-\d{1,2}-\d{1,2}", " ", conclusion)
    clean = re.sub(r"\b(?:19|20)\d{2}\b", " ", clean)
    main_nums = re.findall(r"(?:为|：|:)\s*(-?\d+(?:\.\d+)?)\s*%?", clean)
    nums = [abs(float(x)) for x in main_nums] or \
           [abs(float(x)) for x in re.findall(r"-?\d+(?:\.\d+)?", clean)]
    result_vals = [abs(float(r.get(metric.value_field))) for r in rows
                   if isinstance(r.get(metric.value_field), (int, float))]
    matched = next((n for n in nums
                    if any(abs(n - v) < 1e-6 or abs(n - v * 100) < 0.011
                           for v in result_vals)), None)
    c = matched is not None
    detail = f"命中 {matched}，可对账" if c else "全部对不上"
    reason_c = f"主数值 {nums[:4]} vs 结果 {result_vals[:4]} → {detail}"

    judges = {"A": ("PASS" if a else "FAIL") + " | " + reason_a,
              "B": ("PASS" if b else "FAIL") + " | " + reason_b,
              "C": ("PASS" if c else "FAIL") + " | " + reason_c}
    # fail-closed：三裁判必须全票通过。少一票就拦，与准入校验同一套设计哲学。
    votes = sum([a, b, c])
    passed = a and b and c
    return passed, judges, (
        f"三裁判 {votes}/3 票，全票通过" if passed
        else f"三裁判 {votes}/3 票，任一异议即拦截(fail-closed)")


def run_guards(db_path, question, cand, conclusion_builder):
    """按序跑四道闸；任一失败即记录并返回。"""
    judges = {}
    sql = cand.sql

    ok, why = gate1_static(sql)
    if not ok:
        _log_block(db_path, question, sql, "Gate1-静态白名单", why, judges)
        return GateResult(False, "Gate1-静态白名单", why, judges)

    ok, why = gate2_schema(sql)
    if not ok:
        _log_block(db_path, question, sql, "Gate2-字段表校验", why, judges)
        return GateResult(False, "Gate2-字段表校验", why, judges)

    # 只读执行：authorizer 保证内核级不可写；臆造字段会在此编译报错
    rdb = ReadOnlyDB(db_path)
    try:
        rows, cols = rdb.query(sql, cand.params)
    except sqlite3.Error as e:
        rdb.close()
        why = f"只读执行失败（字段臆造/权限被拒/参数错误）：{e}"
        _log_block(db_path, question, sql, "Gate2-只读执行", why, judges)
        return GateResult(False, "Gate2-只读执行", why, judges)
    finally:
        try:
            rdb.close()
        except Exception:
            pass

    metric = BY_KEY.get(cand.metric_key)
    if metric is None:
        _log_block(db_path, question, sql, "Gate3-量级校验", "候选指标不在语义层注册表中", judges)
        return GateResult(False, "Gate3-量级校验", "候选指标不在语义层注册表中", judges)

    ok, why = gate3_volume(rows, cols, metric)
    if not ok:
        _log_block(db_path, question, sql, "Gate3-量级校验", why, judges)
        return GateResult(False, "Gate3-量级校验", why, judges, rows=rows, cols=cols)

    conclusion = conclusion_builder(question, cand, metric, rows)
    ok, judges, why = gate4_judges(question, sql, metric, rows, conclusion)
    if not ok:
        _log_block(db_path, question, sql, "Gate4-三裁判", why, judges)
        return GateResult(False, "Gate4-三裁判", why, judges, rows=rows, cols=cols)

    return GateResult(True, "全部通过", "四道闸全部通过", judges, rows=rows, cols=cols)


def blocked_count(db_path):
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM blocked_queries").fetchone()[0]
    conn.close()
    return n


def blocked_recent(db_path, limit=20):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT stage, reason, substr(question,1,40) AS q FROM blocked_queries "
        "ORDER BY id DESC LIMIT ?", (limit,))]
    conn.close()
    return rows
