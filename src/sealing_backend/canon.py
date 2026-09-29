"""确定性序列化与摘要工具。

封账域的所有摘要（输入摘要、内容摘要、分块摘要）都依赖同一份
规范化 JSON 表示：键排序、无冗余空白、UTF-8 编码，保证同一内容
在任何时刻、任何进程中得到完全相同的字节串。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_bytes(value: Any) -> bytes:
    """生成确定性 JSON 字节串。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """计算字节串的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(data).hexdigest()


def digest_of(value: Any) -> str:
    """任意可 JSON 序列化对象的稳定摘要。"""
    return sha256_hex(canonical_bytes(value))


def split_chunks(document: bytes, chunk_size: int) -> list[bytes]:
    """把导出件切分为定长分块（最后一块可能较短）。"""
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须为正数")
    return [document[offset : offset + chunk_size] for offset in range(0, len(document), chunk_size)]
