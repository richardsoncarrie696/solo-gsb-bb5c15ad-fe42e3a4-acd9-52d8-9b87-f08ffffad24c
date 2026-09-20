"""Jinja2 沙箱渲染，同时提供模板辅助函数与会话级确定性随机源。"""
from __future__ import annotations

import json
import random
import re
import string
import uuid
from datetime import datetime, timezone
from typing import Any

from jinja2 import StrictUndefined
from jinja2.exceptions import UndefinedError
from jinja2.sandbox import SandboxedEnvironment

_EXPR_ONLY_RE = re.compile(r"^\{\{(.+)\}\}$", re.S)


class RenderError(Exception):
    """模板渲染失败。"""


def _now(fmt: str | None = None) -> str:
    """UTC 当前时间；传入 strftime 格式串时按格式返回。"""
    moment = datetime.now(timezone.utc)
    if fmt:
        return moment.strftime(fmt)
    return moment.isoformat()


def _random_str(length: int = 12, prefix: str = "") -> str:
    length = max(0, int(length))
    chars = "".join(random.choices(string.ascii_lowercase + string.digits, k=length))
    return f"{prefix}{chars}"


def _choice(values: list[Any]) -> Any:
    return random.choice(values)


def _seed_material(seed: Any) -> str:
    """把 JSON 标量 seed 规范化为跨进程稳定的播种材料。

    ``random.Random`` 对 str 的播种算法（SHA-512 混合）在 CPython 各版本间
    稳定，因此先序列化为规范 JSON 字符串再播种，避免依赖 ``hash()``。
    """
    return json.dumps(seed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _encode_random_state(state: tuple) -> list:
    """``random.Random.getstate()`` -> 可 JSON 持久化的嵌套列表。"""
    version, internal, gauss_next = state
    return [version, list(internal), gauss_next]


def _decode_random_state(raw: Any) -> tuple:
    """``_encode_random_state`` 的逆过程；内部状态恢复为整型元组。"""
    version, internal, gauss_next = raw
    return (int(version), tuple(int(x) for x in internal), gauss_next)


class SessionRNG:
    """会话级可复现随机源（seed 会话专用）。

    状态以 ``_rng`` 键随会话上下文一并持久化（``seed`` + ``random.Random``
    内部状态 + 累计抽样步数 ``draws``），并在同一个 SQLite 写事务中随会话
    推进：同一 seed、同一初始状态与同一请求链必然让 ``uuid4()`` /
    ``random_str()`` / ``randint()`` / ``choice()`` 产生相同结果。预演通过
    :meth:`from_context` 克隆状态，消耗只发生在内存副本上，不写回即不影响
    真实序列。
    """

    STATE_KEY = "_rng"

    def __init__(self, seed: Any, *, state: Any = None, draws: int = 0):
        self.seed = seed
        self.draws = int(draws)
        self._random = random.Random(_seed_material(seed))
        if state is not None:
            self._random.setstate(_decode_random_state(state))

    @classmethod
    def fresh(cls, seed: Any) -> "SessionRNG":
        """按 seed 从头初始化（创建会话 / reset 传入 seed 时使用）。"""
        return cls(seed)

    @classmethod
    def from_context(cls, context: dict[str, Any]) -> "SessionRNG | None":
        """从会话上下文克隆随机状态；未启用 seed 的会话返回 None。"""
        raw = context.get(cls.STATE_KEY)
        if not isinstance(raw, dict) or "seed" not in raw:
            return None
        return cls(raw["seed"], state=raw.get("state"), draws=raw.get("draws", 0))

    @classmethod
    def info(cls, context: dict[str, Any]) -> dict[str, Any] | None:
        """会话查询用摘要：仅暴露 seed 与累计抽样步数，不泄露内部状态。"""
        raw = context.get(cls.STATE_KEY)
        if not isinstance(raw, dict) or "seed" not in raw:
            return None
        return {"seed": raw["seed"], "draws": int(raw.get("draws", 0))}

    def snapshot(self) -> dict[str, Any]:
        """导出可 JSON 持久化的完整状态，写回会话上下文。"""
        return {
            "seed": self.seed,
            "state": _encode_random_state(self._random.getstate()),
            "draws": self.draws,
        }

    # -- 模板函数：每次调用计 1 步抽样 ------------------------------------

    def uuid4(self) -> str:
        self.draws += 1
        return str(uuid.UUID(int=self._random.getrandbits(128), version=4))

    def random_str(self, length: int = 12, prefix: str = "") -> str:
        self.draws += 1
        length = max(0, int(length))
        chars = "".join(self._random.choices(string.ascii_lowercase + string.digits, k=length))
        return f"{prefix}{chars}"

    def randint(self, a: int, b: int) -> int:
        self.draws += 1
        return self._random.randint(a, b)

    def choice(self, values: list[Any]) -> Any:
        self.draws += 1
        return self._random.choice(values)


class Renderer:
    """每个请求一个实例：携带 seq 计数器、可选的会话随机源与完整上下文。"""

    def __init__(self, context: dict[str, Any], *, trace_calls: bool = False,
                 rng: SessionRNG | None = None):
        self.context = dict(context)
        self.counters: dict[str, int] = dict(self.context.get("_seq") or {})
        # 预演时记录本次渲染中 seq() 的调用轨迹（计数器仍只存在于本实例内存中）
        self.trace_calls = trace_calls
        self.seq_calls: list[dict[str, Any]] = []
        # seed 会话的确定性随机源；为 None 时四个随机函数保持进程级随机行为
        self.rng = rng

        self.env = SandboxedEnvironment(
            undefined=StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
        )
        if rng is None:
            self.env.globals.update(
                uuid4=lambda: str(uuid.uuid4()),
                random_str=_random_str,
                randint=random.randint,
                choice=_choice,
            )
        else:
            self.env.globals.update(
                uuid4=rng.uuid4,
                random_str=rng.random_str,
                randint=rng.randint,
                choice=rng.choice,
            )
        self.env.globals.update(
            now=_now,
            seq=self._seq,
        )

    def _seq(self, name: str = "default", start: int = 1) -> int:
        existed = name in self.counters
        if not existed:
            self.counters[name] = start
        value = self.counters[name]
        self.counters[name] += 1
        if self.trace_calls:
            self.seq_calls.append({
                "name": name,
                "start": start,
                "value": value,
                "existed": existed,
            })
        return value

    # -- 公开方法 --------------------------------------------------------

    def render_value(self, value: Any) -> Any:
        """渲染任意数据结构；整体是单个 {{ expr }} 时保留原生类型。"""
        if isinstance(value, str):
            return self._render_str(value)
        if isinstance(value, list):
            return [self.render_value(v) for v in value]
        if isinstance(value, dict):
            return {k: self.render_value(v) for k, v in value.items()}
        return value

    def render_template(self, text: str) -> str:
        return self.env.from_string(text).render(**self.context)

    # -- 内部 ------------------------------------------------------------

    def _render_str(self, text: str) -> Any:
        stripped = text.strip()
        expr = _EXPR_ONLY_RE.match(stripped)
        if expr is not None:
            # 整个值就是一个表达式：返回原生 Python 对象
            try:
                compiled = self.env.compile_expression(expr.group(1), undefined_to_none=False)
                result = compiled(**self.context)
            except UndefinedError as exc:
                raise RenderError(str(exc)) from exc
            except Exception as exc:
                raise RenderError(f"表达式渲染失败: {exc}") from exc
            # StrictUndefined 在整体表达式取值时不会自动抛错（仅在被使用时），
            # 这里显式令其与嵌入式模板一样报未定义，避免返回不可序列化的对象
            if isinstance(result, StrictUndefined):
                try:
                    result._fail_with_undefined_error()
                except UndefinedError as exc:
                    raise RenderError(str(exc)) from exc
            return result
        try:
            return self.env.from_string(text).render(**self.context)
        except UndefinedError as exc:
            raise RenderError(str(exc)) from exc
        except Exception as exc:
            raise RenderError(f"模板渲染失败: {exc}") from exc
