"""服务层统一错误。HTTP 层按 status 映射为状态码。"""
from __future__ import annotations


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 422,
                 extra: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra or {}

    def to_body(self) -> dict:
        body = {"error": {"code": self.code, "message": self.message}}
        if self.extra:
            body["error"]["details"] = self.extra
        return body


def not_found(resource: str, key: str) -> ServiceError:
    return ServiceError("not_found", f"{resource}不存在：{key}", 404)


def conflict(message: str, **extra: object) -> ServiceError:
    return ServiceError("conflict", message, 409, extra)


def forbidden(message: str) -> ServiceError:
    return ServiceError("forbidden", message, 403)


def bad_request(message: str) -> ServiceError:
    return ServiceError("bad_request", message, 400)
