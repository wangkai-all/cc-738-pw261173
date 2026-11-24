"""离散传播核：构造、校验与约束校准。

核约束
------
* 每个权重非负；
* 总权重守恒：``sum(weights) == 1``（容差内）；
* 权重只在用户给定的滞后范围 ``[lag_min, lag_max]``（采样格）内非零。

校准是一个单纯形约束最小二乘：

    min_w || A w - z ||^2
    s.t.  w_j >= 0,  sum_j w_j = 1

使用投影梯度 + Armijo 线搜索（纯标准库，无需 numpy/scipy）。
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from .timeseries import is_nan

SUM_TOL = 1e-6
NEG_TOL = -1e-8


class KernelError(ValueError):
    """核不满足非负 / 守恒 / 滞后范围约束。"""


def make_kernel(lag_min: int, weights: Sequence[float]) -> dict:
    lag_min = int(lag_min)
    w = [float(v) for v in weights]
    return {"lag_min": lag_min, "lag_max": lag_min + len(w) - 1, "weights": w}


def validate_kernel(k: dict, sum_tol: float = 1e-5) -> None:
    w = k.get("weights")
    if not isinstance(w, list) or len(w) == 0:
        raise KernelError("传播核权重为空")
    lag_min = int(k.get("lag_min", 0))
    lag_max = int(k.get("lag_max", lag_min + len(w) - 1))
    if lag_max - lag_min + 1 != len(w):
        raise KernelError("lag_max-lag_min+1 与权重长度不一致")
    if lag_min < 0:
        raise KernelError("最小滞后不能为负（不允许预见未来）")
    s = 0.0
    for i, v in enumerate(w):
        if not isinstance(v, (int, float)) or is_nan(float(v)):
            raise KernelError(f"第 {i} 个权重不是有效数值")
        if v < NEG_TOL:
            raise KernelError(f"第 {i} 个权重为负（{v:.6g}），核必须非负")
        s += float(v)
    if not (1.0 - sum_tol <= s <= 1.0 + sum_tol):
        raise KernelError(f"传播核总权重应守恒为 1，实际为 {s:.6f}")


def normalized(weights: Sequence[float]) -> List[float]:
    """截断微小负值并归一化到和为 1。"""
    w = [max(0.0, float(v)) for v in weights]
    s = sum(w)
    if s <= 1e-15:
        n = len(w)
        return [1.0 / n] * n
    return [v / s for v in w]


def default_kernel(lag_min: int, lag_max: int, peak: Optional[int] = None) -> dict:
    """滞后范围上的缺省单峰（三角形）守恒核。"""
    lag_min = int(lag_min)
    lag_max = int(lag_max)
    if lag_max < lag_min:
        raise KernelError("lag_max 必须 >= lag_min")
    if peak is None:
        peak = lag_min + (lag_max - lag_min) // 2
    peak = int(min(max(peak, lag_min), lag_max))
    lags = list(range(lag_min, lag_max + 1))
    raw = []
    for lag in lags:
        if lag <= peak:
            v = (lag - lag_min + 1) / max(1, peak - lag_min + 1)
        else:
            v = max(0.0, (lag_max - lag + 1) / max(1, lag_max - peak))
        raw.append(max(0.05, v))  # 保留小基底，避免过早把抽头判成零
    return make_kernel(lag_min, normalized(raw))


def hydrograph_kernel(lag_min: int, lag_max: int, peak_lag: float, shape: float = 2.2) -> dict:
    """合成用的偏态单位过程线核（非负、归一化），峰值在 peak_lag 附近。"""
    lag_min = int(lag_min)
    lag_max = int(lag_max)
    raw = []
    for lag in range(lag_min, lag_max + 1):
        d = max(0, lag - lag_min)
        pk = max(0.1, peak_lag - lag_min)
        # 伽马型：d^(shape-1) exp(-d/scale)，scale 让众数接近 peak
        scale = max(0.5, pk / (shape - 1.0))
        v = ((d + 1) ** (shape - 1.0)) * math.exp(-(d + 1) / scale)
        raw.append(v)
    return make_kernel(lag_min, normalized(raw))


# --------------------------------------------------------------------------- #
# 单纯形投影与约束最小二乘
# --------------------------------------------------------------------------- #

def project_simplex(v: Sequence[float], z: float = 1.0) -> List[float]:
    """投影到 {w>=0, sum w = z}（Duchi 排序算法）。"""
    if len(v) == 0:
        return []
    u = sorted((float(x) for x in v), reverse=True)
    css = 0.0
    rho = 0
    theta = 0.0
    n = len(u)
    cumsum = 0.0
    for j, uj in enumerate(u):
        cumsum += uj
        candidate = uj + (z - cumsum) / (j + 1)
        if candidate > 0:
            rho = j + 1
            css = cumsum
    theta = (css - z) / rho
    return [max(float(x) - theta, 0.0) for x in v]


def _mat_vec(G: Sequence[Sequence[float]], w: Sequence[float]) -> List[float]:
    k = len(w)
    return [sum(G[i][j] * w[j] for j in range(k)) for i in range(k)]


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _solve_linear(A: List[List[float]], b: List[float]) -> List[float]:
    """带部分选主元的高斯消元（小规模稠密 KKT 系统）。"""
    n = len(b)
    M = [list(A[i]) + [float(b[i])] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-14:
            raise KernelError("校准正规方程奇异（输入可能缺乏足够的洪峰激励）")
        if piv != col:
            M[col], M[piv] = M[piv], M[col]
        pv = M[col][col]
        for r in range(col + 1, n):
            factor = M[r][col] / pv
            if factor == 0.0:
                continue
            M[r][col] = 0.0
            for c in range(col + 1, n + 1):
                M[r][c] -= factor * M[col][c]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        s = M[i][n] - sum(M[i][j] * x[j] for j in range(i + 1, n))
        x[i] = s / M[i][i]
    return x


def _active_set_refine(G, h, w0, add_tol: float = 1e-7):
    """在投影梯度给出的支撑集上解等式约束 QP，并做增删主元精修。"""
    k = len(w0)
    active = [i for i, v in enumerate(w0) if v > 1e-6]
    if not active:
        active = [max(range(k), key=lambda i: w0[i])]

    lam = 0.0
    for _outer in range(200):
        r = len(active)
        dim = r + 1
        K = [[0.0] * dim for _ in range(dim)]
        rhs = [0.0] * dim
        for a, i in enumerate(active):
            rhs[a] = h[i]
            for b, j in enumerate(active):
                K[a][b] = G[i][j]
            K[a][r] = 1.0
            K[r][a] = 1.0
        rhs[r] = 1.0
        try:
            sol = _solve_linear(K, rhs)
        except KernelError:
            break
        ww = [0.0] * k
        for a, i in enumerate(active):
            ww[i] = sol[a]
        lam = sol[r]

        neg = [i for i in active if ww[i] < -1e-9]
        if neg:
            worst = min(neg, key=lambda i: ww[i])
            active.remove(worst)
            continue

        # 非负截断微小负值
        for i in active:
            if ww[i] < 0.0:
                ww[i] = 0.0

        grad = [sum(G[i][j] * ww[j] for j in range(k)) - h[i] for i in range(k)]
        inactive = [i for i in range(k) if i not in set(active)]
        viol = [i for i in inactive if grad[i] + lam < -add_tol * (1.0 + abs(lam))]
        if viol:
            worst = min(viol, key=lambda i: grad[i] + lam)
            active.append(worst)
            continue

        resid = math.sqrt(sum((grad[i] + lam) ** 2 for i in active))
        return ww, True, resid

    w = project_simplex(w0)
    return w, False, float("inf")


def fit_simplex_ls(
    A: Sequence[Sequence[float]],
    b: Sequence[float],
    max_iter: int = 1200,
    tol: float = 1e-7,
) -> dict:
    """求解 min ||Aw-b||^2 s.t. w>=0, sum w=1。

    返回 ``{"weights", "iterations", "converged", "projected_grad"}``。
    """
    m = len(A)
    k = len(A[0]) if m else 0
    if k == 0:
        raise KernelError("没有可拟合的滞后列")
    if m == 0:
        raise KernelError("校准期内没有同时满足观测与滞后要求的样本")

    # Gram 矩阵 G=A'A, h=A'b，把迭代成本降到 O(k^2)。
    G = [[0.0] * k for _ in range(k)]
    h = [0.0] * k
    for row, bv in zip(A, b):
        for i in range(k):
            ai = row[i]
            if ai == 0.0:
                continue
            h[i] += ai * bv
            for j in range(i, k):
                G[i][j] += ai * row[j]
    for i in range(k):
        for j in range(i):
            G[i][j] = G[j][i]

    # 最大特征值的幂迭代，用来估 Lipschitz 常数。
    L = _estimate_largest_eig(G)
    L = max(L, 1e-8)

    w = project_simplex([1.0 / k] * k)
    grad = [0.0] * k
    alpha = 1.0 / L
    prev_w: Optional[List[float]] = None
    prev_grad: Optional[List[float]] = None
    bb_alpha = alpha

    def obj(ww):
        gw = _mat_vec(G, ww)
        return 0.5 * _dot(ww, gw) - _dot(h, ww)

    converged = False
    pg_norm = float("inf")
    it = 0
    for it in range(1, max_iter + 1):
        Gw = _mat_vec(G, w)
        grad = [Gw[i] - h[i] for i in range(k)]

        # BB 步长加速（带保护），否则用 1/L。
        if prev_w is not None and prev_grad is not None:
            s = [w[i] - prev_w[i] for i in range(k)]
            y = [grad[i] - prev_grad[i] for i in range(k)]
            sy = _dot(s, y)
            ss = _dot(s, s)
            if sy > 1e-16 and ss > 0:
                bb = ss / sy
                bb_alpha = min(max(bb, 1e-12), 1.0 / L * 20.0)
            else:
                bb_alpha = 1.0 / L
        step0 = bb_alpha

        f0 = obj(w)
        accepted = False
        step = step0
        w_new = w
        for _bt in range(40):
            cand = project_simplex([w[i] - step * grad[i] for i in range(k)])
            d = [cand[i] - w[i] for i in range(k)]
            # Armijo 条件
            if obj(cand) <= f0 + 1e-4 * _dot(grad, d) + 1e-18:
                w_new = cand
                accepted = True
                break
            step *= 0.5
        if not accepted:
            step = 1.0 / L
            w_new = project_simplex([w[i] - step * grad[i] for i in range(k)])

        # 投影梯度映射的范数作为收敛判据
        move = [w_new[i] - w[i] for i in range(k)]
        pg_norm = math.sqrt(_dot(move, move)) / max(step, 1e-12)

        prev_w = w
        prev_grad = grad
        w = w_new
        if pg_norm < tol:
            converged = True
            break

    # 主动集精修：在 PG 识别出的支撑上求精确的等式约束解。
    refined, as_ok, kkt_resid = _active_set_refine(G, h, w)
    w = project_simplex(refined)
    w = [0.0 if abs(x) < 1e-10 else x for x in w]
    w = normalized(w)  # 消除数值漂移，严格守恒
    return {
        "weights": w,
        "iterations": it,
        "converged": bool(converged or as_ok),
        "projected_grad": pg_norm,
        "kkt_residual": kkt_resid if isinstance(kkt_resid, float) and math.isfinite(kkt_resid) else None,
        "n_samples": m,
    }


def _estimate_largest_eig(G: Sequence[Sequence[float]], iters: int = 40) -> float:
    k = len(G)
    v = [1.0 / math.sqrt(k)] * k
    eig = 0.0
    for _ in range(iters):
        Gv = _mat_vec(G, v)
        nrm = math.sqrt(_dot(Gv, Gv))
        if nrm < 1e-15:
            return 0.0
        v = [x / nrm for x in Gv]
        eig = _dot(v, _mat_vec(G, v))
    return max(0.0, eig)


def build_design(
    x: Sequence[float],
    target: Sequence[float],
    lag_min: int,
    lag_max: int,
    i0: int,
    i1: int,
    extra_valid: Optional[Sequence[bool]] = None,
) -> Tuple[List[List[float]], List[float], List[int]]:
    """构造校准设计矩阵。

    只保留同时满足以下条件的目标时刻 t：
      * i0 <= t <= i1（严格限定在校准切片内）；
      * target[t] 有效（通常是扣除其他支流后的对照量 z）；
      * 每个候选滞后 l∈[lag_min,lag_max] 对应的 x[t-l] 在界内且非缺测；
      * extra_valid[t] 不为 False（用户排除区间等）。

    返回 A、b、以及每行对应的目标时刻索引。
    """
    if lag_max < lag_min:
        raise KernelError("滞后上限小于下限")
    if lag_min < 0:
        raise KernelError("滞后不能为负")
    n = len(x)
    cols = lag_max - lag_min + 1
    A: List[List[float]] = []
    b: List[float] = []
    used_t: List[int] = []
    lo = max(0, i0)
    hi = min(n - 1, i1)
    for t in range(lo, hi + 1):
        z = target[t] if t < len(target) else float("nan")
        if is_nan(z):
            continue
        if extra_valid is not None and not extra_valid[t]:
            continue
        row = [0.0] * cols
        ok = True
        for j, lag in enumerate(range(lag_min, lag_max + 1)):
            ii = t - lag
            if ii < 0 or ii >= n or is_nan(x[ii]):
                ok = False
                break
            row[j] = x[ii]
        if not ok:
            continue
        A.append(row)
        b.append(float(z))
        used_t.append(t)
    return A, b, used_t


def regression_metrics(y_true: Sequence[float], y_pred: Sequence[float], indices: Optional[Sequence[int]] = None) -> dict:
    """在给定索引（或全部成对有限点）上计算 RMSE/MAE/偏差/R²/样本数。"""
    pairs: List[Tuple[float, float]] = []
    if indices is not None:
        for i in indices:
            if 0 <= i < len(y_true) and i < len(y_pred):
                a, p = y_true[i], y_pred[i]
                if not is_nan(a) and not is_nan(p):
                    pairs.append((a, p))
    else:
        for a, p in zip(y_true, y_pred):
            if not is_nan(a) and not is_nan(p):
                pairs.append((a, p))
    n = len(pairs)
    if n == 0:
        return {"n": 0, "rmse": None, "mae": None, "bias": None, "r2": None}
    err = [p - a for a, p in pairs]
    sse = sum(e * e for e in err)
    rmse = math.sqrt(sse / n)
    mae = sum(abs(e) for e in err) / n
    bias = sum(err) / n
    mean_a = sum(a for a, _ in pairs) / n
    sst = sum((a - mean_a) ** 2 for a, _ in pairs)
    r2 = 1.0 - sse / sst if sst > 1e-15 else None
    return {"n": n, "rmse": rmse, "mae": mae, "bias": bias, "r2": r2}
