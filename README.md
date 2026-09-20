# 有状态 API 场景模拟器（Stateful API Scenario Simulator）

用于联调的有状态 API Mock 服务：提交一份包含**路由、请求匹配条件、响应模板、状态迁移**的
YAML 场景，服务按**会话令牌**隔离每个调用方的当前状态，并在**单个 SQLite 写事务**中
完成「选择最高优先级匹配 → 渲染响应 → 推进状态」，保证同一会话的并发请求不会重复触发同一条迁移。

场景按**不可变版本（revision）**发布：每次 `PUT` 校验通过后产生递增 revision 并置为当前版本，
旧版本定义保留不覆盖；会话在创建时绑定当时的当前 revision，之后无论场景再发布多少新版本，
该会话的真实请求与预演都始终按绑定版本执行。

- 技术栈：Python 3.12 · FastAPI · SQLite（WAL）· Jinja2 沙箱模板 · PyYAML
- 容器内启动即自动完成依赖安装与数据库初始化（含旧库就地升级），并自动加载 `scenarios/` 下的示例场景

---

## 一条命令启动

```bash
docker compose up --build -d
```

服务地址：<http://localhost:8080>

| 用途 | 地址 |
| --- | --- |
| 健康检查 | <http://localhost:8080/__health> |
| Swagger 文档 | <http://localhost:8080/docs> |
| 场景管理 | `http://localhost:8080/__scenarios` |
| 会话状态 | `http://localhost:8080/__sessions/{token}` |
| 请求预演 | `POST http://localhost:8080/__sessions/{token}/preview` |
| 连续请求链预演 | `POST http://localhost:8080/__sessions/{token}/preview/chain` |
| 模拟业务流量 | 除上述管理前缀外的任意路径，如 `POST http://localhost:8080/orders` |

停止：`docker compose down`（数据保留在命名卷 `simulator-data` 中；加 `-v` 一并清除）。

不使用容器时：

```bash
pip install -r requirements.txt
SIM_DB_PATH=./data/simulator.db SIM_SCENARIOS_DIR=./scenarios \
  uvicorn app.main:app --host 0.0.0.0 --port 8080
```

---

## 快速体验

镜像自带 `scenarios/order-flow.yaml`（下单 → 支付 → 发货 → 收货，含取消分支）。

```bash
BASE=http://localhost:8080

# 1) 用令牌 t1 发起第一次业务请求（会话自动创建，初始状态 init）
curl -s -X POST $BASE/orders \
  -H 'sim-session-token: t1' -H 'Content-Type: application/json' \
  -d '{"sku":"A-1","quantity":2}'
# -> 201, 状态 init -> created

# 2) 支付（created -> paid）
curl -s -X POST $BASE/orders/42/pay \
  -H 'sim-session-token: t1' -H 'Content-Type: application/json' \
  -d '{"channel":"alipay"}'

# 3) 查询该会话当前状态与上下文
curl -s $BASE/__sessions/t1

# 4) 查询请求/迁移事件历史
curl -s "$BASE/__sessions/t1/events"
```

每个模拟响应还带有调试响应头：

- `X-Sim-Matched: order-flow:pay-order:pay-created`（场景:路由:规则）
- `X-Sim-Revision: 1`（实际执行的场景 revision）
- `X-Sim-State: created->paid`（迁移前后状态）

---

## 场景 YAML 写法

```yaml
name: order-flow                 # 必填，场景名（字母数字 . _ -）
description: 可选描述
initial_state: init              # 新会话的初始状态
states: [init, created, paid, shipped, completed, cancelled]

routes:
  - id: pay-order                # 路由 id，仅用于标识
    path: /orders/{order_id}/pay # 支持 {path_param} 路径参数
    methods: [POST]              # 默认 [GET, POST]
    rules:                       # 同一路由下按 priority 从高到低匹配
      - name: pay-created
        priority: 100
        when:                    # 所有条件为 AND；全部满足即命中
          - source: state        # state: 当前会话状态
            op: eq
            value: created
          - source: body         # body / query / header / path / state
            path: channel        # 点路径，支持 a.b.0 与 a[0].b
            op: in
            value: [alipay, wechat, card]
        response:
          status: 200            # 也支持 Jinja 模板，如 "{{ 200 if body.ok else 400 }}"
          headers:
            Content-Type: application/json
          body:                  # dict/list 自动按 JSON 返回；字符串按文本返回
            order_id: "{{ path.order_id }}"
            trace: "{{ uuid4() }}"
            paid_at: "{{ now('%Y-%m-%dT%H:%M:%SZ') }}"
        set:                     # 命中后写入会话上下文（持久化）
          pay_channel: "{{ body.channel }}"
        next_state: paid         # 命中后迁移；省略则状态不变
        delay_ms: 0              # 可选：响应前延迟（模拟慢接口）

      - name: double-pay-rejected # 低优先级兜底规则
        priority: 50
        when:
          - {source: state, op: in, value: [paid, shipped, completed]}
        response:
          status: 409
          body: {error: already_paid}
```

### 匹配条件（when）

| 字段 | 说明 |
| --- | --- |
| `source` | `body`（默认，JSON 解析）、`query`、`header`（键名小写）、`path`（路径参数）、`state` |
| `path` | 点路径：`a.b.0` / `a[0].b`；`source=state` 时忽略 |
| `op` | `eq` `ne` `gt` `gte` `lt` `lte` `in` `contains` `starts_with` `ends_with` `regex` `exists` `truthy` |
| `value` | 比较右值；`eq/ne/gt...` 会尽量按数字比较（如 `"2"` 与 `2` 相等） |

匹配语义：规则按 `priority` 降序（相同按定义顺序），**取第一条全部条件满足的规则**；
没有规则命中时返回 `409 no_matching_rule`（不会推进状态）；路径无匹配返回 `404`，
方法不支持返回 `405`。

### 响应模板（Jinja2 沙箱）

- 整个值写成单个 `"{{ expr }}"` 时保留原生类型（数字、布尔、列表、对象）；
  嵌在字符串中则渲染为文本。
- 可用变量：`state`、`body`、`query`、`header`、`path`、`context`（已持久化的上下文）、
  `request`（`method`/`path`/`route`）。
- 内置函数：`uuid4()`、`now(fmt)`、`random_str(n, prefix='')`、`randint(a,b)`、
  `choice(list)`、`seq(name, start=1)`（会话内自增计数器，跨请求持久化）。
- 过滤器：Jinja2 标准过滤器（如 `body.x | default('v')`）。
- 响应体 `body` 为 dict/list 时按 JSON 返回；为字符串时配合 `Content-Type` 头
  可返回 HTML/纯文本等。

### 校验规则

载入时会校验：场景名合法、`initial_state` 在 `states` 内、路径/方法合法、
条件 `source/op` 合法、`next_state` 已声明、状态码范围、模板语法正确、
路由 id 与「路径+方法」组合不重复、业务路径不得占用 `/__*`、`/docs` 等管理前缀。

完整示例见 [`scenarios/order-flow.yaml`](scenarios/order-flow.yaml)。

---

## 管理接口

### 场景

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/__scenarios/validate` | 只校验不入库。body 为 YAML 文本（`Content-Type: application/yaml`）或 JSON |
| GET | `/__scenarios` | 列出全部场景及解析后的结构（含 `current_revision` 与版本数 `revisions`） |
| GET | `/__scenarios/{name}` | 查看单个场景摘要（当前版本） |
| GET | `/__scenarios/{name}/yaml` | 取回原始 YAML；`?revision=N` 可取指定历史版本 |
| PUT | `/__scenarios/{name}` | **校验并发布**：产生递增 revision 并置为当前版本；内容与当前版本完全相同时幂等返回原 revision |
| GET | `/__scenarios/{name}/revisions` | **版本列表**：全部不可变 revision，标注当前版本 |
| GET | `/__scenarios/{name}/revisions/{rev}` | 查看指定 revision 的解析结构 |
| POST | `/__scenarios/{name}/revisions/{rev}/activate` | **激活**指定 revision 为当前版本（只影响之后新建的会话）；目标不存在返回 404 |
| DELETE | `/__scenarios/{name}` | 删除场景及其全部版本；仍有会话引用时返回 409，加 `?force=true` 级联删除 |

```bash
# 校验
curl -s -X POST $BASE/__scenarios/validate \
  -H 'Content-Type: application/yaml' --data-binary @my-scenario.yaml

# 发布（文件名/场景名任意，以 URL 中的 name 为准）
curl -s -X PUT $BASE/__scenarios/my-scenario \
  -H 'Content-Type: application/yaml' --data-binary @my-scenario.yaml
# -> {"loaded":true,"action":"created","revision":1,"current_revision":1,...}
#    再次 PUT 修改后的定义 -> {"action":"published","revision":2,...}
#    重复 PUT 与当前版本相同的内容 -> {"action":"unchanged","revision":2,...}（幂等，不产生新版本）
```

#### 场景版本（revision）与会话绑定

- 每次 `PUT` 校验通过后，定义作为**新的不可变 revision** 落库并置为当前版本，
  旧版本保留在 `scenario_revisions` 表中，永不覆盖；
- **新会话**（显式 `POST /__sessions` 或首个业务请求懒创建）绑定**当时的当前
  revision**，之后该会话的真实请求、单次预演与请求链预演都按该版本执行——
  场景再发布新版本或激活旧版本都**不影响既有会话**；
- **激活**只切换「新会话的默认版本」，用于回滚：把旧 revision 置为当前版本后，
  只有之后新建的会话使用它；
- 场景查询、会话查询、事件历史与模拟响应（`X-Sim-Revision` 头）都会暴露实际
  revision；再次发布时 revision 始终取 `max+1`（激活过旧版本也不会回绕或覆盖）。

```bash
# 查看版本列表（current 标注新会话默认版本）
curl -s $BASE/__scenarios/order-flow/revisions
# -> {"name":"order-flow","current_revision":2,
#     "revisions":[{"revision":1,"created_at":"...","current":false},
#                  {"revision":2,"created_at":"...","current":true}]}

# 回滚：激活 revision 1（只影响之后新建的会话）
curl -s -X POST $BASE/__scenarios/order-flow/revisions/1/activate

# 取回历史版本的原始 YAML
curl -s "$BASE/__scenarios/order-flow/yaml?revision=1"
```

### 会话

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/__sessions` | 显式创建会话：`{"token":"t1","scenario":"order-flow","state":"created","context":{}}`，字段均可省略；绑定**当前 revision** 并在响应中返回 |
| GET | `/__sessions/{token}` | **会话状态查询**：当前状态、绑定的 `revision`、上下文、可用状态、路由列表（按绑定版本给出） |
| GET | `/__sessions/{token}/events?limit=50` | 最近的匹配与状态迁移历史（新→旧，含每次命中时的 `revision`） |
| POST | `/__sessions/{token}/preview` | **请求预演**：不落库地试运行一次业务请求，返回拟响应与 state/context 差异（按会话绑定的 revision 执行） |
| POST | `/__sessions/{token}/preview/chain` | **连续请求链预演**：有序请求数组基于同一会话快照依次试跑（最多 50 步），逐步返回命中/拟响应/差异与最终快照，不落库 |
| POST | `/__sessions/{token}/reset` | 重置到初始（或指定）状态并清空事件；不改变会话绑定的 revision |
| DELETE | `/__sessions/{token}` | 删除会话及其事件 |

#### 请求预演（dry-run）

在**不改动会话**的前提下，基于该会话的当前快照走一遍与真实请求完全相同的
路由匹配、`when` 条件判定与 Jinja 模板渲染，用于联调前确认"如果我发这个请求会发生什么"。

- 全程在只读事务中执行并最终回滚：**不写 sessions、不写 events**；
- 即使模板调用了 `seq()`，计数也只在预演渲染器内存中自增——随后的真实请求
  仍从**预演前的计数值**开始（可多次预演，结果幂等）；
- 命中时 HTTP 状态码为 `200`，拟返回的状态码在 `response.status_code` 中；
  未命中时直接返回**与真实请求一致的状态码与错误详情**（404 / 405 / 409 /
  500 等），并附带逐条件判定轨迹 `trace`；
- 预演要求会话**已存在**（不会懒创建）；会话不存在返回 404。

请求体字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `path` | 是 | 业务路径（不含 query），如 `/orders/42/pay` |
| `method` | 否 | 默认 `GET`，自动转大写；支持 GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS |
| `query` | 否 | 查询参数对象，值会按字符串处理（与真实 query 一致） |
| `headers` | 否 | 请求头对象，键名自动归一化为小写（与真实请求一致） |
| `body` | 否 | 请求体，按已解析的 JSON 传入（与真实 `application/json` 请求一致），缺省为 `{}` |

```bash
# 先把会话置到 created
curl -s -X POST $BASE/__sessions -H 'Content-Type: application/json' \
  -d '{"token":"t1","state":"created"}'

# 预演一次支付：返回命中规则、拟响应、状态/上下文差异与判定轨迹
curl -s -X POST $BASE/__sessions/t1/preview \
  -H 'Content-Type: application/json' \
  -d '{"method":"POST","path":"/orders/42/pay","body":{"channel":"alipay"}}'
```

命中时响应（节选）：

```json
{
  "preview": {"persisted": false},
  "revision": 1,
  "matched": {"scenario": "order-flow", "revision": 1, "route": "pay-order",
              "rule": "pay-created", "path_params": {"order_id": "42"}},
  "response": {
    "status_code": 200,
    "headers": {},
    "body": {"order_id": "42", "paid": true, "channel": "alipay",
             "paid_at": "2026-09-19T10:00:00Z", "trace": "..."},
    "delay_ms": 0
  },
  "state_diff": {"before": "created", "after": "paid", "changed": true},
  "context_diff": {"added": {"pay_channel": "alipay"}, "changed": {}, "removed": []},
  "seq_preview": [
    {"name": "order", "start": 1000, "value": 1000, "existed": false,
     "before": null,
     "note": "仅预演内自增，不会持久化；随后真实请求仍从预演前的计数开始"}
  ],
  "trace": [
    {"stage": "route", "route": "pay-order", "path": "/orders/{order_id}/pay",
     "path_match": true, "method_match": true, "path_params": {"order_id": "42"}},
    {"stage": "route_selected", "route": "pay-order"},
    {"stage": "rule", "rule": "pay-created", "priority": 100, "matched": true,
     "conditions": [
       {"source": "state", "op": "eq", "expected": "created",
        "present": true, "actual": "created", "passed": true},
       {"source": "body", "path": "channel", "op": "in",
        "expected": ["alipay", "wechat", "card"],
        "present": true, "actual": "alipay", "passed": true}
    ]},
    {"stage": "rule", "rule": "double-pay-rejected", "matched": false, "skipped": true}
  ]
}
```

随后发起真实请求，状态照常 `created -> paid`，且 `seq()` 的值与预演时一致
（预演从未消耗计数）。未命中时 `trace` 会逐条给出每个条件的左值来源、取值
（`actual`）、期望值与是否通过，并保留与真实引擎一致的短路顺序；例如在
`paid` 状态预演支付，预演接口信封为 `200` 但 `response.status_code` 为
`409`，错误体与真实请求完全相同。

#### 连续请求链预演（chain dry-run）

在**不改动会话**的前提下，把一组**有序请求**当作一条真实调用链依次试跑：
从该会话的当前快照出发，**后一步一定能看到前一步拟产生的 `state`、
`context` 与 `seq` 计数变化**，用于一次性确认"我按顺序发这一串请求会发生
什么"（如下单 → 支付 → 发货 → 收货）。

- 请求体只有一个字段 `requests`：元素结构与单次预演**完全相同**（`path`
  必填，`method` 默认 `GET`，`query`/`headers`/`body` 语义一致），**最多
  50 步**，超出返回 422；
- 每一步复用与单次预演/真实请求**同一份**路由匹配、规则判定与 Jinja 渲染
  逻辑；每步返回内容与单次预演一致：`matched` / `request` / `response` /
  `state_diff` / `context_diff` / `seq_preview` / `trace`；
- 拟响应为错误状态码但**确实命中了规则**（如 400/409 兜底规则）属于正常
  模拟响应，链路继续；只有**路由失败**（404/405）、**无规则命中**（409
  `no_matching_rule`）或**渲染失败**（500 `render_error`）才会立即停止，
  返回 `failed_index`、失败类型 `failure.kind`（`route`/`rule`/`render`）、
  失败请求与错误体（含 `trace`），以及此前已成功的各步；
- 全程在只读事务中执行并最终回滚：**不写 sessions、不写 events、不推进序列
  计数**；`seq()` 只在预演内存中沿链路自增（后一步看得到、库中不保留），
  会话快照不变时**多次调用结果可重复**（模板内的 `now()`/`uuid4()` 与单次
  预演一样是运行时值）；
- 同样要求会话**已存在**，不存在返回 404。

```bash
# 新建一个 init 状态的会话
curl -s -X POST $BASE/__sessions -H 'Content-Type: application/json' \
  -d '{"token":"c1","state":"init"}'

# 连续预演：下单 -> 支付 -> 发货 -> 收货
curl -s -X POST $BASE/__sessions/c1/preview/chain \
  -H 'Content-Type: application/json' -d '{
    "requests": [
      {"method":"POST","path":"/orders","body":{"sku":"A-1","quantity":2}},
      {"method":"POST","path":"/orders/1000/pay","body":{"channel":"alipay"}},
      {"method":"POST","path":"/orders/1000/ship","body":{"carrier":"sf"}},
      {"method":"POST","path":"/orders/1000/confirm"}
    ]
  }'
```

响应（节选）：

```json
{
  "preview": {"persisted": false, "chain": true},
  "token": "c1",
  "scenario": "order-flow",
  "revision": 1,
  "steps_completed": 4,
  "steps_total": 4,
  "stopped": false,
  "failed_index": null,
  "failure": null,
  "steps": [
    {"index": 0, "matched": {"scenario": "order-flow", "route": "create-order",
                             "rule": "create-success", "path_params": {}},
     "response": {"status_code": 201, "headers": {}, "body": {"...": "..."},
                  "delay_ms": 0},
     "state_diff": {"before": "init", "after": "created", "changed": true},
     "context_diff": {"added": {"sku": "A-1", "quantity": 2},
                      "changed": {}, "removed": []},
     "seq_preview": [{"name": "order", "start": 1000, "value": 1000,
                      "existed": false, "before": null}],
     "trace": [ {"...": "与单次预演相同的逐条件轨迹..."} ]},
    {"index": 1, "state_diff": {"before": "created", "after": "paid",
                                "changed": true}, "...": "..."},
    {"index": 2, "state_diff": {"before": "paid", "after": "shipped",
                                "changed": true}, "...": "..."},
    {"index": 3, "state_diff": {"before": "shipped", "after": "completed",
                                "changed": true}, "...": "..."}
  ],
  "final_snapshot": {
    "state": "completed",
    "context": {"sku": "A-1", "quantity": 2, "pay_channel": "alipay",
                "carrier": "sf"},
    "seq": {"order": 1001},
    "note": "链路预演的拟最终快照，不会持久化；随后真实请求仍从预演前的快照开始"
  }
}
```

链中途失败时，HTTP 信封仍为 `200`，但 `stopped` 为 `true`、
`steps_completed` 只含失败前的成功步骤数，例如第 2 步打了一个不存在的路径：

```json
{
  "steps_completed": 1,
  "steps_total": 2,
  "stopped": true,
  "failed_index": 1,
  "failure": {
    "index": 1,
    "kind": "route",
    "status_code": 404,
    "request": {"method": "GET", "path": "/ghost/9", "query": {},
                "headers": {}, "body": {}},
    "error": {"error": "no_route", "path": "/ghost/9", "trace": [ "..."]}
  },
  "steps": [ {"index": 0, "...": "失败前的成功步骤..."} ],
  "final_snapshot": {"state": "created", "context": {"...": "..."}, "seq": {}}
}
```

会话也可以**懒创建**：业务请求只需携带令牌；首个匹配到路由的请求自动建会话。
当已载入多个场景时，懒创建需通过请求头 `sim-scenario: <name>` 指定场景
（或设置环境变量 `SIM_DEFAULT_SCENARIO`）。

### 业务请求约定

- 会话令牌：请求头 `sim-session-token: <token>`（也支持 `?sim_session_token=`）。
- 可选请求头 `sim-scenario: <name>`：仅新会话首次请求时用于绑定场景。
- 请求体：`application/json` 按 JSON 解析；`application/x-www-form-urlencoded`
  按表单解析；YAML 的 content-type 按 YAML 解析；其他文本保存为 `body._raw`。

---

## 并发安全：为什么迁移不会重复触发

所有会读写会话的业务请求都在同一个 `BEGIN IMMEDIATE` 事务中执行：

1. 立即获取 SQLite 写锁（同库同刻只有一个写事务）；
2. 读取会话行（`state`/`context`）；
3. 匹配路由与最高优先级规则；
4. 渲染响应、计算 `set` 与 `next_state`；
5. 更新会话并写入一条 `events` 记录，然后提交。

拿不到锁时按 `SIM_BUSY_TIMEOUT_MS` 等待并在应用层重试（`SIM_DB_RETRIES` 次）。
因此同一会话的并发请求会被**串行化**，每个请求看到的都是前一个请求提交后的新状态：
例如 20 个并发支付请求中恰好 1 个得到 200 并完成 `created -> paid`，
其余 19 个得到 409（已内置压测验证，见下方"压测"说明）。

> 部署限制：SQLite 的写锁是**进程内文件级**的，因此容器固定 `--workers 1`。
> 单 worker + 异步框架足以支撑联调量级的并发；会话状态使用线程局部连接，
> 同步事务段很短，不阻塞事件循环。

---

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SIM_PORT`（仅 compose） | `8080` | 宿主机映射端口，改后访问 `http://localhost:<端口>` |
| `SIM_DB_PATH` | `/data/simulator.db` | SQLite 文件路径（compose 已挂卷持久化） |
| `SIM_SCENARIOS_DIR` | `/app/scenarios` | 启动时自动加载的场景目录（`*.yaml`/`*.yml`，文件名即场景名） |
| `SIM_DEFAULT_SCENARIO` | 空 | 多场景时未显式指定场景的默认场景名 |
| `SIM_BUSY_TIMEOUT_MS` | `5000` | 写锁等待毫秒 |
| `SIM_DB_RETRIES` | `20` | 锁冲突时的应用侧重试次数（每次 20ms 起退避） |
| `SIM_LOG_LEVEL` | `info` | 日志级别 |

自定义场景目录（改 YAML 无需重新构建镜像）：

```bash
# docker-compose.yml 中取消注释挂载行，或：
SIM_SCENARIOS_DIR=/app/scenarios docker compose run --rm \
  -v "$PWD/my-scenarios:/app/scenarios" -p 8080:8080 api-simulator
```

---

## 项目结构

```
.
├── app/
│   ├── main.py            # FastAPI 入口、生命周期初始化、全路径转发
│   ├── config.py          # 环境变量配置
│   ├── db.py              # 连接/WAL/建表/旧库迁移/IMMEDIATE 写事务（含只读回滚事务）与锁重试
│   ├── scenario.py        # YAML 解析与校验
│   ├── models.py          # 场景/路由/规则数据模型
│   ├── matching.py        # 路径模式、点路径取值、条件运算符
│   ├── renderer.py        # Jinja2 沙箱渲染与模板函数
│   ├── engine.py          # 核心：单事务匹配→渲染→迁移；同快照只读单请求/请求链预演（零副作用）
│   ├── store.py           # 已编译场景缓存（按 name+revision）、发布/激活
│   └── routers/
│       ├── scenarios.py   # 场景校验/版本发布/版本列表/激活/查询/删除
│       └── sessions.py    # 会话创建/状态查询/重置/事件/单请求与请求链预演
├── scenarios/
│   └── order-flow.yaml    # 示例场景（随启动自动加载）
├── requirements.txt
├── Dockerfile             # 依赖安装 + DB 初始化随启动完成
├── docker-compose.yml
└── README.md
```

数据库表：`scenarios`（场景名 → 当前 revision）、`scenario_revisions`（不可变的
场景定义版本，`(scenario, revision)` 主键）、`sessions`（令牌 → 场景/绑定
revision/状态/上下文）、`events`（每次命中的 revision、路由、规则、前后状态与
状态码）。启动时自动检测并就地升级旧版表结构：旧定义固化为 revision 1，
既有会话回填绑定该版本，历史事件的 revision 置空。

---

## 并发自测（可选）

```bash
BASE=http://localhost:8080
curl -s -X POST $BASE/__sessions -H 'Content-Type: application/json' \
  -d '{"token":"ct","state":"created"}' >/dev/null
seq 1 20 | xargs -P 20 -I{} curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST $BASE/orders/1/pay \
  -H 'sim-session-token: ct' -H 'Content-Type: application/json' \
  -d '{"channel":"card"}' | sort | uniq -c
# 期望: 1 个 200，19 个 409
curl -s "$BASE/__sessions/ct/events"   # 事件中恰好 1 次 created -> paid
```
