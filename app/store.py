"""已编译场景的进程内缓存（单 worker 部署即权威缓存）。

场景定义按 ``(name, revision)`` 不可变发布：每次 PUT 校验通过后产生递增
revision 并置为当前版本，旧版本定义保留在 ``scenario_revisions`` 表中。
会话在创建时绑定当时的当前 revision，之后始终按该版本执行；因此缓存以
``(name, revision)`` 为键，旧版本按需从数据库惰性编译。
"""
from __future__ import annotations

import threading
from typing import Iterable

from .db import get_conn, write_tx
from .models import Scenario
from .scenario import parse_scenario


class ScenarioStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._compiled: dict[tuple[str, int], Scenario] = {}
        self._current: dict[str, int] = {}  # name -> 当前（默认）revision

    def load_all_from_db(self) -> int:
        conn = get_conn()
        rows = conn.execute("SELECT name, current_revision FROM scenarios").fetchall()
        with self._lock:
            self._current = {r["name"]: r["current_revision"] for r in rows}
            self._compiled.clear()  # 之后按需从 scenario_revisions 惰性编译
        return len(rows)

    def publish(self, name: str, definition: str) -> tuple[Scenario, int, str]:
        """校验并发布新 revision，返回 (编译结果, revision, action)。

        action 为 ``created``（新场景）/ ``published``（新 revision）/
        ``unchanged``（提交内容与当前版本完全相同，幂等返回原 revision，
        不产生新版本）。revision 递增与当前版本切换在同一写事务中完成。
        """
        scenario = parse_scenario(definition, name_override=name)  # 先校验，失败不落库
        conn = get_conn()
        with write_tx() as tx:
            row = tx.execute(
                "SELECT current_revision FROM scenarios WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                revision, action = 1, "created"
                tx.execute(
                    "INSERT INTO scenarios(name, current_revision, created_at, updated_at)"
                    " VALUES (?, ?, datetime('now'), datetime('now'))",
                    (name, revision),
                )
                tx.execute(
                    "INSERT INTO scenario_revisions(scenario, revision, definition)"
                    " VALUES (?, ?, ?)",
                    (name, revision, definition),
                )
            else:
                current = row["current_revision"]
                current_def = tx.execute(
                    "SELECT definition FROM scenario_revisions"
                    " WHERE scenario = ? AND revision = ?",
                    (name, current),
                ).fetchone()["definition"]
                if current_def == definition:
                    revision, action = current, "unchanged"
                else:
                    # 激活过旧版本时 current 可能小于 max，revision 始终取 max+1
                    max_rev = tx.execute(
                        "SELECT MAX(revision) AS m FROM scenario_revisions WHERE scenario = ?",
                        (name,),
                    ).fetchone()["m"] or 0
                    revision, action = max_rev + 1, "published"
                    tx.execute(
                        "INSERT INTO scenario_revisions(scenario, revision, definition)"
                        " VALUES (?, ?, ?)",
                        (name, revision, definition),
                    )
                    tx.execute(
                        "UPDATE scenarios SET current_revision = ?, updated_at = datetime('now')"
                        " WHERE name = ?",
                        (revision, name),
                    )
        with self._lock:
            self._compiled[(name, revision)] = scenario
            self._current[name] = revision
        return scenario, revision, action

    def activate(self, name: str, revision: int) -> bool:
        """把指定 revision 置为当前版本（只影响之后新建的会话）。

        场景或 revision 不存在时返回 False（路由层转为 404）。
        """
        conn = get_conn()
        with write_tx() as tx:
            exists = tx.execute(
                "SELECT 1 FROM scenario_revisions WHERE scenario = ? AND revision = ?",
                (name, revision),
            ).fetchone()
            if exists is None:
                return False
            tx.execute(
                "UPDATE scenarios SET current_revision = ?, updated_at = datetime('now')"
                " WHERE name = ?",
                (revision, name),
            )
        with self._lock:
            self._current[name] = revision
        return True

    def delete(self, name: str) -> bool:
        conn = get_conn()
        with write_tx() as tx:
            tx.execute("DELETE FROM scenario_revisions WHERE scenario = ?", (name,))
            cur = tx.execute("DELETE FROM scenarios WHERE name = ?", (name,))
        with self._lock:
            existed = self._current.pop(name, None) is not None
            self._compiled = {k: v for k, v in self._compiled.items() if k[0] != name}
        return cur.rowcount > 0 or existed

    def get(self, name: str, revision: int | None = None) -> Scenario | None:
        """取指定 revision 的编译结果；revision 缺省为当前版本。"""
        with self._lock:
            if revision is None:
                revision = self._current.get(name)
                if revision is None:
                    return None
            key = (name, revision)
            cached = self._compiled.get(key)
        if cached is not None:
            return cached
        row = get_conn().execute(
            "SELECT definition FROM scenario_revisions WHERE scenario = ? AND revision = ?",
            (name, revision),
        ).fetchone()
        if row is None:
            return None
        scenario = parse_scenario(row["definition"], name_override=name)
        with self._lock:
            self._compiled[key] = scenario
        return scenario

    def get_current(self, name: str) -> tuple[Scenario, int] | None:
        """原子地取 (当前版本编译结果, 当前 revision)。"""
        with self._lock:
            revision = self._current.get(name)
        if revision is None:
            return None
        scenario = self.get(name, revision)
        if scenario is None:
            return None
        return scenario, revision

    def current_revision(self, name: str) -> int | None:
        with self._lock:
            return self._current.get(name)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._current)

    def all(self) -> Iterable[Scenario]:
        with self._lock:
            items = list(self._current.items())
        return [s for s in (self.get(name, rev) for name, rev in items) if s is not None]


store = ScenarioStore()
