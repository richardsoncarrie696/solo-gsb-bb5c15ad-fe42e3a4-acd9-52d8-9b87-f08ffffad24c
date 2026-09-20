"""SQLite 连接、建表与事务封装。

使用 WAL 提升读并发；写事务统一采用 ``BEGIN IMMEDIATE``，
配合 busy_timeout 与锁冲突重试，保证“选匹配 / 渲染 / 推状态”
在同一事务中串行执行，避免同一会话的并发请求重复触发迁移。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS scenarios (
    name             TEXT PRIMARY KEY,
    current_revision INTEGER,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scenario_revisions (
    scenario    TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    definition  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (scenario, revision),
    FOREIGN KEY (scenario) REFERENCES scenarios(name)
);

CREATE TABLE IF NOT EXISTS sessions (
    token            TEXT PRIMARY KEY,
    scenario         TEXT NOT NULL,
    revision         INTEGER,
    state            TEXT NOT NULL,
    context          TEXT NOT NULL,
    rand_seed        TEXT,
    rand_state       TEXT,
    rand_draws       INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (scenario) REFERENCES scenarios(name)
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token       TEXT NOT NULL,
    scenario    TEXT NOT NULL,
    revision    INTEGER,
    method      TEXT NOT NULL,
    path        TEXT NOT NULL,
    route       TEXT,
    rule        TEXT,
    from_state  TEXT,
    to_state    TEXT,
    status_code INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (token) REFERENCES sessions(token)
);

CREATE INDEX IF NOT EXISTS idx_events_token ON events(token, id);
"""

_local = threading.local()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.db_path,
        timeout=settings.busy_timeout_ms / 1000.0,
        isolation_level=None,  # 手动控制事务
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {settings.busy_timeout_ms}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_conn() -> sqlite3.Connection:
    conn: sqlite3.Connection | None = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


def init_db() -> None:
    import os

    directory = os.path.dirname(settings.db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = get_conn()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """把 revision 化之前的旧库就地升级，已有数据全部保留。

    - scenarios 增加 current_revision；旧定义原样固化为 revision 1 并置为当前版本；
    - sessions 增加 revision，既有会话回填为其场景当时的当前版本（迁移后均为 1）；
    - events 增加 revision（历史事件无法回溯版本，保留 NULL）；
    - sessions 增加 rand_seed/rand_state/rand_draws（可复现随机序列，旧会话不播种）。
    """
    scenario_cols = _column_names(conn, "scenarios")
    if "current_revision" not in scenario_cols:
        conn.execute("ALTER TABLE scenarios ADD COLUMN current_revision INTEGER")
        if "definition" in scenario_cols:  # 旧表内联保存定义：迁移为不可变的 revision 1
            for row in conn.execute("SELECT name, definition, created_at FROM scenarios"):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO scenario_revisions(scenario, revision, definition, created_at)
                    VALUES (?, 1, ?, ?)
                    """,
                    (row["name"], row["definition"], row["created_at"]),
                )
        conn.execute("UPDATE scenarios SET current_revision = 1 WHERE current_revision IS NULL")

    if "revision" not in _column_names(conn, "sessions"):
        conn.execute("ALTER TABLE sessions ADD COLUMN revision INTEGER")
        conn.execute(
            """
            UPDATE sessions SET revision = (
                SELECT current_revision FROM scenarios
                WHERE scenarios.name = sessions.scenario
            ) WHERE revision IS NULL
            """
        )

    if "revision" not in _column_names(conn, "events"):
        conn.execute("ALTER TABLE events ADD COLUMN revision INTEGER")

    _migrate_session_rng(conn)


def _migrate_session_rng(conn: sqlite3.Connection) -> None:
    """会话可复现随机序列的三列就地升级（旧会话保持未播种的全局随机行为）。"""
    session_cols = _column_names(conn, "sessions")
    if "rand_seed" not in session_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN rand_seed TEXT")
    if "rand_state" not in session_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN rand_state TEXT")
    if "rand_draws" not in session_cols:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN rand_draws INTEGER NOT NULL DEFAULT 0"
        )


class WriteLocked(sqlite3.OperationalError):
    """重试次数耗尽后仍拿不到写锁。"""


@contextmanager
def read_tx() -> Iterator[sqlite3.Connection]:
    """打开一个只读快照事务，退出时始终回滚。

    供请求预演使用：块内绝不应发起写操作；即使误写或抛出
    ``commit_on_abort`` 异常，回滚也保证不产生任何持久化副作用。
    """
    conn = get_conn()
    conn.execute("BEGIN")  # 延迟事务：首次读时建立一致性快照，不获取写锁
    try:
        yield conn
    finally:
        conn.rollback()


@contextmanager
def write_tx(retries: int | None = None) -> Iterator[sqlite3.Connection]:
    """打开一个 IMMEDIATE 写事务，锁冲突时重试。

    块内抛出的异常若带有 ``commit_on_abort = True`` 属性，则提交事务
    （用于“会话已建立但无匹配规则”这类需要落库并返回错误码的场景）。
    """
    conn = get_conn()
    attempts = settings.db_retries if retries is None else retries
    for attempt in range(attempts + 1):
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception as exc:
                if getattr(exc, "commit_on_abort", False):
                    conn.commit()
                else:
                    conn.rollback()
                raise
            else:
                conn.commit()
                return
        except sqlite3.OperationalError as exc:  # database is locked
            if "locked" not in str(exc).lower() or attempt == attempts:
                raise WriteLocked(str(exc)) from exc
            time.sleep(0.02 * min(attempt + 1, 10))
    raise WriteLocked("unreachable")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
