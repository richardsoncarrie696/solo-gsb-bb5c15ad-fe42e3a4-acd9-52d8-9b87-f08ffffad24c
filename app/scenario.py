"""YAML 场景解析与校验。"""
from __future__ import annotations

import re
from typing import Any

import yaml
from jinja2 import TemplateError
from jinja2.sandbox import SandboxedEnvironment

from .matching import OPERATORS, compile_path
from .models import Condition, Route, Rule, Scenario

DEFAULT_INITIAL_STATE = "init"
RESERVED_PREFIXES = ("/__scenarios", "/__sessions", "/__health", "/docs", "/openapi.json", "/redoc")
_VALID_SOURCES = {"body", "query", "header", "path", "state"}


class ScenarioError(ValueError):
    """场景定义不合法。"""


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ScenarioError(message)


def _parse_condition(raw: Any, route_id: str, rule_name: str) -> Condition:
    _require(isinstance(raw, dict), f"路由 {route_id} 规则 {rule_name} 的 when 条件必须是对象")

    source = raw.get("source", "body")
    _require(source in _VALID_SOURCES,
             f"路由 {route_id} 规则 {rule_name}: 不支持的条件来源 {source!r}")

    path = str(raw.get("path", ""))
    if source == "state":
        path = ""
    else:
        _require(path != "", f"路由 {route_id} 规则 {rule_name}: 条件缺少 path")

    op = str(raw.get("op", "eq"))
    _require(op in OPERATORS,
             f"路由 {route_id} 规则 {rule_name}: 未知运算符 {op!r}，可选: {sorted(OPERATORS)}")

    if op in {"exists", "truthy"}:
        value = raw.get("value", True)
    else:
        _require("value" in raw,
                 f"路由 {route_id} 规则 {rule_name}: 运算符 {op} 需要 value")
        value = raw["value"]

    return Condition(source=source, path=path, op=op, value=value)


def _parse_rules(raw_rules: Any, route_id: str, states: list[str]) -> list[Rule]:
    _require(isinstance(raw_rules, list) and raw_rules,
             f"路由 {route_id}: rules 必须是非空数组")
    rules: list[Rule] = []
    seen_names: set[str] = set()
    priorities: set[int] = set()

    for index, raw in enumerate(raw_rules):
        _require(isinstance(raw, dict), f"路由 {route_id}: 第 {index} 条规则必须是对象")
        name = str(raw.get("name") or f"rule-{index + 1}")
        _require(name not in seen_names, f"路由 {route_id}: 规则名 {name} 重复")
        seen_names.add(name)

        priority = raw.get("priority", 100 - index)
        _require(isinstance(priority, int) and not isinstance(priority, bool),
                 f"路由 {route_id} 规则 {name}: priority 必须是整数")
        if priority in priorities:
            # 不阻断载入，但优先级相同时按定义顺序兜底
            pass
        priorities.add(priority)

        when = raw.get("when", [])
        if isinstance(when, dict):
            when = [when]
        _require(isinstance(when, list), f"路由 {route_id} 规则 {name}: when 必须是对象或数组")
        conditions = [_parse_condition(c, route_id, name) for c in when]

        response = raw.get("response", {})
        _require(isinstance(response, dict), f"路由 {route_id} 规则 {name}: response 必须是对象")
        status = response.get("status", 200)
        _require(isinstance(status, int) or isinstance(status, str),
                 f"路由 {route_id} 规则 {name}: response.status 必须是整数或模板字符串")
        if isinstance(status, int):
            _require(100 <= status <= 599, f"路由 {route_id} 规则 {name}: 非法状态码 {status}")
        if "headers" in response:
            _require(isinstance(response["headers"], dict),
                     f"路由 {route_id} 规则 {name}: response.headers 必须是对象")

        delay_ms = raw.get("delay_ms", 0)
        _require(isinstance(delay_ms, int) and not isinstance(delay_ms, bool) and delay_ms >= 0,
                 f"路由 {route_id} 规则 {name}: delay_ms 必须是非负整数")

        next_state = raw.get("next_state")
        if next_state is not None:
            next_state = str(next_state)
            _require(next_state in states,
                     f"路由 {route_id} 规则 {name}: next_state {next_state!r} 未在 states 中声明")

        updates = raw.get("set", {})
        _require(isinstance(updates, dict), f"路由 {route_id} 规则 {name}: set 必须是对象")

        rules.append(Rule(
            name=name,
            priority=priority,
            conditions=conditions,
            response=response,
            next_state=next_state,
            set=updates,
            delay_ms=delay_ms,
        ))

    rules.sort(key=lambda r: r.priority, reverse=True)
    return rules


def _parse_route(raw: Any, index: int, states: list[str]) -> Route:
    _require(isinstance(raw, dict), f"routes[{index}] 必须是对象")
    route_id = str(raw.get("id") or f"route-{index + 1}")
    path = raw.get("path")
    _require(isinstance(path, str) and path.startswith("/"),
             f"路由 {route_id}: path 必须是 / 开头的字符串")
    for prefix in RESERVED_PREFIXES:
        _require(not path.startswith(prefix),
                 f"路由 {route_id}: 路径 {path} 与管理接口前缀 {prefix} 冲突")

    methods_raw = raw.get("methods", ["GET", "POST"])
    _require(isinstance(methods_raw, list) and methods_raw,
             f"路由 {route_id}: methods 必须是非空数组")
    methods = [str(m).upper() for m in methods_raw]
    valid_methods = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
    for m in methods:
        _require(m in valid_methods, f"路由 {route_id}: 不支持的 HTTP 方法 {m}")

    try:
        path_re, path_params = compile_path(path)
    except ValueError as exc:
        raise ScenarioError(str(exc)) from exc

    rules = _parse_rules(raw.get("rules"), route_id, states)
    return Route(id=route_id, path=path, methods=methods, path_re=path_re,
                 path_params=path_params, rules=rules)


def parse_scenario(text: str, *, name_override: str | None = None) -> Scenario:
    """解析 YAML 文本，失败抛 ScenarioError。"""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ScenarioError(f"YAML 解析失败: {exc}") from exc

    _require(isinstance(data, dict), "场景根节点必须是映射 (mapping)")

    name = name_override or data.get("name")
    _require(isinstance(name, str) and name.strip(), "场景缺少 name")
    name = name.strip()
    _require(re.fullmatch(r"[a-zA-Z0-9_.\-]+", name),
             f"场景名 {name!r} 只允许字母数字及 . _ -")

    initial_state = data.get("initial_state", DEFAULT_INITIAL_STATE)
    _require(isinstance(initial_state, str), "initial_state 必须是字符串")

    declared_states = data.get("states")
    if declared_states is None:
        states = [initial_state]
    else:
        _require(isinstance(declared_states, list) and all(isinstance(s, str) for s in declared_states),
                 "states 必须是字符串数组")
        states = declared_states
        _require(initial_state in states,
                 f"initial_state {initial_state!r} 未在 states 中声明")

    routes_raw = data.get("routes")
    _require(isinstance(routes_raw, list) and routes_raw, "routes 必须是非空数组")

    routes = [_parse_route(r, i, states) for i, r in enumerate(routes_raw)]
    seen_route_ids: set[str] = set()
    seen_signatures: set[tuple[str, tuple[str, ...]]] = set()
    for route in routes:
        _require(route.id not in seen_route_ids, f"路由 id {route.id} 重复")
        seen_route_ids.add(route.id)
        sig = (route.path, tuple(sorted(route.methods)))
        _require(sig not in seen_signatures,
                 f"路由 {route.id}: 路径 + 方法组合 {route.path} {sorted(route.methods)} 重复")
        seen_signatures.add(sig)

    _validate_templates(routes, initial_state)

    return Scenario(
        name=name,
        initial_state=initial_state,
        states=states,
        routes=routes,
        description=str(data.get("description", "")),
        raw=data,
    )


def _validate_templates(routes: list[Route], initial_state: str) -> None:
    """预编译所有模板字符串，载入阶段即暴露 Jinja 语法错误。"""
    env = SandboxedEnvironment()

    def check(text: str, where: str) -> None:
        try:
            env.compile(text)
        except TemplateError as exc:
            raise ScenarioError(f"{where} 模板语法错误: {exc}") from exc

    for route in routes:
        for rule in route.rules:
            resp = rule.response
            if "body" in resp:
                _check_nested(resp["body"], f"路由 {route.id} 规则 {rule.name} 响应体", check)
            for key, value in (resp.get("headers") or {}).items():
                if isinstance(value, str):
                    check(value, f"路由 {route.id} 规则 {rule.name} 响应头 {key}")
            if isinstance(resp.get("status"), str):
                check(resp["status"], f"路由 {route.id} 规则 {rule.name} 状态码")
            for key, value in rule.set.items():
                if isinstance(value, str):
                    check(value, f"路由 {route.id} 规则 {rule.name} 的 set.{key}")
            if rule.next_state is not None and ("{{" in rule.next_state or "{%" in rule.next_state):
                check(rule.next_state, f"路由 {route.id} 规则 {rule.name} 的 next_state")


def _check_nested(value: Any, where: str, check) -> None:
    if isinstance(value, str):
        if "{{" in value or "{%" in value:
            check(value, where)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _check_nested(item, f"{where}[{i}]", check)
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_nested(item, f"{where}.{key}", check)
