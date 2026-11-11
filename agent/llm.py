# -*- coding: utf-8 -*-
"""
LLM 适配器：可插拔，本地零凭证可跑，生产接 Workers AI / 任意 OpenAI 兼容端点。

设计取舍（大小模型混搭）：
  - MockAdapter = 语义层路由（确定性基线）。它不是「假装 LLM」，而是真实工业界的
    「指标平台 + LLM 路由」里的指标平台那一半：意图 → 口径 → 参数化 SQL。
    好处：可复现、可测试、可离线、token 成本 0，且天然不会臆造字段。
  - HTTPAdapter = 生产路径。把 semantic_context（pcache 缓存的 schema 片段）注入
    prompt，让模型产出候选 SQL；产出后依旧必须过 guard 四道闸，绝不信任模型输出。
"""
import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import timedelta

from metrics import METRICS, REGIONS, semantic_context


@dataclass
class Candidate:
    sql: str
    params: dict
    metric_key: str
    source: str = "mock"
    note: str = ""
    reasoning: str = ""


def resolve_metric(question):
    """按最长关键词优先独立解析意图（Judge A 也复用它，形成交叉校验）。"""
    best, best_len = None, -1
    for m in METRICS:
        for kw in m.keywords:
            if kw.lower() in question.lower() and len(kw) > best_len:
                best, best_len = m, len(kw)
    return best


def resolve_region(question):
    for r in REGIONS:
        if r in question:
            return r
    return "ALL"


def resolve_time(question, anchor):
    """返回 (start, end, 口径说明)。「上周」取上一个完整自然周，避免观察窗未满。"""
    q = question
    mon = anchor - timedelta(days=anchor.weekday())
    if "上上周" in q:
        return mon - timedelta(days=14), mon - timedelta(days=8), "上上周（周一~周日，完整周）"
    if "上周" in q:
        return mon - timedelta(days=7), mon - timedelta(days=1), "上周（周一~周日，完整周）"
    if "本周" in q:
        return mon, anchor, "本周（周一至今日）"
    if "昨日" in q or "昨天" in q:
        d = anchor - timedelta(days=1)
        return d, d, "昨日"
    if "近30天" in q or "最近一个月" in q or "近一月" in q:
        return anchor - timedelta(days=29), anchor, "近 30 天滚动窗口"
    if "本月" in q:
        return anchor.replace(day=1), anchor, "本自然月"
    if "近7天" in q or "最近一周" in q or "近一周" in q:
        return anchor - timedelta(days=6), anchor, "近 7 天滚动窗口"
    return anchor - timedelta(days=6), anchor, "默认近 7 天滚动窗口"


class MockAdapter:
    """语义层路由：意图 → 指标口径 → 参数化 SQL。参数一律绑定，绝不字符串拼接。"""

    name = "mock"

    def generate(self, question, anchor):
        m = resolve_metric(question)
        if m is None:
            raise ValueError("未识别到指标，语义层无对应口径（拒绝臆造 SQL）")
        start, end, time_note = resolve_time(question, anchor)
        region = resolve_region(question)
        if m.key == "repurchase_rate":
            time_note = "全周期口径（该指标不按时间窗切分）"
        params = {"start": start.isoformat(), "end": end.isoformat(), "region": region}
        return Candidate(
            sql=m.sql, params=params, metric_key=m.key, source="mock",
            note=time_note,
            reasoning=f"命中指标口径 {m.key}({m.name})；时间窗 {time_note}；区域 {region}；"
                      f"注入上下文仅含 {m.key} 的 schema 片段（pcache 命中，token 未整库注入）",
        )


class HTTPAdapter:
    """生产路径：Workers AI / 任意 OpenAI 兼容 chat-completions 端点。
    环境变量：LLM_ENDPOINT、LLM_TOKEN、LLM_MODEL。凭证只走 env，不落明文。"""

    name = "http"

    def __init__(self):
        self.endpoint = os.environ.get("LLM_ENDPOINT", "")
        self.token = os.environ.get("LLM_TOKEN", "")
        self.model = os.environ.get("LLM_MODEL", "@cf/meta/llama-3.1-8b-instruct")

    def available(self):
        return bool(self.endpoint and self.token)

    def generate(self, question, anchor):
        start, end, time_note = resolve_time(question, anchor)
        region = resolve_region(question)
        ctx = semantic_context()
        sys = (
            "你是 Text-to-SQL 生成器。严格依据给定口径输出 JSON，不要输出其它文字。\n"
            "格式：{\"sql\": \"...\", \"params\": {\"start\":\"YYYY-MM-DD\","
            "\"end\":\"YYYY-MM-DD\",\"region\":\"...\"}, \"metric_key\": \"...\"}\n"
            "只允许单条 SELECT/WITH，禁止 SELECT *，必须参数化，禁止臆造表或字段。\n\n"
            f"{ctx}\n\n时间参考：今天={anchor.isoformat()}，{time_note} => start={start}, end={end}。"
            f"区域={region}。"
        )
        payload = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": sys},
                         {"role": "user", "content": question}],
            "temperature": 0,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint, data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.token}"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError("模型输出未包含 JSON")
        obj = json.loads(m.group(0))
        return Candidate(
            sql=obj["sql"], params=obj["params"], metric_key=obj["metric_key"],
            source="http", note=time_note,
            reasoning=f"{self.model} 生成，上下文为 semantic_context() 缓存片段",
        )


def get_adapter():
    """有凭证走大模型，无凭证走语义层基线；生产建议两者都留，互为兜底。"""
    h = HTTPAdapter()
    return h if h.available() else MockAdapter()
