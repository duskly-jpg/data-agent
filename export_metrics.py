# -*- coding: utf-8 -*-
"""
从 metrics.py（唯一真源）导出 metrics.json，供 Cloudflare Worker 共用。
改口径只改 metrics.py，然后跑一次本脚本；CI 会校验两者一致，防口径漂移。
"""
import json
import os

from metrics import METRICS, ALLOWED_TABLES, REGIONS

HERE = os.path.dirname(os.path.abspath(__file__))
out = {
    "metrics": [
        {
            "key": m.key, "name": m.name, "desc": m.desc, "dims": m.dims,
            "sql": m.sql, "keywords": m.keywords, "value_field": m.value_field,
            "bounds": list(m.bounds), "max_rows": m.max_rows,
            "required_tokens": m.required_tokens,
        }
        for m in METRICS
    ],
    "allowed_tables": sorted(ALLOWED_TABLES),
    "regions": REGIONS,
}
path = os.path.join(HERE, "worker", "metrics.json")
with open(path, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(f"[export_metrics] 已导出 {len(out['metrics'])} 个指标 -> {path}")
