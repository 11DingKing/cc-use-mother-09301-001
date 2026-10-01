"""HTTP 接口（标准库 http.server，零三方依赖）。

路由：
- GET  /health
- GET  /api/rulesets[/<version>]
- POST /api/rulesets
- GET  /api/experts ; POST /api/experts ; GET /api/experts/<id>
- GET  /api/cases ; POST /api/cases
- GET  /api/cases/<id>
- POST /api/cases/<id>/accept
- POST /api/cases/<id>/review/start
- POST /api/cases/<id>/experts            指派专家
- POST /api/cases/<id>/experts/<eid>/recuse
- POST /api/cases/<id>/evidence
- POST /api/cases/<id>/reviews
- POST /api/cases/<id>/sign
- GET  /api/cases/<id>/decision
- POST /api/cases/<id>/reconsiderations
- GET  /api/cases/<id>/reconsiderations | /api/reconsiderations
- POST /api/cases/<id>/reconsiderations/resolve
- GET  /api/cases/<id>/events

所有 POST 均要求客户端提供 idempotency_key（由服务层强制）。
"""
from __future__ import annotations

import json
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .config import Settings
from .service import ClassificationService
from .services.errors import ServiceError
from .storage import Database


def _make_handler(service: ClassificationService) -> type[BaseHTTPRequestHandler]:
    routes: list[tuple[str, re.Pattern[str], Callable[..., Any]]] = []

    def route(method: str, pattern: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            routes.append((method, re.compile("^" + pattern + "$"), func))
            return func
        return decorator

    @route("GET", r"/health")
    def health(_handler: "Handler", _groups: tuple[str, ...]) -> tuple[int, dict]:
        return 200, {"status": "ok"}

    @route("GET", r"/api/rulesets")
    def list_rulesets(_h: "Handler", _g: tuple[str, ...]) -> tuple[int, dict]:
        return 200, {"rulesets": service.list_rulesets()}

    @route("POST", r"/api/rulesets")
    def publish_ruleset(h: "Handler", _g: tuple[str, ...]) -> tuple[int, dict]:
        return 201, service.publish_ruleset(h.read_body())

    @route("GET", r"/api/rulesets/(?P<version>[^/]+)")
    def get_ruleset(_h: "Handler", g: tuple[str, ...]) -> tuple[int, dict]:
        return 200, service.get_ruleset(g[0])

    @route("GET", r"/api/experts")
    def list_experts(_h: "Handler", _g: tuple[str, ...]) -> tuple[int, dict]:
        return 200, {"experts": service.list_experts()}

    @route("POST", r"/api/experts")
    def register_expert(h: "Handler", _g: tuple[str, ...]) -> tuple[int, dict]:
        return 201, service.register_expert(h.read_body())

    @route("GET", r"/api/experts/(?P<expert_id>[^/]+)")
    def get_expert(_h: "Handler", g: tuple[str, ...]) -> tuple[int, dict]:
        return 200, service.get_expert(g[0])

    @route("GET", r"/api/cases")
    def list_cases(_h: "Handler", _g: tuple[str, ...]) -> tuple[int, dict]:
        return 200, {"cases": service.list_cases()}

    @route("POST", r"/api/cases")
    def create_case(h: "Handler", _g: tuple[str, ...]) -> tuple[int, dict]:
        return 201, service.create_case(h.read_body())

    @route("GET", r"/api/cases/(?P<case_id>[^/]+)")
    def get_case(_h: "Handler", g: tuple[str, ...]) -> tuple[int, dict]:
        return 200, service.get_case(g[0])

    @route("POST", r"/api/cases/(?P<case_id>[^/]+)/accept")
    def accept_case(_h: "Handler", g: tuple[str, ...]) -> tuple[int, dict]:
        return 200, service.accept_case(g[0])

    @route("POST", r"/api/cases/(?P<case_id>[^/]+)/review/start")
    def start_review(_h: "Handler", g: tuple[str, ...]) -> tuple[int, dict]:
        return 200, service.start_review(g[0])

    @route("POST", r"/api/cases/(?P<case_id>[^/]+)/experts")
    def assign_expert(h: "Handler", g: tuple[str, ...]) -> tuple[int, dict]:
        return 200, service.assign_expert(g[0], h.read_body())

    @route("POST", r"/api/cases/(?P<case_id>[^/]+)/experts/(?P<expert_id>[^/]+)/recuse")
    def recuse_expert(h: "Handler", g: tuple[str, ...]) -> tuple[int, dict]:
        return 200, service.recuse_expert(g[0], g[1], h.read_body())

    @route("POST", r"/api/cases/(?P<case_id>[^/]+)/evidence")
    def submit_evidence(h: "Handler", g: tuple[str, ...]) -> tuple[int, dict]:
        return 201, service.submit_evidence(g[0], h.read_body())

    @route("POST", r"/api/cases/(?P<case_id>[^/]+)/reviews")
    def submit_review(h: "Handler", g: tuple[str, ...]) -> tuple[int, dict]:
        return 201, service.submit_review(g[0], h.read_body())

    @route("POST", r"/api/cases/(?P<case_id>[^/]+)/sign")
    def sign_decision(h: "Handler", g: tuple[str, ...]) -> tuple[int, dict]:
        return 201, service.sign_decision(g[0], h.read_body())

    @route("GET", r"/api/cases/(?P<case_id>[^/]+)/decision")
    def get_decision(_h: "Handler", g: tuple[str, ...]) -> tuple[int, dict]:
        return 200, service.get_decision(g[0])

    @route("POST", r"/api/cases/(?P<case_id>[^/]+)/reconsiderations")
    def request_reconsideration(h: "Handler", g: tuple[str, ...]) -> tuple[int, dict]:
        return 201, service.request_reconsideration(g[0], h.read_body())

    @route("GET", r"/api/cases/(?P<case_id>[^/]+)/reconsiderations")
    def list_case_reconsiderations(_h: "Handler", g: tuple[str, ...]) -> tuple[int, dict]:
        return 200, {"reconsiderations": service.list_reconsiderations(g[0])}

    @route("GET", r"/api/reconsiderations")
    def list_all_reconsiderations(_h: "Handler", _g: tuple[str, ...]) -> tuple[int, dict]:
        return 200, {"reconsiderations": service.list_reconsiderations()}

    @route("POST", r"/api/cases/(?P<case_id>[^/]+)/reconsiderations/resolve")
    def resolve_reconsideration(h: "Handler", g: tuple[str, ...]) -> tuple[int, dict]:
        return 200, service.resolve_reconsideration(g[0], h.read_body())

    @route("GET", r"/api/cases/(?P<case_id>[^/]+)/events")
    def events(_h: "Handler", g: tuple[str, ...]) -> tuple[int, dict]:
        return 200, {"events": service.events(g[0])}

    class Handler(BaseHTTPRequestHandler):
        server_version = "ClassificationReview/0.2"

        def read_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ServiceError("bad_request", "请求体不是合法 UTF-8 JSON", 400) from exc
            if not isinstance(value, dict):
                raise ServiceError("bad_request", "请求体必须是 JSON 对象", 400)
            return value

        def _write(self, status: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self) -> None:
            path = self.path.split("?", 1)[0]
            for method, pattern, func in routes:
                if method != self.command:
                    continue
                match = pattern.match(path)
                if match is None:
                    continue
                try:
                    status, body = func(self, tuple(match.groups()))
                except ServiceError as exc:
                    self._write(exc.status, exc.to_body())
                    return
                except sqlite3.OperationalError as exc:
                    if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                        self._write(409, {"error": {
                            "code": "conflict",
                            "message": "并发写入冲突，请使用原幂等键重试"}})
                        return
                    self._write(503, {"error": {"code": "storage_unavailable",
                                                "message": str(exc)}})
                    return
                self._write(status, body)
                return
            self._write(404, {"error": {"code": "not_found", "message": f"无此路由：{path}"}})

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def log_message(self, fmt: str, *args: Any) -> None:
            # 访问日志交给调用方/容器收集，避免污染测试输出。
            return

    return Handler


def create_app(db: Database) -> ClassificationService:
    db.initialize()
    service = ClassificationService(db)
    service.seed()
    return service


def run(settings: Settings) -> None:
    settings.ensure_parent_dir()
    db = Database(settings.db_path)
    service = create_app(db)
    handler = _make_handler(service)
    server = ThreadingHTTPServer((settings.host, settings.port), handler)
    print(f"高校分类定位论证服务已启动：http://{settings.host}:{settings.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        db.close()
