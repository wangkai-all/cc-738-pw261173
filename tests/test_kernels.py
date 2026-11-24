import math
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from rivernet.kernels import (
    KernelError,
    build_design,
    default_kernel,
    fit_simplex_ls,
    hydrograph_kernel,
    project_simplex,
    validate_kernel,
)
from rivernet.timeseries import route_series


class TestKernelConstraints(unittest.TestCase):
    def test_default_kernel_nonneg_and_conserved(self):
        k = default_kernel(2, 9)
        self.assertEqual(k["lag_min"], 2)
        self.assertEqual(len(k["weights"]), 8)
        self.assertTrue(all(w >= 0 for w in k["weights"]))
        self.assertAlmostEqual(sum(k["weights"]), 1.0, places=9)
        validate_kernel(k)

    def test_reject_negative(self):
        with self.assertRaises(KernelError):
            validate_kernel({"lag_min": 0, "weights": [1.2, -0.2]})

    def test_reject_wrong_mass(self):
        with self.assertRaises(KernelError):
            validate_kernel({"lag_min": 0, "weights": [0.5, 0.3]})

    def test_accept_conserved(self):
        validate_kernel({"lag_min": 1, "weights": [0.25, 0.75]})

    def test_simplex_projection_sums_one(self):
        for v in ([0.2, 0.4, -0.1, 2.0], [1, 1, 1], [-3, -2], [0.01, 0.02]):
            p = project_simplex(v)
            self.assertAlmostEqual(sum(p), 1.0, places=9)
            self.assertTrue(all(x >= -1e-12 for x in p))


class TestCalibrationRecovery(unittest.TestCase):
    def _make_data(self, n=200, seed_events=((30, 45), (95, 55), (165, 38))):
        x = [2.0 + 0.2 * math.sin(i / 8.0) for i in range(n)]
        for center, amp in seed_events:
            for d in range(14):
                if center + d < n:
                    x[center + d] += amp * math.exp(-d / 3.2)
        return x

    def test_recovers_known_kernel(self):
        true_k = hydrograph_kernel(1, 6, peak_lag=4.0)
        x = self._make_data()
        y = route_series(x, true_k)
        A, b, used = build_design(x, y, 0, 8, 0, 140)
        res = fit_simplex_ls(A, b)
        fit_k = {"lag_min": 0, "weights": res["weights"]}
        # 约束始终成立
        self.assertAlmostEqual(sum(res["weights"]), 1.0, places=8)
        self.assertTrue(all(w >= -1e-9 for w in res["weights"]))
        # 在真实滞后 1..6 上逐权重接近
        for j, tw in enumerate(true_k["weights"]):
            self.assertLess(abs(res["weights"][j + 1] - tw), 0.02,
                            msg=f"lag {j+1}: {res['weights'][j+1]} vs {tw}")
        # 0 与 7,8 滞后应基本为 0
        self.assertLess(res["weights"][0], 0.02)
        self.assertLess(res["weights"][7] + res["weights"][8], 0.03)
        self.assertTrue(res["converged"])

    def test_fit_is_deterministic_and_slice_scoped(self):
        true_k = hydrograph_kernel(0, 4, peak_lag=2.0)
        x = self._make_data()
        y = route_series(x, true_k)
        A1, b1, _ = build_design(x, y, 0, 5, 10, 90)
        r1 = fit_simplex_ls(A1, b1)["weights"]
        # 改动校准窗口之外（评估期）的 y，不影响校准设计矩阵与结果
        y2 = list(y)
        for i in range(91, len(y2)):
            y2[i] = y2[i] + 999.0
        A2, b2, _ = build_design(x, y2, 0, 5, 10, 90)
        r2 = fit_simplex_ls(A2, b2)["weights"]
        self.assertEqual(r1, r2)

    def test_missing_rows_are_excluded(self):
        true_k = hydrograph_kernel(0, 3, peak_lag=1.5)
        x = self._make_data(n=120)
        y = route_series(x, true_k)
        # 把一段 x 打成缺测
        for i in range(40, 46):
            x[i] = float("nan")
        A, b, used = build_design(x, y, 0, 3, 0, 119)
        # 任何用到 40..46 输入的目标行都不应出现
        for t in used:
            for lag in range(0, 4):
                ii = t - lag
                if 0 <= ii < len(x):
                    self.assertFalse(math.isnan(x[ii]))

    def test_empty_fit_raises(self):
        with self.assertRaises(KernelError):
            fit_simplex_ls([], [])


if __name__ == "__main__":
    unittest.main()
