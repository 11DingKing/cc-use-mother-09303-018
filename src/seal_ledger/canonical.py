"""确定性序列化与摘要工具。

封账的全部效力依赖"同一输入永远得到同一字节"：数据版本、规则版本、
输入摘要、报告正文与分块摘要都通过这里的规范 JSON 与 SHA-256 计算，
时间戳等环境因素一律不参与摘要。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> bytes:
    """生成字节稳定的规范 JSON（键排序、无空白、UTF-8）。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest(value: Any) -> str:
    """对可 JSON 化对象计算 SHA-256。"""
    return digest_bytes(canonical_json(value))


def digest_bytes(value: bytes) -> str:
    """对原始字节计算 SHA-256。"""
    return hashlib.sha256(value).hexdigest()
