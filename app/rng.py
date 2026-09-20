"""会话级可复现随机序列。

会话在创建或 reset 时传入 ``seed`` 后，模板函数 ``uuid4()`` /
``random_str()`` / ``randint()`` / ``choice()`` 不再使用进程全局随机源，
而是推进该会话私有的 ``random.Random`` 实例；其内部状态与已抽样步数
（``draws``，每次函数调用计 1 步）持久化在 ``sessions`` 表中，随真实
请求的写事务一并提交或回滚：

- 真实请求成功 → 随机状态在同一事务内推进；
- 真实请求失败（无路由 / 无命中规则 / 渲染异常）→ 事务回滚，序列不消耗；
- 预演（单次 / 请求链）→ 从持久化快照**克隆**一份随机状态使用，
  链内后一步看得到前一步的消耗，但预演结束真实序列不变；
- 不传 seed 时持有方不建立私有随机源，模板函数继续使用进程全局
  ``random`` / ``uuid``，行为与接口与旧版本完全一致。
"""
from __future__ import annotations

import json
import random
import string
import uuid
from typing import Any

_ALPHABET = string.ascii_lowercase + string.digits


def encode_state(rng: random.Random) -> str:
    """把 random.Random 的内部状态序列化为 JSON 文本。"""
    version, internalstate, gauss_next = rng.getstate()
    return json.dumps([version, list(internalstate), gauss_next])


def decode_state(blob: str) -> tuple[int, tuple, Any]:
    """encode_state 的逆操作，恢复出可传给 Random.setstate 的元组。"""
    version, internalstate, gauss_next = json.loads(blob)
    return version, tuple(internalstate), gauss_next


class SessionRng:
    """单个会话的随机序列持有方；一次请求渲染期间被原地推进。

    ``rng`` 为 None 表示未播种的真实请求：直接委托进程全局随机源
    （含 ``uuid.uuid4()``），保持历史行为。预演未播种会话时也会
    克隆一份全局状态到本地 Random，因此预演本身不扰动全局序列。
    """

    def __init__(self, *, seeded: bool, seed: int | str | None,
                 rng: random.Random | None, draws: int = 0):
        self.seeded = seeded
        self.seed = seed
        self.rng = rng
        self.draws = draws

    # -- 构造 ------------------------------------------------------------

    @classmethod
    def fresh(cls, seed: int | str | None = None) -> "SessionRng":
        """创建/重置会话时使用：传入 seed 即从头初始化随机序列。"""
        if seed is None:
            return cls(seeded=False, seed=None, rng=None, draws=0)
        rng = random.Random()
        rng.seed(seed)
        return cls(seeded=True, seed=seed, rng=rng, draws=0)

    @classmethod
    def from_persisted(cls, seed_blob: str | None, state_blob: str | None,
                       draws: int | None) -> "SessionRng":
        """从 sessions 行恢复；旧库/未播种会话的空列退回全局随机行为。"""
        if seed_blob is None:
            return cls(seeded=False, seed=None, rng=None, draws=draws or 0)
        seed = json.loads(seed_blob)
        rng = random.Random()
        if state_blob:
            rng.setstate(decode_state(state_blob))
        else:  # 兜底：有种子却缺状态（异常旧数据）时从头播种
            rng.seed(seed)
        return cls(seeded=True, seed=seed, rng=rng, draws=draws or 0)

    def clone(self) -> "SessionRng":
        """克隆出预演专用副本：与真实序列隔离，但起点完全相同。"""
        if self.rng is not None:
            cloned = random.Random()
            cloned.setstate(self.rng.getstate())
        else:
            # 未播种的真实会话：快照一份全局随机状态，避免预演扰动真实序列
            cloned = random.Random()
            cloned.setstate(random.getstate())
        return SessionRng(seeded=self.seeded, seed=self.seed, rng=cloned,
                          draws=self.draws)

    # -- 持久化辅助 ------------------------------------------------------

    def seed_blob(self) -> str | None:
        return json.dumps(self.seed, ensure_ascii=False) if self.seeded else None

    def state_blob(self) -> str | None:
        return encode_state(self.rng) if self.rng is not None else None

    def describe(self) -> dict[str, Any]:
        return {"seeded": self.seeded, "seed": self.seed, "draws": self.draws}

    # -- 模板函数（每调用一次抽样步数 +1）--------------------------------

    def _tick(self) -> None:
        self.draws += 1

    def uuid4(self) -> str:
        self._tick()
        if self.rng is not None:
            return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))
        return str(uuid.uuid4())

    def random_str(self, length: int = 12, prefix: str = "") -> str:
        length = max(0, int(length))
        self._tick()
        source = self.rng if self.rng is not None else random
        chars = "".join(source.choices(_ALPHABET, k=length))
        return f"{prefix}{chars}"

    def randint(self, a: int, b: int) -> int:
        self._tick()
        source = self.rng if self.rng is not None else random
        return source.randint(a, b)

    def choice(self, values: list[Any]) -> Any:
        self._tick()
        source = self.rng if self.rng is not None else random
        return source.choice(values)
