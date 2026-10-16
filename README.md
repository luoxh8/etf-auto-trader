# etf-auto-trader

用命令行（CLI）驱动 ETF 自动交易的本地工具。为 **国元证券 + 同花顺内核客户端** 设计，
但券商层是可插拔的，任何能被 `easytrader` 驱动的同花顺系客户端都适用。

**核心工作方式**：你**手动登录**交易客户端一次，之后所有下单由 CLI 完成 ——
程序里**不存任何密码**。

```
┌──────────────┐   JSON   ┌──────────────┐  自动化  ┌──────────────────┐  UI   ┌─────────┐
│  AI / 你     │ ───────► │  etfa CLI    │ ───────► │ easytrader       │ ────► │ 国元远航 │
│  或 shell    │ ◄─────── │ 风控+策略     │ ◄─────── │ (pywinauto 驱动)  │ ◄──── │ 已登录   │
└──────────────┘   JSON   └──────────────┘          └──────────────────┘       └─────────┘
```

---

## 这是给谁用的

个人投资者，账户在国元证券（或任何提供同花顺内核 PC 客户端的券商），
想用程序/让 AI 托管做 ETF 的网格或日内 T。

**不是**给机构或高频交易用的。这份代码是分钟级、限价单、少量委托的工具。

> ⚠ **必须用电脑客户端，不能用手机 App。** 本项目靠 Windows UI 自动化驱动
> 桌面窗口，因此**驱动不了「国元点金」手机 App**。要自动化就得用国元的
> **PC 客户端**（国元远航，同花顺内核）。手机 App 只能手动交易，或者用
> 国元点金自带的「网格交易」条件单（官方云端全自动，与本项目互补）。

---

## 三个必须先知道的前提

> 📌 **先读 [docs/GO-LIVE.md](docs/GO-LIVE.md)** —— 它把「怎么真正用起来」拆成两条路，
> 并说明**哪条更适合你**：① 国元点金 App 自带的条件单/网格（不用装任何东西、券商云端执行、
> 官方支持）；② 本仓库的 CLI（需要装国元远航 PC 客户端，但能跑自定义策略供 AI 驱动）。

### 1. 股票型 ETF 是 T+1，跨境 ETF 才是 T+0

| 类型 | 代表代码 | 当日回转 | 做 T 限制 |
|---|---|---|---|
| A 股股票型 ETF | 510300 / 510500 / 159915 | **T+1** | 只能「底仓反 T」：先卖昨天持有的份额，当天再买回 |
| **跨境 ETF（QDII）** | **513050** / 159920 / 513100 | **T+0** | 当天买当天可卖，正 T 反 T 都行 |
| 黄金 ETF | 518880 | T+0 | 同上 |
| 债券 / 货币 ETF | 511xxx | T+0 | 同上 |

在 `config.json` 的 `trading.t0_codes` 里声明哪些是 T+0，策略会据此决定允不允许「正 T」。

### 2. 成本决定网格间距（5 万资金尤其致命）

ETF 免印花税、场内无过户费，只剩佣金。但**单笔最低 5 元**是 5 万资金的主要摩擦：

| 每格金额 | 双边成本 | 占金额比 |
|---|---|---|
| 3000 元（触最低 5 元） | 10 元 | 33 bp |
| 10000 元（触最低 5 元） | 10 元 | 10 bp |

**结论**：网格间距别小于 2%（推荐 2.5%~3%），每格金额别低于 3000 元。
详见 [docs/COSTS.md](docs/COSTS.md)。

### 3. 合规：程序化交易要报备

《证券市场程序化交易管理规定（试行）》2024-10-08 施行。个人账户做程序化交易
需向券商报备。你的委托量远低于高频线（每秒 300 笔 / 全日 20000 笔），
但**报备义务仍在**。详见 [docs/COMPLIANCE.md](docs/COMPLIANCE.md)。

---

## 安装

本项目**独立 venv**，不用系统 Python（本机 Python 由 uv 托管，禁止直接 pip 装包）。

```powershell
# 1) 建 venv
D:\codex_proj\dev\toolchains\python\runtime\python.exe -m venv .venv

# 2) 装本项目（核心零依赖）
.\.venv\Scripts\python.exe -m pip install -e . -i https://pypi.org/simple

# 3) 装实盘依赖（easytrader + pywin32）
.\.venv\Scripts\python.exe -m pip install easytrader pywin32 -i https://pypi.org/simple
```

> ⚠ **必须带 `-i https://pypi.org/simple`**。本机 pip 默认走清华镜像，
> 而清华镜像**没有收录 easytrader**，不加这个参数会报 "No matching distribution found"。

验证：

```powershell
.\.venv\Scripts\python.exe -m etfa doctor
```

---

## 快速开始（离线演练，不碰真钱）

```powershell
# 看状态
.\.venv\Scripts\python.exe -m etfa --broker mock status
.\.venv\Scripts\python.exe -m etfa --broker mock balance
.\.venv\Scripts\python.exe -m etfa --broker mock positions

# 看网格价目表
.\.venv\Scripts\python.exe -m etfa grid ladder --base 1.092 --spacing 3 --qty 2800

# 模拟买入（--broker mock 用的是 var/mock-account.json 里的假账户）
.\.venv\Scripts\python.exe -m etfa --broker mock buy 513050 --amount 3000 --force

# 干跑网格策略，只看它想做什么，不下单
.\.venv\Scripts\python.exe -m etfa --broker mock plan grid --code 513050 --base 1.092 --spacing 3 --qty 2800
```

`mock` 账户状态存在 `var/mock-account.json`，初次从 `config/mock_seed.json` 初始化。
想重置就删掉它。

---

## 接入真实账户（国元证券）

### 第 1 步：确认国元支持哪种客户端

国元有两个 PC 客户端：

- **国元远航** —— 同花顺内核（原名「国元证券同花顺」）。**这是本项目要用的。**
- 国元领航 —— 通达信内核。**本项目不支持。**

国元点金是手机 App，本项目**不驱动手机 App**。

### 第 2 步：装客户端并手动登录

**下载**：国元官网 → 网厅 → 软件下载 → **电脑客户端下载**
（`https://www.gyzq.com.cn/main/wangting/software_download/index.html`）。
找「**国元远航版PC客户端**」（当前 v11.3.18.2），**不要**装成手机 App。

> 已核实：国元官网自己写的名字就是「**国元证券远航版PC端（同花顺）**」，
> 并注明「支持港股通 ETF、债券、基金、科创板、创业板等各类业务品种的交易」。
> 所以它确实是同花顺内核，且支持 ETF。

装完**手动登录**你的国元账号，**让窗口保持开着**。

> 先向国元客服（**95578**）确认「是否支持用同花顺客户端登录交易」。
> 早年国元曾以「登记委托来源」为由不支持第三方客户端，现状以客服口径为准。

### 第 3 步（推荐）：跑一键接线脚本

装好并登录后，**不用手动找路径、不用手改配置**：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\onboard.ps1
```

它会自动：① 全盘搜出 `xiadan.exe`；② 把它写进 `config/config.json` 的 `exe_path`；
③ 依次跑 `doctor` → `status --connect` → **`probe`**。

**最后看 `probe` 的输出就是结论**：`readable:true` 且 `fields_missing:[]` → 兼容可用。
（想手动指定路径：`-ExePath "C:\国元远航\xiadan.exe"`。只想搜不写配置：跑 `scripts\find-client.ps1`。）

如果你更想手工做，跳过这步、走下面的第 4~6 步。

### 第 4 步：找到客户端 exe 路径（手工路线）

> 若已跑过 `scripts\onboard.ps1`，这一步与第 5 步都已自动完成，直接跳到第 6 步。

在任务管理器里找到 `xiadan.exe`（同花顺下单程序）的路径，例如：

```
C:\国元远航\xiadan.exe
```

### 第 5 步：改配置

```powershell
Copy-Item config\config.example.json config\config.json
```

编辑 `config/config.json`：

```json
{
  "broker": {
    "type": "easytrader_ths",
    "clienttype": "ths",
    "exe_path": "C:\\国元远航\\xiadan.exe"
  },
  "trading": {
    "allowed_codes": ["513050"],
    "t0_codes": ["513050"],
    "min_order_amount": 1000.0,
    "max_order_amount": 10000.0,
    "max_daily_amount": 30000.0,
    "max_position_pct": 80.0
  }
}
```

### 第 6 步：验证连接

```powershell
.\.venv\Scripts\python.exe -m etfa doctor
.\.venv\Scripts\python.exe -m etfa status --connect
.\.venv\Scripts\python.exe -m etfa probe        # ← 最关键：看能不能真读到数据
.\.venv\Scripts\python.exe -m etfa balance
.\.venv\Scripts\python.exe -m etfa positions
```

**如果不知从哪里下手，就跑 `probe`。** 它是只读诊断，会打印客户端返回的
**原始字段名**以及本项目的字段映射是否匹配：

```jsonc
{"ok": true, "verdict": {
  "balance":  {"readable": true, "rows": 1, "fields_missing": []},
  "position": {"readable": true, "rows": 1, "fields_missing": []},
  ...
}, "raw": {"position": {"columns_returned": ["证券代码","证券名称",...], ...}}}
```

- `readable: true` 且 `fields_missing: []` → 客户端兼容，可以往下走。
- `fields_missing` 非空 → 客户端的列名和本项目预期不同，把 `raw` 里的
  `columns_returned` 发给作者/贴 issue 就能加映射。
- `readable: false` → 见 [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)。

### 第 7 步：先小额实盘验证

```powershell
# 先干跑，确认价格和数量算对了
.\.venv\Scripts\python.exe -m etfa buy 513050 --qty 100 --dry-run

# 确认无误再真下单，金额要小
.\.venv\Scripts\python.exe -m etfa buy 513050 --qty 100 --reason "首次实盘验证"
```

---

## 让 AI 驱动

CLI 的每个命令都**只输出一个 JSON 对象**，所以 AI 可以直接解析、判断、连续调用：

```jsonc
// etfa --broker mock positions
{"ok": true, "positions": [{"code": "513050", "qty": 18000, "available": 18000, ...}]}

// 失败也是 JSON，带稳定错误码
{"ok": false, "error": "risk_rejected", "message": "notional ... above trading.max_order_amount ..."}
```

错误码：`config_error` / `broker_error` / `not_connected` / `risk_rejected` /
`outside_trading_hours` / `unsupported_feature`。

给 AI 的典型指令序列：

```
1. etfa doctor                    → 确认能交易
2. etfa positions                 → 看持仓
3. etfa quote 513050              → 看价格
4. etfa plan grid --code 513050 ... → 看策略想做什么
5. etfa run grid --code 513050 ...  → 执行
6. etfa audit --tail 20           → 复核
```

**不要把 `watch` 交给 AI 无限跑** —— 它有意做了 `--iterations` 上限。

---

## 命令一览

| 命令 | 作用 |
|---|---|
| `status [--connect]` | 配置 + 交易时段 + 可选实时余额 |
| `session` | 当前交易时段分类 |
| `doctor` | 起飞前检查（不交易） |
| `probe` | **实盘只读诊断**：打印客户端原始字段与映射是否匹配 |
| `config` | 打印生效配置 |
| `balance` / `positions` | 资金 / 持仓 |
| `quote [codes...]` | 行情 |
| `orders [--open]` | 当日委托 |
| `cancel [order_id]` | 撤单（不填 id = 撤所有未成交） |
| `buy` / `sell` | 手动下单，过风控 |
| `plan <strategy>` | 策略干跑，**绝不下单** |
| `run <strategy>` | 策略单次执行 |
| `watch <strategy>` | 策略循环（有上限） |
| `grid ladder` | 打印网格价目表 |
| `simulate` | **离线跑模拟交易日**：三种行情下网格赚不赚、手续费吃掉多少 |
| `audit [--tail N]` | 审计日志 |

策略：`grid`（等比网格）、`t`（底仓做 T）。

```powershell
# 网格
etfa run grid --code 513050 --base 1.092 --spacing 3 --levels-up 4 --levels-down 4 --qty 2800
# 底仓做 T（--no-t0 用于 T+1 标的）
etfa run t --code 513050 --qty 2800 --trigger 1.5 --take-profit 1.5 --t0
```

---

## 安全设计

- **不存密码**：`easytrader_ths` 附着到已手动登录的客户端。若用 `universal_client`，
  密码从 `ETFA_BROKER_PASSWORD` 环境变量读，**永不落盘**。
- **默认拒绝**：`allowed_codes` 白名单、单笔/单日金额上限、单标的仓位上限、
  交易时段检查 —— 任一不满足即拒绝，并写审计。
- **`plan` 永不下单**：想看清策略意图就用它。
- **全程留痕**：每个意图、每次拒单、每笔委托都写 `var/audit.jsonl`（追加式）。
- **`--force` 只跳过时段检查**，不跳过任何金额/白名单/仓位限制。

---

## 目录结构

```
etf-auto-trader\
├── src\etfa\
│   ├── cli.py              命令行接口（AI 的入口）
│   ├── config.py           配置加载（不含密钥）
│   ├── risk.py             风控闸门
│   ├── audit.py            审计日志
│   ├── timeutil.py         交易时段
│   ├── models.py           数据模型
│   ├── broker\             券商适配层
│   │   ├── mock.py         离线假账户（可持久化）
│   │   ├── easytrader_ths.py  实盘：驱动同花顺内核客户端
│   │   └── qmt.py          QMT 占位（门槛约 50 万）
│   └── strategies\
│       ├── grid.py         等比网格
│       └── t_intraday.py   底仓做 T
├── config\                 配置（config.json 不含密钥）
├── tests\                  27 个测试，全部离线
├── docs\                   成本 / 合规 / 兼容性说明
└── var\                    运行时状态（审计、策略状态、mock 账户）
```

---

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

**50 项全绿，全部离线**（不需要客户端、不需要网络）。其中
`tests/test_live_path.py` 用**假 easytrader 客户端**驱动**真实适配器 + 真实 CLI**，
所以「适配器对 easytrader API 的假设」是被测试钉住的 —— 假设错了，测试会按真机的方式失败。

## 已知风险（务必先读）

0. **easytrader 的 `connect()` 会关闭标题不匹配的 `#32770` 窗口**，而客户端主窗口正是该类。
   若国元远航的主窗口标题不等于 easytrader 预设的 `网上股票交易系统5.0`，
   连接时可能**关掉你的交易窗口**。本项目已在 attach 前自动探测并固定标题
   （`probe` 的 `title_source` 会说明来源）；若它显示 `easytrader default`，
   请按 [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) 手动设置 `broker.expected_title`。
1. **GUI 自动化很脆** —— easytrader 靠控件树找按钮，同花顺/国元客户端一升级就可能失效。
   **锁定客户端版本、关掉自动更新。**
2. **国元的控件兼容性未实测** —— 本机没装国元客户端，无法验证。见 docs/COMPATIBILITY.md。
3. **可能违反券商协议** —— 用第三方程序接管交易可能违反委托来源要求。先问客服。
4. **只做分钟级** —— 每步操作有秒级延迟，别指望秒级做 T。
5. **没有行情源** —— easytrader 不提供报价，`quote` 只能读你**已持有**标的的市价；
   策略命令需要时显式给 `--price`。见 docs/COMPATIBILITY.md。
6. **实盘委托用最新价，不是盘口价** —— 拿不到买卖档，急行情限价单可能不成交。

---

## License

MIT
