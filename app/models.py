"""场景的数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Condition:
    source: str          # body / query / header / path / state
    path: str
    op: str
    value: Any = None


@dataclass(frozen=True)
class Rule:
    name: str
    priority: int
    conditions: list[Condition]
    response: dict[str, Any]
    next_state: str | None
    set: dict[str, Any]
    delay_ms: int = 0


@dataclass
class Route:
    id: str
    path: str
    methods: list[str]
    path_re: Any            # 编译后的 re.Pattern
    path_params: list[str]
    rules: list[Rule]       # 已按 priority 降序排序


@dataclass
class Scenario:
    name: str
    initial_state: str
    states: list[str]
    routes: list[Route]
    description: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
