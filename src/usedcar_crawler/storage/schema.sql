-- ============================================================
-- 参考 DDL（MySQL 8 语法）。程序启动时由 SQLAlchemy 自动建表（幂等），
-- 本文件用于 DBA 评审、迁移脚本编写与文档说明。
-- SQLite 由 ORM 层自动降级（MySQL 专有语法不使用）。
-- ============================================================

CREATE TABLE IF NOT EXISTS ev_vehicles (
    id                  BIGINT       NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    vehicle_key         VARCHAR(80)  NOT NULL COMMENT '车源唯一键 MD5(平台+车源ID)',
    source_platform     VARCHAR(40)  NOT NULL COMMENT '来源平台标识',
    source_id           VARCHAR(80)  NULL     COMMENT '平台内车源 ID',
    match_key           VARCHAR(200) NULL     COMMENT '跨源同车归并键',
    title_raw           VARCHAR(500) NULL     COMMENT '原始标题（审计用）',
    brand               VARCHAR(50)  NULL     COMMENT '品牌',
    model               VARCHAR(120) NULL     COMMENT '车型',
    price_wan           DECIMAL(10,2) NULL    COMMENT '售价（万元）',
    new_car_price_wan   DECIMAL(10,2) NULL    COMMENT '新车指导价（万元）',
    retention_rate      DECIMAL(8,4) NULL     COMMENT '保值率 = 售价 / 指导价',
    mileage_km          INT          NULL     COMMENT '表显里程（公里）',
    reg_year            INT          NULL     COMMENT '上牌年份',
    reg_month           TINYINT      NULL     COMMENT '上牌月份',
    transfer_count      TINYINT      NULL     COMMENT '过户次数',
    location_city       VARCHAR(40)  NULL     COMMENT '车辆所在地',
    detail_url          VARCHAR(700) NULL     COMMENT '详情页链接',

    -- 新能源专属
    battery_type        VARCHAR(20)  NULL     COMMENT '电池类型：三元锂/磷酸铁锂/钠离子/未知',
    range_km            INT          NULL     COMMENT '标称续航（公里）',
    range_standard      VARCHAR(10)  NULL     COMMENT '续航口径：CLTC/NEDC/WLTP',
    battery_health      DECIMAL(5,1) NULL     COMMENT 'SOH 电池健康度（%）',
    fast_charge_kw      DECIMAL(6,1) NULL     COMMENT '快充峰值功率（kW）',

    -- 元数据
    raw_ref             VARCHAR(300) NULL     COMMENT '原始快照相对路径（可溯源）',
    captured_at         DATETIME     NOT NULL COMMENT '本次抓取时间',
    last_seen_at        DATETIME     NOT NULL COMMENT '最近一次出现的抓取时间',
    missing_count       INT          NOT NULL DEFAULT 0 COMMENT '连续未见次数',
    is_deleted          TINYINT      NOT NULL DEFAULT 0 COMMENT '是否已下架（1=是）',
    PRIMARY KEY (id),
    UNIQUE KEY uk_ev_vehicle_key (vehicle_key),
    KEY idx_ev_brand_price (brand, price_wan),
    KEY idx_ev_captured (captured_at),
    KEY idx_ev_city (location_city),
    KEY idx_ev_match (match_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='新能源二手车车源明细';

CREATE TABLE IF NOT EXISTS fuel_vehicles (
    id                  BIGINT       NOT NULL AUTO_INCREMENT,
    vehicle_key         VARCHAR(80)  NOT NULL,
    source_platform     VARCHAR(40)  NOT NULL,
    source_id           VARCHAR(80)  NULL,
    match_key           VARCHAR(200) NULL,
    title_raw           VARCHAR(500) NULL,
    brand               VARCHAR(50)  NULL,
    model               VARCHAR(120) NULL,
    price_wan           DECIMAL(10,2) NULL,
    new_car_price_wan   DECIMAL(10,2) NULL,
    retention_rate      DECIMAL(8,4) NULL,
    mileage_km          INT          NULL,
    reg_year            INT          NULL,
    reg_month           TINYINT      NULL,
    transfer_count      TINYINT      NULL,
    location_city       VARCHAR(40)  NULL,
    detail_url          VARCHAR(700) NULL,

    -- 燃油专属
    displacement_l      DECIMAL(4,1) NULL COMMENT '排量（升）',
    gearbox             VARCHAR(10)  NULL COMMENT '变速箱：MT/AT/CVT/DCT/AMT/单速',
    emission_standard   VARCHAR(10)  NULL COMMENT '排放标准：国一 ~ 国六B',

    raw_ref             VARCHAR(300) NULL,
    captured_at         DATETIME     NOT NULL,
    last_seen_at        DATETIME     NOT NULL,
    missing_count       INT          NOT NULL DEFAULT 0,
    is_deleted          TINYINT      NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY uk_fuel_vehicle_key (vehicle_key),
    KEY idx_fuel_brand_price (brand, price_wan),
    KEY idx_fuel_captured (captured_at),
    KEY idx_fuel_emission (emission_standard),
    KEY idx_fuel_match (match_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='燃油二手车车源明细';

CREATE TABLE IF NOT EXISTS crawl_log (
    id              BIGINT      NOT NULL AUTO_INCREMENT,
    business_line   VARCHAR(10) NOT NULL COMMENT 'ev / fuel',
    source_platform VARCHAR(40) NOT NULL,
    task_type       VARCHAR(20) NOT NULL COMMENT 'daily_incr / weekly_full / retry / selftest',
    status          VARCHAR(10) NOT NULL COMMENT 'success / partial / failed',
    pages_fetched   INT         NOT NULL DEFAULT 0,
    fetched_count   INT         NOT NULL DEFAULT 0 COMMENT '抓到的卡片数',
    parsed_count    INT         NOT NULL DEFAULT 0 COMMENT '解析通过数',
    inserted_count  INT         NOT NULL DEFAULT 0,
    updated_count   INT         NOT NULL DEFAULT 0,
    dup_count       INT         NOT NULL DEFAULT 0 COMMENT '批内去重丢弃数',
    error_count     INT         NOT NULL DEFAULT 0,
    missing_ratio   DECIMAL(6,4) NOT NULL DEFAULT 0 COMMENT '必需字段缺失率',
    error_detail    TEXT        NULL,
    started_at      DATETIME    NOT NULL,
    finished_at     DATETIME    NULL,
    PRIMARY KEY (id),
    KEY idx_log_source_time (source_platform, started_at),
    KEY idx_log_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='采集任务运行日志（异常记录）';
