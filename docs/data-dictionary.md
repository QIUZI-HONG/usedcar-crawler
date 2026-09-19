# 数据字典与采集说明

> 本文档是"数据交付契约"：业务方按此使用数据，采集方按此维护字段。
> 任何字段口径变更都必须同步修改本文件与 `pipeline/cleaners.py`、`parsers/`。

## 1. 表结构总览

| 表 | 说明 | 数据源业务线 |
|---|---|---|
| `ev_vehicles` | 新能源二手车车源明细 | 新能源（EV） |
| `fuel_vehicles` | 燃油二手车车源明细 | 燃油（FUEL） |
| `crawl_log` | 采集任务运行日志（异常记录） | 两条线共用 |

## 2. 通用字段（两表共有）

| 字段 | 类型 | 说明 | 缺失时的处理 |
|---|---|---|---|
| `vehicle_key` | varchar(80) | 唯一键，`MD5(平台 + 车源ID)`；无车源ID时退化为标题+价格+里程指纹 | 必填，缺失即丢弃该条 |
| `source_platform` | varchar(40) | 来源平台标识（对应 `sites.yaml` 的 `key`） | 必填 |
| `source_id` | varchar(80) | 平台内车源 ID | 允许为空 |
| `match_key` | varchar(200) | 跨源同车归并键 `品牌\|车型\|年款\|上牌月\|里程分段` | 核心维度缺失时不生成（宁可不归并） |
| `title_raw` | varchar(500) | 原始标题，不做改写（审计用） | 允许为空 |
| `brand` / `model` | varchar(50/120) | 标准化品牌 / 车型 | 无法识别时为 NULL |
| `price_wan` | decimal(10,2) | 售价，单位**万元** | **NULL 即丢弃该条**（"面议"不是有效价格） |
| `new_car_price_wan` | decimal(10,2) | 新车指导价，单位万元 | 允许为空（保值率随之缺失） |
| `retention_rate` | decimal(8,4) | 保值率 = 售价 / 新车指导价，保留 4 位小数 | 分母缺失或非正时不计算 |
| `mileage_km` | int | 表显里程，统一为**公里** | 缺失记 NULL，**绝不写 0** |
| `reg_year` / `reg_month` | int | 上牌年月；年份限定 1990 ~ 当前年+1 | 月份缺失记 NULL |
| `transfer_count` | int | 过户次数 | 缺失记 NULL |
| `location_city` | varchar(40) | 车辆所在地（去"市"后缀） | 允许为空 |
| `detail_url` | varchar(700) | 详情页绝对链接 | 允许为空 |
| `raw_ref` | varchar(300) | 原始快照相对路径（可溯源） | 关闭快照时为 NULL |
| `captured_at` | datetime | 本次抓取时间 | 必填 |
| `last_seen_at` | datetime | 最近一次被成功抓取到的时间 | 必填（系统维护） |
| `missing_count` | int | 连续未见轮数 | 系统维护 |
| `is_deleted` | tinyint | 是否已下架（1=是），由 `missing_count` 达阈值自动置位 | 系统维护 |

### 2.1 新能源专属字段（`ev_vehicles`）

| 字段 | 类型 | 口径 | 示例 → 入库值 |
|---|---|---|---|
| `battery_type` | varchar(20) | 三元锂 / 磷酸铁锂 / 钴酸锂 / 锰酸锂 / 钠离子 / 未知 | `电池类型：三元锂电池` → `三元锂`；`刀片电池` → `磷酸铁锂` |
| `range_km` | int | 标称续航里程（公里），仅接受 30~2000 的合理值 | `续航 CLTC 605km` → `605` |
| `range_standard` | varchar(10) | 续航口径：CLTC / NEDC / WLTP / EPA / 综合 | `NEDC续航 468公里` → `NEDC` |
| `battery_health` | decimal(5,1) | 电池健康度 SOH（%），仅接受 0 < x ≤ 100 | `SOH 91.5%` → `91.5` |
| `fast_charge_kw` | decimal(6,1) | 快充峰值功率（kW） | `快充 110kW` → `110.0` |

### 2.2 燃油专属字段（`fuel_vehicles`）

| 字段 | 类型 | 口径 | 示例 → 入库值 |
|---|---|---|---|
| `displacement_l` | decimal(4,1) | 排量（升），仅接受 0.6~8.0 | `1.5T` → `1.5`；`2.0L` → `2.0` |
| `gearbox` | varchar(10) | MT / AT / CVT / DCT / AMT / 单速 / 未知 | `CVT无级变速` → `CVT`；`手自一体` → `AT` |
| `emission_standard` | varchar(10) | 国一 ~ 国六B（长串优先匹配） | `国六B` → `国六B`（不会误判为 `国六`） |

## 3. 采集说明

| 项 | 说明 |
|---|---|
| 更新频率 | 每日增量（默认 02:30 / 02:50），每周六 04:00 全量校准；由 `settings.yaml` 的 `schedule.jobs` 控制 |
| 单站限速 | 默认 QPS 0.5（2 秒 1 次），站点可在 `sites.yaml` 覆盖；瓜子设为 0.25 |
| 请求特征 | 使用真实浏览器 UA 与请求头（Scrapling `stealthy_headers`），夜间低峰执行 |
| 翻页 | 路径式翻页优先；**禁止使用目标站 robots.txt 禁止的 query 参数形式** |
| 快照留存 | 原始响应 gzip 留存，含 URL / 状态码 / 档位 / 内容 MD5 / 大小 |
| 去重 | 同一车源重复抓取走 UPDATE（价格更新为最新值），不新增行 |
| 下架标记 | 连续 3 轮（可配置）未出现 → `is_deleted=1`；数据保留但默认不参与统计与导出 |

## 4. 异常与降级规则

| 场景 | 判定 | 处置 |
|---|---|---|
| robots.txt 禁止 | `can_fetch` 为假 | **一票否决**，抛 `RobotsDeniedError`，不重试、不升档；任务标记 failed 并告警 |
| 配置档案已判禁止 | `compliance.robots_status=disallowed` | 连请求都不发，直接拦截并告警（`compliance_blocked`） |
| 页面不存在（404/410） | 状态码闸门 | 立即失败，不重试、不升档 |
| 被反爬拦截（401/403/405/429/503 或特征文案） | 状态码 + 关键词 | **升档**（http → stealth → dynamic），不原地重试；末档仍被拦则抛 `BlockedError` |
| JS 空壳页 | 页面含 `id="app"`/`__NEXT_DATA__` 等且体积过小 | 升档到浏览器渲染 |
| 网络抖动（超时、连接重置、5xx） | `retryable=True` | 指数退避重试（默认 3 次，基数 2 秒） |
| 容器选择器零命中 | 解析层 | 抛 `ParseError`，判定为结构性改版，停止该页翻页 |
| 必需字段缺失率 > 30% | 解析层（按字段单独判定） | 抛 `SchemaDriftError`、任务标记 `partial`、推送 `schema_drift` 告警并留存快照 |
| 单条数据校验失败 | Pydantic 模型 | 丢弃该条并留样（最多 5 条样本进日志），不中断整批 |
| 入库失败 | SQLAlchemy 异常 | 抛 `StorageError`，任务标 failed，其他源不受影响 |

## 5. 数据质量指标（`stats` 命令输出）

| 指标 | 含义 | 健康参考 |
|---|---|---|
| `total` / `active` | 累计车源数 / 在售车源数 | — |
| `deleted` | 已下架数 | 占比过高说明源不稳定或判定阈值过严 |
| `by_platform` | 各源车源数分布 | 单源占比 > 80% 属"单点依赖"，需补充数据源 |
| `avg_price_wan` / `min` / `max` | 价格分布 | 极值异常通常意味着单位解析错误 |
| `avg_retention_rate` | 平均保值率 | 新能源线普遍低于燃油线属正常 |
| `crawl_log.missing_ratio` | 最差必需字段缺失率 | > 30% 即视为改版信号 |
| `crawl_log.error_count` | 错误 + 被拒数据条数 | 持续 > 10% 需检查选择器或目标站变动 |

## 6. 使用示例

```sql
-- 某品牌近 30 天在售车源均价（运营临时取数场景）
SELECT brand, model, COUNT(*) AS cnt, ROUND(AVG(price_wan), 2) AS avg_price
FROM ev_vehicles
WHERE is_deleted = 0 AND brand = '比亚迪' AND captured_at >= NOW() - INTERVAL 30 DAY
GROUP BY brand, model ORDER BY cnt DESC;

-- 电池类型 × 续航区间的价格分布（新能源专属分析）
SELECT battery_type,
       CASE WHEN range_km < 400 THEN '<400'
            WHEN range_km < 600 THEN '400-600'
            ELSE '600+' END AS range_bucket,
       COUNT(*) AS cnt, ROUND(AVG(price_wan), 2) AS avg_price, ROUND(AVG(retention_rate), 4) AS avg_retention
FROM ev_vehicles WHERE is_deleted = 0
GROUP BY battery_type, range_bucket ORDER BY cnt DESC;

-- 跨源同一台车的比价（依赖 match_key）
SELECT match_key, COUNT(DISTINCT source_platform) AS platforms,
       MIN(price_wan) AS min_price, MAX(price_wan) AS max_price
FROM fuel_vehicles WHERE is_deleted = 0 AND match_key IS NOT NULL
GROUP BY match_key HAVING platforms > 1 ORDER BY (max_price - min_price) DESC LIMIT 20;
```
