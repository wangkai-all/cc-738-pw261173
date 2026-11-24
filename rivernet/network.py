"""有向无环河网（DAG）工具：拓扑、环路诊断、下游依赖与路径枚举。"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Dict, List, Optional, Sequence, Set


class NetworkError(ValueError):
    def __init__(self, code: str, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def node_ids(stations: Sequence[dict]) -> List[str]:
    return [str(s["id"]) for s in stations]


def adjacency(edges: Sequence[dict]):
    out: Dict[str, List[dict]] = defaultdict(list)
    inn: Dict[str, List[dict]] = defaultdict(list)
    for e in edges:
        out[str(e["from"])].append(e)
        inn[str(e["to"])].append(e)
    return out, inn


def find_cycle(stations: Sequence[dict], edges: Sequence[dict]) -> Optional[List[str]]:
    """若存在有向环，返回环上的节点序列（首尾相同）；否则 None。"""
    ids = set(node_ids(stations))
    # 边可能引用了未声明节点，也一并纳入，便于给出环。
    for e in edges:
        ids.add(str(e["from"]))
        ids.add(str(e["to"]))
    out, _ = adjacency(edges)

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in ids}
    stack: List[str] = []

    def dfs(u: str):
        color[u] = GRAY
        stack.append(u)
        for e in out.get(u, []):
            v = str(e["to"])
            if color.get(v, WHITE) == GRAY:
                idx = stack.index(v)
                return stack[idx:] + [v]
            if color.get(v, WHITE) == WHITE:
                found = dfs(v)
                if found:
                    return found
        stack.pop()
        color[u] = BLACK
        return None

    for n in list(ids):
        if color[n] == WHITE:
            found = dfs(n)
            if found:
                return found
    return None


def validate_network(stations: Sequence[dict], edges: Sequence[dict]) -> dict:
    """结构校验。返回 {ok, errors:[{code,message,...}]}，不抛异常，便于接口直接序列化。"""
    errors: List[dict] = []
    ids = node_ids(stations)
    idset = set(ids)

    if len(ids) != len(idset):
        errors.append({"code": "DUPLICATE_STATION", "message": "测站 id 存在重复"})

    edge_ids: Set[str] = set()
    for e in edges:
        eid = str(e.get("id", f"{e.get('from')}->{e.get('to')}"))
        if eid in edge_ids:
            errors.append({"code": "DUPLICATE_EDGE", "message": f"河段 id 重复：{eid}"})
        edge_ids.add(eid)
        u, v = str(e.get("from")), str(e.get("to"))
        if u == v:
            errors.append({"code": "SELF_LOOP", "message": f"河段 {eid} 是自环：{u}→{v}", "edge_id": eid, "cycle": [u, v]})
        if u not in idset:
            errors.append({"code": "UNKNOWN_FROM", "message": f"河段 {eid} 起点 {u} 不在测站表中", "edge_id": eid})
        if v not in idset:
            errors.append({"code": "UNKNOWN_TO", "message": f"河段 {eid} 终点 {v} 不在测站表中", "edge_id": eid})

    cycle = find_cycle(stations, edges)
    if cycle:
        errors.append({
            "code": "CYCLE_DETECTED",
            "message": "河网必须是有向无环图（DAG），检测到环路：" + " → ".join(cycle),
            "cycle": cycle,
        })

    return {"ok": not errors, "errors": errors}


def topological_order(stations: Sequence[dict], edges: Sequence[dict]) -> List[str]:
    """Kahn 拓扑序；有环时抛 NetworkError(CYCLE_DETECTED)。"""
    validation = validate_network(stations, edges)
    cycle_err = next((x for x in validation["errors"] if x["code"] == "CYCLE_DETECTED"), None)
    if cycle_err:
        raise NetworkError("CYCLE_DETECTED", cycle_err["message"], {"cycle": cycle_err["cycle"]})
    structural = [x for x in validation["errors"] if x["code"] != "CYCLE_DETECTED"]
    if structural:
        raise NetworkError(structural[0]["code"], structural[0]["message"], structural[0])

    ids = node_ids(stations)
    _, inn = adjacency(edges)
    out, _ = adjacency(edges)
    indeg = {n: len(inn.get(n, [])) for n in ids}
    q = deque([n for n in ids if indeg[n] == 0])
    order: List[str] = []
    while q:
        u = q.popleft()
        order.append(u)
        for e in out.get(u, []):
            v = str(e["to"])
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)
    if len(order) != len(ids):  # 理论上 validate 已拦住
        cyc = find_cycle(stations, edges)
        raise NetworkError("CYCLE_DETECTED", "河网存在环路", {"cycle": cyc})
    return order


def downstream_nodes(edges: Sequence[dict], start: str, include_start: bool = False) -> Set[str]:
    """从 start 沿河段方向可达的节点。"""
    out, _ = adjacency(edges)
    seen: Set[str] = set()
    q = deque([start])
    while q:
        u = q.popleft()
        for e in out.get(u, []):
            v = str(e["to"])
            if v not in seen:
                seen.add(v)
                q.append(v)
    if include_start:
        seen.add(start)
    return seen


def dirty_edges_after_change(edges: Sequence[dict], changed_edge_id: str) -> List[str]:
    """修改某条河段核后需要重算的边：该边自身 + 其终点下游的所有边。

    返回按拓扑安全的顺序（这里用输入边序的稳定过滤；引擎会再按节点拓扑计算）。
    """
    changed = next((e for e in edges if str(e["id"]) == changed_edge_id), None)
    if changed is None:
        return [changed_edge_id]
    tail = str(changed["to"])
    reachable = downstream_nodes(edges, tail, include_start=True)
    dirty: List[str] = []
    for e in edges:
        eid = str(e["id"])
        if eid == changed_edge_id or str(e["from"]) in reachable:
            dirty.append(eid)
    return dirty


def all_paths(edges: Sequence[dict], src: str, dst: str, max_paths: int = 64) -> List[List[dict]]:
    """枚举 src→dst 的所有有向路径（每条为 edge dict 列表）。

    河网通常很小；设置 max_paths 防止异常稠密 DAG 爆炸。
    """
    out, _ = adjacency(edges)
    paths: List[List[dict]] = []

    def walk(u: str, acc: List[dict], visited: Set[str]):
        if len(paths) >= max_paths:
            return
        if u == dst:
            paths.append(list(acc))
            return
        for e in out.get(u, []):
            v = str(e["to"])
            if v in visited:  # DAG 下不会发生，双保险
                continue
            visited.add(v)
            acc.append(e)
            walk(v, acc, visited)
            acc.pop()
            visited.discard(v)

    walk(src, [], {src})
    return paths


def edge_map(edges: Sequence[dict]) -> Dict[str, dict]:
    return {str(e["id"]): e for e in edges}
