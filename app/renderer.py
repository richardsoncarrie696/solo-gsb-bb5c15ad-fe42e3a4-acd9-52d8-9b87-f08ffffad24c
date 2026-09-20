"""Jinja2 沙箱渲染，同时提供模板辅助函数。"""
from __future__ import annotations

import random
import re
import string
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from jinja2 import StrictUndefined
from jinja2.exceptions import UndefinedError
from jinja2.sandbox import SandboxedEnvironment

if TYPE_CHECKING:
    from .rng import SessionRng

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


class Renderer:
    """每个请求一个实例：携带 seq 计数器、完整上下文与（可选的）会话随机源。

    传入 ``session_rng`` 时，``uuid4()`` / ``random_str()`` / ``randint()`` /
    ``choice()`` 推进该会话的可复现随机序列并累计抽样步数；不传时使用进程
    全局随机源，保持旧行为。
    """

    def __init__(self, context: dict[str, Any], *, trace_calls: bool = False,
                 session_rng: "SessionRng | None" = None):
        self.context = dict(context)
        self.counters: dict[str, int] = dict(self.context.get("_seq") or {})
        # 预演时记录本次渲染中 seq() 的调用轨迹（计数器仍只存在于本实例内存中）
        self.trace_calls = trace_calls
        self.seq_calls: list[dict[str, Any]] = []
        self.session_rng = session_rng

        if session_rng is not None:
            uuid4 = session_rng.uuid4
            random_str = session_rng.random_str
            randint = session_rng.randint
            choice = session_rng.choice
        else:
            uuid4 = lambda: str(uuid.uuid4())
            random_str = _random_str
            randint = random.randint
            choice = _choice

        self.env = SandboxedEnvironment(
            undefined=StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
        )
        self.env.globals.update(
            uuid4=uuid4,
            now=_now,
            random_str=random_str,
            randint=randint,
            choice=choice,
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
