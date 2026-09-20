"""会话管理接口：创建 / 查询状态 / 重置 / 事件历史 / 单请求与请求链预演。"""
from __future__ import annotations

import json
import secrets
import sqlite3
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from ..db import dumps, get_conn, write_tx
from ..engine import MAX_PREVIEW_STEPS, SimAbort, preview_chain, preview_session
from ..rng import SessionRng
from ..store import store

router = APIRouter(prefix="/__sessions", tags=["sessions"])


def _validate_seed_value(value: Any) -> int | str | None:
    """seed 只接受整数（bool 除外）或非空字符串；null 表示不播种。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("seed 只能是整数或字符串")
    if isinstance(value, str) and not value.strip():
        raise ValueError("seed 不能是空字符串")
    return value


class CreateSessionBody(BaseModel):
    token: str | None = Field(default=None, description="不传则自动生成")
    scenario: str | None = None
    state: str | None = None
    context: dict = Field(default_factory=dict)
    seed: Any = Field(
        default=None,
        description="可复现随机序列种子（整数或字符串）；传入后从头初始化，"
                    "同种子/同初始状态/同请求链下 uuid4()、random_str()、"
                    "randint()、choice() 结果一致；不传则保持非确定性随机行为",
    )

    @field_validator("seed")
    @classmethod
    def _validate_seed(cls, value: Any) -> int | str | None:
        return _validate_seed_value(value)


def _session_row(token: str):
    return get_conn().execute(
        """
        SELECT s.token, s.scenario, s.revision, s.state, s.context,
               s.rand_seed, s.rand_state, s.rand_draws,
               s.created_at, s.updated_at
        FROM sessions s WHERE s.token = ?
        """,
        (token,),
    ).fetchone()


def _serialize(row) -> dict:
    return {
        "token": row["token"],
        "scenario": row["scenario"],
        "revision": row["revision"],
        "state": row["state"],
        "context": json.loads(row["context"]),
        # 随机状态 blob 不对外暴露；仅暴露是否播种、种子与已抽样步数
        "random": SessionRng.from_persisted(
            row["rand_seed"], row["rand_state"], row["rand_draws"]).describe(),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.post("")
async def create_session(body: CreateSessionBody) -> JSONResponse:
    token = body.token or f"sess-{secrets.token_hex(12)}"

    scenario_name = body.scenario
    if scenario_name is None:
        names = store.names()
        if len(names) == 1:
            scenario_name = names[0]
        elif not names:
            return JSONResponse(status_code=400, content={"error": "no_scenario_loaded"})
        else:
            return JSONResponse(status_code=409, content={
                "error": "ambiguous_scenario", "scenarios": names,
                "detail": "存在多个场景，请在 body.scenario 中指定",
            })

    found = store.get_current(scenario_name)
    if found is None:
        return JSONResponse(status_code=404, content={"error": "scenario_not_found",
                                              "scenario": scenario_name})
    scenario, revision = found  # 绑定此刻的当前 revision，之后不再随发布变化

    state = body.state or scenario.initial_state
    if state not in scenario.states:
        return JSONResponse(status_code=422, content={"error": "unknown_state",
                                              "state": state, "states": scenario.states})

    context = dict(body.context)
    context.setdefault("_seq", {})

    # 传入 seed 即从头初始化可复现随机序列（状态在会话行内随事务推进）
    rng = SessionRng.fresh(body.seed)

    with write_tx() as conn:
        try:
            conn.execute(
                "INSERT INTO sessions(token, scenario, revision, state, context,"
                " rand_seed, rand_state, rand_draws)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (token, scenario_name, revision, state, dumps(context),
                 rng.seed_blob(), rng.state_blob(), rng.draws),
            )
        except sqlite3.IntegrityError:
            return JSONResponse(status_code=409, content={"error": "session_exists", "token": token})

    row = _session_row(token)
    return JSONResponse(status_code=201, content=_serialize(row))


@router.get("/{token}")
async def get_session(token: str) -> JSONResponse:
    row = _session_row(token)
    if row is None:
        return JSONResponse(status_code=404, content={"error": "session_not_found", "token": token})
    result = _serialize(row)
    # 可用状态与路由按会话绑定的 revision 给出，而非当前版本
    scenario = store.get(row["scenario"], row["revision"])
    if scenario is not None:
        result["available_states"] = scenario.states
        result["routes"] = [
            {"id": r.id, "path": r.path, "methods": r.methods}
            for r in scenario.routes
        ]
    return JSONResponse(result)


@router.get("/{token}/events")
async def list_events(token: str, limit: int = 50) -> JSONResponse:
    if _session_row(token) is None:
        return JSONResponse(status_code=404, content={"error": "session_not_found", "token": token})
    limit = max(1, min(limit, 500))
    rows = get_conn().execute(
        """
        SELECT id, revision, method, path, route, rule, from_state, to_state,
               status_code, created_at
        FROM events WHERE token = ? ORDER BY id DESC LIMIT ?
        """,
        (token, limit),
    ).fetchall()
    return JSONResponse({"token": token, "events": [dict(r) for r in rows]})


class ResetBody(BaseModel):
    state: str | None = None
    context: dict | None = None
    seed: Any = Field(
        default=None,
        description="传入则用该种子从头初始化随机序列；显式传 null 清除播种、"
                    "恢复非确定性随机；字段缺省（不传）时保留会话当前随机序列",
    )

    @field_validator("seed")
    @classmethod
    def _validate_seed(cls, value: Any) -> int | str | None:
        return _validate_seed_value(value)


_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


class PreviewBody(BaseModel):
    method: str | None = Field(default=None, description="HTTP 方法，默认 GET")
    path: str = Field(description="业务路径，如 /orders/42/pay（不含 query）")
    query: dict | None = Field(default=None, description="查询参数（多值取最后一个）")
    headers: dict | None = Field(default=None, description="请求头，键名会归一化为小写")
    body: Any = Field(default=None, description="请求体；与真实 JSON 请求一致，缺省视为空对象 {}")

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not isinstance(value, str) or not value.startswith("/"):
            raise ValueError("path 必须是以 / 开头的字符串")
        return value

    @field_validator("method")
    @classmethod
    def _validate_method(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.upper()
            if value not in _ALLOWED_METHODS:
                raise ValueError(f"不支持的 HTTP 方法: {value}")
        return value

    @field_validator("headers")
    @classmethod
    def _validate_headers(cls, value: dict | None) -> dict | None:
        if value is None:
            return None
        if not all(isinstance(k, str) for k in value):
            raise ValueError("headers 的键必须是字符串")
        return {k.lower(): _stringify(v) for k, v in value.items()}

    @field_validator("query")
    @classmethod
    def _validate_query(cls, value: dict | None) -> dict | None:
        if value is None:
            return None
        if not all(isinstance(k, str) for k in value):
            raise ValueError("query 的键必须是字符串")
        return {k: _stringify(v) for k, v in value.items()}


def _stringify(value: Any) -> str:
    """真实 query/header 的值都是字符串；dict/list 退化为 JSON 文本。"""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


@router.post("/{token}/preview")
async def preview_request(token: str, body: PreviewBody) -> JSONResponse:
    """**请求预演**：基于当前会话快照复用真实匹配/渲染逻辑，返回拟响应与差异，不落库。"""
    method = body.method or "GET"
    query = body.query or {}
    headers = body.headers or {}
    req_body = body.body if body.body is not None else {}

    try:
        status_code, payload = preview_session(
            token=token,
            method=method,
            path=body.path,
            query=query,
            headers=headers,
            body=req_body,
            default_scenario=None,
        )
    except SimAbort as exc:
        # 与真实请求一致的状态码和错误详情（404 无路由 / 405 方法不允许 /
        # 410 场景已卸载 / 500 渲染错误等），并附判定轨迹
        payload = dict(exc.body)
        if exc.trace is not None:
            payload["trace"] = exc.trace
        payload["preview"] = {"persisted": False}
        status_code = exc.status_code
    return JSONResponse(status_code=status_code, content=payload)


class ChainPreviewBody(BaseModel):
    requests: list[PreviewBody] = Field(
        description="有序业务请求数组；从会话当前快照起依次执行，"
                    f"后一步看到前一步拟产生的变化，最多 {MAX_PREVIEW_STEPS} 步",
        json_schema_extra={"maxItems": MAX_PREVIEW_STEPS},
    )

    @field_validator("requests")
    @classmethod
    def _validate_requests(cls, value: list[PreviewBody]) -> list[PreviewBody]:
        if len(value) > MAX_PREVIEW_STEPS:
            raise ValueError(
                f"连续预演最多 {MAX_PREVIEW_STEPS} 步，收到 {len(value)} 步")
        return value


@router.post("/{token}/preview/chain")
async def preview_request_chain(token: str, body: ChainPreviewBody) -> JSONResponse:
    """**连续请求链预演**：有序请求基于同一会话快照依次试跑，不落库。

    每一步复用与单次预演/真实请求完全相同的匹配与渲染语义，并能看到此前
    各步拟产生的 state/context/seq 变化；任一步路由、规则或渲染失败立即
    停止并返回失败步骤下标与此前结果。全程不写 sessions/events/计数。
    """
    requests = [
        {
            "method": step.method or "GET",
            "path": step.path,
            "query": step.query or {},
            "headers": step.headers or {},
            "body": step.body if step.body is not None else {},
        }
        for step in body.requests
    ]

    try:
        status_code, payload = preview_chain(token=token, requests=requests)
    except SimAbort as exc:
        # 与真实请求一致的状态码和错误详情（410 场景已卸载等），并附判定轨迹
        payload = dict(exc.body)
        if exc.trace is not None:
            payload["trace"] = exc.trace
        payload["preview"] = {"persisted": False, "chain": True}
        status_code = exc.status_code
    return JSONResponse(status_code=status_code, content=payload)


@router.post("/{token}/reset")
async def reset_session(token: str, body: ResetBody) -> JSONResponse:
    row = _session_row(token)
    if row is None:
        return JSONResponse(status_code=404, content={"error": "session_not_found", "token": token})
    # 重置不改变会话绑定的 revision：状态合法性按绑定版本校验
    scenario = store.get(row["scenario"], row["revision"])
    if scenario is None:
        return JSONResponse(status_code=410, content={"error": "scenario_unloaded"})

    state = body.state or scenario.initial_state
    if state not in scenario.states:
        return JSONResponse(status_code=422, content={"error": "unknown_state",
                                              "state": state, "states": scenario.states})
    context = body.context if body.context is not None else {}
    context.setdefault("_seq", {})

    # seed 语义：字段缺省 -> 保留当前随机序列；显式 null -> 清除播种；
    # 提供整数/字符串 -> 用该种子从头初始化
    if "seed" in body.model_fields_set:
        rng = SessionRng.fresh(body.seed)  # None 即恢复未播种的全局随机行为
    else:
        rng = SessionRng.from_persisted(row["rand_seed"], row["rand_state"],
                                        row["rand_draws"])

    with write_tx() as conn:
        conn.execute(
            "UPDATE sessions SET state = ?, context = ?, rand_seed = ?,"
            " rand_state = ?, rand_draws = ?, updated_at = datetime('now')"
            " WHERE token = ?",
            (state, dumps(context), rng.seed_blob(), rng.state_blob(),
             rng.draws, token),
        )
        conn.execute("DELETE FROM events WHERE token = ?", (token,))
    return JSONResponse(_serialize(_session_row(token)))


@router.delete("/{token}")
async def delete_session(token: str) -> JSONResponse:
    with write_tx() as conn:
        cur = conn.execute("DELETE FROM events WHERE token = ?", (token,))
        cur2 = conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    if cur2.rowcount == 0:
        return JSONResponse(status_code=404, content={"error": "session_not_found", "token": token})
    return JSONResponse({"deleted": True, "token": token})
