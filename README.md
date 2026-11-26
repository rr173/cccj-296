# 会话准入后端（按并行使用量授权）

Python + FastAPI + SQLite 实现的并行席位授权与会话准入服务。无支付、无商品页面、
无客户端程序；时间推进只走显式逻辑时钟，**不依赖睡眠**，也没有心跳超时淘汰规则。

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
  schema.sql     表结构（权益版本、会话、凭据代次、幂等账、席位轨迹、事件流、逻辑时钟）
  db.py          连接、事务、逻辑时钟、序号
  service.py     再平衡引擎与全部业务操作
  main.py        FastAPI 路由
tests/           30 个 pytest 用例（含线程与真双进程竞争）
demo/
  demo_scenarios.py   本机场景演示（挪用/到期降额/旧凭据/重复关闭/追溯）
  demo_race.py        双进程抢最后一份额度的端到端演示
```

## 运行

```bash
pip install -r requirements.txt

# 测试
python3 -m pytest

# 本机演示（无需起服务，直接操作 SQLite）
python3 -m demo.demo_scenarios
python3 -m demo.demo_race

# HTTP 服务
LICENSE_DB=/tmp/admission.db LICENSE_EPOCH=2026-01-01T00:00:00Z \
  python3 -m uvicorn app.main:app --port 8000
```

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
| GET | `/clock` / POST `/clock/advance` | 读取/推进逻辑时钟（`to` 或 `delta_seconds`，不可回拨） |
| GET | `/events?org=` | 全局事件流，按 `seq` 体现提交先后 |

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
