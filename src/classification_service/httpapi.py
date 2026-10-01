"""线程化 HTTP/JSON 接口。

仅依赖标准库 :mod:`http.server`。幂等约定：

- 写请求可携带 ``Idempotency-Key`` 头；服务端在单个立即写事务内
  “查重 → 执行业务 → 存响应”，并发重复请求只会有一个真正执行，
  其余拿到首份响应，进程重启后键仍然有效。
- 同键不同请求体直接拒绝（409），杜绝键复用。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .canonical import content_hash
from .db import initialize
from .errors import (
    ConflictError,
    DomainError,
    DuplicateSubmissionError,
    NotFoundError,
    RecusalError,
    RuleVersionError,
    StateError,
)
from .store import Store
from .workflow import Workflow

JSON_CONTENT = "application/json; charset=utf-8"


class ServiceContext:
    """共享的工作流实例与串行化写锁（SQLite 写锁之外的应用层兜底）。"""

    def __init__(self, db_path: str) -> None:
        self.store = Store(db_path)
        initialize(self.store.conn)
        self.workflow = Workflow(self.store)
        # 所有写路径在 SQLite BEGIN IMMEDIATE 下已串行；此锁主要保证
        # “幂等查重 + 业务执行 + 响应落库”在同一应用进程内原子可见。
        self.write_lock = threading.RLock()


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "ClassificationService/0.2"
    ctx: ServiceContext  # 由 server 注入

    # ------------------------------------------------------------------ #
    def _send_json(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", JSON_CONTENT)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, code: str, message: str) -> None:
        self._send_json(status, {"error": {"code": code, "message": message}})

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError(f"请求体不是合法 JSON：{exc}", code="bad_json")
        if not isinstance(value, dict):
            raise DomainError("请求体必须是 JSON 对象", code="bad_json")
        return value

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[http] {self.address_string()} - {fmt % args}")

    # ------------------------------------------------------------------ #
    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route_get()
        except DomainError as exc:
            self._domain_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._route_post()
        except DomainError as exc:
            self._domain_error(exc)

    def _domain_error(self, exc: DomainError) -> None:
        if isinstance(exc, NotFoundError):
            status = 404
        elif isinstance(exc, (ConflictError, DuplicateSubmissionError,
                              RecusalError, RuleVersionError)):
            status = 409
        elif isinstance(exc, StateError):
            status = 422
        else:
            status = 400
        self._error(status, exc.code, str(exc))

    # ------------------------------------------------------------------ #
    def _route_get(self) -> None:
        wf = self.ctx.workflow
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = parse_qs(parsed.query)

        if parts == ["api", "rules"]:
            self._send_json(200, {"packages": wf.list_rule_packages()})
            return
        if parts == ["api", "applications"]:
            self._send_json(200, {"applications": wf.list_applications()})
            return
        if len(parts) == 3 and parts[:2] == ["api", "applications"]:
            app_id = parts[2]
            if "history" in query:
                self._send_json(200, wf.history(app_id))
            else:
                self._send_json(200, wf.get_application(app_id))
            return
        if len(parts) == 4 and parts[:2] == ["api", "applications"] and parts[3] == "decision":
            self._send_json(200, wf.get_decision(parts[2]))
            return
        if len(parts) == 4 and parts[:2] == ["api", "applications"] and parts[3] == "decisions":
            self._send_json(200, {"decisions": self.ctx.store.list_decisions(parts[2])})
            return
        if len(parts) == 4 and parts[:2] == ["api", "applications"] and parts[3] == "assignments":
            self._send_json(200, {"assignments": wf.list_assignments(parts[2])})
            return
        if len(parts) == 4 and parts[:2] == ["api", "applications"] and parts[3] == "evaluation":
            self._send_json(200, wf.preview_evaluation(
                parts[2], query.get("rule_version", [None])[0]))
            return
        if parts == ["api", "experts"]:
            self._send_json(200, {"experts": self.ctx.store.list_experts()})
            return
        if len(parts) == 3 and parts[:2] == ["api", "appeals"]:
            self._send_json(200, wf.recompute_appeal(parts[2]))
            return
        if parts == ["api", "health"]:
            self._send_json(200, {"status": "ok"})
            return
        self._error(404, "not_found", f"未知路径：{parsed.path}")

    def _route_post(self) -> None:
        body = self._read_json()
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        idem_key = self.headers.get("Idempotency-Key")
        self._dispatch_write(parts, body, idem_key)

    def _dispatch_write(self, parts: list[str], body: dict[str, Any], idem_key: str | None) -> None:
        """在统一写锁与幂等保护下执行 POST 用例。"""
        wf = self.ctx.workflow
        request_hash = content_hash({"path": "/".join(parts), "body": body})

        def tx() -> tuple[int, Any, bool]:
            # 查重与响应落库都在立即写事务内：与业务原子提交，
            # 进程崩溃不会出现“业务已提交、幂等键丢失”的窗口。
            if idem_key:
                self.ctx.store.check_request_fingerprint(idem_key, request_hash)
                cached = self.ctx.store.get_idempotent(idem_key)
                if cached is not None:
                    return cached["status_code"], cached["body"], True

            status, result = self._execute(parts, body, wf)

            if idem_key:
                self.ctx.store.save_idempotent(idem_key, request_hash, status, result)
            return status, result, False

        with self.ctx.write_lock:
            status, result, replayed = self.ctx.store.write(tx)
        self._send_json(status, {**result, "idempotent_replayed": True} if replayed else result)

    def _execute(self, parts: list[str], body: dict[str, Any], wf: Workflow) -> tuple[int, Any]:
        # ----- 规则管理 -----
        if parts == ["api", "rules"]:
            return 201, wf.publish_rules(
                body["package_version"], body.get("display_name", body["package_version"]),
                body["rules"],
            )
        if len(parts) == 3 and parts[:2] == ["api", "rules"] and parts[2] == "activate":
            return 200, wf.activate_rules(body["package_version"])

        # ----- 专家与回避 -----
        if parts == ["api", "experts"]:
            return 201, wf.register_expert(body["expert_id"], body["name"], body["org"])
        if len(parts) == 4 and parts[:2] == ["api", "applications"] and parts[3] == "recusals":
            return 201, wf.declare_recusal(parts[2], body["expert_id"], body["reason"])
        if len(parts) == 4 and parts[:2] == ["api", "applications"] and parts[3] == "assignments":
            return 201, wf.assign_expert(parts[2], body["expert_id"], body["actor_id"])

        # ----- 申请、证据、状态机 -----
        if parts == ["api", "applications"]:
            return 201, wf.create_application(
                body["applicant_code"], body["applicant_name"], body["mission_statement"],
            )
        if len(parts) == 4 and parts[:2] == ["api", "applications"] and parts[3] == "evidence":
            return 201, wf.submit_evidence(
                parts[2], body["slot"], body["payload"], body["submitted_by"],
            )
        if len(parts) == 4 and parts[:2] == ["api", "applications"] and parts[3] == "accept":
            return 200, wf.accept(parts[2], body["actor_id"])
        if len(parts) == 4 and parts[:2] == ["api", "applications"] and parts[3] == "review":
            return 200, wf.start_review(parts[2], body["actor_id"])
        if len(parts) == 4 and parts[:2] == ["api", "applications"] and parts[3] == "sign":
            return 200, wf.sign(parts[2], body["official_id"], body["official_name"],
                               rule_version=body.get("rule_version"))
        if len(parts) == 4 and parts[:2] == ["api", "applications"] and parts[3] == "appeals":
            return 201, wf.open_appeal(parts[2], body["reason"], body["created_by"])

        # ----- 评审意见 -----
        if len(parts) == 3 and parts[:2] == ["api", "assignments"] and parts[2] == "opinions":
            return 201, wf.submit_opinion(
                body["assignment_id"], body["suggested_type"], body["rationale"],
            )

        # ----- 复议 -----
        if len(parts) == 4 and parts[:2] == ["api", "appeals"] and parts[3] == "resolve":
            return 200, wf.resolve_appeal(parts[2], body["result"], body["official_id"])

        raise DomainError(f"未知路径：/{'/'.join(parts)}", code="not_found")


def build_server(host: str, port: int, db_path: str) -> ThreadingHTTPServer:
    ctx = ServiceContext(db_path)

    class _Handler(ApiHandler):
        pass

    _Handler.ctx = ctx
    server = ThreadingHTTPServer((host, port), _Handler)
    server.ctx = ctx  # type: ignore[attr-defined]
    return server


def serve(host: str = "127.0.0.1", port: int = 8080, db_path: str = "classification.db") -> None:
    server = build_server(host, port, db_path)
    print(f"高校分类定位论证服务已启动：http://{host}:{port}（数据库 {db_path}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
