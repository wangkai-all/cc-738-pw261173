"""本地 HTTP 服务：JSON API + 静态网页（仅标准库）。

启动：``python run.py`` 或 ``python -m rivernet``，默认 http://127.0.0.1:8000
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .engine import Project, ProjectError
from .importer import ImportError_, build_bundle_from_text
from .kernels import KernelError

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WEB_DIR = os.path.join(ROOT, "web")
EXAMPLES_DIR = os.path.join(ROOT, "examples", "data")

API_LOCK = threading.RLock()

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".csv": "text/csv; charset=utf-8",
}


class SessionManager:
    def __init__(self):
        self.sessions: dict[str, Project] = {}

    def create(self, bundle: dict) -> str:
        sid = uuid.uuid4().hex[:12]
        self.sessions[sid] = Project(bundle)
        return sid

    def get(self, sid: str | None) -> Project:
        if not sid or sid not in self.sessions:
            raise ProjectError("NO_SESSION", "会话不存在或已过期，请重新打开示例/导入数据", 404)
        return self.sessions[sid]

    def example_summaries(self):
        out = []
        if not os.path.isdir(EXAMPLES_DIR):
            return out
        for name in sorted(os.listdir(EXAMPLES_DIR)):
            pj = os.path.join(EXAMPLES_DIR, name, "project.json")
            if not os.path.isfile(pj):
                continue
            try:
                with open(pj, "r", encoding="utf-8") as f:
                    b = json.load(f)
                out.append({
                    "id": b.get("id", name),
                    "dir": name,
                    "name": b.get("name", name),
                    "kind": b.get("kind", ""),
                    "description": b.get("description", ""),
                    "dt_minutes": b.get("grid", {}).get("dt_minutes"),
                    "n": b.get("grid", {}).get("n"),
                    "ok_hint": b.get("known", {}).get("expect_error") != "CYCLE_DETECTED",
                })
            except Exception as ex:  # 单个样例损坏不影响列表
                out.append({"id": name, "dir": name, "name": name, "error": str(ex)})
        return out

    def open_example(self, example_id: str) -> tuple[str, Project]:
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", example_id):
            raise ProjectError("BAD_EXAMPLE", "非法样例 id", 400)
        pj = os.path.join(EXAMPLES_DIR, example_id, "project.json")
        if not os.path.isfile(pj):
            # 允许用 bundle 内 id 与目录名不同：扫描匹配
            for d in os.listdir(EXAMPLES_DIR):
                cand = os.path.join(EXAMPLES_DIR, d, "project.json")
                if os.path.isfile(cand):
                    with open(cand, "r", encoding="utf-8") as f:
                        b = json.load(f)
                    if b.get("id") == example_id:
                        pj = cand
                        break
        if not os.path.isfile(pj):
            raise ProjectError("UNKNOWN_EXAMPLE", f"找不到样例 {example_id}", 404)
        with open(pj, "r", encoding="utf-8") as f:
            bundle = json.load(f)
        sid = self.create(bundle)
        return sid, self.sessions[sid]


MANAGER = SessionManager()


class Handler(BaseHTTPRequestHandler):
    server_version = "RiverNet/1.0"

    def log_message(self, fmt, *args):  # 安静日志
        return

    # ---------------------------- 基础响应 ---------------------------- #
    def _send_json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_csv(self, text: str, filename: str):
        body = ("﻿" + text).encode("utf-8")  # BOM 方便 Excel 识别中文
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError as ex:
            raise ProjectError("BAD_JSON", f"请求体不是合法 JSON：{ex}", 400)

    def _sid(self, qs, body=None):
        sid = (qs.get("sid", [None])[0] if qs else None)
        if not sid and body:
            sid = body.get("sid")
        if not sid:
            sid = self.headers.get("X-Sid")
        return sid

    # ---------------------------- GET --------------------------------- #
    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            if path.startswith("/api/"):
                with API_LOCK:
                    return self._route_get(path, qs)
            return self._serve_static(path)
        except ProjectError as ex:
            return self._send_json({"ok": False, "error": {"code": ex.code, "message": str(ex), "details": ex.details}}, ex.status)
        except (KernelError, ImportError_) as ex:
            return self._send_json({"ok": False, "error": {"code": "BAD_INPUT", "message": str(ex)}}, 400)
        except Exception as ex:  # noqa: BLE001
            return self._send_json({"ok": False, "error": {"code": "INTERNAL", "message": f"{type(ex).__name__}: {ex}"}}, 500)

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            with API_LOCK:
                body = self._read_json()
                return self._route_post(path, body)
        except ProjectError as ex:
            return self._send_json({"ok": False, "error": {"code": ex.code, "message": str(ex), "details": ex.details}}, ex.status)
        except (KernelError, ImportError_) as ex:
            return self._send_json({"ok": False, "error": {"code": "BAD_INPUT", "message": str(ex)}}, 400)
        except Exception as ex:  # noqa: BLE001
            return self._send_json({"ok": False, "error": {"code": "INTERNAL", "message": f"{type(ex).__name__}: {ex}"}}, 500)

    def _route_get(self, path: str, qs):
        if path == "/api/health":
            return self._send_json({"ok": True, "service": "rivernet", "version": "1.0.0"})

        if path == "/api/examples":
            return self._send_json({"ok": True, "examples": MANAGER.example_summaries()})

        sid = self._sid(qs)
        proj = MANAGER.get(sid)

        if path == "/api/state":
            return self._send_json({"ok": True, "sid": sid, "state": proj.state()})

        if path == "/api/edge":
            eid = qs.get("edge_id", [None])[0]
            return self._send_json({"ok": True, "view": proj.edge_view(eid)})

        if path == "/api/contributions":
            target = qs.get("target", [None])[0]
            if target is None:
                target = proj.bundle.get("selected_target") or proj.station_ids[-1]
            return self._send_json({"ok": True, "data": proj.contributions(target)})

        if path == "/api/contributions.csv":
            target = qs.get("target", [proj.bundle.get("selected_target") or proj.station_ids[-1]])[0]
            fmt = qs.get("format", ["wide"])[0]
            if fmt == "long":
                return self._send_csv(proj.contributions_long_csv(target), f"contributions_{target}_long.csv")
            return self._send_csv(proj.contributions_csv(target), f"contributions_{target}.csv")

        if path == "/api/residual":
            target = qs.get("target", [proj.bundle.get("selected_target") or proj.station_ids[-1]])[0]
            return self._send_json({"ok": True, "data": proj.residual_sources(target)})

        if path == "/api/peaks":
            source = qs.get("source", [None])[0]
            target = qs.get("target", [None])[0]
            if not source or not target:
                raise ProjectError("BAD_PARAMS", "peaks 需要 source 与 target")
            i0 = qs.get("i0", [None])[0]
            i1 = qs.get("i1", [None])[0]
            return self._send_json({"ok": True, "data": proj.peak_movement(
                source, target,
                int(i0) if i0 not in (None, "") else None,
                int(i1) if i1 not in (None, "") else None)})

        if path == "/api/scheme":
            scheme_id = qs.get("scheme_id", [None])[0]
            s = proj.get_scheme(scheme_id)
            return self._send_json({"ok": True, "scheme": s})

        raise ProjectError("NOT_FOUND", f"未知接口 {path}", 404)

    # ---------------------------- POST -------------------------------- #
    def _route_post(self, path: str, body: dict):
        if path == "/api/session/example":
            example_id = body.get("example_id")
            sid, proj = MANAGER.open_example(example_id)
            return self._send_json({"ok": True, "sid": sid, "state": proj.state()})

        if path == "/api/session/bundle":
            bundle = body.get("bundle") if isinstance(body.get("bundle"), dict) else body
            sid = MANAGER.create(bundle)
            return self._send_json({"ok": True, "sid": sid, "state": MANAGER.get(sid).state()})

        if path == "/api/import":
            bundle = build_bundle_from_text(
                flows_csv=body.get("flows_csv", ""),
                network=body.get("network_json", body.get("network", {})),
                name=body.get("name", "导入的河网项目"),
                dt_minutes=body.get("dt_minutes"),
            )
            sid = MANAGER.create(bundle)
            return self._send_json({"ok": True, "sid": sid, "state": MANAGER.get(sid).state()})

        sid = self._sid(None, body)
        proj = MANAGER.get(sid)

        if path == "/api/edge/lag_range":
            report = proj.set_lag_range(body["edge_id"], int(body["lag_min"]), int(body["lag_max"]))
            return self._send_json({"ok": True, "report": report,
                                    "edge": proj._edge_public(proj.edge_by_id[body["edge_id"]])})

        if path == "/api/edge/kernel":
            report = proj.update_kernel(body["edge_id"], body["weights"], body.get("lag_min"))
            return self._send_json({"ok": True, "report": report,
                                    "edge": proj._edge_public(proj.edge_by_id[body["edge_id"]])})

        if path == "/api/calibrate":
            out = proj.calibrate(
                eid=body["edge_id"],
                i0=int(body["i0"]), i1=int(body["i1"]),
                lag_min=body.get("lag_min"), lag_max=body.get("lag_max"),
                name=body.get("name"), apply=bool(body.get("apply", True)),
                exclude_intervals=body.get("exclude_intervals"),
            )
            return self._send_json({"ok": True, **out})

        if path == "/api/scheme/apply":
            out = proj.apply_scheme(body["scheme_id"])
            return self._send_json({"ok": True, **out})

        if path == "/api/scheme/refit":
            return self._send_json({"ok": True, "result": proj.refit_snapshot(body["scheme_id"])})

        raise ProjectError("NOT_FOUND", f"未知接口 {path}", 404)

    # ---------------------------- 静态 --------------------------------- #
    def _serve_static(self, path: str):
        if path in ("", "/"):
            rel = "index.html"
        else:
            rel = path.lstrip("/")
        # 只允许 web 目录内文件
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(os.path.abspath(WEB_DIR) + os.sep) and full != os.path.abspath(WEB_DIR):
            return self._send_json({"ok": False, "error": {"code": "FORBIDDEN", "message": "非法路径"}}, 403)
        if not os.path.isfile(full):
            return self._send_json({"ok": False, "error": {"code": "NOT_FOUND", "message": f"找不到 {path}"}}, 404)
        ext = os.path.splitext(full)[1].lower()
        ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if ext in (".html", ".js", ".css"):
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def build_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd


def main():
    import argparse
    parser = argparse.ArgumentParser(description="河网卷积分析本地服务")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()
    httpd = build_server(args.host, args.port)
    print(f"河网卷积分析工具已启动： http://127.0.0.1:{args.port}")
    print("全部数据为虚构，仅用于方法演示，不构成真实水文预警。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
