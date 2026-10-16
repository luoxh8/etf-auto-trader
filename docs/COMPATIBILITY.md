# 兼容性：easytrader 与国元客户端

## 结论先说

**国元的客户端兼容性未在本机实测**（本机没安装国元远航）。这份文档说明
已知情况、判断方法和不通时的退路。

## 已知事实

### 国元的两个 PC 客户端

| 客户端 | 内核 | 本项目支持 |
|---|---|---|
| **国元远航** | 同花顺（原名「国元证券同花顺」） | ✅ 目标 |
| 国元领航 | 通达信 | ❌ 不支持 |

国元点金是手机 App，本项目不驱动手机。

> 依据：第三方软件站标注「国元证券远航版电脑版原名：国元证券同花顺」；
> 另有「国元领航版通达信版」。**内核须实机确认**：装完看进程名是
> `xiadan.exe` / `hexin.exe`（同花顺系）还是 `tdxw.exe`（通达信系）。

### easytrader 的支持矩阵

`easytrader` 的 clienttype：

| clienttype | 对应 |
|---|---|
| `ths` | 券商专用同花顺客户端（需要匹配的控件类） |
| `universal_client` | **通用**同花顺客户端 |
| `gj_client` / `ht_client` / `htzq_client` / `yh_client` / `gf_client` / `wk_client` | 国金 / 华泰 / 海通 / 银河 / 广发 / 五矿 专用 |
| `miniqmt` | QMT |
| `xq` | 雪球 |

**国元不在任何专用列表里** —— 只能走 `ths` 或 `universal_client`。

### 为什么 `ths` 可能不匹配

easytrader 为每个券商实现了控件定位类。国元远航虽是同花顺内核，
但**界面布局不等于已支持的那几家**，控件名可能对不上。

本项目的适配器不假设某个布局，而是**运行时探测**所需方法；
缺能力时抛 `UnsupportedFeature`，并明确告诉你缺哪个方法，
而不是在订单中途莫名其妙失败。

## 排查步骤

### 1. 确认进程与内核

```powershell
Get-Process | Where-Object { $_.ProcessName -match 'xiadan|hexin|tdxw' } |
  Select-Object ProcessName, Path
```

- 有 `xiadan.exe` / `hexin.exe` → 同花顺内核，走本项目。
- 有 `tdxw.exe` → 通达信，**不支持**。

### 2. 试 `ths`

`config.json`：

```json
{ "broker": { "type": "easytrader_ths", "clienttype": "ths",
              "exe_path": "C:\\国元远航\\xiadan.exe" } }
```

客户端**打开并登录**后：

```powershell
.\.venv\Scripts\python.exe -m etfa status --connect
.\.venv\Scripts\python.exe -m etfa balance
```

- 能读到余额 → 成功。
- 报 `client lacks [...]` → 见第 3 步。

### 3. 退到 `universal_client`

通用同花顺客户端路径自己处理登录：

```json
{ "broker": { "type": "easytrader_ths", "clienttype": "universal_client",
              "exe_path": "C:\\国元远航\\xiadan.exe", "user": "你的资金账号" } }
```

密码走环境变量，**不写进配置文件**：

```powershell
$env:ETFA_BROKER_PASSWORD = "你的密码"
```

或装**通用同花顺客户端**（官网 `download.10jqka.com.cn`），登录国元账号，
再用 `ths` 指向它的 `xiadan.exe`。

### 4. 都失败时的退路

| 退路 | 说明 |
|---|---|
| 换券商 | 开一个支持 miniQMT 的账户（国金/华西约 10 万门槛可谈），改 `broker.type=qmt`（需自行实现，当前是占位） |
| 官方云条件单 | 国元点金 App 自带的网格交易条件单是**官方云端全自动**，不需要本工具，但策略自由度低 |
| 报 issue | 把 `easytrader` 的控件报错贴给 easytrader 项目，请求加国元支持 |

## 降低脆性（若走通了）

1. **锁定客户端版本** —— 备份安装包，关掉自动更新。客户端一升级控件可能全变。
2. **固定窗口** —— 不最大化/最小化，保持登录状态，别锁屏。
3. **优先用 UIA 而非坐标** —— easytrader 内部已是控件定位，比纯坐标稳。
4. **下单前二次校验** —— 本项目风控已做金额/白名单/仓位检查，可再加价格偏离阈值。
5. **成交靠查持仓确认**，不要假设「点到了就成交」。
6. **先 `plan` 再 `run`**。

## 参考

- easytrader：<https://github.com/shidenggui/easytrader>
- 版本：0.23.7（PyPI 2025-04-16 发布，仓库 2026-02 仍有推送）
- pywinauto 运行时需 `pywin32`（其元数据遗漏，本项目 `[live]` 已补上）
