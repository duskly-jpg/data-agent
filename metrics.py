# -*- coding: utf-8 -*-
"""
语义层 / 指标口径 —— 全项目唯一真源。

一份 Metric 同时服务于三件事：
  1) 注入 LLM 上下文（desc + dims + keywords），即「pcache 缓存的 schema 片段」
  2) 生成候选 SQL（Mock 路由 / Workers AI 的 few-shot 模板）
  3) 结果量级二次校验（bounds / max_rows / value_field），即幻觉治理闸门 3
"""
from dataclasses import dataclass, field


@dataclass
class Metric:
    key: str
    name: str
    desc: str                 # 口径定义，必须写清「分母/时间窗/观察期」
    dims: list                # 可下钻维度
    sql: str                  # 参数化 SQL，占位符 :start :end :region
    keywords: list            # 意图识别关键词（按优先级从具体到宽泛排列）
    value_field: str          # 需要做区间校验的结果列
    bounds: tuple             # (min, max) 合法区间，用于量级校验
    max_rows: int = 200
    required_tokens: list = field(default_factory=list)  # 口径一致性裁判必需片段
    # 组合查询（排行/对比/趋势）用的聚合规格，见文件末尾 AGG_SPEC
    source_view: str = ""
    agg_expr: str = ""
    date_col: str = ""


# region='ALL' 表示不分区域；观察窗约束统一放在分层视图里，避免各指标口径漂移
METRICS = [
    Metric(
        key="retention_7d", name="7日留存率",
        desc="注册日为 cohort，注册后第 1~7 天内出现过 app_open 记为留存；"
             "分母=该 cohort 新客数，仅统计观察窗已满 7 天的 cohort（口径写死在 ads_retention 视图）。"
             "注意：不是「近 7 天活跃」，是「注册后 7 日回访」。",
        dims=["cohort_dt", "region"],
        sql="""SELECT SUM(new_users) AS new_users,
                      SUM(retained_7d) AS retained_7d,
                      ROUND(1.0*SUM(retained_7d)/NULLIF(SUM(new_users),0),4) AS retention_7d_rate
               FROM ads_retention
               WHERE cohort_dt BETWEEN :start AND :end
                 AND (:region='ALL' OR region=:region)""",
        keywords=["7日留存", "七日留存", "7 日留存", "周留存", "7日回访", "留存"],
        value_field="retention_7d_rate", bounds=(0.0, 1.0),
        required_tokens=["ads_retention", "retention_7d_rate"],
    ),
    Metric(
        key="retention_1d", name="次日留存率",
        desc="注册后第 1 天（D+1）出现 app_open 记为留存；分母=该 cohort 新客数。",
        dims=["cohort_dt", "region"],
        sql="""SELECT SUM(new_users) AS new_users,
                      SUM(retained_1d) AS retained_1d,
                      ROUND(1.0*SUM(retained_1d)/NULLIF(SUM(new_users),0),4) AS retention_1d_rate
               FROM ads_retention
               WHERE cohort_dt BETWEEN :start AND :end
                 AND (:region='ALL' OR region=:region)""",
        keywords=["次日留存", "1日留存", "次留", "D+1留存"],
        value_field="retention_1d_rate", bounds=(0.0, 1.0),
        required_tokens=["ads_retention", "retention_1d_rate"],
    ),
    Metric(
        key="repurchase_rate", name="复购率",
        desc="全周期口径（不按时间窗切分）：下单天数>=2 的付费用户 / 全部付费用户；按用户去重，非按订单数。",
        dims=["region"],
        sql="""SELECT SUM(repurchase_users) AS repurchase_users,
                      SUM(total_users) AS total_users,
                      ROUND(1.0*SUM(repurchase_users)/NULLIF(SUM(total_users),0),4) AS repurchase_rate
               FROM ads_repurchase
               WHERE :region='ALL' OR region=:region""",
        keywords=["复购率", "复购", "重复购买"],
        value_field="repurchase_rate", bounds=(0.0, 1.0),
        required_tokens=["ads_repurchase", "repurchase_rate"],
    ),
    Metric(
        key="order_cvr", name="下单转化率",
        desc="checkout_uv / view_uv，同一自然日同一区域内去重用户数之比；口径为浏览→下单，非点击→支付。",
        dims=["dt", "region"],
        sql="""SELECT dt, region,
                      ROUND(1.0*SUM(checkout_uv)/NULLIF(SUM(view_uv),0),4) AS order_cvr
               FROM ads_daily_region
               WHERE dt BETWEEN :start AND :end
                 AND (:region='ALL' OR region=:region)
               GROUP BY dt, region ORDER BY dt LIMIT 200""",
        keywords=["下单转化", "浏览转化", "转化率", "转化"],
        value_field="order_cvr", bounds=(0.0, 1.0),
        required_tokens=["ads_daily_region", "checkout_uv", "view_uv"],
    ),
    Metric(
        key="pay_cvr", name="支付转化率",
        desc="pay_uv / checkout_uv，下单→支付的去重用户转化；与 GMV 无直接换算关系。",
        dims=["dt", "region"],
        sql="""SELECT dt, region,
                      ROUND(1.0*SUM(pay_uv)/NULLIF(SUM(checkout_uv),0),4) AS pay_cvr
               FROM ads_daily_region
               WHERE dt BETWEEN :start AND :end
                 AND (:region='ALL' OR region=:region)
               GROUP BY dt, region ORDER BY dt LIMIT 200""",
        keywords=["支付转化", "付款转化", "下单支付转化"],
        value_field="pay_cvr", bounds=(0.0, 1.0),
        required_tokens=["ads_daily_region", "pay_uv", "checkout_uv"],
    ),
    Metric(
        key="aov", name="客单价",
        desc="GMV / 已支付订单数，非「GMV / 付费用户数」；金额为元，四舍五入 2 位。",
        dims=["dt", "region"],
        sql="""SELECT dt, region,
                      ROUND(1.0*SUM(gmv)/NULLIF(SUM(paid_orders),0),2) AS aov
               FROM ads_daily_region
               WHERE dt BETWEEN :start AND :end
                 AND (:region='ALL' OR region=:region)
               GROUP BY dt, region ORDER BY dt LIMIT 200""",
        keywords=["客单价", "平均订单金额", "AOV", "单均价"],
        value_field="aov", bounds=(0.0, 100000.0),
        required_tokens=["ads_daily_region", "paid_orders", "gmv"],
    ),
    Metric(
        key="gmv", name="成交总额(GMV)",
        desc="仅 status='paid' 的订单金额求和；不含退款订单，不含未支付订单。",
        dims=["dt", "region"],
        sql="""SELECT dt, region, SUM(gmv) AS gmv
               FROM ads_daily_region
               WHERE dt BETWEEN :start AND :end
                 AND (:region='ALL' OR region=:region)
               GROUP BY dt, region ORDER BY dt LIMIT 200""",
        keywords=["GMV", "成交额", "成交总额", "销售额", "营收", "卖了多少"],
        value_field="gmv", bounds=(0.0, 1e12),
        required_tokens=["ads_daily_region", "gmv"],
    ),
    Metric(
        key="new_users", name="新增用户数",
        desc="按注册日(register_date)计，非首单日；按 user_id 去重，一个用户只计一次。",
        dims=["dt", "region"],
        sql="""SELECT dt, region, SUM(new_users) AS new_users
               FROM ads_daily_region
               WHERE dt BETWEEN :start AND :end
                 AND (:region='ALL' OR region=:region)
               GROUP BY dt, region ORDER BY dt LIMIT 200""",
        keywords=["新增用户", "新客数", "新增", "注册数", "拉新"],
        value_field="new_users", bounds=(0.0, 1e7),
        required_tokens=["ads_daily_region", "new_users"],
    ),
    Metric(
        key="dau", name="日活跃用户数(DAU)",
        desc="当日出现 app_open 事件的去重用户数；不含仅浏览/下单未打开 App 的用户。",
        dims=["dt", "region"],
        sql="""SELECT dt, region, SUM(dau) AS dau
               FROM ads_daily_region
               WHERE dt BETWEEN :start AND :end
                 AND (:region='ALL' OR region=:region)
               GROUP BY dt, region ORDER BY dt LIMIT 200""",
        keywords=["DAU", "日活", "日活跃", "活跃用户"],
        value_field="dau", bounds=(0.0, 1e7),
        required_tokens=["ads_daily_region", "dau"],
    ),
    Metric(
        key="mau", name="月活跃用户数(MAU)",
        desc="截至 end 日往前 30 天窗口内出现任意事件的去重用户数；窗口为滚动 30 天，非自然月。",
        dims=["region"],
        sql="""SELECT :region AS region, COUNT(DISTINCT user_id) AS mau
               FROM dwd_event
               WHERE dt BETWEEN date(:end,'-29 day') AND :end
                 AND (:region='ALL' OR region=:region)""",
        keywords=["MAU", "月活", "月活跃"],
        value_field="mau", bounds=(0.0, 1e7),
        required_tokens=["dwd_event", "COUNT(DISTINCT"],
    ),
]

BY_KEY = {m.key: m for m in METRICS}
ALLOWED_TABLES = {"ads_daily_region", "ads_retention", "ads_repurchase", "dwd_event", "dwd_user", "dwd_order"}
REGIONS = ["华东", "华北", "华南", "华中", "西南", "西北", "东北"]

# 组合查询（排行 / 多区域对比 / 趋势）的聚合规格。
# 与单指标 SQL 同源同口径：率类指标用「分子 SUM / 分母 SUM」加权，不是按日平均，
# 因此跨区域/跨日聚合后仍是正确口径。放在这里保证只有一处口径定义。
AGG_SPEC = {
    "retention_7d":    ("ads_retention",    "ROUND(1.0*SUM(retained_7d)/NULLIF(SUM(new_users),0),4)",      "cohort_dt"),
    "retention_1d":    ("ads_retention",    "ROUND(1.0*SUM(retained_1d)/NULLIF(SUM(new_users),0),4)",      "cohort_dt"),
    "repurchase_rate": ("ads_repurchase",   "ROUND(1.0*SUM(repurchase_users)/NULLIF(SUM(total_users),0),4)", ""),
    "order_cvr":       ("ads_daily_region", "ROUND(1.0*SUM(checkout_uv)/NULLIF(SUM(view_uv),0),4)",       "dt"),
    "pay_cvr":         ("ads_daily_region", "ROUND(1.0*SUM(pay_uv)/NULLIF(SUM(checkout_uv),0),4)",        "dt"),
    "aov":             ("ads_daily_region", "ROUND(1.0*SUM(gmv)/NULLIF(SUM(paid_orders),0),2)",           "dt"),
    "gmv":             ("ads_daily_region", "SUM(gmv)",                                                    "dt"),
    "new_users":       ("ads_daily_region", "SUM(new_users)",                                              "dt"),
    "dau":             ("ads_daily_region", "SUM(dau)",                                                    "dt"),
    "mau":             ("dwd_event",        "COUNT(DISTINCT user_id)",                                     "dt"),
}
for _k, _spec in AGG_SPEC.items():
    BY_KEY[_k].source_view, BY_KEY[_k].agg_expr, BY_KEY[_k].date_col = _spec

# MAU 是滚动 30 天窗口，与 :start~:end 不等价，故不参与组合查询（避免口径漂移）
COMPOSE_EXCLUDE = {"mau"}


def semantic_context(metric_keys=None):
    """注入 LLM 的 schema 片段（pcache 缓存的就是这段，避免把整库 schema 塞进上下文）。"""
    keys = metric_keys or [m.key for m in METRICS]
    lines = ["可用指标口径（只能使用以下视图与字段，禁止臆造表/字段）："]
    for k in keys:
        m = BY_KEY[k]
        lines.append(f"- {m.key} | {m.name} | {m.desc} | 维度:{','.join(m.dims)}")
    lines.append(f"可查询视图：{', '.join(sorted(ALLOWED_TABLES))}")
    lines.append("参数：:start :end 为 YYYY-MM-DD，:region 为区域名或 'ALL'。")
    lines.append("约束：只允许单条 SELECT/WITH；禁止 SELECT *；必须自带 LIMIT 或聚合。")
    return "\n".join(lines)
