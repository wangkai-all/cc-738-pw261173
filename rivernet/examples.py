"""虚构样例生成：守恒多级汇合基准、环路错误、不同采样间隔。

所有测站与流量均为虚构，仅用于演示卷积河网分析，不用于真实水文预警。

合成数据严格按引擎同一个递推生成，因此在无缺测、核正确时：

    Q_n = I_n + Σ_{e=(u,n)} share_e * k_e ⊛ Q_u

贡献分解可以逐点闭合（质量守恒），并有 ``synthetic_truth`` 完整序列可供测试。
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

from .kernels import hydrograph_kernel
from .timeseries import NaN, to_jsonable


def _pulse(i: float, center: float, amp: float, rise: float, fall: float) -> float:
    d = i - center
    if d < 0:
        return amp * math.exp(-(d * d) / (2.0 * rise * rise))
    return amp * math.exp(-d / fall)


def _flow(i: float, base: float, events) -> float:
    v = base
    for c, amp, rise, fall in events:
        v += _pulse(i, c, amp, rise, fall)
    return v


def _effective(k: dict, share: float) -> dict:
    if share == 1.0:
        return k
    return {"lag_min": k["lag_min"], "weights": [w * share for w in k["weights"]]}


def _generate_recurrence(stations, edges_spec, local_events, n: int, pad: int):
    """在扩展网格上按拓扑递推 Q_n = I_n + Σ share*k⊛Q_u，再切回 n 点。"""
    ext_n = n + pad
    # edges_spec: (eid, u, v, kernel, share)
    incoming: Dict[str, List[dict]] = {s["id"]: [] for s in stations}
    for eid, u, v, k, share in edges_spec:
        incoming[v].append({"id": eid, "from": u, "kernel": k, "share": share})

    Qext: Dict[str, List[float]] = {}
    Iext: Dict[str, List[float]] = {}
    for s in stations:
        sid = s["id"]
        base = s.get("base", 3.0)
        evs = local_events.get(sid, [])
        Iarr = [_flow(ext - pad, base if not incoming[sid] else s.get("local_base", 1.2), evs)
                for ext in range(ext_n)]
        Iext[sid] = Iarr
        q = list(Iarr)
        for e in incoming[sid]:
            u = e["from"]
            uq = Qext[u]  # 拓扑生成顺序由调用方保证（站点按拓扑给出）
            keff = _effective(e["kernel"], e["share"])
            routed = _conv_ext(uq, keff)
            for t in range(ext_n):
                q[t] += routed[t]
        Qext[sid] = q

    # 切出 delivered 窗口
    Q = {sid: arr[pad:pad + n] for sid, arr in Qext.items()}
    I = {sid: arr[pad:pad + n] for sid, arr in Iext.items()}
    return Q, I


def _conv_ext(x: List[float], k: dict) -> List[float]:
    """扩展网格上的因果卷积；扩展前缀足够长，全部按越界 0 处理即可。"""
    l0 = int(k["lag_min"])
    w = k["weights"]
    n = len(x)
    y = [0.0] * n
    for t in range(n):
        acc = 0.0
        for j, wv in enumerate(w):
            i = t - (l0 + j)
            if 0 <= i < n:
                acc += wv * x[i]
        y[t] = acc
    return y


def _series_with_gaps(full: List[float], gaps) -> List[Optional[float]]:
    arr: List[float] = list(full)
    for a, b in gaps:
        for i in range(a, b + 1):
            if 0 <= i < len(arr):
                arr[i] = NaN
    return to_jsonable(arr)


# --------------------------------------------------------------------------- #
# 1) 多级汇合 + 守恒基准（小时序列）
# --------------------------------------------------------------------------- #

def build_conservation_bundle(gaps: bool = True) -> dict:
    n = 240
    pad = 14
    stations = [
        {"id": "YL", "name": "云岭（北源）", "base": 4.0},
        {"id": "QJ", "name": "青涧（南源）", "base": 3.2},
        {"id": "SG", "name": "松谷汇合站", "local_base": 1.4},
        {"id": "BS", "name": "白石溪（侧支）", "base": 2.6},
        {"id": "LJK", "name": "临江口（总控制）", "local_base": 1.1},
    ]
    k13 = hydrograph_kernel(1, 3, peak_lag=2.0)
    k23 = hydrograph_kernel(2, 6, peak_lag=4.2)
    k45 = hydrograph_kernel(1, 4, peak_lag=2.4)
    k35 = hydrograph_kernel(1, 5, peak_lag=3.0)
    edges_spec = [
        ("E-YL-SG", "YL", "SG", k13, 1.0),
        ("E-QJ-SG", "QJ", "SG", k23, 1.0),
        ("E-BS-LJK", "BS", "LJK", k45, 1.0),
        ("E-SG-LJK", "SG", "LJK", k35, 1.0),
    ]
    local_events = {
        "YL": [(28, 42, 2.2, 7.0), (118, 48, 2.4, 8.0), (196, 36, 2.2, 6.5)],
        "QJ": [(41, 33, 3.0, 9.0), (133, 40, 3.2, 10.0), (206, 28, 2.6, 8.0)],
        "BS": [(66, 25, 2.0, 6.0), (152, 30, 2.2, 7.5)],
        "SG": [(82, 6.5, 2.0, 5.0), (164, 7.5, 2.2, 6.0)],
        "LJK": [(92, 5.0, 2.0, 5.5), (178, 6.0, 2.2, 6.5)],
    }
    Q, I = _generate_recurrence(stations, edges_spec, local_events, n, pad)

    # 青涧站：校准后一段内部缺测 + 末尾停测，用于展示可识别范围。
    qj_gaps = [(138, 145), (216, n - 1)] if gaps else []
    series = {s["id"]: _series_with_gaps(Q[s["id"]], qj_gaps if s["id"] == "QJ" else [])
              for s in stations}

    edges = []
    for eid, u, v, k, share in edges_spec:
        edges.append({
            "id": eid, "from": u, "to": v, "share": share,
            "lag_min": k["lag_min"], "weights": [round(w, 6) for w in k["weights"]],
        })

    return {
        "id": "conservation-tree",
        "name": "守恒合成河网 · 两级汇合（1 小时）",
        "kind": "conservation",
        "description": (
            "由已知非负、总权重为 1 的传播核按 DAG 递推生成的虚构数据。"
            "云岭、青涧在松谷一级汇合，再与白石溪在临江口二级汇合。"
            "青涧站在评估期有内部缺测并在末尾停测。"
        ),
        "disclaimer": "全部为虚构数据，仅用于方法演示，不构成任何真实水文预警。",
        "grid": {"start": "2026-03-01T00:00", "dt_minutes": 60, "n": n},
        "stations": stations,
        "series": series,
        "edges": edges,
        "calibration": [8, 112],
        "evaluation": [113, 214],
        "selected_edge": "E-QJ-SG",
        "selected_target": "LJK",
        "known": {
            "conservation": True,
            "expected_closure_tol": 1e-6,
            "note": "核正确且数据完整时，各站增量贡献之和在可识别域内逐点等于实测流量。",
        },
        "synthetic_truth": {
            "series": {sid: to_jsonable(Q[sid], 6) for sid in Q},
            "incremental": {sid: to_jsonable(I[sid], 6) for sid in I},
        },
    }


# --------------------------------------------------------------------------- #
# 2) 不同采样间隔：同一物理尺度的两支汇合
# --------------------------------------------------------------------------- #

def build_interval_bundle(dt_minutes: int) -> dict:
    span_hours = {15: 132, 60: 168, 360: 240}[dt_minutes]
    n = int(round(span_hours * 60 / dt_minutes))
    sph = 60.0 / dt_minutes  # 每小时样本数

    def lag(hours_lo, hours_hi, peak_hours):
        lmin = max(0, int(round(hours_lo * sph)))
        lmax = max(lmin, int(round(hours_hi * sph)))
        peak = peak_hours * sph
        return hydrograph_kernel(lmin, lmax, peak_lag=peak - lmin if lmin else peak)

    kA = lag(1.0, 4.0, 2.2)     # 北沟：1–4 小时
    kB = lag(3.0, 9.0, 5.5)     # 南坡：3–9 小时
    pad = int(round(10 * sph)) + 4

    stations = [
        {"id": "N", "name": "北沟", "base": 2.2},
        {"id": "S", "name": "南坡", "base": 1.8},
        {"id": "C", "name": "三岔河汇合", "local_base": 0.8},
    ]
    edges_spec = [
        ("E-N-C", "N", "C", kA, 1.0),
        ("E-S-C", "S", "C", kB, 1.0),
    ]

    def ev(center_h, amp, rise_h, fall_h):
        return (center_h * sph, amp, max(0.6, rise_h * sph), max(0.6, fall_h * sph))

    local_events = {
        "N": [ev(20, 20, 1.5, 6), ev(70, 24, 1.6, 7), ev(108, 18, 1.4, 5)] if dt_minutes <= 60
        else [ev(24, 20, 3, 12), ev(120, 24, 3, 18), ev(200, 18, 3, 16)],
        "S": [ev(26, 16, 2.2, 9), ev(78, 20, 2.4, 10), ev(116, 15, 2, 8)] if dt_minutes <= 60
        else [ev(36, 16, 6, 24), ev(140, 20, 6, 30), ev(214, 15, 6, 24)],
        "C": [ev(50, 3.0, 1.5, 5)] if dt_minutes <= 60 else [ev(160, 3.0, 6, 18)],
    }
    Q, _ = _generate_recurrence(stations, edges_spec, local_events, n, pad)
    series = {s["id"]: to_jsonable(Q[s["id"]], 5) for s in stations}
    edges = [{
        "id": eid, "from": u, "to": v, "share": share,
        "lag_min": k["lag_min"], "weights": [round(w, 6) for w in k["weights"]],
    } for eid, u, v, k, share in edges_spec]

    cal_end = int(n * 0.55)
    return {
        "id": f"interval-{dt_minutes}min",
        "name": f"采样间隔对照 · {_dt_label(dt_minutes)}",
        "kind": "interval",
        "description": (
            f"与其它采样间隔样例保持相近的物理滞后（北沟 1–4 小时、南坡 3–9 小时），"
            f"但在 {_dt_label(dt_minutes)} 网格上滞后样本数不同。"
            "滞后范围可在“小时”和“样本”之间对照编辑。"
        ),
        "disclaimer": "全部为虚构数据，仅用于方法演示，不构成任何真实水文预警。",
        "grid": {"start": "2026-04-12T00:00", "dt_minutes": dt_minutes, "n": n},
        "stations": stations,
        "series": series,
        "edges": edges,
        "calibration": [max(0, int(n * 0.08)), cal_end],
        "evaluation": [cal_end + 1, n - 2],
        "selected_edge": "E-S-C",
        "selected_target": "C",
        "known": {"conservation": True},
    }


def _dt_label(dt_minutes: int) -> str:
    if dt_minutes % 1440 == 0:
        return f"{dt_minutes // 1440} 天"
    if dt_minutes % 60 == 0:
        return f"{dt_minutes // 60} 小时"
    return f"{dt_minutes} 分钟"


# --------------------------------------------------------------------------- #
# 3) 环路错误样例
# --------------------------------------------------------------------------- #

def build_cycle_bundle() -> dict:
    n = 96
    stations = [
        {"id": "A", "name": "东沟"},
        {"id": "B", "name": "西坡"},
        {"id": "C", "name": "环形断面"},
        {"id": "D", "name": "下游出口"},
    ]
    # 故意构造 A→B→C→A 环路，外加 C→D。
    edges = [
        {"id": "E-AB", "from": "A", "to": "B", "lag_min": 0, "weights": [0.4, 0.6]},
        {"id": "E-BC", "from": "B", "to": "C", "lag_min": 0, "weights": [0.5, 0.5]},
        {"id": "E-CA", "from": "C", "to": "A", "lag_min": 0, "weights": [0.3, 0.7]},
        {"id": "E-CD", "from": "C", "to": "D", "lag_min": 1, "weights": [1.0]},
    ]
    series = {}
    for s in stations:
        sid = s["id"]
        series[sid] = [round(_flow(i, 3.0, [(20 + ord(sid) % 5 * 6, 12, 2, 6)]), 3) for i in range(n)]
    return {
        "id": "cycle-error",
        "name": "错误样例 · 有向环路",
        "kind": "cycle",
        "description": "该河网包含 A→B→C→A 的有向环。工具必须拒绝传播计算并高亮环路，而不是静默给出结果。",
        "disclaimer": "全部为虚构数据，仅用于方法演示，不构成任何真实水文预警。",
        "grid": {"start": "2026-02-01T00:00", "dt_minutes": 60, "n": n},
        "stations": stations,
        "series": series,
        "edges": edges,
        "calibration": [10, 60],
        "evaluation": [61, 90],
        "selected_edge": "E-CA",
        "selected_target": "D",
        "known": {"conservation": False, "expect_error": "CYCLE_DETECTED"},
    }


# --------------------------------------------------------------------------- #
# 4) 干净的单级/多级数值基准（供测试与“精确恢复”演示，无缺测无噪声）
# --------------------------------------------------------------------------- #

def build_clean_chain_bundle() -> dict:
    """两支 → 汇 → 出口，无缺测，核已知，用于严格数值验证。"""
    n = 120
    pad = 12
    stations = [
        {"id": "U1", "name": "U1", "base": 2.0},
        {"id": "U2", "name": "U2", "base": 2.0},
        {"id": "M", "name": "M", "local_base": 0.0},
        {"id": "T", "name": "T", "local_base": 0.0},
    ]
    k1 = hydrograph_kernel(0, 4, peak_lag=2.0)
    k2 = hydrograph_kernel(1, 5, peak_lag=3.5)
    k3 = hydrograph_kernel(1, 3, peak_lag=2.0)
    edges_spec = [
        ("e1", "U1", "M", k1, 1.0),
        ("e2", "U2", "M", k2, 1.0),
        ("e3", "M", "T", k3, 1.0),
    ]
    local_events = {
        "U1": [(25, 30, 2, 6), (80, 34, 2, 7)],
        "U2": [(35, 26, 2.5, 8), (88, 28, 2.5, 8)],
    }
    Q, I = _generate_recurrence(stations, edges_spec, local_events, n, pad)
    edges = [{
        "id": eid, "from": u, "to": v, "share": sh,
        "lag_min": k["lag_min"], "weights": k["weights"],
    } for eid, u, v, k, sh in edges_spec]
    return {
        "id": "clean-chain",
        "grid": {"start": "2026-01-01T00:00", "dt_minutes": 60, "n": n},
        "stations": stations,
        "series": {s["id"]: to_jsonable(Q[s["id"]]) for s in stations},
        "edges": edges,
        "calibration": [0, 70],
        "evaluation": [71, 119],
        "_Q": Q,
        "_I": I,
    }


BUILDERS = {
    "conservation-tree": lambda: build_conservation_bundle(gaps=True),
    "interval-15min": lambda: build_interval_bundle(15),
    "interval-60min": lambda: build_interval_bundle(60),
    "interval-360min": lambda: build_interval_bundle(360),
    "cycle-error": build_cycle_bundle,
    "clean-chain": build_clean_chain_bundle,
}


def public_example_ids() -> List[str]:
    return ["conservation-tree", "interval-15min", "interval-60min", "interval-360min", "cycle-error"]
