"""河网逐段卷积与缺测支持域工具（纯标准库）。

时间序列约定
------------
* 内部用 ``float('nan')`` 表示缺测；进出 JSON 时统一在 ``jsonio`` 里转成 ``None``。
* 一条核由 ``lag_min`` 与等长 ``weights`` 描述：``weights[j]`` 对应绝对滞后
  ``lag = lag_min + j`` 个采样间隔。
* 传播公式：``y[t] = sum_j weights[j] * x[t - lag]``。
* 只要某个“显著非零权重”对应的输入越界或缺测，该输出点就不可识别，记为 NaN，
  绝不把缺测当 0 参与制造“支流流入”。
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

# 大于该阈值的权重才要求对应输入必须有观测；极小权重视为数值零。
ACTIVE_EPS = 1e-8

NaN = float("nan")


def is_nan(v) -> bool:
    return isinstance(v, float) and math.isnan(v)


def to_internal(values: Sequence[Optional[float]]) -> List[float]:
    """JSON 数组（None 表示缺测）-> 内部 NaN 数组。"""
    out: List[float] = []
    for v in values:
        if v is None or v == "":
            out.append(NaN)
        else:
            f = float(v)
            out.append(NaN if math.isnan(f) else f)
    return out


def to_jsonable(values: Sequence[float], ndigits: Optional[int] = None) -> List[Optional[float]]:
    """内部 NaN 数组 -> 严格 JSON 可序列化数组（NaN -> None）。"""
    out: List[Optional[float]] = []
    for v in values:
        if v is None or is_nan(v):
            out.append(None)
        elif ndigits is not None:
            out.append(round(float(v), ndigits))
        else:
            out.append(float(v))
    return out


def observed_mask(x: Sequence[float]) -> List[bool]:
    return [not is_nan(v) for v in x]


def convolution(
    x: Sequence[float],
    weights: Sequence[float],
    lag_min: int = 0,
    edge: str = "missing",
    active_eps: float = ACTIVE_EPS,
) -> List[float]:
    """对齐的一维离散卷积（输出与 ``x`` 等长，索引 t 对应同一时刻）。

    参数
    ----
    edge:
        * ``"missing"``：越界输入与缺测输入同等对待，导致该输出 NaN（默认可识别语义）；
        * ``"zero"``：越界输入按 0 处理（用于和教科书卷积对照的边界测试），
          但序列内部的 NaN 仍会使对应输出 NaN。

    权重为 0（或 <= active_eps）的抽头不要求输入有效。
    """
    if edge not in ("missing", "zero"):
        raise ValueError("edge 必须是 'missing' 或 'zero'")
    n = len(x)
    m = len(weights)
    y: List[float] = [NaN] * n
    for t in range(n):
        acc = 0.0
        supported = True
        for j, w in enumerate(weights):
            if w == 0.0:
                continue
            lag = lag_min + j
            i = t - lag
            if i < 0 or i >= n:
                if edge == "missing" and abs(w) > active_eps:
                    supported = False
                    break
                # edge == 'zero'：越界项贡献 0
                continue
            xv = x[i]
            if is_nan(xv):
                if abs(w) > active_eps:
                    supported = False
                    break
                continue
            acc += float(w) * xv
        if supported:
            y[t] = acc
    return y


def route_series(x: Sequence[float], kernel: dict, edge: str = "missing") -> List[float]:
    """按核对象路由一条序列。kernel = {'lag_min':int,'weights':[...]}。"""
    return convolution(
        x,
        kernel["weights"],
        int(kernel.get("lag_min", 0)),
        edge=edge,
    )


def convolution_detail(
    x: Sequence[float],
    weights: Sequence[float],
    lag_min: int = 0,
    active_eps: float = ACTIVE_EPS,
) -> dict:
    """三态卷积，区分“完整可识别 / 部分抽头 / 完全不可识别”。

    对每个时刻 t，只累加“在界内且有观测”的抽头：

    * ``exact[t]``：所有显著权重抽头都有效（known_fraction≈1）时的完整值，否则 NaN；
    * ``partial[t]``：只有部分权重抽头有效时的“已知抽头和”，否则 NaN。
      对非负流量，它是真实值的下界；若被卷积量可能为负（如增量残差），则只是部分和；
    * ``fraction[t]``：有效抽头权重占总 active 权重的比例 [0,1]。

    停测尾部因此不会被瞬间当成 0：最后实测 L、active 滞后区间 [a,b] 时，
    exact 通常延续到 L+a，partial 延续到 L+b，再往后 fraction=0 不可识别。
    """
    n = len(x)
    m = len(weights)
    active = [(lag_min + j, float(w)) for j, w in enumerate(weights) if abs(float(w)) > active_eps]
    total_w = sum(abs(w) for _, w in active)
    exact: List[float] = [NaN] * n
    partial: List[float] = [NaN] * n
    fraction: List[float] = [0.0] * n
    if not active or total_w <= 1e-15:
        return {"exact": exact, "partial": partial, "fraction": fraction}

    for t in range(n):
        acc = 0.0
        known_w = 0.0
        for lag, w in active:
            i = t - lag
            if i < 0 or i >= n:
                continue  # 史前/越界抽头未知
            xv = x[i]
            if is_nan(xv):
                continue
            acc += w * xv
            known_w += abs(w)
        frac = known_w / total_w
        fraction[t] = frac
        if frac >= 1.0 - 1e-8:
            exact[t] = acc
        elif known_w > active_eps:
            partial[t] = acc
    return {"exact": exact, "partial": partial, "fraction": fraction}


def compose_kernels(k1: dict, k2: dict) -> dict:
    """两段核串联：滞后区间相加，权重做多项式卷积。

    满足结合律，因此多段路径可以先合并成一个路径核，再一次性卷积。
    """
    l1 = int(k1["lag_min"])
    l2 = int(k2["lag_min"])
    w1 = [float(v) for v in k1["weights"]]
    w2 = [float(v) for v in k2["weights"]]
    if not w1 or not w2:
        raise ValueError("核权重为空")
    out = [0.0] * (len(w1) + len(w2) - 1)
    for i, a in enumerate(w1):
        if a == 0.0:
            continue
        for j, b in enumerate(w2):
            if b != 0.0:
                out[i + j] += a * b
    return {"lag_min": l1 + l2, "lag_max": l1 + l2 + len(out) - 1, "weights": out}


def align_kernel(k: dict, lag_min: int, lag_max: int) -> List[float]:
    """把核权重铺到 [lag_min, lag_max] 的公共网格上（区间外视为 0）。"""
    size = lag_max - lag_min + 1
    row = [0.0] * size
    k0 = int(k["lag_min"])
    for j, w in enumerate(k["weights"]):
        lag = k0 + j
        if lag_min <= lag <= lag_max:
            row[lag - lag_min] = float(w)
    return row


def merge_path_kernels(kernels: Sequence[dict]) -> Optional[dict]:
    """把同一源到同一汇的多条路径核在公共滞后网格上相加（按路径合并）。

    注意：这里是“各路径质量相加”。若河网存在真正的分流-再汇合，应当在边或
    路径上乘分流比例（share），而不是在本函数里重新归一化。
    """
    if not kernels:
        return None
    lo = min(int(k["lag_min"]) for k in kernels)
    hi = max(int(k.get("lag_max", int(k["lag_min"]) + len(k["weights"]) - 1)) for k in kernels)
    merged = [0.0] * (hi - lo + 1)
    for k in kernels:
        row = align_kernel(k, lo, hi)
        for i, v in enumerate(row):
            merged[i] += v
    return {"lag_min": lo, "lag_max": hi, "weights": merged}


def mask_ranges(mask: Sequence[bool], value: bool = True) -> List[Tuple[int, int]]:
    """返回 mask 中等于 value 的连续闭区间 [start, end]（含端点）。"""
    ranges: List[Tuple[int, int]] = []
    n = len(mask)
    i = 0
    while i < n:
        if bool(mask[i]) == value:
            j = i
            while j + 1 < n and bool(mask[j + 1]) == value:
                j += 1
            ranges.append((i, j))
            i = j + 1
        else:
            i += 1
    return ranges


def support_mask(x: Sequence[float], kernel: dict, active_eps: float = ACTIVE_EPS) -> List[bool]:
    """逐点给出路由结果是否可识别（所有显著抽头在界内且有观测）。"""
    n = len(x)
    weights = kernel["weights"]
    lag_min = int(kernel.get("lag_min", 0))
    out = [False] * n
    active_lags = [
        (lag_min + j, abs(float(w)) > active_eps)
        for j, w in enumerate(weights)
    ]
    active_lags = [(lag, True) for lag, active in active_lags if active]
    if not active_lags:
        return out
    min_lag = min(lag for lag, _ in active_lags)
    max_lag = max(lag for lag, _ in active_lags)
    for t in range(n):
        if t - max_lag < 0 or t - min_lag >= n:
            # 仍可能部分可算，交给下面逐抽头判断
            pass
        ok = True
        for lag, _ in active_lags:
            i = t - lag
            if i < 0 or i >= n or is_nan(x[i]):
                ok = False
                break
        out[t] = ok
    return out


def identifiability(x: Sequence[float], kernel: dict) -> dict:
    """汇总一条路由序列的可识别范围。

    返回
    ----
    {
      "supported": [ [i0,i1], ... ],      # 可识别连续区间（闭区间索引）
      "missing_runs": [[i0,i1], ...],      # 输入测站本身的缺测段
      "first_supported": int|None,
      "last_supported": int|None,
      "last_observed": int|None,
      "tail_extend_samples": int|None,     # 停测后核最大滞后仍可识别的延伸
    }
    """
    n = len(x)
    obs = observed_mask(x)
    missing_runs = mask_ranges(obs, False)
    last_obs = None
    for i in range(n - 1, -1, -1):
        if obs[i]:
            last_obs = i
            break
    sm = support_mask(x, kernel)
    supported = mask_ranges(sm, True)
    first_sup = supported[0][0] if supported else None
    last_sup = supported[-1][1] if supported else None
    l0 = int(kernel.get("lag_min", 0))
    active_lags_all = [l0 + j for j, w in enumerate(kernel["weights"]) if abs(float(w)) > ACTIVE_EPS]
    min_active_lag = min(active_lags_all) if active_lags_all else l0
    max_lag = max(active_lags_all) if active_lags_all else l0
    tail_extend = None
    last_full = last_sup
    last_possible = None
    partial_ranges: List[Tuple[int, int]] = []
    if last_obs is not None:
        # 严格全抽头：通常到 last_obs + min_active_lag。
        tail_extend = min(n - 1, last_obs + min_active_lag) - last_obs
        last_possible = min(n - 1, last_obs + max_lag)
        if last_full is not None and last_possible > last_full:
            a, b = last_full + 1, last_possible
            if a <= b:
                partial_ranges.append((a, b))
    return {
        "supported": [[a, b] for a, b in supported],
        "partial_ranges": [[a, b] for a, b in partial_ranges],
        "missing_runs": [[a, b] for a, b in missing_runs],
        "first_supported": first_sup,
        "last_supported": last_sup,
        "last_full": last_full,
        "last_possible": last_possible,
        "last_observed": last_obs,
        "min_active_lag": min_active_lag,
        "max_lag": max_lag,
        "tail_extend_samples": tail_extend,
    }


def peak_at(y: Sequence[float], i0: int = 0, i1: Optional[int] = None) -> Optional[dict]:
    """区间内有限最大值及其位置；全缺测返回 None。"""
    n = len(y)
    if i1 is None:
        i1 = n - 1
    i0 = max(0, i0)
    i1 = min(n - 1, i1)
    best_i = None
    best_v = -float("inf")
    for i in range(i0, i1 + 1):
        v = y[i]
        if not is_nan(v) and v > best_v:
            best_v = v
            best_i = i
    if best_i is None:
        return None
    return {"index": best_i, "value": best_v}


def sum_finite(y: Sequence[float]) -> float:
    return sum(v for v in y if not is_nan(v))


def mean_finite(y: Sequence[float]) -> float:
    vals = [v for v in y if not is_nan(v)]
    return sum(vals) / len(vals) if vals else NaN
