# 给 AI 的操作手册

这份文件是给**驱动本 CLI 的 AI** 看的。它规定了正确的调用顺序与安全边界。

## 你的角色

你通过 `etfa` CLI 帮用户操作一个真实的证券账户。**每一笔委托都花真钱。**
任何时候不确定，就停下来问用户，而不是猜。

## 输出约定

每个命令**只输出一个 JSON 对象**（stdout 一行）。判断成功看 `ok` 字段。

```json
{"ok": true, ...}
{"ok": false, "error": "risk_rejected", "message": "人类可读的原因"}
```

错误码含义：

| 错误码 | 含义 | 你该怎么办 |
|---|---|---|
| `config_error` | 配置缺失/非法 | 停下来，告诉用户改配置 |
| `not_connected` | 连不上客户端 | 让用户确认客户端**已打开并登录** |
| `broker_error` | 券商层报错 | 读 message；常见是客户端版本不匹配 |
| `risk_rejected` | 风控拒绝 | **不要试图绕过**；把原因告诉用户 |
| `outside_trading_hours` | 非交易时段 | 等开市，或用户明确要求才加 `--force` |
| `unsupported_feature` | 该客户端不支持此操作 | 读 message，参考 docs/COMPATIBILITY.md |

## 标准流程

```
1. etfa doctor                  → ready=true 且 can_trade_now=true 才继续
2. etfa probe                   → 首次接入必跑：readable=true 且 fields_missing=[] 才继续
3. etfa positions               → 记下每个标的的 qty 和 available
4. etfa quote <code>            → 拿到现价（可能没有：easytrader 无行情源）
5. etfa plan <strategy> ...     → 看策略意图（绝不下单）
6. 把 plan 结果给用户看，等确认
7. etfa run <strategy> ...      → 执行
8. etfa audit --tail 20         → 复核
```

## 价格从哪来（重要）

`easytrader` **不提供行情**。所以 `quote` 只能读到**你已持有**标的的市价。

- 若 `quote` 返回 `missing`，说明该代码没有价格源；
- 这时策略命令必须显式传 `--price`（例如从别的数据源拿到的价格）：

```
etfa plan grid --code 513050 --base 1.092 --spacing 3 --qty 2800 --price 1.095
```

**不要凭空编一个价格传进去** —— 先问用户或接一个行情源。

## 硬性禁止

1. **禁止用 `--force` 绕过交易时段**，除非用户明确要求。
2. **禁止修改 `allowed_codes` 之外的下单** —— 风控会拒绝，别去改配置绕过它。
3. **禁止在用户未确认时执行 `run`/`buy`/`sell`**。先 `plan` 或 `--dry-run`。
4. **禁止把 `watch` 当守护进程无限跑**。它必须是有限次数。
5. **禁止把密码写进任何文件**。若用 `universal_client`，密码只放环境变量。
6. **禁止假设成交**。下单后要查 `positions` 或 `orders` 确认。

## 关键判据

- **T+0 vs T+1**：`positions` 里 `qty` 是总持仓、`available` 是今日可卖。
  当天买入的份额**不进 `available`**（T+1 标的）。用 T 策略时，
  T+1 标的必须加 `--no-t0`，否则策略会试图做非法的「正 T」。
- **金额 vs 数量**：`--amount` 是元，`--qty` 是股（会自动向下取整到 100 股整手）。
- **成交与否**：`order.status == "filled"` 才是成交；`"reported"` 只是已报。
- **当日额度**：`audit` 输出里的 `notional_today` 是当日已下单金额，
  受 `max_daily_amount` 限制。

## 常用命令速查

```powershell
etfa doctor
etfa status --connect
etfa positions
etfa quote 513050
etfa plan grid --code 513050 --base 1.092 --spacing 3 --qty 2800
etfa run  grid --code 513050 --base 1.092 --spacing 3 --qty 2800
etfa plan t    --code 513050 --qty 2800 --trigger 1.5 --take-profit 1.5 --t0
etfa buy  513050 --qty 100 --dry-run
etfa buy  513050 --qty 100 --reason "AI 建议：跌破网格第 2 档"
etfa sell 513050 --qty 100 --reason "AI 建议：触及止盈"
etfa orders --open
etfa cancel            # 撤所有未成交
etfa audit --tail 20
```

## 出错时的正确反应

- 报 `not_connected` → **不要重试**，让用户去登录客户端。
- 报 `risk_rejected` → **不要重试、不要改配置绕过**，报告给用户。
- 报 `unsupported_feature` → 停下来，参考 docs/COMPATIBILITY.md 讨论退路。
- `ok=false` 但错误码未知 → 把完整 JSON 原文给用户，不要自己解释。
