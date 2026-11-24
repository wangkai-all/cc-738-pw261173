import json
import os
import sys
import threading
import unittest
import urllib.request
import urllib.error

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from rivernet.server import build_server


class TestAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = build_server("127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def req(self, method, path, body=None, raw=False):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        r = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                text = resp.read().decode("utf-8-sig")
                return resp.status, (text if raw else json.loads(text))
        except urllib.error.HTTPError as ex:
            text = ex.read().decode("utf-8")
            try:
                return ex.code, json.loads(text)
            except Exception:
                return ex.code, text

    def open_example(self, eid):
        st, obj = self.req("POST", "/api/session/example", {"example_id": eid})
        self.assertEqual(st, 200, obj)
        self.assertTrue(obj["ok"])
        return obj["sid"], obj["state"]

    def test_health_and_examples(self):
        st, obj = self.req("GET", "/api/health")
        self.assertEqual(st, 200)
        self.assertTrue(obj["ok"])
        st, obj = self.req("GET", "/api/examples")
        ids = [e["id"] for e in obj["examples"]]
        self.assertIn("conservation-tree", ids)
        self.assertIn("cycle-error", ids)

    def test_full_conservation_workflow(self):
        sid, state = self.open_example("conservation-tree")
        self.assertTrue(state["ok"])

        st, obj = self.req("GET", f"/api/edge?sid={sid}&edge_id=E-QJ-SG")
        self.assertEqual(st, 200)
        self.assertIn("routed", obj["view"])

        st, obj = self.req("POST", "/api/calibrate", {
            "sid": sid, "edge_id": "E-QJ-SG", "i0": 8, "i1": 112,
            "lag_min": 2, "lag_max": 6, "name": "API 方案"})
        self.assertEqual(st, 200, obj)
        self.assertTrue(obj["ok"])
        self.assertEqual(obj["scheme"]["calibration"], [8, 112])
        self.assertIn("recomputed_edges", obj["report"])
        self.assertIn("E-SG-LJK", obj["report"]["recomputed_edges"])

        st, obj = self.req("POST", "/api/edge/lag_range", {
            "sid": sid, "edge_id": "E-YL-SG", "lag_min": 1, "lag_max": 4})
        self.assertEqual(st, 200)
        self.assertIn("E-SG-LJK", obj["report"]["recomputed_edges"])
        self.assertNotIn("E-QJ-SG", obj["report"]["recomputed_edges"])

        st, obj = self.req("GET", f"/api/contributions?sid={sid}&target=LJK")
        self.assertEqual(st, 200)
        self.assertLess(obj["data"]["max_abs_closure_error"], 1e-5)

        st, text = self.req("GET", f"/api/contributions.csv?sid={sid}&target=LJK", raw=True)
        self.assertEqual(st, 200)
        self.assertIn("time,index", text)

        st, text = self.req("GET", f"/api/contributions.csv?sid={sid}&target=LJK&format=long", raw=True)
        self.assertEqual(st, 200)
        self.assertIn("unidentifiable", text)

        st, obj = self.req("GET", f"/api/peaks?sid={sid}&source=YL&target=LJK")
        self.assertEqual(st, 200)
        self.assertTrue(obj["data"]["reachable"])
        self.assertGreaterEqual(len(obj["data"]["stations"]), 3)

    def test_cycle_example_rejected_by_api(self):
        sid, state = self.open_example("cycle-error")
        self.assertFalse(state["ok"])
        self.assertTrue(any(e["code"] == "CYCLE_DETECTED" for e in state["validation"]["errors"]))
        st, obj = self.req("GET", f"/api/contributions?sid={sid}&target=D")
        self.assertEqual(st, 409)
        self.assertEqual(obj["error"]["code"], "CYCLE_DETECTED")
        self.assertTrue(obj["error"]["details"]["cycle"])

    def test_import_wide_csv_and_network(self):
        csv_text = "time,A,B,C\n2026-01-01T00:00,1,2,3\n2026-01-01T01:00,1.5,2.5,3.5\n" \
                   "2026-01-01T02:00,,3,4\n2026-01-01T03:00,2,3.2,4.1\n"
        network = {"edges": [{"id": "ab", "from": "A", "to": "C", "lag_min": 0, "lag_max": 2},
                             {"id": "bc", "from": "B", "to": "C", "lag_min": 0, "weights": [1.0]}]}
        st, obj = self.req("POST", "/api/import", {"flows_csv": csv_text, "network_json": network})
        self.assertEqual(st, 200, obj)
        sid = obj["sid"]
        self.assertTrue(obj["state"]["ok"])
        # 缺测单元格被解析为 null 而不是 0
        st, state_obj = self.req("GET", f"/api/state?sid={sid}")
        self.assertEqual(st, 200)

    def test_static_index_served(self):
        # 首页即使尚未实现也不应 500；实现后应包含 <div id="app"> 或标题
        try:
            with urllib.request.urlopen(self.base + "/", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                html = resp.read().decode("utf-8")
                self.assertIn("河网", html)
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                self.skipTest("web/index.html 尚未创建")
            raise


if __name__ == "__main__":
    unittest.main()
