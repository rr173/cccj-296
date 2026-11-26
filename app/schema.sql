-- 会话准入后端 SQLite schema
-- 所有时间以规范化 UTC ISO-8661 文本存储（带 +00:00 偏移），可直接按字典序比较。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 逻辑时钟：全系统唯一一行，时间推进只能通过 API 显式进行（不依赖睡眠/wall clock）
CREATE TABLE IF NOT EXISTS clock (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    now_iso TEXT NOT NULL
);

-- 单调序号计数器：所有可核对的提交按 events.seq 排序
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

-- 权益（按版本留痕）：同一逻辑权益 ent_id 在修改时插入新版本行，
-- 旧版本置为 superseded；撤销置 revoked。查询可追溯全部历史版本。
CREATE TABLE IF NOT EXISTS entitlements (
    row_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ent_id        TEXT NOT NULL,
    version       INTEGER NOT NULL,
    org           TEXT NOT NULL,
    tier          TEXT NOT NULL CHECK (tier IN ('standard', 'premium')),
    seats         INTEGER NOT NULL CHECK (seats >= 0),
    effective_at  TEXT NOT NULL,
    expire_at     TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'revoked')),
    supersedes    INTEGER,                      -- 被替代版本的 row_id
    revoke_reason TEXT,
    created_seq   INTEGER NOT NULL,
    ended_seq     INTEGER,                      -- 被替代/撤销时的事件序号
    UNIQUE (ent_id, version),
    FOREIGN KEY (supersedes) REFERENCES entitlements(row_id)
);
CREATE INDEX IF NOT EXISTS idx_ent_org ON entitlements(org, status);

-- 会话：状态机 active -> revoked | closed（终态不可逆，权益恢复也不复活）
CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,
    org               TEXT NOT NULL,
    tier              TEXT NOT NULL CHECK (tier IN ('standard', 'premium')),
    status            TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'closed')),
    created_seq       INTEGER NOT NULL,
    ended_seq         INTEGER,
    end_reason        TEXT,
    admit_request_id  TEXT NOT NULL,
    current_cred_id   INTEGER,
    last_ent_id       TEXT                      -- 最近实际占用的权益（便于快速追溯）
);
CREATE INDEX IF NOT EXISTS idx_sess_org_status ON sessions(org, status);

-- 凭据代次：每次续用作废旧代、签发新代；只存哈希
CREATE TABLE IF NOT EXISTS credentials (
    cred_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    generation   INTEGER NOT NULL,
    secret_hash  TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('current', 'consumed', 'invalid')),
    issued_seq   INTEGER NOT NULL,
    consumed_seq INTEGER,
    UNIQUE (session_id, generation),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

-- 准入请求幂等账：request_id 全局唯一，首次结果整体留痕，重试原样返回
CREATE TABLE IF NOT EXISTS admission_requests (
    request_id       TEXT PRIMARY KEY,
    org              TEXT NOT NULL,
    tier             TEXT NOT NULL,
    first_session_id TEXT,
    first_status     TEXT NOT NULL CHECK (first_status IN ('admitted', 'rejected')),
    reject_code      TEXT,
    response_json    TEXT NOT NULL,
    created_seq      INTEGER NOT NULL
);

-- 续用/关闭操作幂等账：同 operation_id 重试返回第一次的结果
CREATE TABLE IF NOT EXISTS operations (
    operation_id  TEXT PRIMARY KEY,
    op_type       TEXT NOT NULL CHECK (op_type IN ('renew', 'close')),
    session_id    TEXT NOT NULL,
    presented_cred_id INTEGER,
    result        TEXT NOT NULL CHECK (result IN ('ok', 'failed')),
    error_code    TEXT,
    response_json TEXT NOT NULL,
    seq           INTEGER NOT NULL
);

-- 会话实际占用权益的区间历史（一个会话可因再平衡在权益间迁移）
CREATE TABLE IF NOT EXISTS slot_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL,
    ent_id         TEXT NOT NULL,
    assigned_seq   INTEGER NOT NULL,
    released_seq   INTEGER,
    release_reason TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_slot_open ON slot_history(session_id, released_seq);

-- 全局事件流：提交顺序的权威来源
CREATE TABLE IF NOT EXISTS events (
    seq         INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    org         TEXT,
    at_iso      TEXT NOT NULL,
    detail_json TEXT NOT NULL
);

-- ================================================================ 组织购权
-- 套餐目录：价格/币种/权益种类/席位/时长均为预设固定值；订单只引用套餐快照，不能改价。
-- charge_mode 只是收款方演练用的预设收款方式（direct=直接扣款，freeze=冻结后扣款）。
CREATE TABLE IF NOT EXISTS packages (
    code             TEXT PRIMARY KEY,
    price_cents      INTEGER NOT NULL CHECK (price_cents > 0),
    currency         TEXT NOT NULL,
    tier             TEXT NOT NULL CHECK (tier IN ('standard', 'premium')),
    seats            INTEGER NOT NULL CHECK (seats >= 0),
    duration_seconds INTEGER NOT NULL CHECK (duration_seconds > 0),
    charge_mode      TEXT NOT NULL DEFAULT 'direct' CHECK (charge_mode IN ('direct', 'freeze')),
    created_seq      INTEGER NOT NULL
);

-- 购买订单状态机：
--   processing --(确认足额扣款/足额冻结)--> promised --(实际扣款确认)--> delivered
--   processing/failed --(接受取消)--> cancel_pending --(确认无款终结)--> canceled
--   processing --(支付明确拒绝且未接受取消)--> failed
-- promised/delivered 不可取消（明确拒绝），服务继续履约；权益只在 captured 后发放。
-- 同一组织同一 request_id 唯一；参数（套餐）变化拒绝。
CREATE TABLE IF NOT EXISTS purchase_orders (
    order_id            TEXT PRIMARY KEY,
    org                 TEXT NOT NULL,
    request_id          TEXT NOT NULL,
    package_code        TEXT NOT NULL,
    merchant_order_id   TEXT NOT NULL UNIQUE,
    price_cents         INTEGER NOT NULL,
    currency            TEXT NOT NULL,
    tier                TEXT NOT NULL CHECK (tier IN ('standard', 'premium')),
    seats               INTEGER NOT NULL,
    duration_seconds    INTEGER NOT NULL,
    charge_mode         TEXT NOT NULL CHECK (charge_mode IN ('direct', 'freeze')),
    status              TEXT NOT NULL CHECK (status IN (
                            'processing', 'promised', 'delivered',
                            'failed', 'cancel_pending', 'canceled')),
    charge_id           TEXT,
    charge_amount_cents INTEGER,
    charge_currency     TEXT,
    charge_state        TEXT NOT NULL DEFAULT 'none' CHECK (charge_state IN (
                            'none', 'unknown', 'frozen', 'captured',
                            'voided', 'refunded', 'rejected')),
    create_attempted    INTEGER NOT NULL DEFAULT 0,
    create_finished     INTEGER NOT NULL DEFAULT 0,
    create_outcome      TEXT NOT NULL DEFAULT 'unknown' CHECK (create_outcome IN (
                            'unknown', 'ok', 'rejected')),
    delivery_basis      TEXT CHECK (delivery_basis IN ('captured', 'frozen_guarantee')),
    promised_seq        INTEGER,
    promised_at_iso     TEXT,
    issued_ent_id       TEXT,
    ent_effective_at    TEXT,          -- 实际发放时锁定的窗口；重试/恢复不再重算
    ent_expire_at       TEXT,
    ent_at_iso          TEXT,          -- 实际发放时的服务逻辑时间
    delivered_seq       INTEGER,
    cancel_request_id   TEXT,
    cancel_accepted_seq INTEGER,
    cancel_accepted_at  TEXT,
    cancel_settlement   TEXT NOT NULL DEFAULT 'none' CHECK (cancel_settlement IN (
                            'none', 'pending', 'void_confirmed',
                            'refund_confirmed', 'no_charge')),
    last_error          TEXT,
    created_seq         INTEGER NOT NULL,
    updated_seq         INTEGER NOT NULL,
    UNIQUE (org, request_id)
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON purchase_orders(status, created_seq);
CREATE INDEX IF NOT EXISTS idx_orders_org ON purchase_orders(org, created_seq);

-- 与独立收款方的每一次交互留痕：ok / rejected（明确业务拒绝）/ unknown（超时或响应丢失）
CREATE TABLE IF NOT EXISTS purchase_money_ops (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id          TEXT NOT NULL,
    op_type           TEXT NOT NULL CHECK (op_type IN (
                          'create', 'query_merchant', 'query_charge',
                          'capture', 'void', 'refund')),
    merchant_order_id TEXT NOT NULL,
    charge_id         TEXT,
    outcome           TEXT NOT NULL CHECK (outcome IN ('ok', 'rejected', 'unknown')),
    detail_json       TEXT NOT NULL,
    seq               INTEGER NOT NULL,
    at_iso            TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES purchase_orders(order_id)
);
CREATE INDEX IF NOT EXISTS idx_money_ops_order ON purchase_money_ops(order_id, id);

-- ================================================================ 离线借席
-- 服务级键值表（目前存 HMAC 签名密钥）
CREATE TABLE IF NOT EXISTS service_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL
);

-- 离线包：组织在中心领取、带到指定站点使用的限时席位授权。
-- 状态机 active --(交回凭据验证通过)--> returned；expired 由窗口派生不入库。
-- 只要 status='active' 且当前逻辑时间 < expire_at，其席位就是不可再分配的
-- 硬性预留（中心在线会话与其他离线包都不能重复使用这份额度）。
CREATE TABLE IF NOT EXISTS offline_packages (
    package_id        TEXT PRIMARY KEY,
    org               TEXT NOT NULL,
    site_id           TEXT NOT NULL,
    standard_seats    INTEGER NOT NULL CHECK (standard_seats >= 0),
    premium_seats     INTEGER NOT NULL CHECK (premium_seats >= 0),
    issued_at         TEXT NOT NULL,          -- 签发即生效
    expire_at         TEXT NOT NULL,          -- 该时刻立即失效（左闭右开）
    request_id        TEXT NOT NULL,
    return_secret_hash TEXT NOT NULL,         -- 交回凭据内随机秘密的哈希
    status            TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'returned')),
    created_seq       INTEGER NOT NULL,
    returned_seq      INTEGER,
    returned_at       TEXT,
    UNIQUE (org, request_id)
);
CREATE INDEX IF NOT EXISTS idx_offline_org_status
    ON offline_packages(org, status);

-- 离线签发请求幂等账：同 org 同 request_id 重试必须得到原结果，改参数拒绝。
-- 被拒请求同样留痕，重试返回第一次的拒绝。
CREATE TABLE IF NOT EXISTS offline_requests (
    request_id       TEXT NOT NULL,
    org              TEXT NOT NULL,
    site_id          TEXT NOT NULL,
    standard_seats   INTEGER NOT NULL,
    premium_seats    INTEGER NOT NULL,
    expire_at        TEXT NOT NULL,
    first_status     TEXT NOT NULL CHECK (first_status IN ('issued', 'rejected')),
    reject_code      TEXT,
    response_json    TEXT NOT NULL,
    created_seq      INTEGER NOT NULL,
    PRIMARY KEY (org, request_id)
);

-- 交回凭据处理账：凭据可重送，但每个包至多释放一次额度。
CREATE TABLE IF NOT EXISTS offline_returns (
    package_id       TEXT PRIMARY KEY,
    operation_id     TEXT,
    response_json    TEXT NOT NULL,
    seq              INTEGER NOT NULL,
    FOREIGN KEY (package_id) REFERENCES offline_packages(package_id)
);
