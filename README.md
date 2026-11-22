# FPH — 区域法拍房全生命周期分析库与捡漏监控

面向**个人买房捡漏**与**区域行情分析**的法拍房数据仓库（默认预置 **福建省福州市** + **福建省龙岩市** 住宅用房）。

## 核心设计

1. **标的物（`properties`）与拍卖轮次（`auction_rounds`）分离**
   - 自动剥离标题中的轮次噪音（`【一拍】`/`（二拍）`/`变卖`/`#楼`/`栋`/`按现状拍卖`等），将同一套房的多次上拍归并到同一个 `property_key`，准确追踪降价轨迹与真实成交率。
   - 自动区分物业类型（`residential` / `parking` / `commercial` / `industrial` / `land`）并识别部分产权（如 `50%产权份额`），统计住宅行情和筛选捡漏时自动剔除脏数据。
2. **分层低频采集与增量早停**
   - **列表同步**：同步目标城市的「正在进行 / 即将开始」与「已结束」房源；默认开启近 2 年时间窗口（`lookback_days = 730`，在拍/预告不受此限），已结束列表遇到整页超期或整页已入库终态时自动提前停止翻页，大幅减少无效请求。
   - **详情与公告补抓（每套仅抓 1 次）**：默认仅针对在拍与预告标的自动补抓详情（`enrich_statuses = ["ongoing", "upcoming"]`），自动提取建筑面积、折合单价、处置法院，并自动标出风险标签（`不交吉/不腾退`、`有人占用`、`带租约`、`买受人全额担税`、`欠费风险`、`无证/产权瑕疵`、`部分产权`等）；异常失败自动记录重试次数与冷却退避，避免死循环请求。
   - **结拍回刷**：仅对结拍时间在回刷窗口内（默认近 30 天）且状态尚未终结的标的回刷最终成交价与出价次数，对无法解析终态的标的同样触发退避保护。
3. **可插拔采集驱动（支持阿里/淘宝法拍 `taobao_ego` 与京东司法拍卖 `jd_ego`）**
   - 采集层与分析库完全解耦。通过 `ego-browser nodejs` 在独立 `Task Space` 中复用本机 `ego(lite)` 登录态，不抢占日常浏览标签页，也不消耗 LLM Token。
   - 淘宝与京东采集到同一套房产时，按标准化 `property_key` 自动归并到同一 `property_id`，保留各自平台的轮次记录与价格变化。
   - 遇到登录失效、弹窗确认或安全验证时，安全暂停并提示你在 `ego(lite)` 对应 Space 中手动接管，不做任何绕过或破解。
   - 支持 `--provider taobao` / `--provider jd` 按平台单独同步或补抓详情。

---

## 环境准备（macOS & Windows 通用）

- **Python 3.10+**（推荐 3.11+，纯标准库零重依赖）
- **ego(lite)** 桌面端已安装并完成初始登录（已在其中登录淘宝账号），确保终端可执行 `ego-browser` 命令。

### 安装

```bash
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

```powershell
# Windows (PowerShell)
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

### 初始化与环境自检

```bash
# 1. 生成默认 config.toml（预置福州市 350100 + 龙岩市 350800）并初始化 SQLite 数据库
fph init

# 2. 检查本机 Python、数据库、ego-browser 状态及目标采集 URL
fph doctor
```

---

## 常用命令

### 1. 执行采集

```bash
# 执行完整一轮同步（福州 + 龙岩：列表同步 + 新标的详情补抓 + 结拍结果回刷）
fph sync

# 仅同步福州，且先只抓 2 页试跑
fph sync --region 福州 --max-pages 2

# 仅同步京东司法拍卖（福州住宅，1 页试跑）
fph sync --provider jd --region 福州 --max-pages 1 --no-enrich --no-refresh-ended

# 仅同步淘宝法拍并补抓详情
fph sync --provider taobao --region 福州 --max-pages 2

# 仅同步正在进行与即将开始的活跃房源（跳过已结束列表，适合日常高频捡漏）
fph sync --active-only

# 临时覆盖历史时间窗口（默认 config.toml 中 lookback_days = 730 即近 2 年；0 表示不限制）
fph sync --lookback-days 365
fph sync --lookback-days 0

# 临时覆盖自动补抓详情的目标状态（默认 config.toml 中 enrich_statuses = ["ongoing", "upcoming"]）
fph sync --enrich-statuses all

# 单独补抓尚未抓取详情/公告的标的（可加 --provider / --statuses / --lookback-days）
fph enrich --limit 20
fph enrich --statuses sold --lookback-days 365 --limit 20
fph enrich --statuses all --limit 20

# 仅采集住宅类型（config.toml 默认已设置 allowed_property_types = ["residential"]）
# 也可通过命令行临时覆盖（传 all 表示本次放开采集全部类型）：
fph sync --only-residential
fph sync --property-types all
```

> 若运行中提示 `auth_required`：打开本机 `ego(lite)`，点击右上角 **Space** 切入 `fph-auction-collector`，手动完成淘宝登录或验证后重新执行 `fph sync` 即可。

### 1.5 历史数据清理与修复（`fph cleanup`）

可一键预览或清理超期历史数据、非目标物业类型记录，并重置被风控滑块污染的详情与修复 `revocation` 撤拍状态：

```bash
# 先以预览模式检查将清理/修复的条目数（只读预览，不修改数据库）
fph cleanup --dry-run

# 确认无误后执行实际清理（默认读取 config.toml 中的 lookback_days 与 allowed_property_types）
fph cleanup

# 自定义参数示例：按 365 天窗口预览，但保留非住宅类型且不重置滑块详情
fph cleanup --dry-run --lookback-days 365 --keep-non-residential --no-reset-captcha
```

### 2. 区域行情分析

```bash
# 查看福州、龙岩整体住宅法拍行情（成交率、成交均折、成交均价、在拍均价）
fph stats

# 按区县细分（如福州鼓楼/仓山/晋安/福清，龙岩新罗/永定/上杭）
fph stats --city 福州 --by-district

# 按月份追踪成交走势
fph stats --city 福州 --by-month
```

### 3. 个人捡漏筛选

```bash
# 筛选当前在拍/预告、8折以内、中低风险的纯住宅全产权房源（二拍/变卖优先）
fph bargains

# 精准筛选：福州仓山区、75折以内、总价 200 万以内、面积 >= 85㎡、仅低风险
fph bargains --city 福州 --district 仓山 --max-discount 0.75 --max-total 200 --min-area 85 --max-risk low

# 仅看龙岩市二拍房源
fph bargains --city 龙岩 --stage 二拍
```

### 4. 导出数据（Excel 直接打开不乱码）

```bash
# 导出全量宽表（UTF-8 with BOM，Windows Excel 双击直接打开无乱码）
fph export --dataset all --format csv

# 导出住宅成交/流拍行情表
fph export --dataset deals --city 福州 --format csv

# 导出当前捡漏候选为 JSON
fph export --dataset bargains --format json
```

### 5. 本地 Web 看板（可视化浏览与筛选）

```bash
# 启动本地 Web 数据看板（默认 http://127.0.0.1:8765 并自动打开浏览器）
fph web

# 指定端口且不自动弹出浏览器
fph web --port 8765 --no-open
```

### 6. 定时运行

- **方式 A：内置跨平台常驻调度**（适合终端常开，支持随机抖动）
  ```bash
  fph schedule --interval 360
  ```
- **方式 B：系统级定时任务（推荐）**
  - **macOS**：使用 `launchd` 定时调用 `.venv/bin/fph sync`（需保持电脑唤醒且 `ego(lite)` 运行）
  - **Windows**：使用「任务计划程序」定时调用 `.venv\Scripts\fph.exe sync`

---

## SQLite 内置分析视图

你可以直接用 DB Browser for SQLite / DBeaver / DataGrip 打开 `data/fph.db`，直接查询预建视图：

- `v_auction_full`：标的物与拍卖轮次关联宽表（含实时折扣率、单价、该套房累计上拍次数）
- `v_residential_deals`：纯住宅、全产权的已结拍成交/流拍数据（行情分析专用）
- `v_bargain_candidates`：纯住宅、全产权的在拍/预告候选池
