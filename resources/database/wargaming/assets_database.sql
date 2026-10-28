-- WARGAMING（WG）服 3D 缓存库架构 —— 与 Lesta 版独立，逐点调整
-- 检修：WG assets_data 表结构差异只改本文件（不要动 lesta/assets_database.sql）。
-- 加载：services/assets_cache_service.py 按 is_wg() 选本文件。
-- 加载方式：resources.qrc（:/resources/database/wargaming/assets_database.sql）→ 文件系统回退。
--
-- 设计（2026-10-28 重构）：全部去掉 bin_folder 主键列，改按 model_id 组织 ——
-- 否则 build 一变，未变动的船那上百万行也得 UPDATE 到新 build，等于全量重写。
-- 库只反映「最后一次同步」的那个 build，build 号记在 meta 里。

-- 库级元信息：build / game_version / wows_type / synced_at / schema_ver
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- 舰体/模型的 HP_ 挂点（解码后：坐标 pos / 方向四元数 quat / 缩放 scale）
CREATE TABLE IF NOT EXISTS skeleton_mounts (
    model_id TEXT NOT NULL,
    hp_name  TEXT NOT NULL,
    pos_x REAL NOT NULL DEFAULT 0,
    pos_y REAL NOT NULL DEFAULT 0,
    pos_z REAL NOT NULL DEFAULT 0,
    rot_qx REAL NOT NULL DEFAULT 0,
    rot_qy REAL NOT NULL DEFAULT 0,
    rot_qz REAL NOT NULL DEFAULT 0,
    rot_qw REAL NOT NULL DEFAULT 1,
    scale_x REAL NOT NULL DEFAULT 1,
    scale_y REAL NOT NULL DEFAULT 1,
    scale_z REAL NOT NULL DEFAULT 1,
    PRIMARY KEY(model_id, hp_name)
);

-- 完整骨骼（bind pose 按 parent 累积）：引用小零件定位 / Root_BlendBone
CREATE TABLE IF NOT EXISTS skeleton_bones (
    model_id  TEXT NOT NULL,
    bone_name TEXT NOT NULL,
    pos_x REAL NOT NULL DEFAULT 0,
    pos_y REAL NOT NULL DEFAULT 0,
    pos_z REAL NOT NULL DEFAULT 0,
    rot_qx REAL NOT NULL DEFAULT 0,
    rot_qy REAL NOT NULL DEFAULT 0,
    rot_qz REAL NOT NULL DEFAULT 0,
    rot_qw REAL NOT NULL DEFAULT 1,
    scale_x REAL NOT NULL DEFAULT 1,
    scale_y REAL NOT NULL DEFAULT 1,
    scale_z REAL NOT NULL DEFAULT 1,
    PRIMARY KEY(model_id, bone_name)
);

-- visual 渲染集：shape(.vertices) → 材质 / mfm / 是否损伤网格
-- model_id 冗余存一份：增量重建要按模型目录整片删，SQL 里没法从路径反推。
CREATE TABLE IF NOT EXISTS render_set (
    geom_path TEXT NOT NULL,
    shape     TEXT NOT NULL,
    model_id  TEXT NOT NULL DEFAULT '',
    material  TEXT NOT NULL DEFAULT '',
    mfm       TEXT NOT NULL DEFAULT '',
    damage    INTEGER NOT NULL DEFAULT 0,
    skinned   INTEGER NOT NULL DEFAULT 0,
    nodes     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(geom_path, shape)
);
CREATE INDEX IF NOT EXISTS idx_rs_model ON render_set(model_id);

-- 材质 diffuseMap 贴图原始路径（只存路径，字节渲染时实时从客户端 pkg 解包）
-- 全局表：一个 mfm 被多船共用，无法归属到单个模型，每次同步全量刷新。
CREATE TABLE IF NOT EXISTS mfm_textures (
    mfm_path     TEXT PRIMARY KEY,
    texture_path TEXT NOT NULL DEFAULT ''
);

-- 材质完整信息：shader_id / 技术族 / fx 路径 / 全部贴图路径(JSON) / INDEXED vec4 数组(JSON)
CREATE TABLE IF NOT EXISTS material_full (
    mfm_path      TEXT PRIMARY KEY,
    shader_id     TEXT NOT NULL DEFAULT '0x0',
    family        TEXT NOT NULL DEFAULT '',
    material_hash TEXT NOT NULL DEFAULT '',
    fx_path       TEXT NOT NULL DEFAULT '',
    textures      TEXT NOT NULL DEFAULT '',
    indexed       TEXT NOT NULL DEFAULT ''
);

-- *.vertices 名字哈希 → 名字（LOD/crack 兜底跳过用），全局表
CREATE TABLE IF NOT EXISTS shape_names (
    hash INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

-- schema 版本（低版本 → initialize 重建全表，随后记录当前版本号）
CREATE TABLE IF NOT EXISTS meta_schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 涂装/外观（Exterior，GameParams 直读）：species 区分类别
-- （Camouflage/皮肤/Decal/旗帜等）；parts/custom 等按 JSON 存。
CREATE TABLE IF NOT EXISTS exteriors (
    ext_index    TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    camouflage   TEXT NOT NULL DEFAULT '',
    species      TEXT NOT NULL DEFAULT '',
    nation       TEXT NOT NULL DEFAULT '',
    cost_gold    INTEGER DEFAULT 0,
    can_buy      INTEGER DEFAULT 0,
    parts_json   TEXT DEFAULT '[]',
    custom_json  TEXT DEFAULT '{}',
    unpeculiar_camouflage TEXT DEFAULT '',
    color_scheme_id INTEGER DEFAULT 0,
    icon_path    TEXT DEFAULT '',
    data_json    TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_exteriors ON exteriors(species);

-- camouflages.xml 条目（包根，含 shipgroups/colorschemes/camouflages 三段）
CREATE TABLE IF NOT EXISTS camouflages (
    name         TEXT PRIMARY KEY,
    realm        TEXT NOT NULL DEFAULT '',
    tiled        INTEGER DEFAULT 0,
    use_color_scheme INTEGER DEFAULT 0,
    color_scheme TEXT DEFAULT '',
    textures_json TEXT DEFAULT '{}',
    uv_json      TEXT DEFAULT '{}',
    ship_groups_json TEXT DEFAULT '[]',
    target_ships_json TEXT DEFAULT '[]',
    icon_path    TEXT DEFAULT ''
);

-- colorschemes.xml 颜色方案（color0..3 各 4 float，JSON）
CREATE TABLE IF NOT EXISTS camouflage_color_schemes (
    name   TEXT PRIMARY KEY,
    color0 TEXT, color1 TEXT, color2 TEXT, color3 TEXT
);

-- shipgroups.xml 全局船组映射（组名 → 船 index）
CREATE TABLE IF NOT EXISTS camo_ship_groups (
    group_name TEXT NOT NULL,
    ship       TEXT NOT NULL,
    PRIMARY KEY(group_name, ship)
);
