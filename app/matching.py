"""请求匹配：路径模式、取值路径与条件运算符。"""
from __future__ import annotations

import re
from typing import Any

from .models import Condition

# ---------------------------------------------------------------------------
# 路径模式：/orders/{id}/items/{item_id}
# ---------------------------------------------------------------------------

_PARAM_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def compile_path(pattern: str) -> tuple[re.Pattern[str], list[str]]:
    if not pattern.startswith("/"):
        raise ValueError(f"路径必须以 / 开头: {pattern}")
    params: list[str] = []
    pieces: list[str] = []
    cursor = 0
    for m in _PARAM_RE.finditer(pattern):
        name = m.group(1)
        if name in params:
            raise ValueError(f"路径 {pattern} 中参数 {name} 重复")
        params.append(name)
        pieces.append(re.escape(pattern[cursor:m.start()]))
        pieces.append(f"(?P<{name}>[^/]+)")
        cursor = m.end()
    pieces.append(re.escape(pattern[cursor:]))
    return re.compile("^" + "".join(pieces) + "/?$"), params


def match_path(path_re: re.Pattern[str], path: str) -> dict[str, str] | None:
    m = path_re.match(path)
    if m is None:
        return None
    return m.groupdict()


# ---------------------------------------------------------------------------
# 取值路径：a.b.0.c / a[0].b
# ---------------------------------------------------------------------------

_PATH_TOKEN_RE = re.compile(r"[^.[\]]+|\[\d+\]")


def resolve(data: Any, dotted: str) -> tuple[bool, Any]:
    """按点路径取值，返回 (是否存在, 值)。"""
    current = data
    for token in _PATH_TOKEN_RE.findall(dotted):
        token = token.strip()
        if token.startswith("["):
            index = int(token[1:-1])
            if not isinstance(current, list) or index >= len(current):
                return False, None
            current = current[index]
            continue
        if not isinstance(current, dict) or token not in current:
            return False, None
        current = current[token]
    return True, current


# ---------------------------------------------------------------------------
# 运算符
# ---------------------------------------------------------------------------

def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _cmp_numbers(left: Any, right: Any) -> tuple[float, float] | None:
    a, b = _as_number(left), _as_number(right)
    if a is None or b is None:
        return None
    return a, b


OPERATORS = {
    "eq", "ne", "gt", "gte", "lt", "lte",
    "in", "contains", "starts_with", "ends_with",
    "regex", "exists", "truthy",
}


def evaluate_condition(cond: Condition, actual: Any, present: bool) -> bool:
    if cond.op == "exists":
        return present

    if not present:
        return False

    expected = cond.value

    if cond.op == "truthy":
        return bool(actual)

    if cond.op == "eq":
        nums = _cmp_numbers(actual, expected)
        if nums is not None:
            return nums[0] == nums[1]
        return actual == expected

    if cond.op == "ne":
        nums = _cmp_numbers(actual, expected)
        if nums is not None:
            return nums[0] != nums[1]
        return actual != expected

    if cond.op in {"gt", "gte", "lt", "lte"}:
        nums = _cmp_numbers(actual, expected)
        if nums is None:
            return False
        a, b = nums
        return {
            "gt": a > b, "gte": a >= b,
            "lt": a < b, "lte": a <= b,
        }[cond.op]

    if cond.op == "in":
        if isinstance(expected, list):
            return actual in expected
        if isinstance(actual, dict) and isinstance(expected, str):
            return expected in actual
        if isinstance(actual, (list, str)) and isinstance(expected, (list, str)):
            return expected in actual
        return False

    if cond.op == "contains":
        if isinstance(actual, (list, str, dict)):
            return expected in actual
        return False

    if cond.op == "starts_with":
        return isinstance(actual, str) and actual.startswith(str(expected))

    if cond.op == "ends_with":
        return isinstance(actual, str) and actual.endswith(str(expected))

    if cond.op == "regex":
        return isinstance(actual, str) and re.search(str(expected), actual) is not None

    raise ValueError(f"未知运算符: {cond.op}")


def get_condition_value(source: str, cond_path: str, bag: dict[str, Any]) -> tuple[bool, Any]:
    """从匹配数据袋中取条件左值。"""
    if source == "state":
        return True, bag.get("state")
    container = bag.get(source, {})
    return resolve(container, cond_path)
