# -*- coding: utf-8 -*-
"""
组合意图编译器 —— 解决「只能回答 10 个预设问题」这个短板。

不是再加 10 个模板，而是把「排行 / 多区域对比 / 趋势」这类**非预设问题**
编译成动态 SQL（按区域或按日聚合），然后**照样走同一套四道闸 + 三裁判**。
因此治理逻辑完全不依赖预设模板，这也是本项目最值得讲的一点。

设计要点：
  - 聚合规格来自 metrics.py 的 AGG_SPEC（单一真源），率类用「分子 SUM / 分母 SUM」
    加权，跨区域/跨日聚合后口径依旧正确，不是按日平均。
  - 复合问题不绕开治理：动态 SQL 与模板 SQL 一样过 Gate1~Gate4，拦截同样留痕。
  - 识别不了就返回 None，回退到单指标链路；再识别不了就诚实拒答，不硬猜。
"""
from datetime import timedelta

from metrics import BY_KEY, COMPOSE_EXCLUDE, REGIONS
from agent.llm import Candidate, resolve_metric, resolve_time

RANK_WORDS = ["各区域", "所有区域", "每个区域", "排名", "排行", "top", "最高", "最大",
              "最好", "哪个区域", "谁最", "分布", "对比一下", "比较一下"]
COMPARE_WORDS = ["和", "跟", "与", "vs", "对比", "相比", "哪个高", "谁更", "差多少"]
TREND_WORDS = ["趋势", "走势", "变化", "每天", "逐日", "这几天", "波动", "涨了", "跌了"]
RATE_KEYS = {"retention_7d", "retention_1d", "repurchase_rate", "order_cvr", "pay_cvr"}


def detect(question, anchor):
    """返回 (kind, metric, regions, time_note)；无法组合则返回 None。"""
    m = resolve_metric(question)
    if m is None or m.key in COMPOSE_EXCLUDE or not m.source_view:
        return None
    q = question.lower()
    regions = [r for r in REGIONS if r in question]
    if any(w in q for w in TREND_WORDS) and m.date_col:
        kind = "trend"
    elif len(regions) >= 2 and any(w in q for w in COMPARE_WORDS):
        kind = "compare"
    elif any(w in q for w in RANK_WORDS):
        kind = "rank"
    else:
        return None
    start, end, note = resolve_time(question, anchor)
    return kind, m, regions, note, start, end


def build_sql(kind, m, start, end):
    """只查 ADS/DWD 白名单视图，参数化，自带 GROUP BY + LIMIT，符合 Gate1/Gate2。"""
    f = m.value_field
    where = f"WHERE {m.date_col} BETWEEN :start AND :end" if m.date_col else ""
    if kind == "trend":
        sql = (f"SELECT {m.date_col} AS dt, {m.agg_expr} AS {f} "
               f"FROM {m.source_view} {where} "
               f"GROUP BY {m.date_col} ORDER BY dt LIMIT 200")
    else:
        sql = (f"SELECT region, {m.agg_expr} AS {f} "
               f"FROM {m.source_view} {where} "
               f"GROUP BY region ORDER BY {f} DESC LIMIT 20")
    params = {"start": start.isoformat(), "end": end.isoformat()} if m.date_col else {}
    return " ".join(sql.split()), params


def make_conclusion_builder(kind, m, regions, note):
    """结论由结果行生成（Judge C 会对账），不引入任何模型自由发挥的数字。"""
    f = m.value_field
    is_rate = m.key in RATE_KEYS

    def fmt(v):
        return f"{round(v * 100, 2)}%" if is_rate else f"{round(float(v), 2)}"

    def builder(question, cand, metric, rows):
        if kind == "trend":
            a, b = rows[0], rows[-1]
            d = b[f] - a[f]
            word = "上升" if d > 0 else ("下降" if d < 0 else "持平")
            return (f"【{m.name}趋势】{note}，共 {len(rows)} 天：{a['dt']} 为 {fmt(a[f])}，"
                    f"{b['dt']} 为 {fmt(b[f])}，整体{word} {fmt(abs(d))}。"
                    f"（口径：{m.desc}）")

        ordered = sorted(rows, key=lambda r: r[f], reverse=True)
        top, bottom = ordered[0], ordered[-1]
        # 「为 X」格式让 Judge C 能提取到主数值与结果对账（刻意设计，不是凑文案）
        base = (f"【{m.name}区域排行】{note}，共 {len(rows)} 个区域："
                f"最高 {top['region']}，为 {fmt(top[f])}；"
                f"最低 {bottom['region']}，为 {fmt(bottom[f])}。")
        if kind == "compare" and len(regions) >= 2:
            lookup = {r["region"]: r[f] for r in rows}
            a, b = regions[0], regions[1]
            if a in lookup and b in lookup:
                d = lookup[a] - lookup[b]
                lead = a if d > 0 else (b if d < 0 else "两者")
                diff_fmt = f"{round(abs(d) * 100, 2)}pp" if is_rate else f"{round(float(abs(d)), 2)}"
                extra = (f" 对比结果：{a} 为 {fmt(lookup[a])}，{b} 为 {fmt(lookup[b])}，"
                         f"{lead}{'领先' if d else '持平'} {diff_fmt}。")
                base += extra
        return base + f"（口径：{m.desc}）"

    return builder


def compile_question(question, anchor):
    """返回 (Candidate, conclusion_builder)，或 None 表示这不是组合问题。"""
    d = detect(question, anchor)
    if d is None:
        return None
    kind, m, regions, note, start, end = d
    sql, params = build_sql(kind, m, start, end)
    cand = Candidate(
        sql=sql, params=params, metric_key=m.key, source="composed-sql",
        note=note,
        reasoning=(f"组合意图编译：kind={kind}、聚合规格来自 AGG_SPEC[{m.key}]、"
                   f"动态生成 SQL（非模板），仍完整过四道闸"),
    )
    return cand, make_conclusion_builder(kind, m, regions, note)
