import csv
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from rivernet.engine import Project
from rivernet.examples import (
    build_clean_chain_bundle,
    build_conservation_bundle,
    build_cycle_bundle,
)


class TestClosureAndConfluence(unittest.TestCase):
    def test_clean_multilevel_closure_to_machine_precision(self):
        p = Project(build_clean_chain_bundle())
        c = p.contributions("T")
        self.assertIsNotNone(c["max_abs_closure_error"])
        self.assertLess(c["max_abs_closure_error"], 1e-8)
        # U1 -> M -> T 与 U2 -> M -> T 都是两级路径
        u1 = next(s for s in c["sources"] if s["station_id"] == "U1")
        self.assertEqual(u1["paths"][0]["nodes"], ["U1", "M", "T"])

    def test_conservation_example_closes(self):
        p = Project(build_conservation_bundle(gaps=False))
        c = p.contributions("LJK")
        self.assertLess(c["max_abs_closure_error"], 1e-6)

    def test_diamond_paths_merge_with_shares_and_conserve_mass(self):
        # S 经 0.6/0.4 两条分流在 T 汇合
        bundle = {
            "id": "diamond",
            "grid": {"start": "2026-01-01T00:00", "dt_minutes": 60, "n": 40},
            "stations": [{"id": "S"}, {"id": "A"}, {"id": "B"}, {"id": "T"}],
            "series": {
                "S": [0, 0, 10] + [0] * 37,
                "A": [0] * 40, "B": [0] * 40, "T": [0] * 40,
            },
            "edges": [
                {"id": "sa", "from": "S", "to": "A", "share": 0.6, "lag_min": 1, "weights": [1.0]},
                {"id": "at", "from": "A", "to": "T", "share": 1.0, "lag_min": 1, "weights": [1.0]},
                {"id": "sb", "from": "S", "to": "B", "share": 0.4, "lag_min": 2, "weights": [1.0]},
                {"id": "bt", "from": "B", "to": "T", "share": 1.0, "lag_min": 2, "weights": [1.0]},
            ],
        }
        p = Project(bundle)
        c = p.contributions("T")
        s = next(x for x in c["sources"] if x["station_id"] == "S")
        self.assertEqual(s["n_paths"], 2)
        # 路径核已乘分流比：总权重 = 0.6 + 0.4 = 1（质量守恒，不翻倍）
        total_kernel_mass = sum(
            w for path in s["paths"] for w in [path["share_product"]]
        )
        self.assertAlmostEqual(total_kernel_mass, 1.0, places=9)
        # 脉冲在 T 处两条路径分别贡献 6 与 4，合计 10
        vals = [v for v in s["series"] if v is not None]
        self.assertAlmostEqual(sum(vals), 10.0, places=6)


class TestIncrementalRecompute(unittest.TestCase):
    def test_only_downstream_edges_recompute(self):
        p = Project(build_clean_chain_bundle())
        before = dict(p.route_counts)
        rep = p.update_kernel("e2", p.edge_by_id["e2"]["weights"])  # 同核也触发
        self.assertEqual(set(rep["recomputed_edges"]), {"e2", "e3"})
        self.assertIn("e1", rep["cached_edges"])
        self.assertEqual(p.route_counts["e1"] - before["e1"], 0)
        self.assertEqual(p.route_counts["e2"] - before["e2"], 1)
        self.assertEqual(p.route_counts["e3"] - before["e3"], 1)

    def test_lag_range_edit_recomputes_downstream(self):
        p = Project(build_conservation_bundle())
        rep = p.set_lag_range("E-YL-SG", 1, 4)
        self.assertIn("E-YL-SG", rep["recomputed_edges"])
        self.assertIn("E-SG-LJK", rep["recomputed_edges"])
        self.assertIn("LJK", rep["recomputed_nodes"])
        self.assertNotIn("E-QJ-SG", rep["recomputed_edges"])


class TestMissingDataIdentifiability(unittest.TestCase):
    def test_stopped_station_not_zero_filled(self):
        p = Project(build_conservation_bundle(gaps=True))
        view = p.edge_view("E-QJ-SG")
        ident = view["identifiability"]
        self.assertEqual(ident["last_observed"], 215)
        self.assertIsNotNone(ident["last_full"])
        self.assertGreaterEqual(ident["last_possible"], ident["last_full"])
        # 远尾部的严格路由值是 None（JSON null），不是 0
        self.assertIsNone(view["routed"][239])

        c = p.contributions("LJK")
        qj = next(s for s in c["sources"] if s["station_id"] == "QJ")
        self.assertIsNone(qj["series"][239])
        # 存在部分抽头（残波）区间
        self.assertTrue(any(v is not None for v in qj["partial_series"]))

    def test_csv_blank_and_status_for_unidentifiable(self):
        p = Project(build_conservation_bundle(gaps=True))
        wide = p.contributions_csv("LJK")
        rows = list(csv.reader(io.StringIO(wide)))
        header = rows[0]
        qj_col = next(i for i, h in enumerate(header) if h.startswith("QJ_"))
        self.assertEqual(rows[240][qj_col], "")  # index 239 -> 数据行 240（含表头）

        long_csv = p.contributions_long_csv("LJK")
        lrows = list(csv.DictReader(io.StringIO(long_csv)))
        qj_tail = [r for r in lrows if r["station_id"] == "QJ" and int(r["index"]) > 227]
        self.assertTrue(qj_tail)
        self.assertTrue(any(r["status"] == "unidentifiable" and r["contribution_exact"] == ""
                            for r in qj_tail))

    def test_completely_missing_source_volume_is_null_not_type_error(self):
        bundle = build_conservation_bundle(gaps=False)
        bundle["series"]["QJ"] = [None] * bundle["grid"]["n"]
        p = Project(bundle)

        c = p.contributions("LJK")
        qj = next(s for s in c["sources"] if s["station_id"] == "QJ")
        self.assertIsNone(qj["mean"])
        self.assertIsNone(qj["volume"])
        self.assertTrue(all(v is None for v in qj["series"]))

        # CSV 导出也必须把全缺测写成空白，而不是抛出 500。
        wide = p.contributions_csv("LJK")
        self.assertIn("QJ_", wide.splitlines()[0])
        long_csv = p.contributions_long_csv("LJK")
        self.assertIn("QJ", long_csv)


class TestPeakMovementBoundaries(unittest.TestCase):
    def test_source_equal_target_returns_single_station_chain(self):
        p = Project(build_conservation_bundle())
        out = p.peak_movement("YL", "YL", 20, 120)
        self.assertTrue(out["reachable"])
        self.assertTrue(out["same_station"])
        self.assertEqual(out["chain"], ["YL"])
        self.assertEqual(out["n_paths"], 1)
        self.assertEqual(out["path_kernel"]["weights"], [1.0])
        self.assertEqual(out["predicted_arrival"]["lag_samples"], 0)
        self.assertEqual(len(out["stations"]), 1)


class TestSchemesAndCalibration(unittest.TestCase):
    def test_engine_recovers_edge_kernel(self):
        p = Project(build_clean_chain_bundle())
        true_w = p.edge_by_id["e1"]["weights"]
        p.calibrate("e1", 0, 70, lag_min=0, lag_max=5, name="k1")
        got = p.get_scheme("S001")["kernel"]["weights"]
        # 拟合网格 0..5，真实核在 0..4
        for j, tw in enumerate(true_w):
            self.assertLess(abs(got[j] - tw), 0.03)

    def test_eval_period_never_changes_fit(self):
        p = Project(build_clean_chain_bundle())
        p.calibrate("e1", 5, 60, lag_min=0, lag_max=5)
        w1 = list(p.get_scheme("S001")["kernel"]["weights"])
        # 大幅污染评估期（>60）数据
        for i in range(61, p.n):
            p.x["T"][i] += 999.0
            p.x["M"][i] += 999.0
        p.calibrate("e1", 5, 60, lag_min=0, lag_max=5)
        w2 = p.get_scheme("S002")["kernel"]["weights"]
        self.assertEqual(w1, w2)
        # 旧方案保持不变
        self.assertEqual(p.get_scheme("S001")["kernel"]["weights"], w1)

    def test_scheme_bound_to_its_own_slice(self):
        p = Project(build_conservation_bundle())
        p.calibrate("E-QJ-SG", 8, 112, lag_min=2, lag_max=6, name="A")
        original = list(p.get_scheme("S001")["kernel"]["weights"])
        # 之后改滞后范围、再建立新方案，都不应改动 S001
        p.set_lag_range("E-QJ-SG", 0, 9)
        p.calibrate("E-QJ-SG", 40, 180, lag_min=1, lag_max=8, name="B")
        self.assertEqual(p.get_scheme("S001")["kernel"]["weights"], original)
        self.assertEqual(p.get_scheme("S001")["calibration"], [8, 112])
        # 用 S001 自带快照可独立重放出同一核
        refit = p.refit_snapshot("S001")
        err = max(abs(a - b) for a, b in zip(refit["weights"], refit["original_weights"]))
        self.assertLess(err, 1e-6)

    def test_snapshot_refit_honours_excluded_valid_rows_and_lag_padding(self):
        p = Project(build_conservation_bundle())
        p.calibrate(
            "E-QJ-SG", 8, 112, lag_min=2, lag_max=6,
            exclude_intervals=[[20, 40]], name="exclude",
        )
        scheme = p.get_scheme("S001")
        snap = scheme["snapshot"]
        # 快照包含校准起点之前、最大滞后所需的样本。
        self.assertEqual(snap["pad_start"], 8 - 6)
        self.assertEqual(len(snap["x_padded"]), 112 - snap["pad_start"] + 1)
        self.assertEqual(snap["exclude_intervals"], [[20, 40]])
        refit = p.refit_snapshot("S001")
        err = max(abs(a - b) for a, b in zip(refit["weights"], refit["original_weights"]))
        self.assertLess(err, 1e-6)


class TestCycleRejected(unittest.TestCase):
    def test_cycle_project_not_ok_and_compute_raises(self):
        from rivernet.engine import ProjectError
        p = Project(build_cycle_bundle())
        self.assertFalse(p.ok)
        with self.assertRaises(ProjectError) as ctx:
            p.contributions("D")
        self.assertEqual(ctx.exception.code, "CYCLE_DETECTED")
        self.assertTrue(ctx.exception.details["cycle"])


if __name__ == "__main__":
    unittest.main()
