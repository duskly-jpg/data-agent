# -*- coding: utf-8 -*-
"""编排层：自然语言 → 候选 SQL → 四道闸 → 只读执行 → 结论 → 拦截留痕。"""
import sqlite3
from datetime import date

from metrics import BY_KEY
from agent.llm import get_adapter
from agent.guard import run_guards, blocked_count, blocked_recent

RATE_METRICS = {"retention_7d", "retention_1d", "repurchase_rate", "order_cvr", "pay_cvr"}


def build_conclusion(question, cand, metric, rows):
    """结论必须由结果行生成，禁止模型自由发挥数字（Judge C 会对账）。"""
    f = metric.value_field
    is_rate = metric.key in RATE_METRICS
    region = cand.params.get("region", "ALL")
    region_s = "全区域" if region == "ALL" else region
    head = f"【{metric.name}】{cand.note}，{region_s}"

    if len(rows) == 1 or not any(k in rows[0] for k in ("dt", "cohort_dt")):
        r = rows[0]
        v = r[f]
        shown = f"{round(v * 100, 2)}%" if is_rate else f"{round(float(v), 2)}"
        extra = ""
        if metric.key == "retention_7d":
            extra = (f"（观察窗已满的新客 {int(r['new_users'])} 人，"
                     f"注册后 1~7 日内回访 {int(r['retained_7d'])} 人）")
        elif metric.key == "retention_1d":
            extra = f"（新客 {int(r['new_users'])} 人，次日回访 {int(r['retained_1d'])} 人）"
        elif metric.key == "repurchase_rate":
            extra = f"（付费用户 {int(r['total_users'])} 人，其中复购 {int(r['repurchase_users'])} 人）"
        return f"{head}：{shown}{extra}。口径：{metric.desc}"

    r = rows[-1]
    v = r[f]
    dt = r.get("dt") or r.get("cohort_dt")
    shown = f"{round(v * 100, 2)}%" if is_rate else f"{v:,}"
    return (f"{head}：共返回 {len(rows)} 行按日/区域明细，其中 {dt} 的{metric.name}为 {shown}。"
            f"口径：{metric.desc}")


class DataAgent:
    def __init__(self, db_path):
        self.db_path = db_path
        self.adapter = get_adapter()
        conn = sqlite3.connect(db_path)
        self.anchor = date.fromisoformat(
            conn.execute("SELECT value FROM meta WHERE key='anchor_date'").fetchone()[0])
        conn.close()

    def ask(self, question, candidate_override=None, conclusion_override=None):
        try:
            cand = candidate_override or self.adapter.generate(question, self.anchor)
        except Exception as e:
            from agent.guard import _log_block
            _log_block(self.db_path, question, "", "语义层路由", str(e), {})
            return {"ok": False, "question": question,
                    "stage": "语义层路由", "reason": str(e), "source": self.adapter.name}

        builder = (lambda q, c, m, r: conclusion_override) if conclusion_override is not None \
                  else build_conclusion
        g = run_guards(self.db_path, question, cand, builder)
        metric = BY_KEY.get(cand.metric_key)
        # 拦截时绝不生成结论：失败样本的结果列可能不完整，强行渲染会 KeyError，
        # 更重要的是「被拦的查询不该有任何结论流出」。
        conclusion = ""
        if g.passed:
            conclusion = conclusion_override if conclusion_override is not None else (
                build_conclusion(question, cand, metric, g.rows))

        return {
            "ok": g.passed, "question": question,
            "source": cand.source, "metric": cand.metric_key,
            "metric_name": metric.name if metric else "?",
            "sql": " ".join(cand.sql.split()), "params": cand.params,
            "reasoning": cand.reasoning, "note": cand.note,
            "stage": g.stage, "reason": g.reason, "judges": g.judges,
            "rows": g.rows, "conclusion": conclusion if g.passed else "",
            "blocked_total": blocked_count(self.db_path),
        }
