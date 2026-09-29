"""封账后端的领域错误类型，携带 HTTP 状态码供接口层使用。"""
from __future__ import annotations


class DomainError(Exception):
    """领域错误基类。"""

    status = 400
    code = "domain_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class BadRequest(DomainError):
    """请求参数缺失或格式不合法。"""

    status = 400
    code = "bad_request"


class NotFound(DomainError):
    """目标对象不存在。"""

    status = 404
    code = "not_found"


class Conflict(DomainError):
    """当前状态不允许该操作，或与既有记录冲突。"""

    status = 409
    code = "conflict"


class Unprocessable(DomainError):
    """语义不满足领域规则（例如批准人不独立）。"""

    status = 422
    code = "unprocessable"
