"""核心模拟引擎：在单个 IMMEDIATE 事务中完成匹配、渲染、状态推进。

同一会话快照还支持**预演**（``preview_session`` / ``preview_chain``）：在只读
事务中复用路由匹配、条件判定与模板渲染的同一份代码。单次预演产出拟响应与
state/context 差异；请求链预演（``preview_chain``）从同一快照出发依次执行
有序请求，后一步看到前一步拟产生的 state/context/seq 变化，任一匹配或渲染
失败即停止。两种预演全程回滚、不写任何表；即使模板调用 ``seq()``，计数器
也只在一次性的渲染器实例内自增，随后的真实请求仍看到预演前的值。随机函数
（``uuid4``/``random_str``/``randint``/``choice``）同理：预演从持久化的会话
随机状态**克隆**一份使用——链内共享该克隆（后一步看得到前一步的抽样），
预演结束真实序列不变。真实请求的随机状态则在写事务中随会话行一并提交，
失败（无路由/无规则/渲染异常）回滚，不消耗序列。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

from .db import dumps, read_tx, write_tx
from .matching import evaluate_condition, get_condition_value, match_path
from .models import Route, Rule, Scenario
from .renderer import RenderError, Renderer
from .rng import SessionRng
from .store import store


class SimAbort(Exception):
    """以指定 HTTP 响应终止；commit_on_abort=True 时提交事务。"""

    def __init__(self, status_code: int, body: Any, *, commit_on_abort: bool = False,
                 trace: list[dict[str, Any]] | None = None):
        super().__init__(json.dumps(body, ensure_ascii=False))
        self.status_code = status_code
        self.body = body
        self.commit_on_abort = commit_on_abort
        self.trace = trace


@dataclass
class SimResult:
    status_code: int
    headers: dict[str, str]
    body: Any
    delay_ms: int
    is_json: bool
    matched: dict[str, Any]


# ---------------------------------------------------------------------------
# 请求解析
# ---------------------------------------------------------------------------

async def _read_body(request) -> Any:
    raw_bytes = await request.body()
    raw = raw_bytes.decode("utf-8") if raw_bytes else ""
    if not raw:
        return {}
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type or raw.lstrip().startswith(("{", "[")):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SimAbort(400, {"error": "invalid_json", "detail": str(exc)}) from exc
    if "application/x-www-form-urlencoded" in content_type:
        return dict(parse_qsl(raw, keep_blank_values=True))
    if "yaml" in content_type:
        import yaml
        try:
            return yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            raise SimAbort(400, {"error": "invalid_yaml", "detail": str(exc)}) from exc
    return {"_raw": raw}


def _select_scenario(explicit: str | None, default_scenario: str | None) -> tuple[Scenario, int]:
    """选出 (当前版本的场景, 当前 revision)，用于新会话绑定。"""
    if explicit:
        found = store.get_current(explicit)
        if found is None:
            raise SimAbort(404, {"error": "scenario_not_found", "scenario": explicit})
        return found
    if default_scenario:
        found = store.get_current(default_scenario)
        if found is None:
            raise SimAbort(400, {"error": "no_scenario_bound",
                                 "detail": "会话未指定场景且默认场景不存在"})
        return found
    names = store.names()
    if len(names) == 1:
        return store.get_current(names[0])  # type: ignore[return-value]
    if not names:
        raise SimAbort(400, {"error": "no_scenario_loaded",
                             "detail": "请先通过 /__scenarios 载入场景，或创建会话时指定 scenario"})
    raise SimAbort(409, {"error": "ambiguous_scenario",
                         "detail": "存在多个场景，请在创建会话时指定 scenario",
                         "scenarios": names})


# ---------------------------------------------------------------------------
# 路由匹配与规则判定（真实请求与预演共用；预演时额外产出判定轨迹）
# ---------------------------------------------------------------------------

def _match_routes(scenario: Scenario, method: str, path: str) -> tuple[
        Route, dict[str, str], list[dict[str, Any]]]:
    """返回 (命中路由, 路径参数, 路由判定轨迹)。"""
    trace: list[dict[str, Any]] = []
    candidates: list[tuple[Route, dict[str, str]]] = []
    for route in scenario.routes:
        params = match_path(route.path_re, path)
        if params is not None:
            candidates.append((route, params))
            trace.append({
                "stage": "route",
                "route": route.id,
                "path": route.path,
                "methods": route.methods,
                "path_match": True,
                "path_params": params,
                "method_match": method in route.methods,
            })

    if not candidates:
        raise SimAbort(404, {"error": "no_route", "path": path}, trace=trace)

    method_matches = [c for c in candidates if method in c[0].methods]
    if not method_matches:
        allowed = sorted({m for r, _ in candidates for m in r.methods})
        raise SimAbort(405, {"error": "method_not_allowed", "allowed": allowed},
                       trace=trace)

    trace.append({"stage": "route_selected", "route": method_matches[0][0].id})
    return method_matches[0][0], method_matches[0][1], trace


def _condition_trace(cond, bag: dict[str, Any]) -> dict[str, Any]:
    present, actual = get_condition_value(cond.source, cond.path, bag)
    try:
        passed = evaluate_condition(cond, actual, present)
        error = None
    except ValueError as exc:  # 未知运算符等，载入时已校验，这里仅兜底
        passed = False
        error = str(exc)
    return {
        "source": cond.source,
        "path": cond.path,
        "op": cond.op,
        "expected": cond.value,
        "present": present,
        "actual": actual,
        "passed": passed,
        **({"error": error} if error else {}),
    }


def evaluate_rules_traced(rule_list: list[Rule], bag: dict[str, Any]) -> tuple[
        Rule | None, list[dict[str, Any]]]:
    """按 priority 降序逐条判定规则，返回 (命中规则或 None, 逐条件轨迹)。

    与真实引擎的短路语义完全一致：某规则首个失败条件之后的条件不再判定。
    """
    trace: list[dict[str, Any]] = []
    matched = False
    for rule in rule_list:  # 已按 priority 降序排序
        entry: dict[str, Any] = {
            "stage": "rule",
            "rule": rule.name,
            "priority": rule.priority,
            "matched": False,
            "conditions": [],
        }
        if matched:
            # 已有更高优先级规则命中：真实引擎同样不会再看后续规则
            entry["skipped"] = True
            trace.append(entry)
            continue

        conditions_ok = True
        for cond in rule.conditions:
            cond_trace = _condition_trace(cond, bag)
            entry["conditions"].append(cond_trace)
            if not cond_trace["passed"]:
                conditions_ok = False
                break  # 与真实引擎一致：首个不满足条件即短路
        entry["matched"] = conditions_ok
        trace.append(entry)
        if conditions_ok:
            matched = True

    hit = next((r for r, e in zip(rule_list, trace)
                if not e.get("skipped") and e["matched"]), None)
    return hit, trace


# ---------------------------------------------------------------------------
# 响应渲染与状态推进规划（不触碰数据库；真实/预演共用）
# ---------------------------------------------------------------------------

def _render_response(rule: Rule, renderer: Renderer) -> tuple[int, dict[str, str], Any, bool]:
    spec = rule.response
    status_value = renderer.render_value(spec.get("status", 200))
    try:
        status = int(str(status_value).strip())
    except (TypeError, ValueError) as exc:
        raise SimAbort(500, {"error": "bad_rendered_status", "value": status_value}) from exc
    if not 100 <= status <= 599:
        raise SimAbort(500, {"error": "bad_rendered_status", "value": status})

    headers = {str(k): str(renderer.render_value(v))
               for k, v in (spec.get("headers") or {}).items()}

    if "body" not in spec:
        return status, headers, None, False

    body = renderer.render_value(spec["body"])
    explicit_json = any(k.lower() == "content-type" and "json" in v.lower()
                        for k, v in headers.items())
    is_json = explicit_json or isinstance(body, (dict, list))
    return status, headers, body, is_json


@dataclass
class _Planned:
    """命中规则后的渲染结果与拟推进状态——尚未持久化。"""
    scenario_name: str
    route: Route
    rule: Rule
    path_params: dict[str, str]
    status_code: int
    headers: dict[str, str]
    body: Any
    is_json: bool
    delay_ms: int
    state: str
    next_state: str
    context: dict[str, Any]
    new_context: dict[str, Any]
    counters: dict[str, int]
    seq_calls: list[dict[str, Any]]
    draws_before: int
    draws_after: int
    trace: list[dict[str, Any]]


def _plan_matched(scenario: Scenario, scenario_name: str, route: Route,
                  path_params: dict[str, str], rule: Rule, *, state: str,
                  context: dict[str, Any], method: str, path: str,
                  body: Any, query: dict[str, Any], headers: dict[str, Any],
                  rng: SessionRng, trace: list[dict[str, Any]]) -> _Planned:
    render_ctx = dict(context)
    render_ctx.update({
        "state": state,
        "body": body,
        "query": query,
        "header": headers,
        "path": path_params,
        "request": {"method": method, "path": path, "route": route.id},
        "context": dict(context),
    })
    # 预演（preview=True）时记录 seq() 调用；计数器在任何情况下都只存于本实例。
    # 随机函数（uuid4/random_str/randint/choice）统一推进会话级 rng：
    # 真实请求提交时随事务持久化，预演时 rng 是快照克隆，不会影响真实序列。
    draws_before = rng.draws
    renderer = Renderer(render_ctx, trace_calls=True, session_rng=rng)

    try:
        status, resp_headers, resp_body, is_json = _render_response(rule, renderer)
        updates = renderer.render_value(rule.set) if rule.set else {}
        next_state = state
        if rule.next_state is not None:
            next_state = renderer.render_template(rule.next_state).strip()
            if next_state not in scenario.states:
                raise SimAbort(500, {"error": "render_error",
                                     "detail": f"规则 {rule.name} 渲染后的状态 {next_state!r} 未声明"})
    except RenderError as exc:
        raise SimAbort(500, {"error": "render_error",
                             "rule": rule.name, "detail": str(exc)},
                        trace=trace) from exc

    if not isinstance(updates, dict):
        raise SimAbort(500, {"error": "render_error",
                             "detail": f"规则 {rule.name} 的 set 渲染结果必须是对象"},
                       trace=trace)

    new_context = dict(context)
    new_context.update(updates)
    new_context["_seq"] = renderer.counters

    return _Planned(
        scenario_name=scenario_name,
        route=route,
        rule=rule,
        path_params=path_params,
        status_code=status,
        headers=resp_headers,
        body=resp_body,
        is_json=is_json,
        delay_ms=rule.delay_ms,
        state=state,
        next_state=next_state,
        context=context,
        new_context=new_context,
        counters=dict(renderer.counters),
        seq_calls=list(renderer.seq_calls),
        draws_before=draws_before,
        draws_after=rng.draws,
        trace=trace,
    )


@dataclass
class _Snapshot:
    """一次请求所基于的会话快照（含会话级随机序列持有方）。"""
    scenario: Scenario
    scenario_name: str
    revision: int
    state: str
    context: dict[str, Any]
    rng: SessionRng
    is_new: bool


def _resolve_snapshot(conn, token: str, explicit_scenario: str | None,
                      default_scenario: str | None) -> _Snapshot:
    """解析会话快照；不存在时按真实请求的规则懒创建（仅真实写路径会落库）。

    既有会话始终返回其创建时绑定的 revision；场景之后再次发布或激活
    其他版本都不影响该会话。新会话则绑定此刻的当前 revision。
    随机序列同样从行内恢复；懒创建的新会话没有 seed，使用全局随机源。
    """
    row = conn.execute(
        "SELECT scenario, revision, state, context, rand_seed, rand_state, rand_draws"
        " FROM sessions WHERE token = ?",
        (token,),
    ).fetchone()

    if row is not None:
        scenario_name = row["scenario"]
        revision = row["revision"]
        if revision is None:  # 兜底：异常旧数据缺 revision 时按当前版本处理
            revision = store.current_revision(scenario_name)
        scenario = store.get(scenario_name, revision)
        if scenario is None:
            raise SimAbort(410, {"error": "scenario_unloaded", "scenario": scenario_name})
        return _Snapshot(
            scenario=scenario,
            scenario_name=scenario_name,
            revision=revision,
            state=row["state"],
            context=json.loads(row["context"]),
            rng=SessionRng.from_persisted(row["rand_seed"], row["rand_state"],
                                          row["rand_draws"]),
            is_new=False,
        )

    scenario, revision = _select_scenario(explicit_scenario, default_scenario)
    return _Snapshot(
        scenario=scenario,
        scenario_name=scenario.name,
        revision=revision,
        state=scenario.initial_state,
        context={},
        rng=SessionRng.fresh(None),
        is_new=True,
    )


# ---------------------------------------------------------------------------
# 真实请求主入口
# ---------------------------------------------------------------------------

async def simulate(request, default_scenario: str | None = None) -> SimResult:
    token = (request.headers.get("sim-session-token")
             or request.query_params.get("sim_session_token"))
    if not token:
        raise SimAbort(400, {"error": "missing_session_token",
                             "detail": "请通过 sim-session-token 请求头（或 sim_session_token 查询参数）提供会话令牌"})

    method = request.method
    path = request.url.path
    query = {k: v for k, v in request.query_params.multi_items()
             if k != "sim_session_token"}
    headers = {k.lower(): v for k, v in request.headers.items()}
    body = await _read_body(request)
    # 新会话首次请求时可用 sim-scenario 头指定场景；已有会话忽略
    explicit_scenario = request.headers.get("sim-scenario")

    with write_tx() as conn:
        return _run_locked(conn, token, method, path, query, headers, body,
                           default_scenario, explicit_scenario)


def _run_locked(conn, token, method, path, query, headers, body,
                default_scenario, explicit_scenario) -> SimResult:
    # 1) 查找会话与场景（不存在则按懒创建规则选场景，暂不落库）
    snap = _resolve_snapshot(conn, token, explicit_scenario, default_scenario)
    scenario, scenario_name, revision = snap.scenario, snap.scenario_name, snap.revision
    state, context, rng = snap.state, snap.context, snap.rng

    # 2) 匹配路由（不存在则回滚，不留下空会话，随机序列也不消耗）
    route, path_params, _trace = _match_routes(scenario, method, path)

    # 3) 新会话落库：绑定此刻的当前 revision，之后不再随发布变化
    if snap.is_new:
        try:
            conn.execute(
                "INSERT INTO sessions(token, scenario, revision, state, context,"
                " rand_seed, rand_state, rand_draws)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (token, scenario_name, revision, state, dumps(context),
                 rng.seed_blob(), rng.state_blob(), rng.draws),
            )
        except sqlite3.IntegrityError:
            # 并发首请求：另一事务已插入，转为锁冲突重试
            raise sqlite3.OperationalError("database is locked") from None

    # 4) 选择最高优先级匹配规则
    bag = {"state": state, "body": body, "query": query,
           "header": headers, "path": path_params}
    rule, rule_trace = evaluate_rules_traced(route.rules, bag)
    trace = _trace + rule_trace

    if rule is None:
        # 渲染尚未发生，随机状态未被触碰；commit 仅落新会话行，序列步数不变
        raise SimAbort(409, {
            "error": "no_matching_rule",
            "scenario": scenario_name,
            "revision": revision,
            "route": route.id,
            "state": state,
        }, commit_on_abort=True, trace=trace)

    # 5) 渲染响应 + 推进状态与随机序列（同一事务内；渲染异常整体回滚不消耗）
    planned = _plan_matched(scenario, scenario_name, route, path_params, rule,
                            state=state, context=context, method=method, path=path,
                            body=body, query=query, headers=headers,
                            rng=rng, trace=trace)

    conn.execute(
        "UPDATE sessions SET state = ?, context = ?, rand_state = ?, rand_draws = ?,"
        " updated_at = datetime('now') WHERE token = ?",
        (planned.next_state, dumps(planned.new_context),
         rng.state_blob(), rng.draws, token),
    )
    conn.execute(
        """
        INSERT INTO events(token, scenario, revision, method, path, route, rule,
                           from_state, to_state, status_code)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (token, scenario_name, revision, method, path, route.id, rule.name,
         state, planned.next_state, planned.status_code),
    )

    return SimResult(
        status_code=planned.status_code,
        headers=planned.headers,
        body=planned.body,
        delay_ms=planned.delay_ms,
        is_json=planned.is_json,
        matched={
            "scenario": scenario_name,
            "revision": revision,
            "route": route.id,
            "rule": rule.name,
            "from_state": state,
            "to_state": planned.next_state,
        },
    )


# ---------------------------------------------------------------------------
# 请求预演：只读快照、同一份匹配/渲染逻辑、零持久化副作用
# ---------------------------------------------------------------------------

MAX_PREVIEW_STEPS = 50


def _build_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """以 after 相对 before 的视角生成 added/changed/removed 差异（跳过 _seq）。"""
    added: dict[str, Any] = {}
    changed: dict[str, Any] = {}
    removed: list[str] = []
    keys = set(before) | set(after)
    for key in keys:
        if key == "_seq":
            continue
        if key not in before:
            added[key] = after[key]
        elif key not in after:
            removed.append(key)
        elif before[key] != after[key]:
            changed[key] = {"before": before[key], "after": after[key]}
    return {
        "added": added,
        "changed": changed,
        "removed": sorted(removed),
    }


def _build_seq_preview(context: dict[str, Any], counters: dict[str, int],
                       seq_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """以 context（步骤前快照）为基准，给本次渲染内的每次 seq() 调用标注 before。"""
    prior = context.get("_seq", {}) or {}
    return [
        {
            **call,
            "before": prior.get(call["name"]),
            "note": "仅预演内自增，不会持久化；随后真实请求仍从预演前的计数开始",
        }
        for call in seq_calls
    ]


def _build_random_preview(planned: _Planned, rng: SessionRng) -> dict[str, Any]:
    """本步渲染对随机序列的抽样情况（步数前后与种子）。"""
    return {
        "seeded": rng.seeded,
        "seed": rng.seed,
        "draws_before": planned.draws_before,
        "draws_after": planned.draws_after,
        "draws_consumed": planned.draws_after - planned.draws_before,
        "note": "仅预演内推进（克隆的随机状态），不会持久化；随后真实请求仍从预演前的状态开始",
    }


def _simulate_step(scenario: Scenario, scenario_name: str, revision: int, *, state: str,
                   context: dict[str, Any], method: str, path: str,
                   query: dict[str, Any], headers: dict[str, Any],
                   body: Any, rng: SessionRng) -> tuple[
                       dict[str, Any], str, dict[str, Any], SessionRng]:
    """在内存快照上执行一次匹配/渲染（真实与预演共用的同一套语义）。

    纯函数：不触碰数据库。返回 (本步结构化结果, 拟产生的下一状态,
    拟产生的下一上下文（含 _seq 计数器）, 被推进后的随机序列持有方)。
    路由不匹配、无命中规则或渲染失败时抛出 :class:`SimAbort`（与真实引擎
    的状态码/错误体一致）；此时 ``rng`` 未被渲染推进（与真实失败不消耗一致）。
    """
    route, path_params, trace = _match_routes(scenario, method, path)

    bag = {"state": state, "body": body, "query": query,
           "header": headers, "path": path_params}
    rule, rule_trace = evaluate_rules_traced(route.rules, bag)
    trace += rule_trace

    if rule is None:
        raise SimAbort(409, {
            "error": "no_matching_rule",
            "scenario": scenario_name,
            "revision": revision,
            "route": route.id,
            "state": state,
            "trace": trace,
        }, trace=trace)

    planned = _plan_matched(scenario, scenario_name, route, path_params, rule,
                            state=state, context=context, method=method, path=path,
                            body=body, query=query, headers=headers,
                            rng=rng, trace=trace)

    step = {
        "matched": {
            "scenario": planned.scenario_name,
            "revision": revision,
            "route": planned.route.id,
            "rule": planned.rule.name,
            "path_params": planned.path_params,
        },
        "request": {
            "method": method,
            "path": path,
            "query": query,
            "headers": headers,
            "body": body,
        },
        "response": {
            "status_code": planned.status_code,
            "headers": planned.headers,
            "body": planned.body,
            "delay_ms": planned.delay_ms,
        },
        "state_diff": {"before": planned.state, "after": planned.next_state,
                       "changed": planned.state != planned.next_state},
        "context_diff": _build_diff(planned.context, planned.new_context),
        "seq_preview": _build_seq_preview(planned.context, planned.counters,
                                          planned.seq_calls),
        "random_preview": _build_random_preview(planned, rng),
        "trace": planned.trace,
    }
    return step, planned.next_state, planned.new_context, rng


def _failure_kind(status_code: int) -> str:
    """把模拟失败归类到 README 中承诺的 route/rule/render 三类。"""
    if status_code in (404, 405):
        return "route"
    if status_code == 409:
        return "rule"
    return "render"


def preview_session(token: str, method: str, path: str, query: dict[str, Any],
                    headers: dict[str, Any], body: Any,
                    default_scenario: str | None) -> tuple[int, dict[str, Any]]:
    """预演一次业务请求，返回 (HTTP 状态码, 结构化结果)。

    预演要求会话已存在（不懒创建），全程只读；返回的状态码/错误体与真实
    请求一致，命中时另外给出拟响应与 state/context 差异。
    """
    with read_tx() as conn:
        row = conn.execute(
            "SELECT scenario, revision, state, context, rand_seed, rand_state, rand_draws"
            " FROM sessions WHERE token = ?",
            (token,),
        ).fetchone()
        if row is None:
            # 与真实请求不同：预演不创建会话，直接按管理接口语义报 404
            return 404, {"error": "session_not_found", "token": token,
                         "detail": "预演要求会话已存在；请先创建会话或发起一次真实业务请求",
                         "preview": {"persisted": False}}

        scenario_name = row["scenario"]
        revision = row["revision"]
        if revision is None:
            revision = store.current_revision(scenario_name)
        scenario = store.get(scenario_name, revision)
        if scenario is None:
            raise SimAbort(410, {"error": "scenario_unloaded", "scenario": scenario_name})
        state = row["state"]
        context = json.loads(row["context"])
        # 克隆会话随机状态：预演内的抽样不影响真实序列（含未播种会话——
        # 克隆一份全局状态，预演连进程全局随机源也不扰动）
        rng = SessionRng.from_persisted(
            row["rand_seed"], row["rand_state"], row["rand_draws"]).clone()

        step, _next_state, _next_context, _rng = _simulate_step(
            scenario, scenario_name, revision, state=state, context=context,
            method=method, path=path, query=query, headers=headers, body=body,
            rng=rng)

    # 事务已回滚：以下全部是基于快照的拟结果
    payload = {"preview": {"persisted": False}, "token": token,
               "scenario": scenario_name, "revision": revision, **step}
    return 200, payload


# ---------------------------------------------------------------------------
# 连续请求链预演：基于同一会话快照依次执行，后一步看到前一步的拟变化
# ---------------------------------------------------------------------------

def preview_chain(token: str, requests: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    """连续预演一条有序请求链，返回 (HTTP 状态码, 结构化结果)。

    从会话当前快照出发在纯内存中逐步行进：每一步都能看到此前各步拟产生的
    state、context 与 seq 计数变化。任一路由/规则/渲染失败立即停止。
    全程只读、不落库；不改变会话快照时，重复调用结果可重复。
    """
    with read_tx() as conn:
        row = conn.execute(
            "SELECT scenario, revision, state, context, rand_seed, rand_state, rand_draws"
            " FROM sessions WHERE token = ?",
            (token,),
        ).fetchone()
        if row is None:
            return 404, {
                "error": "session_not_found", "token": token,
                "detail": "链路预演要求会话已存在；请先创建会话或发起一次真实业务请求",
                "preview": {"persisted": False, "chain": True},
            }

        scenario_name = row["scenario"]
        revision = row["revision"]
        if revision is None:
            revision = store.current_revision(scenario_name)
        scenario = store.get(scenario_name, revision)
        if scenario is None:
            raise SimAbort(410, {"error": "scenario_unloaded", "scenario": scenario_name})
        initial_state = row["state"]
        initial_context = json.loads(row["context"])
        # 整链共享同一个克隆随机状态：链内后一步看得到前一步的抽样消耗，
        # 但预演结束（事务回滚）后真实序列不变
        rng = SessionRng.from_persisted(
            row["rand_seed"], row["rand_state"], row["rand_draws"]).clone()

        state = initial_state
        context = initial_context
        steps: list[dict[str, Any]] = []
        failed_index: int | None = None
        failure: dict[str, Any] | None = None

        for index, req in enumerate(requests):
            try:
                step, next_state, next_context, rng = _simulate_step(
                    scenario, scenario_name, revision, state=state, context=context,
                    method=req["method"], path=req["path"],
                    query=req["query"], headers=req["headers"], body=req["body"],
                    rng=rng)
            except SimAbort as exc:
                # 路由、规则或渲染失败：立即停止，仅返回此前成功步骤
                failed_index = index
                error_body = dict(exc.body)
                if exc.trace is not None:
                    error_body["trace"] = exc.trace
                failure = {
                    "index": index,
                    "kind": _failure_kind(exc.status_code),
                    "status_code": exc.status_code,
                    "request": {
                        "method": req["method"], "path": req["path"],
                        "query": req["query"], "headers": req["headers"],
                        "body": req["body"],
                    },
                    "error": error_body,
                }
                break

            steps.append({"index": index, **step})
            # 后一步基于前一步拟产生的快照继续
            state = next_state
            context = next_context

        seq_counters = dict(context.get("_seq", {}) or {})

        final_snapshot = {
            "state": state,
            "context": {k: v for k, v in context.items() if k != "_seq"},
            "seq": seq_counters,
            "random": {
                "seeded": rng.seeded,
                "seed": rng.seed,
                "draws": rng.draws,
            },
            "note": "链路预演的拟最终快照，不会持久化；随后真实请求仍从预演前的快照开始",
        }

        payload: dict[str, Any] = {
            "preview": {"persisted": False, "chain": True},
            "token": token,
            "scenario": scenario_name,
            "revision": revision,
            "steps_completed": len(steps),
            "steps_total": len(requests),
            "stopped": failure is not None,
            "failed_index": failed_index,
            "failure": failure,
            "steps": steps,
            "final_snapshot": final_snapshot,
        }
        return 200, payload
