"""把用户提供的测站 CSV 与河网 JSON 解析成项目包（纯标准库）。

支持两种 CSV：
* 宽表：``time,STA1,STA2,...``；
* 长表：``time,station,flow``（列名允许常见别名）。

缺测单元格（空、``NA``、``NaN``、``-9999`` 等）统一为 None，绝不按 0 处理。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .kernels import default_kernel

MISSING_TOKENS = {"", "na", "nan", "null", "none", "-9999", "-9999.0", "#n/a", "缺测"}


class ImportError_(ValueError):
    pass


def _parse_float(token: str):
    if token is None:
        return None
    s = str(token).strip()
    if s.lower() in MISSING_TOKENS:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    # -9999 之类的水文缺测哨兵
    if v <= -9998:
        return None
    return v


def _parse_time(token: str) -> datetime:
    s = str(token).strip().replace("/", "-")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%m-%d %H:%M"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
    raise ImportError_(f"无法解析时间列：{token!r}（建议 ISO 格式，如 2026-03-01T00:00）")


def _infer_dt(times: List[datetime]) -> int:
    if len(times) < 2:
        return 60
    deltas = []
    for a, b in zip(times, times[1:]):
        d = (b - a).total_seconds() / 60.0
        if d > 0:
            deltas.append(d)
    if not deltas:
        return 60
    deltas.sort()
    return max(1, int(round(deltas[len(deltas) // 2])))


def _read_rows(csv_text: str) -> Tuple[List[str], List[dict]]:
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        raise ImportError_("CSV 为空或缺少表头")
    headers = [h.strip() for h in reader.fieldnames]
    rows = []
    for r in reader:
        rows.append({(k.strip() if k else k): v for k, v in r.items()})
    return headers, rows


def _find_col(headers: List[str], aliases: set) -> Optional[str]:
    low = {h.lower(): h for h in headers}
    for a in aliases:
        if a in low:
            return low[a]
    for h in headers:
        hl = h.lower()
        if any(a in hl for a in aliases):
            return h
    return None


def parse_flows_csv(csv_text: str) -> Tuple[List[datetime], Dict[str, List[Optional[float]]]]:
    headers, rows = _read_rows(csv_text)
    time_col = _find_col(headers, {"time", "timestamp", "datetime", "date", "时间", "时刻"})
    station_col = _find_col(headers, {"station", "site", "station_id", "site_id", "测站", "站名"})
    value_col = _find_col(headers, {"flow", "discharge", "q", "value", "流量", "值"})

    if time_col is None:
        raise ImportError_("CSV 必须包含 time/timestamp（时间）列")

    # 长表
    if station_col and value_col:
        times: List[datetime] = []
        tindex: Dict[datetime, int] = {}
        data: Dict[str, List[Optional[float]]] = {}
        for r in rows:
            t = _parse_time(r[time_col])
            sid = str(r[station_col]).strip()
            if t not in tindex:
                tindex[t] = len(times)
                times.append(t)
            ti = tindex[t]
            data.setdefault(sid, [None] * len(times))
            while len(data[sid]) < len(times):
                data[sid].append(None)
            data[sid][ti] = _parse_float(r.get(value_col))
        for sid in data:
            while len(data[sid]) < len(times):
                data[sid].append(None)
        return times, data

    # 宽表：除时间列外都是测站
    station_cols = [h for h in headers if h != time_col]
    if not station_cols:
        raise ImportError_("宽表 CSV 除 time 列外至少需要一个测站列")
    times = [_parse_time(r[time_col]) for r in rows if r.get(time_col) not in (None, "")]
    data = {sid: [] for sid in station_cols}
    for r in rows:
        if r.get(time_col) in (None, ""):
            continue
        for sid in station_cols:
            data[sid].append(_parse_float(r.get(sid)))
    return times, data


def parse_network(network) -> dict:
    if isinstance(network, str):
        try:
            network = json.loads(network)
        except json.JSONDecodeError as ex:
            raise ImportError_(f"network JSON 解析失败：{ex}")
    if not isinstance(network, dict) or "edges" not in network:
        raise ImportError_("河网定义必须是包含 edges 数组的 JSON 对象")
    return network


def build_bundle_from_text(
    flows_csv: str,
    network,
    name: str = "导入的河网项目",
    dt_minutes: Optional[int] = None,
    start: Optional[str] = None,
) -> dict:
    times, data = parse_flows_csv(flows_csv)
    if not times:
        raise ImportError_("CSV 没有数据行")
    net = parse_network(network)

    dt = int(dt_minutes) if dt_minutes else _infer_dt(times)
    n = len(times)
    grid_start = start or times[0].strftime("%Y-%m-%dT%H:%M")

    stations = net.get("stations")
    if not stations:
        stations = [{"id": sid, "name": sid} for sid in data.keys()]
    station_ids = {str(s["id"]) for s in stations}

    edges_out = []
    for i, e in enumerate(net.get("edges", [])):
        u, v = str(e.get("from", "")).strip(), str(e.get("to", "")).strip()
        if not u or not v:
            raise ImportError_(f"第 {i+1} 条边缺少 from/to")
        eid = str(e.get("id") or f"E-{u}-{v}")
        lag_min = max(0, int(e.get("lag_min", 0)))
        if e.get("weights"):
            weights = [max(0.0, float(w)) for w in e["weights"]]
            s = sum(weights)
            weights = [w / s for w in weights] if s > 1e-12 else default_kernel(lag_min, lag_min + 3)["weights"]
        else:
            lag_max = max(lag_min, int(e.get("lag_max", lag_min + 3)))
            weights = default_kernel(lag_min, lag_max)["weights"]
            lag_min = lag_min
        edges_out.append({
            "id": eid, "from": u, "to": v,
            "share": float(e.get("share", 1.0)),
            "lag_min": lag_min, "weights": weights,
        })
        station_ids.update([u, v])

    # 补齐只在 edges 中出现、stations 未声明的节点
    known = {str(s["id"]) for s in stations}
    for sid in station_ids:
        if sid not in known:
            stations.append({"id": sid, "name": sid})

    series = {}
    for s in stations:
        sid = str(s["id"])
        if sid in data:
            vals = data[sid]
            series[sid] = (vals + [None] * n)[:n] if len(vals) < n else vals[:n]
        else:
            series[sid] = [None] * n  # 未提供序列的测站：全部缺测，不填零

    cal_end = max(2, int(n * 0.55))
    return {
        "id": "imported",
        "name": name,
        "description": net.get("description", "由 CSV 与河网 JSON 导入。"),
        "disclaimer": "虚构/用户数据；本工具仅用于卷积分析演示，不构成真实水文预警。",
        "grid": {"start": grid_start, "dt_minutes": dt, "n": n},
        "stations": stations,
        "series": series,
        "edges": edges_out,
        "calibration": [0, cal_end],
        "evaluation": [min(n - 1, cal_end + 1), n - 1],
        "selected_edge": edges_out[0]["id"] if edges_out else None,
        "selected_target": stations[-1]["id"] if stations else None,
        "known": {"conservation": False},
    }
