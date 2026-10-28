PRAGMA foreign_keys = ON;

-- ═════════════════════════════════════════════════════════════════════
-- game_data.db —— 单版本镜像（只反映最后一次同步的客户端 build）
--
-- 设计（2026-10-28 重构）：
--   · 主表 ship（数字 id 主键）+ 从表 model（模型目录），经 ship_model 关联；
--     「船 : 模型」不是 1:1 —— 一条船有 1 个船体 + N 个挂载，而挂载模型多船共用。
--   · 三张元信息表都带 first_* / last_* 时间戳与 build，用来做跨版本增量：
--       first_build/first_seen  首次入库
--       last_build/last_changed 内容最后一次真变化
--       last_checked            最近一次同步确认（没变也刷）
--   · 全部去掉 version_code 维度 —— 这是增量写入的前提：否则 build 一变，
--     未变动的那上百万行也要全部 UPDATE 到新 build，等于全量重写。
--   · 重数据（快照 JSON）单独放 ship_data，避免 `select * from ship` 拖入大 blob。
-- ═════════════════════════════════════════════════════════════════════

-- 0. 库级元信息（替代原 data_version_registry / meta_import_state）
--    约定键：schema_ver / wows_type / build / game_version / synced_at / import_state
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- 1. 战舰（主表）
CREATE TABLE IF NOT EXISTS ship (
    id           INTEGER PRIMARY KEY,               -- GameParams 数字 id（跨 build 稳定）
    idx          TEXT    NOT NULL UNIQUE,           -- PJSB018
    name         TEXT    NOT NULL UNIQUE,           -- PJSB018_Yamato_1944
    nation       TEXT    NOT NULL DEFAULT '',
    tier         INTEGER NOT NULL DEFAULT 0,
    ship_class   TEXT    NOT NULL DEFAULT '',       -- Battleship / Destroyer ...
    ship_group   TEXT    NOT NULL DEFAULT '',       -- GameParams 的 group（upgradeable 等）
    model_folder TEXT    NOT NULL DEFAULT '',       -- 船体模型目录（A_Hull.model 的父目录）
    status       TEXT    NOT NULL DEFAULT 'active', -- active / removed
    first_build  TEXT    NOT NULL DEFAULT '',
    first_seen   TEXT    NOT NULL DEFAULT '',
    last_build   TEXT    NOT NULL DEFAULT '',
    last_changed TEXT    NOT NULL DEFAULT '',
    last_checked TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ship_status ON ship(status);
CREATE INDEX IF NOT EXISTS idx_ship_folder ON ship(model_folder);

-- 2. 战舰快照（规范化 JSON，导出链路读它拿装甲/挂载/涂装）
CREATE TABLE IF NOT EXISTS ship_data (
    ship_id    INTEGER PRIMARY KEY REFERENCES ship(id) ON DELETE CASCADE,
    data_json  TEXT    NOT NULL,
    json_len   INTEGER NOT NULL DEFAULT 0,
    sha1       TEXT    NOT NULL DEFAULT '',         -- 判「快照有没有变」的唯一依据
    updated_at TEXT    NOT NULL DEFAULT ''
);

-- 3. 模型（从表；一个模型目录一条）
CREATE TABLE IF NOT EXISTS model (
    model_id     TEXT    PRIMARY KEY,               -- BSD100_Waterloo_1956
    model_path   TEXT    NOT NULL DEFAULT '',       -- .model 的 VFS 路径
    kind         TEXT    NOT NULL DEFAULT 'mount',  -- hull / mount
    sig          TEXT    NOT NULL DEFAULT '',       -- 该目录下全部模型文件的签名
    main_crc     INTEGER NOT NULL DEFAULT 0,        -- 主 .geometry（装甲只在主文件里）
    main_size    INTEGER NOT NULL DEFAULT 0,
    file_count   INTEGER NOT NULL DEFAULT 0,
    status       TEXT    NOT NULL DEFAULT 'active',
    first_build  TEXT    NOT NULL DEFAULT '',
    first_seen   TEXT    NOT NULL DEFAULT '',
    last_build   TEXT    NOT NULL DEFAULT '',
    last_changed TEXT    NOT NULL DEFAULT '',
    last_checked TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_model_status ON model(status);

-- 4. 模型文件清单（idx 视角：说得清「到底哪个文件变了」）
CREATE TABLE IF NOT EXISTS model_file (
    model_id TEXT    NOT NULL REFERENCES model(model_id) ON DELETE CASCADE,
    path     TEXT    NOT NULL,
    role     TEXT    NOT NULL DEFAULT '',           -- main / seg
    crc32    INTEGER NOT NULL DEFAULT 0,
    size     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(model_id, path)
);

-- 5. 船 ↔ 模型（N:N；挂载共享，所以不能把 model_id 塞进 ship 表）
CREATE TABLE IF NOT EXISTS ship_model (
    ship_id  INTEGER NOT NULL REFERENCES ship(id) ON DELETE CASCADE,
    model_id TEXT    NOT NULL,
    role     TEXT    NOT NULL DEFAULT '',           -- hull / A_Artillery.HP_JGM_1
    PRIMARY KEY(ship_id, model_id, role)
);
CREATE INDEX IF NOT EXISTS idx_sm_model ON ship_model(model_id);

-- 6. schema 版本（低版本 → initialize 整库重建，之后一直增量）
CREATE TABLE IF NOT EXISTS meta_schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT DEFAULT (datetime('now','localtime'))
);
