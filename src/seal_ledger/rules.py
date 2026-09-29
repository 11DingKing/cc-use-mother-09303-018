"""确定性规则引擎。

规则版本是一段版本化的纯函数：在固定规则版本下，固定的数据版本永远
得到同一份报告正文。规则以受限安全求值器执行，只暴露白名单运算，
且求值结果不得依赖时间、随机数或外部状态。

规则文件示例（JSON）::

    {
      "rules": [
        {"id": "total_employment",
         "title": "合作项目就业总人数",
         "expr": {"sum": [{"path": "rows.employment"}, {"path": "rows.baseline"}]}},
        {"id": "growth_rate",
         "title": "同比增长率（百分数）",
         "expr": {"round": [
            {"*": [{"-": [{"path": "rows.employment"}, {"path": "rows.baseline"}]},
                   {"/": [1, {"path": "rows.baseline"}]}, 100]}, 2]}}
      ]
    }
"""
from __future__ import annotations

from typing import Any

from .canonical import canonical_json, digest
from .errors import ValidationError

# 允许的运算到 Python 函数的映射（全部为纯函数）
_OPERATORS: dict[str, Any] = {
    "+": lambda a, b: a + b,
    "-": lambda a, b: a - b,
    "*": lambda a, b: a * b,
    "/": lambda a, b: a / b,
    "round": lambda v, ndigits=2: round(v, ndigits),
    "sum": lambda *values: sum(values),
    "min": lambda a, b: min(a, b),
    "max": lambda a, b: max(a, b),
}


def _resolve_path(payload: Any, path: str) -> Any:
    cur = payload
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise ValidationError(f"数据缺少路径：{path}")
        cur = cur[part]
    return cur


def _eval_node(node: Any, payload: Any) -> Any:
    if isinstance(node, dict) and len(node) == 1:
        op, args = next(iter(node.items()))
        if op == "path":
            return _resolve_path(payload, str(args))
        if op == "const":
            return args
        if op not in _OPERATORS:
            raise ValidationError(f"不支持的运算：{op}")
        if not isinstance(args, list) or not args:
            raise ValidationError(f"运算 {op} 的参数必须是非空列表")
        values = [_eval_node(arg, payload) for arg in args]
        return _OPERATORS[op](*values)
    if isinstance(node, (int, float, str)):
        return node
    raise ValidationError(f"无法识别的规则节点：{node!r}")


def validate_ruleset(spec: Any) -> None:
    """校验规则版本结构。"""
    if not isinstance(spec, dict) or not isinstance(spec.get("rules"), list) or not spec["rules"]:
        raise ValidationError("规则版本必须包含非空 rules 列表")
    ids: set[str] = set()
    for rule in spec["rules"]:
        if not isinstance(rule, dict) or not rule.get("id") or not rule.get("title") or "expr" not in rule:
            raise ValidationError("每条规则需要 id、title 与 expr")
        if rule["id"] in ids:
            raise ValidationError(f"规则编号重复：{rule['id']}")
        ids.add(rule["id"])


def ruleset_digest(spec: dict[str, Any]) -> str:
    """规则版本的内容摘要。"""
    return digest(spec)


def evaluate(spec: dict[str, Any], payload: Any) -> list[dict[str, Any]]:
    """在数据负载上执行规则版本，返回有序指标列表。"""
    results: list[dict[str, Any]] = []
    for rule in spec["rules"]:
        value = _eval_node(rule["expr"], payload)
        # 统一转成可确定性序列化的标量
        if isinstance(value, float):
            value = round(value, 6)
        results.append({"id": rule["id"], "title": rule["title"], "value": value})
    return results


def render_body(spec: dict[str, Any], results: list[dict[str, Any]]) -> bytes:
    """把指标结果渲染成确定性的报告正文。"""
    lines = [f"{item['id']}\t{item['title']}\t{item['value']}" for item in results]
    return ("\n".join(lines) + "\n").encode("utf-8")


def ruleset_fingerprint(spec: dict[str, Any]) -> dict[str, Any]:
    """规则版本的登记信息。"""
    return {
        "digest": ruleset_digest(spec),
        "byte_length": len(canonical_json(spec)),
        "rule_count": len(spec["rules"]),
    }
