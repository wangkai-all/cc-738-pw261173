import math
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from rivernet.timeseries import (
    NaN,
    compose_kernels,
    convolution,
    convolution_detail,
    identifiability,
    merge_path_kernels,
)


def is_nan(v):
    return isinstance(v, float) and math.isnan(v)


class TestConvolutionBoundaries(unittest.TestCase):
    def test_impulse_lands_at_p_plus_lag(self):
        n = 20
        p = 7
        x = [0.0] * n
        x[p] = 5.0
        w = [0.2, 0.5, 0.3]
        lag_min = 2  # 绝对滞后 2,3,4
        y = convolution(x, w, lag_min, edge="zero")
        for j, wv in enumerate(w):
            self.assertAlmostEqual(y[p + lag_min + j], 5.0 * wv, places=10)
        # 其它位置为 0（edge=zero 且无缺测）
        for t, v in enumerate(y):
            if t not in {p + lag_min + j for j in range(3)}:
                self.assertAlmostEqual(v, 0.0, places=10)

    def test_matches_bruteforce_reference(self):
        n = 37
        x = [((i * 13) % 11) - 4.0 for i in range(n)]
        w = [0.11, 0.29, 0.31, 0.29]
        lag_min = 1
        y = convolution(x, w, lag_min, edge="zero")
        for t in range(n):
            ref = 0.0
            for j, wv in enumerate(w):
                ii = t - (lag_min + j)
                if 0 <= ii < n:
                    ref += wv * x[ii]
            self.assertAlmostEqual(y[t], ref, places=9)

    def test_left_boundary_requires_full_lag_window(self):
        # lag 1..2 且权重都显著：t<2 时最大滞后抽头越界 -> 不可识别(NaN)
        x = [1.0] * 10
        w = [0.5, 0.5]
        y = convolution(x, w, lag_min=1, edge="missing")
        self.assertTrue(is_nan(y[0]))
        self.assertTrue(is_nan(y[1]))
        self.assertFalse(is_nan(y[2]))
        self.assertAlmostEqual(y[2], 1.0)

    def test_zero_edge_differs_from_missing_at_boundary(self):
        x = [1.0] * 6
        w = [0.5, 0.5]
        yz = convolution(x, w, 1, edge="zero")
        ym = convolution(x, w, 1, edge="missing")
        # zero 模式 t=0 只拿到越界按 0 后的部分抽头 = 0
        self.assertAlmostEqual(yz[0], 0.0)
        self.assertTrue(is_nan(ym[0]))

    def test_internal_missing_propagates_nan_not_zero(self):
        x = [1.0, 1.0, NaN, 1.0, 1.0, 1.0]
        w = [0.5, 0.5]  # lag 0,1
        y = convolution(x, w, 0)
        # t=2 输入缺测；t=3 的 lag1 抽到 x2
        self.assertTrue(is_nan(y[2]))
        self.assertTrue(is_nan(y[3]))
        self.assertFalse(is_nan(y[4]))

    def test_tail_outage_three_state_identifiability(self):
        L = 10
        n = 18
        x = [2.0] * (L + 1) + [NaN] * (n - L - 1)
        # 滞后 2..6，权重递增，全部显著
        w = [0.05, 0.1, 0.2, 0.3, 0.35]
        detail = convolution_detail(x, w, lag_min=2)
        exact, partial, frac = detail["exact"], detail["partial"], detail["fraction"]

        # 完整可识别到 L + min_lag = 12
        self.assertFalse(is_nan(exact[L + 2]))
        self.assertTrue(is_nan(exact[L + 3]))
        # 部分抽头区间 L+3 .. L+6
        self.assertFalse(is_nan(partial[L + 3]))
        self.assertFalse(is_nan(partial[L + 6]))
        self.assertTrue(is_nan(partial[L + 7]))
        # L+3 时 lag2 抽头缺失，已知权重为 lag3..6 之和
        self.assertAlmostEqual(frac[L + 3], sum(w[1:]) / sum(w), places=9)
        # L+6 时只剩 lag6 抽头
        self.assertAlmostEqual(frac[L + 6], w[-1] / sum(w), places=9)
        # 超过 L+max_lag 完全不可识别，比例 0
        self.assertEqual(frac[L + 7], 0.0)
        self.assertTrue(is_nan(exact[L + 7]) and is_nan(partial[L + 7]))

        ident = identifiability(x, {"lag_min": 2, "weights": w})
        self.assertEqual(ident["last_observed"], L)
        self.assertEqual(ident["last_full"], L + 2)
        self.assertEqual(ident["last_possible"], L + 6)
        self.assertTrue(ident["partial_ranges"])

    def test_conservation_impulse_mass(self):
        n = 40
        p = 15
        q = 12.5
        x = [0.0] * n
        x[p] = q
        w = [0.18, 0.24, 0.21, 0.16, 0.12, 0.09]  # 和=1
        self.assertAlmostEqual(sum(w), 1.0, places=9)
        y = convolution(x, w, lag_min=1, edge="missing")
        mass = sum(v for v in y if not is_nan(v))
        self.assertAlmostEqual(mass, q, places=8)

    def test_composed_kernel_associative_and_conserving(self):
        k1 = {"lag_min": 1, "weights": [0.3, 0.7]}
        k2 = {"lag_min": 0, "weights": [0.25, 0.25, 0.5]}
        k3 = {"lag_min": 2, "weights": [0.6, 0.4]}
        a = compose_kernels(compose_kernels(k1, k2), k3)
        b = compose_kernels(k1, compose_kernels(k2, k3))
        self.assertEqual(a["lag_min"], b["lag_min"])
        self.assertEqual(len(a["weights"]), len(b["weights"]))
        for x, y in zip(a["weights"], b["weights"]):
            self.assertAlmostEqual(x, y, places=10)
        self.assertAlmostEqual(sum(a["weights"]), 1.0, places=9)
        self.assertEqual(a["lag_min"], 1 + 0 + 2)

    def test_merge_path_kernels_aligns_lags(self):
        k1 = {"lag_min": 1, "weights": [1.0]}          # 滞后1
        k2 = {"lag_min": 2, "weights": [0.5, 0.5]}      # 滞后2,3
        m = merge_path_kernels([k1, k2])
        self.assertEqual(m["lag_min"], 1)
        self.assertEqual(len(m["weights"]), 3)           # 滞后1,2,3
        self.assertAlmostEqual(m["weights"][0], 1.0)
        self.assertAlmostEqual(m["weights"][1], 0.5)
        self.assertAlmostEqual(m["weights"][2], 0.5)
        # 两条路径质量相加 = 2（未做跨路径归一化）
        self.assertAlmostEqual(sum(m["weights"]), 2.0)


if __name__ == "__main__":
    unittest.main()
