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
