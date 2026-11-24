"""河网分析引擎：项目状态、增量重算、校准方案、贡献分解与洪峰分析。

设计要点
--------
* ``routed[e] = k_e ⊛ Q_from`` 只依赖起点测站序列与该边核；
* ``local[n] = Q_n - Σ share·routed[incoming]`` 是“滞后卷积后”的本地/未测增量，
  缺测处一律 NaN（绝不用 0 补）；
* 改一条边的核，只把该边及其下游边/节点标记为脏并重算，上游缓存保留；
* 每个校准方案保存自己的参数、指标和**数据切片快照**，之后改窗口/改核/再校准
  都不会改变旧方案。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from . import network as netmod
from .kernels import (
    KernelError,
    build_design,
    default_kernel,
    fit_simplex_ls,
    make_kernel,
    regression_metrics,
    validate_kernel,
)
from .timeseries import (
    NaN,
    compose_kernels,
    convolution,
    convolution_detail,
    identifiability,
    is_nan,
    merge_path_kernels,
    peak_at,
    route_series,
    to_internal,
    to_jsonable,
)


class ProjectError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}


class Project:
    def __init__(self, bundle: dict):
        self.bundle = bundle
        self.pid = bundle.get("id", "project")
        self.name = bundle.get("name", "未命名河网项目")
        self.description = bundle.get("description", "")
        self.disclaimer = bundle.get("disclaimer", "虚构数据，仅用于方法演示，不构成真实水文预警。")
        grid = bundle.get("grid", {})
        self.start = datetime.fromisoformat(grid.get("start", "2026-01-01T00:00"))
        self.dt_minutes = int(grid.get("dt_minutes", 60))
        self.n = int(grid.get("n", 0))

        self.stations: List[dict] = list(bundle.get("stations", []))
        self.station_ids = [str(s["id"]) for s in self.stations]
        self.station_by_id = {str(s["id"]): s for s in self.stations}

        raw_edges = bundle.get("edges", [])
        self.edges: List[dict] = []
        for e in raw_edges:
            e = dict(e)
            e["id"] = str(e["id"])
            e["from"] = str(e["from"])
            e["to"] = str(e["to"])
            e["share"] = float(e.get("share", 1.0))
            if "lag_min" not in e:
                e["lag_min"] = 0
            if "weights" in e:
                e["weights"] = [float(w) for w in e["weights"]]
                e["lag_max"] = int(e["lag_min"]) + len(e["weights"]) - 1
            self.edges.append(e)
        self.edge_by_id = {e["id"]: e for e in self.edges}

        series_in = bundle.get("series", {})
        self.x: Dict[str, List[float]] = {}
        for sid in self.station_ids:
            vals = series_in.get(sid, [None] * self.n)
            arr = to_internal(vals)
            if len(arr) != self.n:
                # 容忍长度不一致：补齐/截断到 grid.n
                arr = (arr + [NaN] * self.n)[: self.n]
            self.x[sid] = arr

        self.calibration = bundle.get("calibration", [0, max(0, self.n // 2 - 1)])
        self.evaluation = bundle.get("evaluation", [self.n // 2, self.n - 1])

        self.validation = netmod.validate_network(self.stations, self.edges)
        self._topo: Optional[List[str]] = None
        if self.validation["ok"]:
            self._topo = netmod.topological_order(self.stations, self.edges)

        self.schemes: List[dict] = []
        self._scheme_seq = 0
        self.state_rev = 0

        # 缓存与重算计数
        self._routed: Dict[str, List[float]] = {}
        self._local: Dict[str, List[float]] = {}
        self._contrib_cache: Dict[str, dict] = {}
        self.route_counts: Dict[str, int] = {e["id"]: 0 for e in self.edges}
        self.local_counts: Dict[str, int] = {sid: 0 for sid in self.station_ids}
        self.last_report: Optional[dict] = None

        if self.ok:
            self.recompute_all()

    # ----------------------------- 基础属性 ----------------------------- #

    @property
    def ok(self) -> bool:
        return self.validation["ok"]

    def require_ok(self):
        if not self.ok:
            cyc = next((x for x in self.validation["errors"] if x["code"] == "CYCLE_DETECTED"), None)
            if cyc:
                raise ProjectError("CYCLE_DETECTED", cyc["message"], 409, {"cycle": cyc.get("cycle")})
            raise ProjectError("INVALID_NETWORK", "河网结构校验未通过", 409,
                               {"errors": self.validation["errors"]})

    @property
    def topo(self) -> List[str]:
        self.require_ok()
        assert self._topo is not None
        return self._topo

    def time_at(self, i: int) -> datetime:
        return self.start + timedelta(minutes=self.dt_minutes * int(i))

    def time_label(self, i: int) -> str:
        return self.time_at(i).strftime("%m-%d %H:%M")

    def time_iso(self, i: int) -> str:
        return self.time_at(i).strftime("%Y-%m-%dT%H:%M")

    def lag_hours(self, lag_samples) -> float:
        return lag_samples * self.dt_minutes / 60.0

    # ----------------------------- 计算与增量 ---------------------------- #

    def routed(self, eid: str) -> List[float]:
        if eid not in self._routed:
            e = self.edge_by_id[eid]
            self._routed[eid] = route_series(self.x[e["from"]], e)
            self.route_counts[eid] = self.route_counts.get(eid, 0) + 1
        return self._routed[eid]

    def _incoming(self, sid: str) -> List[dict]:
        return [e for e in self.edges if e["to"] == sid]

    def local_inflow(self, sid: str) -> List[float]:
        if sid in self._local:
            return self._local[sid]
        n = self.n
        q = self.x[sid]
        inc = self._incoming(sid)
        local = [NaN] * n
        for t in range(n):
            qv = q[t]
            if is_nan(qv):
                continue
            total = 0.0
            known = True
            for e in inc:
                rv = self.routed(e["id"])[t]
                if is_nan(rv):
                    known = False
                    break
                total += e["share"] * rv
            if known:
                local[t] = qv - total
        self._local[sid] = local
        self.local_counts[sid] = self.local_counts.get(sid, 0) + 1
        return local

    def recompute_all(self) -> dict:
        for e in self.edges:
            self._routed[e["id"]] = route_series(self.x[e["from"]], e)
            self.route_counts[e["id"]] = 1
        for sid in self.topo:
            self._local.pop(sid, None)
            self.local_inflow(sid)
            self.local_counts[sid] = 1
        self._contrib_cache.clear()
        return {"recomputed_edges": [e["id"] for e in self.edges],
                "recomputed_nodes": list(self.topo), "cached_edges": []}

    def update_kernel(self, eid: str, weights: List[float], lag_min: Optional[int] = None) -> dict:
        self.require_ok()
        if eid not in self.edge_by_id:
            raise ProjectError("UNKNOWN_EDGE", f"没有河段 {eid}", 404)
        e = self.edge_by_id[eid]
        if lag_min is not None:
            e["lag_min"] = int(lag_min)
        e["weights"] = [float(w) for w in weights]
        e["lag_max"] = e["lag_min"] + len(e["weights"]) - 1
        try:
            validate_kernel(e)
        except KernelError as ex:
            raise ProjectError("INVALID_KERNEL", str(ex), 400)

        # —— 增量失效：仅该边 + 下游 ——
        dirty_edges = netmod.dirty_edges_after_change(self.edges, eid)
        dirty_edge_set = set(dirty_edges)
        all_edge_ids = [x["id"] for x in self.edges]
        cached_edges = [x for x in all_edge_ids if x not in dirty_edge_set]

        changed = self.edge_by_id[eid]
        dirty_nodes = netmod.downstream_nodes(self.edges, changed["to"], include_start=True)
        # 边自身的 routed 必重算；其余脏边也重算
        for deid in dirty_edges:
            de = self.edge_by_id[deid]
            self._routed[deid] = route_series(self.x[de["from"]], de)
            self.route_counts[deid] = self.route_counts.get(deid, 0) + 1

        # 仅按拓扑重算脏节点的本地增量
        recomputed_nodes: List[str] = []
        for sid in self.topo:
            if sid in dirty_nodes:
                self._local.pop(sid, None)
                self.local_inflow(sid)
                recomputed_nodes.append(sid)
                self.local_counts[sid] = self.local_counts.get(sid, 0) + 1

        # 失效目标位于脏节点集合内的贡献缓存
        for tgt in list(self._contrib_cache.keys()):
            if tgt in dirty_nodes:
                self._contrib_cache.pop(tgt, None)

        self.state_rev += 1
        report = {
            "changed_edge": eid,
            "recomputed_edges": dirty_edges,
            "cached_edges": cached_edges,
            "recomputed_nodes": recomputed_nodes,
            "state_rev": self.state_rev,
        }
        self.last_report = report
        return report

    def set_lag_range(self, eid: str, lag_min: int, lag_max: int) -> dict:
        lag_min, lag_max = int(lag_min), int(lag_max)
        if lag_min < 0:
            raise ProjectError("BAD_RANGE", "最小滞后不能为负")
        if lag_max < lag_min:
            raise ProjectError("BAD_RANGE", "最大滞后必须 ≥ 最小滞后")
        if lag_max - lag_min > 200:
            raise ProjectError("BAD_RANGE", "滞后范围过大（>200 个样本）")
        k = default_kernel(lag_min, lag_max)
        return self.update_kernel(eid, k["weights"], lag_min=lag_min)

    # ----------------------------- 对照量构造 --------------------------- #

    def confluence_input_prediction(self, v: str, exclude_edge: Optional[str] = None) -> List[float]:
        """Σ share·k⊛Q_from；exclude_edge 时跳过该边（用于扣除“其他支流”）。"""
        n = self.n
        out = [NaN] * n
        comps = []
        for e in self._incoming(v):
            if exclude_edge and e["id"] == exclude_edge:
                continue
            r = self.routed(e["id"])
            comps.append((e["share"], r))
        for t in range(n):
            total = 0.0
            known = True
            for share, r in comps:
                rv = r[t]
                if is_nan(rv):
                    known = False
                    break
                total += share * rv
            if known:
                out[t] = total
        return out

    def edge_comparison_target(self, eid: str) -> List[float]:
        """z = Q_to - Σ_{f≠e} share_f·routed_f，即该河段核的拟合对象。"""
        e = self.edge_by_id[eid]
        others = self.confluence_input_prediction(e["to"], exclude_edge=eid)
        y = self.x[e["to"]]
        z = [NaN] * self.n
        for t in range(self.n):
            if not is_nan(y[t]) and not is_nan(others[t]):
                z[t] = y[t] - others[t]
        return z

    # ----------------------------- 校准 --------------------------------- #

    def calibrate(
        self,
        eid: str,
        i0: int,
        i1: int,
        lag_min: Optional[int] = None,
        lag_max: Optional[int] = None,
        name: Optional[str] = None,
        apply: bool = True,
        exclude_intervals: Optional[List[List[int]]] = None,
    ) -> dict:
        self.require_ok()
        if eid not in self.edge_by_id:
            raise ProjectError("UNKNOWN_EDGE", f"没有河段 {eid}", 404)
        e = self.edge_by_id[eid]
        i0, i1 = int(i0), int(i1)
        if i1 < i0:
            raise ProjectError("BAD_SLICE", "校准区间结束早于开始")
        i0 = max(0, i0)
        i1 = min(self.n - 1, i1)

        if lag_min is None:
            lag_min = int(e.get("lag_min", 0))
        if lag_max is None:
            lag_max = int(e.get("lag_max", lag_min + len(e["weights"]) - 1))
        lag_min, lag_max = int(lag_min), int(lag_max)
        if lag_min < 0 or lag_max < lag_min:
            raise ProjectError("BAD_RANGE", "滞后范围非法")

        # 用户显式排除区间
        extra_valid = [True] * self.n
        excluded_by_user = 0
        excluded_intervals_norm: List[List[int]] = []
        for iv in exclude_intervals or []:
            a, b = int(iv[0]), int(iv[1])
            excluded_intervals_norm.append([a, b])
            for t in range(max(0, a), min(self.n - 1, b) + 1):
                if extra_valid[t]:
                    extra_valid[t] = False
                    excluded_by_user += 1

        z = self.edge_comparison_target(eid)
        share = e["share"]
        target_norm = [NaN] * self.n
        for t in range(self.n):
            if not is_nan(z[t]) and share > 0:
                target_norm[t] = z[t] / share

        A, bvec, used_t = build_design(
            self.x[e["from"]], target_norm, lag_min, lag_max, i0, i1, extra_valid
        )
        min_samples = max(3, lag_max - lag_min + 1)
        if len(A) < min_samples:
            raise ProjectError(
                "NOT_ENOUGH_SAMPLES",
                f"可用校准样本仅 {len(A)} 个，至少需要 {min_samples} 个"
                f"（待估滞后数 {lag_max - lag_min + 1}，并保留基本拟合检验样本）；"
                "请扩大校准期、缩小滞后范围或避开缺测区间。",
                422,
                {"n_samples": len(A), "n_lags": lag_max - lag_min + 1, "min_samples": min_samples},
            )

        fit = fit_simplex_ls(A, bvec)
        weights = fit["weights"]
        kernel = make_kernel(lag_min, weights)

        # 样本/排除统计
        cal_span = set(range(i0, i1 + 1))
        n_cal_points = i1 - i0 + 1
        excluded_missing = n_cal_points - len(used_t) - excluded_by_user
        excluded_missing = max(0, excluded_missing)

        # 区间指标（函数式卷积，不改状态）
        pred_full = convolution(self.x[e["from"]], weights, lag_min)
        cal_metrics = regression_metrics(target_norm, pred_full, list(range(i0, i1 + 1)))
        ev0, ev1 = int(self.evaluation[0]), int(self.evaluation[1])
        eval_metrics = regression_metrics(target_norm, pred_full, list(range(ev0, min(self.n - 1, ev1) + 1)))

        self._scheme_seq += 1
        sid = f"S{self._scheme_seq:03d}"
        # 快照必须包含最大滞后所需的校准窗前置样本，才能自包含重放同一设计矩阵。
        pad_start = max(0, i0 - int(lag_max))
        x_padded = [self.x[e["from"]][i] for i in range(pad_start, i1 + 1)]

        scheme = {
            "id": sid,
            "edge_id": eid,
            "name": name or f"方案 {sid} · {eid}",
            "created_rev": self.state_rev,
            "calibration": [i0, i1],
            "calibration_time": [self.time_iso(i0), self.time_iso(i1)],
            "evaluation": [ev0, min(self.n - 1, ev1)],
            "lag_min": lag_min,
            "lag_max": lag_max,
            "dt_minutes": self.dt_minutes,
            "kernel": kernel,
            "share": share,
            "fit": {
                "n_samples": fit["n_samples"],
                "iterations": fit["iterations"],
                "converged": fit["converged"],
                "kkt_residual": fit.get("kkt_residual"),
            },
            "cal_metrics": cal_metrics,
            "eval_metrics": eval_metrics,
            "excluded": {
                "user_points": excluded_by_user,
                "missing_or_boundary_points": excluded_missing,
                "used_points": len(used_t),
            },
            # 不可变数据切片快照：方案永久绑定创建时的校准数据
            "snapshot": {
                "i0": i0,
                "i1": i1,
                "lag_min": lag_min,
                "lag_max": lag_max,
                "pad_start": pad_start,
                "x_upstream": to_jsonable([self.x[e["from"]][i] for i in range(i0, i1 + 1)], 8),
                "x_padded": to_jsonable(x_padded, 8),
                "target": to_jsonable([target_norm[i] for i in range(i0, i1 + 1)], 8),
                "used_offsets": [t - i0 for t in used_t],
                "exclude_intervals": excluded_intervals_norm,
            },
        }
        self.schemes.append(scheme)

        report = None
        if apply:
            report = self.update_kernel(eid, weights, lag_min=lag_min)
            scheme["applied_rev"] = self.state_rev
        return {"scheme": self._public_scheme(scheme), "report": report}

    def _public_scheme(self, s: dict) -> dict:
        """列表/状态里不带庞大快照；单方案详情才带。"""
        return {k: v for k, v in s.items() if k != "snapshot"}

    def get_scheme(self, sid: str) -> dict:
        s = next((x for x in self.schemes if x["id"] == sid), None)
        if not s:
            raise ProjectError("UNKNOWN_SCHEME", f"没有校准方案 {sid}", 404)
        return s

    def apply_scheme(self, sid: str) -> dict:
        s = self.get_scheme(sid)
        k = s["kernel"]
        report = self.update_kernel(s["edge_id"], k["weights"], lag_min=k["lag_min"])
        return {"scheme": self._public_scheme(s), "report": report}

    def refit_snapshot(self, sid: str) -> dict:
        """用方案自带的数据切片快照重新拟合，验证其确实绑定自己的切片。"""
        s = self.get_scheme(sid)
        snap = s["snapshot"]
        l0, l1 = int(snap["lag_min"]), int(snap["lag_max"])

        if snap.get("x_padded") is not None and snap.get("pad_start") is not None:
            # 新快照：包含最大滞后所需的校准窗前置样本，可自包含重建设计矩阵。
            pad_start = int(snap["pad_start"])
            xp = [NaN if v is None else float(v) for v in snap["x_padded"]]
            z_slice = [NaN if v is None else float(v) for v in snap["target"]]
            zp = [NaN] * len(xp)
            target_local_i0 = int(snap["i0"]) - pad_start
            for k, v in enumerate(z_slice):
                zp[target_local_i0 + k] = v

            valid = [True] * len(xp)
            for a0, b0 in snap.get("exclude_intervals", []):
                for global_t in range(int(a0), int(b0) + 1):
                    local_t = global_t - pad_start
                    if 0 <= local_t < len(xp):
                        valid[local_t] = False
            A, b, _ = build_design(
                xp, zp, l0, l1,
                target_local_i0, len(xp) - 1, valid,
            )
        else:
            # 兼容早期快照：只保存校准窗切片。
            x = [NaN if v is None else float(v) for v in snap["x_upstream"]]
            z = [NaN if v is None else float(v) for v in snap["target"]]
            A, b, _ = build_design(x, z, l0, l1, 0, len(x) - 1)

        fit = fit_simplex_ls(A, b)
        return {"scheme_id": sid, "weights": fit["weights"], "original_weights": s["kernel"]["weights"]}

    # ----------------------------- 河段视图 ----------------------------- #

    def edge_view(self, eid: str) -> dict:
        self.require_ok()
        if eid not in self.edge_by_id:
            raise ProjectError("UNKNOWN_EDGE", f"没有河段 {eid}", 404)
        e = self.edge_by_id[eid]
        u, v = e["from"], e["to"]
        routed = self.routed(eid)
        z = self.edge_comparison_target(eid)
        pred_k = routed
        residual = [NaN] * self.n
        for t in range(self.n):
            if not is_nan(z[t]) and not is_nan(pred_k[t]):
                residual[t] = z[t] - e["share"] * pred_k[t]

        siblings = []
        for f in self._incoming(v):
            if f["id"] == eid:
                continue
            fr = self.routed(f["id"])
            eff = [NaN if is_nan(xv) else f["share"] * xv for xv in fr]
            siblings.append({
                "edge_id": f["id"], "from": f["from"],
                "from_name": self.station_by_id[f["from"]].get("name", f["from"]),
                "series": to_jsonable(eff, 4),
            })

        ident = identifiability(self.x[u], e)
        cal = [int(self.calibration[0]), int(self.calibration[1])]
        ev = [int(self.evaluation[0]), int(self.evaluation[1])]
        return {
            "edge": self._edge_public(e),
            "from_station": self._station_public(u),
            "to_station": self._station_public(v),
            "upstream": to_jsonable(self.x[u], 4),
            "downstream": to_jsonable(self.x[v], 4),
            "routed": to_jsonable([e["share"] * rv if not is_nan(rv) else NaN for rv in routed], 4),
            "fit_target": to_jsonable(z, 4),
            "residual": to_jsonable(residual, 4),
            "siblings": siblings,
            "identifiability": ident,
            "calibration": cal,
            "evaluation": ev,
            "cal_metrics": regression_metrics(z, [e["share"] * rv if not is_nan(rv) else NaN for rv in routed],
                                               list(range(cal[0], cal[1] + 1))),
            "eval_metrics": regression_metrics(z, [e["share"] * rv if not is_nan(rv) else NaN for rv in routed],
                                               list(range(ev[0], min(self.n - 1, ev[1]) + 1))),
            "peaks": self.edge_peaks(eid),
        }

    def edge_peaks(self, eid: str, i0: Optional[int] = None, i1: Optional[int] = None) -> dict:
        e = self.edge_by_id[eid]
        u, v = e["from"], e["to"]
        if i0 is None:
            i0, i1 = int(self.calibration[0]), int(self.calibration[1])
        up_peak = peak_at(self.x[u], i0, i1)
        rt = self.routed(eid)
        max_lag = int(e["lag_min"]) + len(e["weights"]) - 1
        search_end = min(self.n - 1, i1 + max_lag + 2)
        rt_peak = peak_at(rt, i0, search_end)
        down_peak = peak_at(self.x[v], i0, search_end)
        return {
            "window": [i0, i1],
            "upstream_peak": self._peak_public(up_peak),
            "routed_peak": self._peak_public(rt_peak),
            "downstream_peak": self._peak_public(down_peak),
            "routed_travel_samples": (rt_peak["index"] - up_peak["index"]) if rt_peak and up_peak else None,
            "observed_travel_samples": (down_peak["index"] - up_peak["index"]) if down_peak and up_peak else None,
            "kernel_mode_lag": int(e["lag_min"]) + max(range(len(e["weights"])), key=lambda j: e["weights"][j]),
        }

    def _peak_public(self, p):
        if not p:
            return None
        return {"index": p["index"], "value": round(p["value"], 4),
                "time": self.time_iso(p["index"]), "label": self.time_label(p["index"])}

    # ----------------------------- 贡献分解 ----------------------------- #

    def contributions(self, target: str) -> dict:
        self.require_ok()
        if target not in self.station_by_id:
            raise ProjectError("UNKNOWN_STATION", f"没有测站 {target}", 404)
        if target in self._contrib_cache:
            return self._contrib_cache[target]

        sources: List[dict] = []
        total = [0.0] * self.n
        total_known = [True] * self.n

        # 目标自身的本地增量
        local_t = self.local_inflow(target)

        for s in self.topo:
            source_x = self.local_inflow(s)
            if s == target:
                kernel = {"lag_min": 0, "weights": [1.0]}
                path_info = [{"nodes": [s, target], "lag_min": 0, "lag_max": 0, "share_product": 1.0}]
            else:
                paths = netmod.all_paths(self.edges, s, target)
                if not paths:
                    continue
                path_kernels = []
                path_info = []
                for pe in paths:
                    pk = {"lag_min": int(pe[0]["lag_min"]), "weights": list(pe[0]["weights"])}
                    share_prod = pe[0]["share"]
                    nodes = [pe[0]["from"], pe[0]["to"]]
                    for edge in pe[1:]:
                        pk = compose_kernels(pk, edge)
                        share_prod *= edge["share"]
                        nodes.append(edge["to"])
                    if share_prod != 1.0:
                        pk = {"lag_min": pk["lag_min"],
                              "weights": [w * share_prod for w in pk["weights"]],
                              "lag_max": pk["lag_max"]}
                    path_kernels.append(pk)
                    path_info.append({
                        "nodes": nodes,
                        "lag_min": pk["lag_min"],
                        "lag_max": pk["lag_min"] + len(pk["weights"]) - 1,
                        "share_product": share_prod,
                    })
                merged = merge_path_kernels(path_kernels)
                kernel = merged

            detail = convolution_detail(source_x, kernel["weights"], kernel["lag_min"])
            carr = detail["exact"]
            partial_arr = detail["partial"]
            fraction_arr = detail["fraction"]

            ident = identifiability(source_x, kernel)
            finite_mean = self._mean(carr)
            finite_sum = self._sum(carr)
            sources.append({
                "station_id": s,
                "station_name": self.station_by_id[s].get("name", s),
                "is_local": s == target,
                "series": to_jsonable(carr, 4),
                "partial_series": to_jsonable(partial_arr, 4),
                "known_fraction": [round(f, 3) for f in fraction_arr],
                "identifiability": ident,
                "paths": path_info,
                "n_paths": len(path_info),
                "mean": finite_mean,
                # 流量·秒 -> m³（若 Q 为 m³/s）；全缺测时保持 null。
                "volume": None if finite_sum is None else finite_sum * self.dt_minutes * 60.0,
            })
            for t in range(self.n):
                if is_nan(carr[t]):
                    total_known[t] = False
                else:
                    total[t] += carr[t]

        total_arr = [total[t] if total_known[t] else NaN for t in range(self.n)]
        observed = self.x[target]
        closure = [NaN] * self.n
        for t in range(self.n):
            if not is_nan(total_arr[t]) and not is_nan(observed[t]):
                closure[t] = observed[t] - total_arr[t]

        result = {
            "target": target,
            "target_name": self.station_by_id[target].get("name", target),
            "dt_minutes": self.dt_minutes,
            "sources": sources,
            "total": to_jsonable(total_arr, 4),
            "observed": to_jsonable(observed, 4),
            "closure_residual": to_jsonable(closure, 6),
            "closure_metrics": regression_metrics(observed, total_arr),
            "identifiable_indices": [i for i in range(self.n) if total_known[i]],
        }
        # 闭合误差统计（可识别域内最大绝对误差）
        finite_closure = [abs(v) for v in closure if not is_nan(v)]
        result["max_abs_closure_error"] = max(finite_closure) if finite_closure else None
        self._contrib_cache[target] = result
        return result

    @staticmethod
    def _mean(arr) -> Optional[float]:
        vals = [v for v in arr if not is_nan(v)]
        return sum(vals) / len(vals) if vals else None

    @staticmethod
    def _sum(arr) -> Optional[float]:
        vals = [v for v in arr if not is_nan(v)]
        return sum(vals) if vals else None

    def contributions_csv(self, target: str) -> str:
        data = self.contributions(target)
        buf = io.StringIO()
        writer = csv.writer(buf)
        header = ["time", "index"] + [f"{s['station_id']}_{s['station_name']}" for s in data["sources"]] \
                 + ["modeled_total", "observed", "closure_residual"]
        writer.writerow(header)
        n = self.n
        for t in range(n):
            row = [self.time_iso(t), t]
            for s in data["sources"]:
                v = s["series"][t]
                row.append("" if v is None else v)
            for key in ("total", "observed", "closure_residual"):
                v = data[key][t]
                row.append("" if v is None else v)
            writer.writerow(row)
        return buf.getvalue()

    def contributions_long_csv(self, target: str) -> str:
        """整洁长表：每站每时一行，带 exact/partial/unidentifiable 状态，绝不以 0 填缺。"""
        data = self.contributions(target)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["time", "index", "target", "station_id", "station_name",
                         "is_local_increment", "status", "contribution_exact",
                         "partial_known_tap_sum_lower_bound", "known_weight_fraction"])
        for s in data["sources"]:
            for t in range(self.n):
                exact = s["series"][t]
                partial = s["partial_series"][t]
                frac = s["known_fraction"][t]
                if exact is not None:
                    status = "exact"
                elif partial is not None:
                    status = "partial_after_outage"
                else:
                    status = "unidentifiable"
                writer.writerow([
                    self.time_iso(t), t, target, s["station_id"], s["station_name"],
                    int(s["is_local"]), status,
                    "" if exact is None else exact,
                    "" if partial is None else partial,
                    frac,
                ])
        return buf.getvalue()

    # ----------------------------- 残差来源 ----------------------------- #

    def residual_sources(self, target: str) -> dict:
        self.require_ok()
        if target not in self.station_by_id:
            raise ProjectError("UNKNOWN_STATION", f"没有测站 {target}", 404)
        incoming = self._incoming(target)
        branches = []
        pred = [0.0] * self.n
        pred_known = [True] * self.n
        for e in incoming:
            r = self.routed(e["id"])
            eff = [NaN if is_nan(v) else e["share"] * v for v in r]
            ident = identifiability(self.x[e["from"]], e)
            branches.append({
                "edge_id": e["id"],
                "station_id": e["from"],
                "station_name": self.station_by_id[e["from"]].get("name", e["from"]),
                "series": to_jsonable(eff, 4),
                "mean": self._mean(eff),
                "identifiability": ident,
                "cal_eval_rmse": self._edge_eval_rmse(e["id"]),
            })
            for t in range(self.n):
                if is_nan(eff[t]):
                    pred_known[t] = False
                else:
                    pred[t] += eff[t]

        observed = self.x[target]
        residual = [NaN] * self.n
        for t in range(self.n):
            if pred_known[t] and not is_nan(observed[t]):
                residual[t] = observed[t] - pred[t]
        local = self.local_inflow(target)  # 与 residual 等价（同一构造），缺测传播一致

        cal = [int(self.calibration[0]), int(self.calibration[1])]
        ev = [int(self.evaluation[0]), int(self.evaluation[1])]
        return {
            "target": target,
            "target_name": self.station_by_id[target].get("name", target),
            "branches": branches,
            "prediction": to_jsonable([pred[t] if pred_known[t] else NaN for t in range(self.n)], 4),
            "observed": to_jsonable(observed, 4),
            "residual": to_jsonable(residual, 4),
            "local_incremental": to_jsonable(local, 4),
            "cal_metrics": regression_metrics(observed, [pred[t] if pred_known[t] else NaN for t in range(self.n)],
                                              list(range(cal[0], cal[1] + 1))),
            "eval_metrics": regression_metrics(observed, [pred[t] if pred_known[t] else NaN for t in range(self.n)],
                                               list(range(ev[0], min(self.n - 1, ev[1]) + 1))),
            "missing_runs_target": identifiability(observed, {"lag_min": 0, "weights": [1.0]})["missing_runs"],
            "interpretation": (
                "残差 = 下游实测 − 各直接支流按当前核传播后的和，对应区间本地入流、未测支流与核误差；"
                "缺测传播导致的空白表示不可识别，不是零入流。"
            ),
        }

    def _edge_eval_rmse(self, eid: str):
        try:
            view_metrics = self.edge_view(eid)["eval_metrics"]
            return view_metrics.get("rmse")
        except Exception:
            return None

    # ----------------------------- 洪峰移动 ----------------------------- #

    def peak_movement(self, source: str, target: str, i0: Optional[int] = None, i1: Optional[int] = None) -> dict:
        self.require_ok()
        if source not in self.station_by_id or target not in self.station_by_id:
            raise ProjectError("UNKNOWN_STATION", "源或目标测站不存在", 404)
        if i0 is None:
            i0, i1 = int(self.calibration[0]), int(self.calibration[1])

        # 同源/目标是 UI 允许的选择；零边路径不应进入 main_path[0]。
        if source == target:
            src_peak = peak_at(self.x[source], i0, i1)
            pub = self._peak_public(src_peak)
            if pub:
                pub["lag_from_source_samples"] = 0
                pub["lag_from_source_hours"] = 0.0
            predicted = None
            if src_peak:
                predicted = {
                    "index": src_peak["index"],
                    "time": self.time_iso(src_peak["index"]),
                    "label": self.time_label(src_peak["index"]),
                    "lag_samples": 0,
                    "lag_hours": 0.0,
                }
            row = {
                "station_id": source,
                "station_name": self.station_by_id[source].get("name", source),
                "peak": pub,
                "series": to_jsonable(self.x[source], 3),
                "expected_arrival_window_samples": [0, 0],
                "expected_arrival_window_hours": [0.0, 0.0],
            }
            return {
                "reachable": True,
                "same_station": True,
                "source": source,
                "target": target,
                "window": [i0, i1],
                "window_time": [self.time_iso(i0), self.time_iso(i1)],
                "dt_minutes": self.dt_minutes,
                "chain": [source],
                "stations": [row],
                "path_kernel": {"lag_min": 0, "weights": [1.0]},
                "predicted_arrival": predicted,
                "n_paths": 1,
            }

        paths = netmod.all_paths(self.edges, source, target)
        if not paths:
            return {"source": source, "target": target, "reachable": False, "stations": []}

        # 取节点最多的主路径（树状河网通常唯一）
        main_path = max(paths, key=lambda pe: len(pe))
        node_chain = [main_path[0]["from"]] + [e["to"] for e in main_path]

        src_peak = peak_at(self.x[source], i0, i1)
        rows = []
        cum_min = 0
        cum_max = 0
        prev_peak_idx = src_peak["index"] if src_peak else i0
        for idx, sid in enumerate(node_chain):
            if idx == 0:
                pk = src_peak
                expected_min = expected_max = 0
            else:
                e = main_path[idx - 1]
                cum_min += int(e["lag_min"])
                cum_max += int(e["lag_min"]) + len(e["weights"]) - 1
                lo = max(0, prev_peak_idx + int(e["lag_min"]) - 2)
                hi = min(self.n - 1, prev_peak_idx + int(e["lag_min"]) + len(e["weights"]) + 2)
                pk = peak_at(self.x[sid], lo, hi)
                expected_min, expected_max = cum_min, cum_max
            pub = self._peak_public(pk)
            if pub:
                pub["lag_from_source_samples"] = pk["index"] - src_peak["index"] if src_peak else None
                pub["lag_from_source_hours"] = self.lag_hours(pub["lag_from_source_samples"]) \
                    if pub["lag_from_source_samples"] is not None else None
                prev_peak_idx = pk["index"]
            rows.append({
                "station_id": sid,
                "station_name": self.station_by_id[sid].get("name", sid),
                "peak": pub,
                "series": to_jsonable(self.x[sid], 3),
                "expected_arrival_window_samples": [expected_min, expected_max] if idx > 0 else [0, 0],
                "expected_arrival_window_hours": [round(self.lag_hours(expected_min), 2),
                                                  round(self.lag_hours(expected_max), 2)] if idx > 0 else [0, 0],
            })

        # 路径核的众数滞后 -> 预测洪峰到达
        pkernel = {"lag_min": int(main_path[0]["lag_min"]), "weights": list(main_path[0]["weights"])}
        for e in main_path[1:]:
            pkernel = compose_kernels(pkernel, e)
        mode_j = max(range(len(pkernel["weights"])), key=lambda j: pkernel["weights"][j])
        mode_lag = int(pkernel["lag_min"]) + mode_j
        predicted_arrival = None
        if src_peak:
            pai = src_peak["index"] + mode_lag
            if pai < self.n:
                predicted_arrival = {"index": pai, "time": self.time_iso(pai), "label": self.time_label(pai),
                                     "lag_samples": mode_lag, "lag_hours": round(self.lag_hours(mode_lag), 2)}

        return {
            "reachable": True,
            "source": source,
            "target": target,
            "window": [i0, i1],
            "window_time": [self.time_iso(i0), self.time_iso(i1)],
            "dt_minutes": self.dt_minutes,
            "chain": node_chain,
            "stations": rows,
            "path_kernel": {"lag_min": pkernel["lag_min"], "weights": [round(w, 5) for w in pkernel["weights"]]},
            "predicted_arrival": predicted_arrival,
            "n_paths": len(paths),
        }

    # ----------------------------- 序列化 ------------------------------- #

    def _edge_public(self, e: dict) -> dict:
        return {
            "id": e["id"], "from": e["from"], "to": e["to"], "share": e["share"],
            "lag_min": e["lag_min"], "lag_max": e.get("lag_max", e["lag_min"] + len(e["weights"]) - 1),
            "weights": e["weights"],
            "lag_min_hours": round(self.lag_hours(e["lag_min"]), 3),
            "lag_max_hours": round(self.lag_hours(e.get("lag_max", e["lag_min"] + len(e["weights"]) - 1)), 3),
        }

    def _station_public(self, sid: str) -> dict:
        s = self.station_by_id[sid]
        return {"id": sid, "name": s.get("name", sid)}

    def state(self) -> dict:
        return {
            "pid": self.pid,
            "name": self.name,
            "description": self.description,
            "disclaimer": self.disclaimer,
            "ok": self.ok,
            "validation": self.validation,
            "grid": {**self.bundle.get("grid", {}),
                     "time_labels": [self.time_label(i) for i in range(self.n)]},
            "stations": self.stations,
            "edges": [self._edge_public(e) for e in self.edges],
            "calibration": self.calibration,
            "evaluation": self.evaluation,
            "schemes": [self._public_scheme(s) for s in self.schemes],
            "state_rev": self.state_rev,
            "last_report": self.last_report,
            "selected_edge": self.bundle.get("selected_edge"),
            "selected_target": self.bundle.get("selected_target"),
            "known": self.bundle.get("known", {}),
        }
