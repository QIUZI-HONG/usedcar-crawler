# 运维手册（Runbook）

面向接手这个项目的人：上线怎么走、平时怎么盯、出问题怎么查、站点改版怎么救。

---

## 1. 首次部署

```bash
python -m venv .venv && .venv\Scripts\activate     # Windows
pip install -r requirements.txt
scrapling install                                   # 仅 stealth/dynamic 档位需要浏览器依赖
pip install -e .                                    # 让 python -m usedcar_crawler 可用
copy .env.example .env                              # 按需修改，密钥只放这里

python -m usedcar_crawler initdb                    # 建表（幂等）
python -m usedcar_crawler selftest                  # 离线全链路自检，必须通过
python -m usedcar_crawler compliance --verify       # 合规巡检，确认目标源可抓
python -m usedcar_crawler probe --source guazi_fuel # 逐源校准选择器命中率
python -m usedcar_crawler run --source guazi_fuel --pages 1   # 单源单页试跑
```

**上线顺序铁律**：`selftest` → `compliance --verify` → `probe` → 单源单页 → 放量 → 挂调度。
不要跳过中间任何一步直接跑全量。

---

## 2. 日常巡检（建议每日一次，或用自动化定时执行）

| 检查项 | 命令 / 位置 | 判读 |
|---|---|---|
| 任务是否都成功 | `usedcar-crawler logs --limit 20` | 出现 `failed` 需立即看 `error_detail` |
| 是否出现改版信号 | `logs` 输出的「缺失率」列 | > 30% → 走 §4 改版应急 |
| 数据量是否异常 | `usedcar-crawler stats --line all` | 在售数与昨日差 ±30% 以上需排查 |
| 下架量是否飙升 | `stats` 的 `deleted` | 突增通常意味着选择器失效（而不是真的集体下架） |
| 合规状态是否过期 | `compliance`（档案）/ `compliance --verify`（实时） | 档案 `checked_at` 超过 90 天建议复核 |
| 磁盘占用 | `data/raw` 目录 | 快照按天分目录，建议保留 30 天后归档 |

---

## 3. 排障手册

### 3.1 任务 failed：`robots.txt 明确禁止抓取`
**这是预期行为，不是 bug。** 说明该源的真实 robots.txt 禁止抓取。
处理：更换数据源，或走官方 API / 数据合作渠道。**不要**把 `respect_robots` 改成 false 绕过。

### 3.2 任务 failed：`所有档位均取数失败`
排查顺序：
1. 看日志里 `tier=http/stealth/dynamic` 各自的失败原因（`errors` 字段会串起来）；
2. 只测 HTTP 档：`probe --source <key>`，看是网络不通还是被拦；
3. 被拦（403/429/503）→ 降 QPS（`sites.yaml` 里的 `qps` 调到 0.1~0.2）、换时间窗重试；
4. 仍失败 → 检查是否需要代理（`.env` 的 `UCC_FETCH__PROXY`）。

### 3.3 `缺失率 > 30%` / `schema_drift` 告警
站点改版了。走 §4。

### 3.4 容器选择器零命中（`ParseError`）
比字段缺失更彻底的结构变化。同样走 §4，但需要重新写 `card` 选择器。

### 3.5 入库量为 0 但抓到很多
看日志里的 `rejected`：
- 大量 `price_wan 缺失或非正数` → 价格文案格式变了（例如从「15.98万」变成「15.98」或图文价格）；
  先看 `data/raw` 里的快照确认实际文案，再决定是改选择器还是改 `cleaners.parse_price_wan`。

### 3.6 出现重复数据
不应该发生（`vehicle_key` 唯一索引 + UPSERT）。若发生，检查：
- `source_id` 提取是否失效（详情链接结构变化 → `extract_source_id` 拿不到 ID → 退化指纹随价格变化而变化）；
- 用 SQL 确认：`SELECT source_id, COUNT(*) FROM ev_vehicles GROUP BY source_id HAVING COUNT(*) > 1;`

### 3.7 告警没收到
`usedcar-crawler notify-test`；确认 `.env` 里 `UCC_NOTIFY__WEBHOOK_URL` 与 `enabled`，
且事件名在 `notify.on_events` 白名单内。

---

## 4. 站点改版应急流程（SOP）

1. **确认影响面**：`logs --limit 20`，确定是单源还是全站；
2. **取证**：打开最近一次快照 `data/raw/<line>/<date>/<source>/<hash>.html.gz`（gzip 解压后即是当时的页面）；
3. **定位变化**：对比快照中卡片内的 DOM 结构，找出改了什么（class 改名 / 字段移到别的节点 / 改成异步加载）；
4. **改选择器**：只改 `config/sites.yaml`，**不改代码**；
5. **校准**：`probe --source <key>`，确认 `missing_required` 为空；
6. **试跑**：`run --source <key> --pages 1`，确认入库量与缺失率正常；
7. **回放验证（可选，推荐）**：用快照重跑解析，确认历史数据口径一致；
8. **放量**：`run --source <key> --pages 10`，观察 `logs` 稳定性；
9. **记录**：在 `sites.yaml` 该源的 `compliance.note` 或提交信息里写下改版日期与变更点，
   方便下次同类问题 5 分钟内定位。

> 若页面改成了异步加载（HTML 里没有数据），把该源的 `tier` 从 `http` 改成 `stealth` 或 `dynamic`。

---

## 5. 变更与发布纪律

| 变更类型 | 必做 |
|---|---|
| 改选择器（`sites.yaml`） | `probe` 校准 → 单源单页试跑 → 记录变更点 |
| 改清洗规则（`cleaners.py`） | 跑 `pytest tests/test_cleaners.py`，并抽样比对历史数据口径 |
| 改表结构 | 编写迁移脚本 + 备份；`schema.sql` 与 `data-dictionary.md` 同步更新 |
| 新增数据源 | 先填 `compliance` 档案（执行 `compliance --verify`）→ 再写选择器 → probe → 小流量试跑 |
| 调整调度频率 | 只改 `settings.yaml` 的 `schedule.jobs`；避免多任务重叠（`max_instances=1` 已保证互斥） |

---

## 6. 合规红线（任何人不得绕过）

1. **只采集公开可访问信息**，不登录、不绕过验证码、不破解付费墙；
2. **robots.txt 一票否决**：禁止即停止，不存在"降低频率就可以抓"；
3. **不在配置档案标为 `disallowed` 的站点上做选择器调试**（`probe` 也会发请求）；
4. **不采集个人身份信息**（卖家姓名、电话、微信等一律不落库）；
5. **保持礼貌**：默认 QPS ≤ 0.5，夜间低峰执行，失败不疯狂重试；
6. 数据仅用于本项目声明的分析用途，不做二次转售。

> 已核查结论（2026-09-18）：**懂车帝、汽车之家二手车 robots.txt 为 `Disallow: /`（全站禁止）**，
> 已在配置中 `enabled: false` 并保留审计痕迹；**瓜子二手车允许抓取但禁止带查询参数的 URL**，
> 配置层已做硬校验（`allow_query_params: false`）。

---

## 7. 常用命令速查

```bash
usedcar-crawler initdb                          # 初始化表结构
usedcar-crawler selftest                        # 离线全链路自检（CI 同款）
usedcar-crawler compliance [--verify]           # 合规巡检（--verify 联网复核）
usedcar-crawler probe --source <key> [--page N] # 选择器校准
usedcar-crawler run --line all --pages 10       # 采集
usedcar-crawler run --source <key> --pages 1    # 单源试跑
usedcar-crawler run --line all --fixture        # 用离线样本跑（零网络）
usedcar-crawler stats --line all --summary      # 统计 + 生成 Markdown 摘要
usedcar-crawler export --line ev --format both  # 导出 Excel/CSV
usedcar-crawler logs --limit 20                 # 最近采集日志
usedcar-crawler schedule                        # 常驻调度
usedcar-crawler schedule --once                 # 立即执行一轮（验证配置）
usedcar-crawler notify-test                     # 告警通道连通性
pytest -q                                       # 全部测试
```

---

## 8. 备份与恢复

| 对象 | 方式 | 频率 |
|---|---|---|
| 数据库 | MySQL 主从 / 每日 `mysqldump`（SQLite 则直接复制 `.db`） | 每日 |
| 原始快照 | `data/raw` 归档到对象存储（按天分目录，便于按日期取用） | 每周 |
| 配置 | `config/*.yaml` 纳入 Git（**不含任何密钥**） | 每次变更 |

恢复：先恢复数据库，再把 `data/raw` 上的快照按需回放（解析层可独立重跑，不需要重新抓取）。
