# FPH — 区域法拍房全生命周期分析库与捡漏监控

面向**个人买房捡漏**与**区域行情分析**的法拍房数据仓库（默认预置 **福建省福州市** + **福建省龙岩市** 住宅用房）。

## 核心设计

1. **标的物（`properties`）与拍卖轮次（`auction_rounds`）分离**
   - 自动剥离标题中的轮次噪音（`【一拍】`/`（二拍）`/`变卖`/`#楼`/`栋`/`按现状拍卖`等），将同一套房的多次上拍归并到同一个 `property_key`，准确追踪降价轨迹与真实成交率。
   - 自动区分物业类型（`residential` / `parking` / `commercial` / `industrial` / `land`）并识别部分产权（如 `50%产权份额`），统计住宅行情和筛选捡漏时自动剔除脏数据。
2. **分层低频采集**
   - **列表同步**：同步目标城市的「正在进行 / 即将开始」与「已结束」房源。
   - **详情与公告补抓（每套仅抓 1 次）**：自动提取建筑面积、折合单价、处置法院，并自动标出风险标签（`不交吉/不腾退`、`有人占用`、`带租约`、`买受人全额担税`、`欠费风险`、`无证/产权瑕疵`、`部分产权`等）。
   - **结拍回刷**：仅对已过结拍时间但状态尚未终结的标的回刷最终成交价与出价次数。
3. **可插拔采集驱动（一期支持 `ego(lite)` / `ego-browser`）**
   - 采集层与分析库完全解耦。一期通过 `ego-browser nodejs` 在独立 `Task Space` 中复用本机 `ego(lite)` 登录态，不抢占日常浏览标签页，也不消耗 LLM Token。
   - 遇到登录失效或安全验证时，安全暂停并提示你在 `ego(lite)` 对应 Space 中手动接管，不做任何绕过或破解。
   - 后续扩展京东法拍、公拍网或纯 HTTP 驱动时，只需新增 `AuctionProvider` 实现即可无缝写入同一 SQLite 分析库。

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

# 单独补抓尚未抓取详情/公告的标的
fph enrich --limit 20
```

> 若运行中提示 `auth_required`：打开本机 `ego(lite)`，点击右上角 **Space** 切入 `fph-auction-collector`，手动完成淘宝登录或验证后重新执行 `fph sync` 即可。

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

### 5. 定时运行

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
