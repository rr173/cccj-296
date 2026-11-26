-- 站点本地 SQLite schema：独立持久化存储，不访问中心数据库。
-- 逻辑时钟、已导入离线包、本地会话、幂等账、事件流全部自包含。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS clock (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    now_iso TEXT NOT NULL
);

-- 导入的离线包（信封已验签、site_id 与本站一致）。
-- status: active=可用；returned=本站已永久停用并导出交回凭据；
-- expired 状态不入库，由 now >= expire_at 派生。
CREATE TABLE IF NOT EXISTS imported_packages (
    package_id      TEXT PRIMARY KEY,
    org             TEXT NOT NULL,
    standard_seats  INTEGER NOT NULL CHECK (standard_seats >= 0),
    premium_seats   INTEGER NOT NULL CHECK (premium_seats >= 0),
    issued_at       TEXT NOT NULL,
    expire_at       TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('active', 'returned')),
    imported_seq    INTEGER NOT NULL,
    returned_seq    INTEGER,
    return_credential_json TEXT
);

-- 本地会话：状态机 active -> closed | expired | returned_terminated（均终态）。
-- 不记录具体占用的是标准还是高级池内的哪个席位，只保证包内并发不超额。
CREATE TABLE IF NOT EXISTS site_sessions (
    session_id     TEXT PRIMARY KEY,
    package_id     TEXT NOT NULL,
    org            TEXT NOT NULL,
    tier           TEXT NOT NULL CHECK (tier IN ('standard', 'premium')),
    status         TEXT NOT NULL CHECK (
                       status IN ('active', 'closed', 'expired', 'returned_terminated')),
    request_id     TEXT NOT NULL,
    created_seq    INTEGER NOT NULL,
    ended_seq      INTEGER,
    end_reason     TEXT,
    UNIQUE (package_id, request_id),
    FOREIGN KEY (package_id) REFERENCES imported_packages(package_id)
);
CREATE INDEX IF NOT EXISTS idx_site_sess_pkg_status
    ON site_sessions(package_id, status);

-- 本地准入幂等账：重试原样返回首次结果（含被拒结果）。
CREATE TABLE IF NOT EXISTS site_admission_requests (
    request_id       TEXT PRIMARY KEY,
    package_id       TEXT NOT NULL,
    tier             TEXT NOT NULL,
    first_status     TEXT NOT NULL CHECK (first_status IN ('admitted', 'rejected')),
    reject_code      TEXT,
    response_json    TEXT NOT NULL,
    created_seq      INTEGER NOT NULL
);

-- 本地关闭操作幂等账。
CREATE TABLE IF NOT EXISTS site_operations (
    operation_id  TEXT PRIMARY KEY,
    op_type       TEXT NOT NULL CHECK (op_type IN ('close', 'return')),
    package_id    TEXT NOT NULL,
    session_id    TEXT,
    response_json TEXT NOT NULL,
    seq           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS site_events (
    seq         INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    at_iso      TEXT NOT NULL,
    detail_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
