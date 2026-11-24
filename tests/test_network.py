import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from rivernet.network import (
    all_paths,
    dirty_edges_after_change,
    downstream_nodes,
    find_cycle,
    topological_order,
    validate_network,
)


def st(*ids):
    return [{"id": i} for i in ids]


def edges(*triples):
    return [{"id": eid, "from": u, "to": v} for eid, u, v in triples]


class TestDAG(unittest.TestCase):
    def test_topological_order_respects_edges(self):
        s = st("A", "B", "C", "D")
        e = edges(("e1", "A", "C"), ("e2", "B", "C"), ("e3", "C", "D"))
        order = topological_order(s, e)
        pos = {n: i for i, n in enumerate(order)}
        self.assertLess(pos["A"], pos["C"])
        self.assertLess(pos["B"], pos["C"])
        self.assertLess(pos["C"], pos["D"])

    def test_detect_simple_cycle(self):
        s = st("A", "B", "C")
        e = edges(("ab", "A", "B"), ("bc", "B", "C"), ("ca", "C", "A"))
        cyc = find_cycle(s, e)
        self.assertIsNotNone(cyc)
        self.assertEqual(cyc[0], cyc[-1])
        self.assertEqual(set(cyc[:-1]), {"A", "B", "C"})
        validation = validate_network(s, e)
        self.assertFalse(validation["ok"])
        self.assertTrue(any(x["code"] == "CYCLE_DETECTED" for x in validation["errors"]))

    def test_self_loop_flagged(self):
        s = st("A")
        e = edges(("x", "A", "A"))
        v = validate_network(s, e)
        codes = [x["code"] for x in v["errors"]]
        self.assertIn("SELF_LOOP", codes)

    def test_downstream_and_dirty_edges(self):
        # A->C, B->C, C->D, D->E
        s = st("A", "B", "C", "D", "E")
        e = edges(("ac", "A", "C"), ("bc", "B", "C"), ("cd", "C", "D"), ("de", "D", "E"))
        down = downstream_nodes(e, "C")
        self.assertEqual(down, {"D", "E"})
        dirty = dirty_edges_after_change(e, "ac")
        self.assertEqual(set(dirty), {"ac", "cd", "de"})
        # bc 不在 ac 的下游影响集合中
        self.assertNotIn("bc", dirty)

    def test_all_paths_diamond(self):
        # S -> A -> T, S -> B -> T （菱形汇合）
        s = st("S", "A", "B", "T")
        e = edges(("sa", "S", "A"), ("at", "A", "T"), ("sb", "S", "B"), ("bt", "B", "T"))
        paths = all_paths(e, "S", "T")
        self.assertEqual(len(paths), 2)
        node_paths = sorted([tuple([p[0]["from"]] + [x["to"] for x in p]) for p in paths])
        self.assertIn(("S", "A", "T"), node_paths)
        self.assertIn(("S", "B", "T"), node_paths)

    def test_unknown_endpoint_reported(self):
        v = validate_network(st("A"), edges(("e", "A", "GHOST")))
        codes = [x["code"] for x in v["errors"]]
        self.assertIn("UNKNOWN_TO", codes)


if __name__ == "__main__":
    unittest.main()
