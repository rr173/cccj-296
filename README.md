# 会话准入后端（按并行使用量授权）

Python + FastAPI + SQLite 实现的并行席位授权与会话准入服务。无支付、无商品页面、
无客户端程序；时间推进只走显式逻辑时钟，**不依赖睡眠**，也没有心跳超时淘汰规则。

## 离线借席（无外网工地）

组织先在中心领取**限时离线授权包**，带到指定站点；站点断开中心后用独立持久化
存储独立开关本地会话。承诺与规则：

- **领包参数**：组织、站点、标准/高级席位数、截止 UTC 时间、`request_id`。
  授权签发即生效、截止时刻立即失效（左闭右开）。同组织同 `request_id` 重试返回
  原结果，改参数拒绝（被拒请求同样留痕、可重试）。
- **中心必须兑现整个借期**：按区间断点逐点校验，允许不同权益在不同时段接替，
  不要求一份权益覆盖整个借期；同组织多个借期重叠的包合在一起检查。高级预留只由
  高级权益支撑，标准预留可用标准或富余高级权益。
- **签发不挤在线**：除全区间可行性外，签发前在当前时刻做带预留的安置试算，
  会撤销任何现有在线会话的签发直接拒绝。
- **预留是硬承诺**：未交回且有效的包，其席位不能再分给在线会话或其他站点。
  在线再平衡先扣预留（幻影占用）再按原优先级安置在线会话——离线席位不会挡住
  本可同时容纳的在线会话。
- **权益变更守卫**：修改/撤销权益若使任一未交回包在剩余借期无法兑现，整笔拒绝；
  有其他权益能接替则允许，在线会话随后按原优先级正常再平衡。
- **站点自治**：独立 SQLite、不访问中心库；只接受中心 HMAC 验签通过、签给本站、
  未篡改、未交回且有效的包。按包计并发（高级会话只用高级席、标准会话两类皆可，
  高级申请可向借用高级席的标准会话要回席位）；并发、重启、重复导入均不多开。
  到期瞬间该包会话全部终止且不可新开。
- **提前还席**：站点永久停用包、终止其会话后导出**中心预签的交回凭据**（可重送）。
  中心验签并核对留痕秘密后才释放额度；包文件或普通关会话回执不是交回凭据；
  重送不重复释放，已交回的包不能在站点重新启用。
- 所有写操作走 `BEGIN IMMEDIATE` 全库写锁，签发/交回/在线准入/权益改动并发时
  承诺仍然成立。

## 业务规则

- 权益分 **标准（standard）/ 高级（premium）** 两类，记录含组织、种类、并行额度、
  UTC 生效/失效时间，区间 **左闭右开**；同组织可有多份重叠权益，席位按权益相加。
- **高级会话只能使用高级额度；标准会话两类都可用**（先标准后高级）。
- 容量变化（权益修改、撤销、时间推进导致到期/生效）时全量再平衡，保留顺序为：
  **高级先于标准 → 同类开始较早先于较晚 → 会话 ID 破同序**，在该顺序下贪心安置，
  尽可能保留可容纳的会话。因此不会因为历史上把标准会话放在高级额度上，
  就拒绝本来仍能容纳的高级申请——再平衡会把标准会话让位，必要时撤销标准会话。
- 容纳不下的会话进入明确的 **`revoked` 终态**（带原因），不是查询里少显示一条；
  终态不可逆，权益恢复也不自动复活，需重新申请。
- 准入带 `request_id` 幂等：同 ID 同参数重试返回第一次的完整结果（稳定会话 ID 与
  凭据）；参数变化拒绝；第一次准入的会话后来被撤销，重试仍指向原会话、不重新占位。
- 续用带当前凭据与 `operation_id`：成功则作废旧代、换发下一代凭据；同操作重试返回
  该次结果；旧凭据不能发起任何新操作；已撤销/已关闭会话不能续用。
- 主动关闭只释放它 **实际占用的那一份** 额度；重复关闭或晚到的旧会话关闭
  `released=false`，不会释放后来其它会话的额度。

## 并发与可核对性

所有写操作以 `BEGIN IMMEDIATE` 开启事务，SQLite 全库写锁串行化提交；
每个提交在 `events` 表取得单调 `seq`，`seq` 即权威的提交先后。
续用与撤销竞争、两进程抢最后一份额度，都会形成确定、可核对的顺序。

## 目录结构

```
app/
  schema.sql        中心库表结构（权益版本、会话、凭据、幂等账、事件流、逻辑时钟、离线包）
  site_schema.sql   站点独立库表结构（导入包、本地会话、本地幂等账、事件流）
  db.py             中心连接、事务、逻辑时钟、序号
  site_db.py        站点连接与事务（独立 SQLite 文件）
  crypto.py         HMAC-SHA256 信封签名/验签、中心密钥管理
  service.py        再平衡引擎（含离线预留幻影占用）与在线业务操作
  offline.py        中心离线借席：签发、全区间可行性、权益变更守卫、交回
  site_service.py   站点业务：验签导入、本地准入/关闭、到期清扫、提前还席
  main.py           中心 FastAPI 路由
  site_main.py      站点 FastAPI 路由（独立进程）
  stdlib_http.py    无第三方依赖时的标准库 HTTP 包装（演示用，业务规则仍在 service 层）
tests/               单元/并发/双进程用例（pytest 风格；无 pip 环境可用 tools/pytest_shim.py）
demo/
  demo_scenarios.py   本机场景演示（挪用/到期降额/旧凭据/重复关闭/追溯）
  demo_race.py        双进程抢最后一份额度的端到端演示
  demo_offline.py     离线借席双进程端到端演示（借→断网使用→还→重新分配→到期）
tools/
  run_center.py / run_site.py  两个独立 OS 进程入口（演示用）
  pytest_shim.py     无 pytest 环境下的兼容运行器
```

## 运行

```bash
pip install -r requirements.txt

# 测试（有 pytest 时）
python3 -m pytest
# 无 pip/外网的本机环境：标准库兼容运行器（HTTP 用例会因缺 fastapi 跳过）
PYTHONPATH=. python3 -m tools.pytest_shim \
  tests.test_rebalance tests.test_edge_cases tests.test_idempotency \
  tests.test_concurrency tests.test_purchase tests.test_purchase_concurrency
PYTHONPATH=. python3 -m unittest \
  tests.test_offline_center tests.test_offline_site tests.test_offline_concurrency

# 本机演示（无需起服务，直接操作 SQLite）
python3 -m demo.demo_scenarios
python3 -m demo.demo_race
# 离线借席：自动拉起中心/站点两个真实独立进程，全程显式推进 UTC 时钟
PYTHONPATH=. python3 -m demo.demo_offline

# HTTP 服务（中心）
LICENSE_DB=/tmp/admission.db LICENSE_EPOCH=2026-01-01T00:00:00Z \
  python3 -m uvicorn app.main:app --port 8000
# HTTP 服务（站点，独立进程/独立库；LICENSE_SIGNING_KEY 必须与中心一致，只用于验签）
SITE_DB=/tmp/site.db SITE_ID=site-A SITE_EPOCH=2026-01-01T00:00:00Z \
LICENSE_SIGNING_KEY=<中心密钥> \
  python3 -m uvicorn app.site_main:app --port 8001
```

`LICENSE_SIGNING_KEY` 约定：中心未设置时在 `service_meta` 表随机生成并持久化；
设置了（64 位 hex 或任意字符串，后者做 SHA-256 派生）则用该值。站点必须通过
带外部署拿到同一把密钥才能验签，站点本身不具备签发能力。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/entitlements` | 创建权益，返回本次实际撤销的会话 |
| PATCH | `/entitlements/{ent_id}` | 修改席位/生效窗口（新版本留痕，旧版本 `superseded`） |
| POST | `/entitlements/{ent_id}/revoke` | 撤销权益，返回本次实际撤销的会话及原因 |
| GET | `/entitlements/{ent_id}/history` | 权益全部版本历史 |
| POST | `/sessions/admit` | 准入 `{org, tier, request_id}`；成功返回稳定会话 ID 与凭据 |
| POST | `/sessions/{id}/renew` | 续用 `{operation_id, credential_secret}`，换发下一代凭据 |
| POST | `/sessions/{id}/close` | 主动关闭；凭据可选，重复关闭 `released=false` |
| GET | `/sessions/{id}` | 会话结局、席位轨迹、凭据代次、原始申请 |
| GET | `/orgs/{org}/sessions` | 组织全部会话（含 revoked/closed 终态） |
| GET | `/clock` / POST | `/clock/advance` | 读取/推进逻辑时钟（`to` 或 `delta_seconds`，不可回拨） |
| GET | `/events?org=` | 全局事件流，按 `seq` 体现提交先后 |

### 离线借席接口（中心）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/offline/packages/issue` | 领包 `{org, site_id, standard_seats, premium_seats, expire_at, request_id}`；约束不满足返回 `issued=false` 与原因，同 request_id 重试返回原结果 |
| POST | `/offline/packages/return` | 交回 `{return_credential, operation_id?}`；验签通过才释放，重送返回 `replayed=true` 不重复释放 |
| GET | `/orgs/{org}/offline-packages` | 组织全部离线包及派生状态（active/expired/returned） |

### 离线站点接口（独立进程，`app.site_main`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/packages/import` | 导入中心包信封；验签、site_id、有效期、重复导入、已交回校验 |
| GET | `/packages` | 本站包及各包当前活跃会话数 |
| POST | `/sessions/admit` | 按包准入 `{package_id, tier, request_id}`，幂等 |
| POST | `/sessions/{id}/close` | 关闭本地会话，释放包内席位 |
| GET | `/sessions` | 本地会话（含终态） |
| POST | `/packages/{id}/return` | 永久停用包、终止会话并导出可重送的交回凭据 |
| GET/POST | `/clock`、`/clock/advance` | 站点独立逻辑时钟；推进时自动清扫到期会话 |

### 快速体验

```bash
curl -s localhost:8000/entitlements -H 'Content-Type: application/json' -d '{
  "org":"acme","tier":"premium","seats":1,
  "effective_at":"2026-01-01T00:00:00Z","expire_at":"2027-01-01T00:00:00Z"}'

curl -s localhost:8000/sessions/admit -H 'Content-Type: application/json' -d '{
  "org":"acme","tier":"standard","request_id":"req-1"}'

curl -s -X POST localhost:8000/clock/advance -H 'Content-Type: application/json' \
  -d '{"delta_seconds": 86400}'
```

## 数据模型要点

- `entitlements`：同一 `ent_id` 多版本行，修改插入新版本、旧版本置 `superseded`，
  撤销置 `revoked` 并记录原因，全部历史可追溯。
- `sessions`：状态机 `active → revoked | closed`（终态）；记录原始 `request_id`、
  结束序号与原因。
- `credentials`：只存 SHA-256 哈希；续用作废旧代（`consumed`）并签发新代。
- `admission_requests` / `operations`：准入与续用/关闭的幂等账，保存首次响应原文，
  重试原样返回。
- `slot_history`：会话实际占用权益的区间历史，迁移与释放都带 `seq` 和原因，
  支撑“只释放自己那份额度”和完整追溯。
- `offline_packages`：离线包主表（`active`/`returned` 状态，到期由窗口派生）；
  存交回秘密的哈希而非秘密本身。`offline_requests` 是签发幂等账（含被拒留痕），
  `offline_returns` 保证每个包至多释放一次。
- 站点库（`site_schema.sql`）与中心库物理隔离：`imported_packages` 保存验签后的
  包与预签交回凭据，`site_sessions`/`site_admission_requests`/`site_operations`
  支撑本地并发上限与幂等，`site_events` 提供站点侧提交顺序。
