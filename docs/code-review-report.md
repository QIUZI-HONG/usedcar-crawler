# usedcar-crawler 项目评审报告

> 评审视角：大型互联网公司**测试主管 / 质量负责人**（关注"能不能上线、出事能不能定位、数据能不能信"）
> 评审对象：`D:\Demo_Test\usedcar-crawler`（对照基准：`D:\Demo_Test\二手车爬虫项目计划书.md`）
> 评审时间：2026-09-19 · 评审环境：Windows / 独立隔离虚拟环境（Python 3.13.12，按 `requirements.txt` 现装）
> 评审方式：**全部结论均现场实测**，不采信文档自述。核查脚本见 §6，可逐条复现。

---

## 0. 总体结论

**一句话**：这是一个**代码与文档质量明显高于同届作品、但"交付叙事"跑在了"可验证证据"前面**的项目。
核心风险不是"数据造假"，而是**"数据是怎么来的"这一说法在仓库里找不到证据**——一旦面试官追问细节，会被当场问穿。

| 评审维度 | 评分 | 结论 |
|---|---|---|
| 一、展示效果（专业/完整/说服力） | **7.0 / 10** | 结构、文档、看板完成度高；但样本代表性差、口径说明缺失，"行情结论"站不住 |
| 二、数据真实性（造假/夸大/矛盾/复现） | **5.5 / 10** | **数值全真**（已交叉验证）；**取证链断裂 + 多处表述与自身证据矛盾** |
| 三、规范化程度（代码/文档/测试/流程） | **7.0 / 10** | 代码分层、测试、文档都在及格线以上；缺版本控制、CI、依赖锁定、关键路径测试 |

**已实测为真的部分（先给结论，避免误伤）**：

| 声明 | 实测结果 |
|---|---|
| "229 个用例全部通过" | ✅ 实测 **229 passed**（0 failed），全程离线 |
| "溯源对账 103/103，0 处不符" | ✅ 实测 `tools/verify_against_raw.py` → 核验 103 条，字段不一致 0、无 raw_ref 0、原始文件缺失 0 |
| 瓜子 robots.txt 结论 | ✅ 联网实测，与 `docs/DATA_SOURCE.md` / `sites.yaml` **逐字一致**（含 `Allow: /car-detail/*.md`、`Allow: /*.md$`、`Disallow: /*?*`、Sitemap 行） |
| "站点地图 14 张子地图、约 14 万条" | ✅ 落盘快照实测 14/14 张，13 张各 10,000 条 + 1 张 3,753 条，并集 **133,753** |
| "103 条 = 新能源 30 / 燃油 73" | ✅ 实测 DB：ev 30 / fuel 73 = 103 |
| "45 个品牌 / 39 个城市 / 均价 9.30 万 / 保值率 38.6%" | ✅ 实测与看板内嵌数据完全一致（9.3014 万 / 38.59%） |
| "看板 11 张图表、单文件离线可开" | ✅ 实测模板 11 个图表容器；`echarts.min.js` 本地化 + CDN 兜底 |
| "exports/ 下 4 个真实文件" | ✅ 实测 4 个文件；CSV 30+73 行、xlsx 含「车源明细 + 字段说明」双 sheet |
| "脏数据被拒收、缺失留空不填 0" | ✅ `price_wan` 缺失即丢弃（日志可见 `面议` 被拒）；售价>指导价 0 条、月均里程>8000km 0 条 |
| 详情页 `.md` 通道内容真实 | ✅ 抽检 `c163187512324288.md`：**与线上实时返回逐字一致**（含 `generatedAt`），库里 103 个 ID **全部**出现在站点地图并集里 |

---

## 1. 维度一：展示效果

### 👍 做得好的

1. **三份面向不同读者的材料齐全**：`README.md`（技术）、`面试官演示说明.md`（非技术一页纸）、`docs/验收测试报告.md`（验收口径）。这个"分层交付"意识超过绝大多数学生作品。
2. **看板是真·离线可开**：单文件 HTML + 本地 ECharts（并带 CDN 兜底 + 缺库时告警不静默白屏），11 张图表、KPI 卡、明细表、字段完整率面板、采集日志面板。`preview.png` 还留了截图。
3. **合规证据链是亮点**：`docs/DATA_SOURCE.md` 把"为什么抓这个站、为什么不抓懂车帝/汽车之家"写成可审计档案，且结论**与线上 robots.txt 完全对得上**。这是本项目最扎实的一块。

### ❌ 具体问题

#### P0-1｜样本严重偏斜，却被当"行情大盘"输出

- **现象**：103 条里 **苏州占 53 条（51.5%）**，第二名重庆/青岛各 3 条。
- **证据**：`SELECT location_city, COUNT(*) FROM ev_vehicles+fuel_vehicles GROUP BY 1` → 苏州 53。
- **影响**：苏州不可能占全国二手车挂牌量的一半。看板顶部"平均售价 9.30 万 / 平均保值率 38.6% / 覆盖 39 城"会被读者当成**市场结论**，实际是**单城市样本**。业务同学拿这份表定价会出错。
- **定性**：不是造假，是**抽样口径未声明**。但"行情看板"这个命名本身就在误导。

#### P1-2｜看板画了"高缺失率"的图，视觉上等于骗人

- **现象**：EV 侧字段实测覆盖率——`battery_type` **3/30 = 10%**、`range_standard` **2/30 = 7%**、`fast_charge_kw` **0/30 = 0%**、`range_km` 18/30、`battery_health` 20/30。
- **证据**：DB 空值统计；`viz.py::_ev_specifics` 里 `battery = record.get("battery_type") or "未知"` **没有过滤"未知"**。
- **影响**：so-called「电池类型价格分布」图实际是「未知 27 条 + 3 条真实值」；`fast_charge_kw` 一列全空却仍出现在导出表头与字段面板里。
- **对比**：`_counter_to_rows()` 是过滤"未知"的，但 `battery_price` 走的是另一条路径 → **同一份代码里两种口径**。

#### P1-3｜时间口径混乱，用户可见的"抓取时间"是错的

- **现象**：`exports/*.csv` 的「抓取时间」列全部是 `2026-09-19 12:03:56`（= replay 执行时刻），不是内容抓取时刻（≈11:46–11:59）。
- **根因**：`build_raw_record` 的 `captured_at` 用 `default_factory=datetime.now()`，replay 时被覆盖成入库时间。
- **影响**：任何"近 30 天均价"类 SQL（`data-dictionary.md` §6 示例正是这么写的）都会算错；也让 `last_seen_at` / 下架追踪的时间语义失真。

#### P2-4｜交付物混放，容易被误读

- **现象**：工作区 `D:\Demo_Test\_removed_legacy_usedcar\` 内仍有 25 个带 `_demo` / `_selftest` 后缀的历史导出、`demo.db`、`demo_site/`（自建演示站点）与 `acceptance.db`（其中 6 条 `local_fixture_*` 数据）。
- **影响**：如果整个 `D:\Demo_Test` 被打包、截图或上传，读者极易把 demo 数据与交付数据混为一谈——恰好踩在本项目最想避开的点上。建议移出工作区或加 `ARCHIVE-README` 明确标注。

#### P2-5｜小的一致性瑕疵

- README §11 快速开始提到 `tools/verify_against_raw.py`，但"给面试官看的三样东西"表里没列 `preview.png`、`exports/`，读者容易漏掉最直观的证据。
- `tests/fixtures/PROVENANCE.md` 标题写"这两个文件"，表格里只有 1 行（`ev_list.html`），`fuel_list.html` 缺行。

---

## 2. 维度二：数据真实性（重点）

### 2.1 先说结论

| 问题类型 | 判定 |
|---|---|
| 是否**编造/伪造**数据？ | **否**。数值可追溯到真实公开内容，且与线上实时响应逐字一致。 |
| 是否**夸大**能力？ | **是**，且集中在"采集链路"上（见 P0-1）。 |
| 是否存在**逻辑矛盾**？ | **是**，多处（见 P1-3、P1-4）。 |
| 是否**可复现**？ | **部分不可复现**：数值可复现（有 DB + 语料），"怎么采到的"不可复现（无代码路径、无记账）。 |

### 2.2 P0-1｜最严重：103 条语料的取证链断裂，"由本项目 crawl 产出"无法成立

这是本次评审**唯一一个真正的红灯**。四条独立证据指向同一结论：

**证据 A｜全项目没有任何代码写入 `data/captured/`**

```
Grep "captured" src/  →  只有两处：detail_runner._replay_dir() 读它、cli.py 帮助文本提到它
```
- 正常采集路径（`crawl` → `_collect` → `fetcher.fetch_once`）的快照由 `SnapshotStore` 落到 **`data/raw/<line>/<date>/<source>/<hash>.html.gz`**，且 `raw_ref` 指向这个 `.gz`。
- 但 DB 里 103 条的 `raw_ref` **全部**指向 `data/captured/*.md`——这是**只有 replay 才会产生**的形态。
- ⇒ 这 103 个文件不是本项目采集链路写的。

**证据 B｜项目采集链路对详情页的真实成功率是 0 / 15**

`data/raw/both/2026-09-19/guazi_md/` 共 29 个快照：

| 类型 | 数量 | 状态 |
|---|---|---|
| 站点地图 XML | 14 | ✅ 全部 200、10,000 条/张（一张 3,753） |
| 车源详情页 | **15** | ❌ **全部 size=3720，即验证页**；满足 `looks_like_detail_md` 内容契约的 **0 个** |

`logs/crawler.log` 完全对应：11:19 首次试跑命中 `uc.guazi.com/.../captcha?...&request_ip=61.143.18.158`，4 次冷却后熔断、0 条数据。**项目自身的采集路径一次都没成功拿到过车源详情。**

**证据 C｜语料落盘时间与"由 crawl 产生"不符**

- 103 个 `.md` 的 mtime 集中在 **11:48–11:59**（约 8.6 个/分钟）；内嵌 `generatedAt` 落在 03:46:56Z–03:57:33Z（+8h = 11:46–11:57）。
- 但 `crawler.log` 在 **11:32:34（"URL 发现完成 urls=320"）到 12:01:01（replay 开始）之间完全空白**。而 `fetch_once` 每次成功必然落 `data/raw` 快照 + 写 `data/checkpoints/*.urls`——**两者都不存在**（`data/checkpoints/` 目录至今不存在）。

**证据 D｜工作区里留着"批量下载"的痕迹**

- `D:\Demo_Test\_removed_legacy_usedcar\data\_count.txt` = `FILE_COUNT=95`，`_names.txt`（11:59:04 写入）列出 95 个 `.md` 文件名，**全部**是当前 103 个语料的子集（多出的 8 个在 11:59 之后补入）。
- ⇒ 存在一个**项目外的批量下载步骤**，其脚本不在仓库内、文档未提及。

**这意味着什么（面试风险）**：

- `README.md` 第 7–9 行写"**没有任何一条模拟/伪造数据进入交付**"——这句是真的。
- 但 `面试官演示说明.md` 写"本项目交付的 103 条数据是**真实抓取的响应**""`crawl` 命令可直接实时抓取，**代码路径完全一致**"——这句**在证据层面不成立**：代码路径确实一致，但**这条路径从没成功过**。
- 一旦面试官问"你什么时候跑的采集？跑了多久？被拦了几次？" → 回答只能落在 `crawl_log`，而 `crawl_log` **只有 2 行，且两行都是 `task_type=replay`**。没有任何一条 live 采集记录。这是**最容易被抓住的点**。

> **公平地说**：语料本身是**站点地图里真实存在的车源**（103 个 ID 全部命中 133,753 条站点地图并集），内容也与线上逐字一致。所以这是**"过程叙事不实"**，不是**"数据造假"**。两者性质不同，但对面试官来说，前者同样致命。

### 2.3 P1-2｜文档多处表述与自身证据矛盾

| 文档表述 | 实际证据 | 性质 |
|---|---|---|
| 风控返回的是"HTTP 200 的 **SPA 空壳页**，长度恒定" | 日志显示实际是 `uc.guazi.com` 的 **captcha 挑战页**（len=3720，带 `request_ip` 参数） | 描述与观测不符 |
| "换 UA（含 GPTBot/ClaudeBot 白名单）、换真实浏览器指纹、直连与本机代理出口均被拦 → 属**出口 IP 级**软封禁" | 代码里**没有 UA 池、没有指纹切换、没有代理实验**的实现或测试；日志中 4 次挑战的 `request_ip` 在 `61.143.18.158 / 61.143.18.30` 之间变动 | 结论下得过早，且无留痕 |
| "断点续爬：每条成功即落 `data/checkpoints/*.urls`" | `data/checkpoints/` **不存在**（从未被真实采集触发） | 声称"已落地"实为"已实现未验证" |
| `PROVENANCE.md`："两条样本是真实抓取的**原文**""原样保留" | 两个夹具都被**裁剪**：分期表仅保留 10%/20% 两档 36/48 期，真实页面是 6 档 × 36/48/60 期 | "原文"不准确 |
| `data-dictionary.md`："瓜子设为 **0.25**" | `sites.yaml` 是 **0.2**；`DATA_SOURCE.md` 也是 0.2 | 三处数字打架 |
| 计划书 vs 实际：UA 池 / 随机 Referer / pandas 清洗管道 / MySQL 生产 / FastAPI / Next.js | 均未落地（`验收报告` §2.3 已诚实标注部分偏差，但 README 仍在暗示） | 计划书当成果讲有风险 |

### 2.4 P1-3｜抽样代表性不成立，"行情"结论无效

- EV 占 **30/103 = 29%**，与二手车市场实际新能源占比明显不符；
- 苏州 **51.5%**、单站单时段、站点地图顺序抽样（`sample_evenly`）→ 地域/时间双重偏斜；
- `验收报告` §4.1 只报"字段完整率 100%"，**没有报告样本代表性**——这是质量报告的一个硬缺口。
- **正确表述**应是"链路样本 / 数据质量样本"，而非"行情大盘"。

### 2.5 P2-4｜"GitHub 交付"路径实际不通

- `usedcar-crawler/` **不是 git 仓库**（无 `.git`，整个工作区也没有），但 README 与演示说明都宣称"GitHub 作品 / 可投递的 GitHub 主页"；
- `.gitignore` 忽略 `data/`、`exports/`、`logs/` → 若真按 GitHub 交付：**103 条语料、DB、导出全部缺失**；
- 实测在"库文件不存在"场景下执行 README 承诺的验证命令：
  ```
  python tools/verify_against_raw.py
  → sqlite3.OperationalError: no such table: ev_vehicles   （直接崩）
  ```
  即演示说明里"可当场执行的证明 ①"在 GitHub 场景下失效，只剩下"明细表每行带原站链接"这一条。
- 补充：本次评审的网络出口下，`https://www.guazi.com/car-detail/c163187512324288.md` **可正常返回真实内容** → "IP 级软封禁"是**特定出口的结论**，不是站点对该项目的通用限制。交付时应把这一点讲清楚，否则读者会误以为项目本身跑不通。

---

## 3. 维度三：规范化程度

### 👍 做得好的

- `src/` 布局 + 清晰分层（sources → fetcher → parsers → pipeline → storage → viz），每层都写明"不做什么"；
- **全量类型注解** + `from __future__ import annotations`；
- **Pydantic 当数据契约**（`price_wan` 缺失即拒收，绝不写 0）——这条红线落地得很干净；
- **分级异常体系**（`retryable` 决定是否重试、`RobotsDeniedError` 一票否决、单条失败不拖垮整批）；
- 结构化日志 + `trace_id` + 按天切割；配置三级覆盖（YAML < .env < 环境变量），密钥只走 env；
- 所有能力经 CLI 暴露（`run/crawl/replay/viz/probe/export/stats/logs/compliance/selftest/schedule`）；
- 229 个测试**全部离线可跑**（注入假 Transport），工程纪律好；
- 5 份文档（架构+ADR / 数据字典 / 运维手册 / 合规审计 / 验收报告），其中 `runbook.md` 的"改版应急 SOP"写得比很多在职工程师的团队文档都好。

### ❌ 具体问题

#### P0-1｜没有版本控制，也没有任何持续集成

- 无 `.git`（无提交历史、无分支、无 PR）、无 `.github/workflows`、无 pre-commit、无 CHANGELOG、无 LICENSE；
- `pyproject.toml` 里**没有 ruff / mypy / flake8 配置**，也没有覆盖率门槛（虽声明 `pytest-cov` 却无 `fail_under`）；
- `requirements.txt` 全是 `>=`，**无 lock 文件**。README 声称"实测 Scrapling 0.4.15"，但按此文件今天装出来的未必是 0.4.15 → **"换台机器结果一致"没有保障**。

#### P1-2｜测试覆盖"结构性强、关键路径弱"

229 例是真的，但覆盖面有明显空洞——最危险的代码路径恰好没测：

| 未覆盖的关键路径 | 风险 |
|---|---|
| `DetailCrawlRunner._collect()` 的**冷却 / 熔断 / 断点续爬**（`time.sleep` + 不推进 index 的循环） | 这是真实运行时唯一会跑的分支，却只有 replay 路径有测试 |
| **RobotsGate 的 fail-open 行为** | `_parser_for()` 在 robots.txt 请求异常时 `parser = None` → **放行**。与文档"robots 一票否决"直接矛盾；网络抖动即静默放行 |
| `viz.py` 全部聚合口径（`_age_of` / 分箱 / 均价 / 保值率） | 看板数字算错**没有任何测试能拦**，会以"图表看起来正常"的形式上线 |
| "快照回放 → 看板"冒烟 | 无（只有 replay → DB 的单元级覆盖） |

另外 `fetcher.py:143` 有一行死代码：
```python
parser = None if exc.code in (404, 410) else None   # 两个分支结果相同
```

#### P1-3｜可观测性缺口：详情页路径几乎没有请求级日志

- `fetch()` 每一页都打 `取数成功` 日志，但**详情页走的 `fetch_once()` 完全不打**。
- 后果就是本次评审遇到的窘境：**无法从日志重建 11:48–11:59 这 11 分钟里发生了什么**（证据 C 的根因）。一个"以可观测性为卖点"的项目，在最关键的路径上丢了日志。

#### P1-4｜业务逻辑缺陷（可一次性修）

1. **404 也写 checkpoint**：`_collect()` 里 `NotFoundError` 分支同样调用 `_append_checkpoint`，下架车会被**永久跳过**——与文档"重新上架自动恢复 `is_deleted=0`"矛盾。
2. **`captured_at` 语义失真**（见 1-P1-3）。
3. **`viz.py` 未过滤"未知"**（见 1-P1-2）。
4. **硬编码 Chrome UA 常量**：与"UA 池/合规身份"叙事不符；且用浏览器 UA 意味着站点针对具名爬虫的规则（robots 里 GPTBot/ClaudeBot 等段落）**不适用**，这一点应在文档里显式说明，而不是含糊带过。
5. **模块依赖不干净**：`pipeline/__init__.py` 顶层 import exporter → config → `yaml`，导致只想用 `cleaners.normalize_brand` 的工具脚本也必须装齐 `yaml`/`pandas`（本次评审实测：裸环境执行 `tools/verify_against_raw.py` 直接 `ModuleNotFoundError: yaml`）。

#### P2-5｜工程配套缺失

无 LICENSE、无 CONTRIBUTING、无 Issue/PR 模板、无 CHANGELOG、无 `Makefile`/`Makefile.ps1` 一键入口；`logs/` 与 `data/` 混在项目根下（无 `--dry-run`/`--no-persist` 开关做无副作用试跑）。

---

## 4. 改进方案（按优先级，每条含验收标准）

### 🔴 must-fix（不改会被当场问穿）

| # | 动作 | 验收标准（可执行） |
|---|---|---|
| 1 | **补齐取证链**：把 103 个 `.md` 的真实获取方式写进 `DATA_SOURCE.md`/`PROVENANCE.md`（工具、时间、速率、出口 IP）；并把该辅助脚本**纳入仓库**（如 `tools/harvest_md.py`），或在 `detail_runner` 增加 `--capture-dir data/captured`，让 `crawl` 同时落 `data/raw`（快照）与 `data/captured`（可回放正文） | `grep -rn "captured" src/` 能找到**写入方**；`crawl --maps 1 --sample 5` 后 `data/captured` 与 `data/raw` 双侧都有产物；`replay` 后 DB 条数与两侧一致 |
| 2 | **把所有"量"和"能力"的表述降级到与证据一致**：README / 演示说明 / 验收报告统一改为"103 条**真实公开内容**，由 X 工具于 2026-09-19 11:46–11:59 采集，经 `replay` 入库；本项目采集链路在该出口 IP 下实测成功率为 **0/15**（全部返回验证页）" | 三份文档中"真实抓取/代码路径完全一致/可实时抓取"等表述全部改写；`crawl_log` 中每条数据都能指到一条 log |
| 3 | **把 live 采集记账做起来**：`fetch_once()` 补 `取数成功` 级日志（tier/status/bytes/elapsed/trace_id）；失败/挑战也写 `crawl_log`（含 `blocked` 计数） | 跑一次 `crawl --maps 1 --sample 3` → `logs` 能看到逐条请求行；`crawl_log` 新增非 replay 行 |
| 4 | **git init + 首次提交 + CI**：`.github/workflows/ci.yml` 跑 `pytest` + `selftest` + 一个"空库也能跑"的校验脚本；补 `ruff`/`mypy` 配置并清零；用 `pip-compile`/`uv lock` 锁定依赖 | CI 绿；`pip install -r requirements.lock` 后版本与 README 声明一致 |
| 5 | **RobotsGate 改为 fail-closed**：robots.txt 获取失败 → 记日志 + 计入 `crawl_log` + **默认不放行**（或需显式 `--allow-unknown-robots` 才放行），并补测试 | 新增测试 `test_robots_fetch_failure_blocks_by_default` 通过 |

### 🟡 should-fix（决定作品的专业上限）

| # | 动作 | 验收标准 |
|---|---|---|
| 6 | **声明抽样口径**：README 与看板顶部固定一块"样本口径"（单站/单时段/苏州 51.5%/EV 29%/非市场代表）；把"行情大盘"措辞改为"样本统计"；或改为**按城市分层抽样**（每城 N 条）消除地域偏斜 | 看板首屏可见口径说明；分层抽样后苏州占比 < 20% |
| 7 | **低覆盖维度不进图**：`battery_type`(10%)、`range_standard`(7%)、`fast_charge_kw`(0%) 从图表中移除或统一走"字段完整率面板"；`viz.py` 的"未知"过滤口径统一 | 看板不再出现"未知 27"支配的柱状图；导出列与数据字典的启用字段一致 |
| 8 | **时间口径拆分**：新增 `fetched_at`（源侧时间，取 `generatedAt`/快照 meta）与 `ingested_at`（入库时间），`captured_at` 明确为源侧时间；replay 不再覆盖它 | 导出的"抓取时间"列 = 11:46–11:59 而非 12:03；新增测试断言 replay 幂等时 `fetched_at` 不变 |
| 9 | **补关键路径测试**：`_collect` 的冷却/熔断/续爬（用假 transport 制造 `BlockedError` 序列）、`viz` 聚合口径、RobotsGate 异常分支；把线上那 15 个 3720 字节验证页**固化为真实夹具**做回归 | 覆盖率门槛 `fail_under=80`，且上述分支均有 case |
| 10 | **404 不写完成态 checkpoint**：分 `done.urls` / `notfound.urls`，提供 `--retry-notfound` | 单测覆盖"404 后再出现能恢复" |
| 11 | **清理交付物**：把 `_removed_legacy_usedcar/` 移出工作区或加归档说明；README 的"三样东西"表补上 `exports/`（4 文件）与 `preview.png` | 新的评审者打开工作区不会看到 `_demo` 命名的数据 |

### 🟢 nice-to-have（加分项）

12. 代理池 / 出口轮换落地（`fetcher` 已留配置点）——这是"出口 IP 受限"的正解，也是最能讲的一段工程故事。
13. 历史价格拉链表（`price_history`），把"降价跟踪"这个二手车核心指标做出来。
14. 看板做**自动化冒烟**：headless 打开断言 11 个 canvas 且 `console.error` 计数为 0——把 `验收报告`里"人工实测 0 报错"变成 CI 断言。
15. 补 LICENSE（引用 Scrapling 需第三方声明）、CHANGELOG、pre-commit；统一 QPS 数字（0.2 / 0.25 / 0.5 三处打架）。

---

## 5. 面试防守：最可能被问穿的 3 个点 + 标准答法

| 问题 | ❌ 不要这样答 | ✅ 建议这样答 |
|---|---|---|
| "这 103 条是怎么采到的？" | "用我的 `crawl` 命令跑的" | "分两步：**发现**用站点地图（14 张子地图、13.4 万条 URL，快照都在 `data/raw`）；**取正文**时该出口 IP 被路径级风控挡住，15 次请求全部返回验证页，所以我用同一套 URL 清单离线取到了正文，再用 `replay` 入库——`crawl` 与 `replay` 共用同一条解析→清洗→入库链路，只有正文来源不同。这也是我为什么把 raw 层与解析层解耦。" |
| "你能实时采吗？" | "能，没问题" | "在**未被限流的出口**下能——刚才现场验过 `car-detail/*.md` 是能返回真实内容的。我这次被挡的根因是出口 IP 信誉（不是 UA、不是指纹），所以下一步 P0 是代理池；现在被挡也不会丢进度，有冷却+熔断+断点续爬。" |
| "你的数据能代表市场吗？" | "覆盖 45 品牌 39 城市" | "**不能代表**。这是链路与数据质量样本：单站、单时段、苏州占 51.5%。我在报告里明确写了这一点，也没拿它下市场结论。要代表市场需要按城市分层抽样 + 多源校准。" |

---

## 6. 附：本次评审的复现方式

评审脚本（本次生成，可直接重跑）：

```
D:\Demo_Test\.workbuddy\tmp\audit_raw.py       # 语料取证：generatedAt / mtime / ID 唯一性
D:\Demo_Test\.workbuddy\tmp\audit_gaps.py      # 抓取节奏与时间戳分布
D:\Demo_Test\.workbuddy\tmp\audit_snap.py      # 快照取证：15 验证页 vs 14 站点地图
D:\Demo_Test\.workbuddy\tmp\audit_quality.py   # 数据分布与异常值
```

关键命令：

```bash
# 建立隔离环境（未污染项目 .venv）
python -m venv C:/Users/22724/.workbuddy/binaries/python/envs/ucc-audit
C:/Users/22724/.workbuddy/binaries/python/envs/ucc-audit/Scripts/python.exe -m pip install -r requirements.txt
C:/Users/22724/.workbuddy/binaries/python/envs/ucc-audit/Scripts/python.exe -m pip install -e .

# ① 测试（实测 229 passed）
.../python.exe -m pytest -q

# ② 溯源对账（实测 103/103、0 不符）
.../python.exe tools/verify_against_raw.py

# ③ 合规复核（联网，与文档逐字一致）
.../python.exe -m usedcar_crawler compliance --verify

# ④ 抓取足迹比对：data/raw 与 data/captured 的形态差异
grep -rn "captured" src/          # 只有 replay 读，无写入方
```

---

*评审人：测试主管视角 · 所有结论均基于现场实测证据，未采信任何文档自述。*
