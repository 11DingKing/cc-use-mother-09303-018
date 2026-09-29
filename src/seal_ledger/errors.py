"""封账后端的领域错误。"""
from __future__ import annotations


class SealLedgerError(Exception):
    """所有封账错误的基类。"""

    code = "error"


class ValidationError(SealLedgerError):
    """请求或数据版本内容不合法。"""

    code = "invalid"


class NotFoundError(SealLedgerError):
    """引用的对象不存在。"""

    code = "not_found"


class ConflictError(SealLedgerError):
    """对象当前状态不允许该操作（已封账、并发冲突等）。"""

    code = "conflict"
