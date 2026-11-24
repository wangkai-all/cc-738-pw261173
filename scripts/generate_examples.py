#!/usr/bin/env python3
"""把内置虚构样例生成到 examples/data/<id>/ 目录。

产物：
* ``project.json``  —— 应用可直接“打开示例”的完整项目包（严格 JSON，缺测为 null）；
* ``flows.csv``     —— 宽表测站序列 time,STA1,STA2,...（缺测为空）；
* ``network.json``  —— 河网与传播核定义。

用法：``python3 scripts/generate_examples.py``
"""

from __future__ import annotations

import csv
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from rivernet.examples import BUILDERS, public_example_ids  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402


def write_example(eid: str, out_root: str) -> str:
    bundle = BUILDERS[eid]()
    d = os.path.join(out_root, eid)
    os.makedirs(d, exist_ok=True)

    # project.json（去掉仅测试用的内部字段）
    proj = {k: v for k, v in bundle.items() if not k.startswith("_")}
    with open(os.path.join(d, "project.json"), "w", encoding="utf-8") as f:
        json.dump(proj, f, ensure_ascii=False, indent=2, allow_nan=False)

    # flows.csv（宽表）
    start = datetime.fromisoformat(bundle["grid"]["start"])
    dt = bundle["grid"]["dt_minutes"]
    n = bundle["grid"]["n"]
    sids = [s["id"] for s in bundle["stations"]]
    with open(os.path.join(d, "flows.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time"] + sids)
        for i in range(n):
            t = (start + timedelta(minutes=dt * i)).strftime("%Y-%m-%dT%H:%M")
            row = [t]
            for sid in sids:
                v = bundle["series"][sid][i]
                row.append("" if v is None else (round(float(v), 4)))
            w.writerow(row)

    # network.json
    net = {
        "stations": bundle["stations"],
        "edges": [{k: v for k, v in e.items()} for e in bundle["edges"]],
    }
    with open(os.path.join(d, "network.json"), "w", encoding="utf-8") as f:
        json.dump(net, f, ensure_ascii=False, indent=2, allow_nan=False)

    return d


def main():
    out_root = os.path.join(ROOT, "examples", "data")
    os.makedirs(out_root, exist_ok=True)
    for eid in public_example_ids():
        d = write_example(eid, out_root)
        print("wrote", os.path.relpath(d, ROOT))


if __name__ == "__main__":
    main()
